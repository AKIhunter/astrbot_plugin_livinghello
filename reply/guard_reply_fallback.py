"""事件局部主模型重试：不重放Agent、工具、请求钩子或平台发送。"""
from __future__ import annotations

import asyncio
import random
import sys
import time

from astrbot.api.provider import LLMResponse
from astrbot.core.platform.message_type import MessageType

try:
    from .guard_reply_health import (
        ERROR_LABELS, FAIL_REPORT, KEY, NOT_AN_ERROR, RETRY_THEN_ROTATE,
        ROTATE_NOW_REPORT, classify,
    )
    from ..observability.guard_request_trace import emit
except ImportError:
    from reply.guard_reply_health import (
        ERROR_LABELS, FAIL_REPORT, KEY, NOT_AN_ERROR, RETRY_THEN_ROTATE,
        ROTATE_NOW_REPORT, classify,
    )
    from observability.guard_request_trace import emit


class ReplyFallback:
    RETRIES = 5
    COOLDOWN = 60
    CALL_TIMEOUT = 60
    DEADLINE = 540
    CAPACITY = 16

    def __init__(self, plugin, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.plugin, self.clock, self.sleep = plugin, clock, sleep
        self.closed = False
        self.bindings, self.tasks = {}, set()

    @property
    def enabled(self):
        return not self.closed and self.plugin.config.get("reply_fallback_enabled", False) is True

    def bind_current(self, event, run_context):
        """宿主钩子未公开runner；只沿当前栈定位同一上下文的本地runner实例。"""
        if not self.enabled or not event.get_extra("_livinghello_reply_approved"):
            return False
        frame = sys._getframe(1)
        try:
            for _ in range(24):
                if frame is None:
                    break
                filename = frame.f_code.co_filename.replace("\\", "/")
                runner = frame.f_locals.get("self")
                if (filename.endswith("/agent/runners/tool_loop_agent_runner.py")
                        and frame.f_code.co_name == "step"
                        and getattr(runner, "run_context", None) is run_context):
                    return self.bind(event, runner)
                frame = frame.f_back
        finally:
            del frame  # 不保留栈帧、消息正文或其他会话的引用。
        emit(event, "reply_retry", "uncollected", reason="unsupported_runner")
        return False

    def bind(self, event, runner):
        if not self.enabled or not event.get_extra("_livinghello_reply_approved"):
            return False
        if id(runner) in self.bindings:
            return True
        if len(self.bindings) >= self.CAPACITY:
            emit(event, "reply_retry", "skipped", reason="retry_capacity")
            return False
        boundary = "_iter_llm_responses_with_fallback"
        original = getattr(runner, boundary, None)
        if (not callable(original) or not callable(getattr(runner, "_iter_llm_responses", None))
                or not callable(getattr(runner, "request_stop", None))):
            emit(event, "reply_retry", "uncollected", reason="unsupported_runner")
            return False
        state = event.get_extra("_livinghello_reply_retry")
        if state is None:
            state = {"retries": 0, "delayed": False, "state": "ready",
                     "deadline": self.clock() + self.DEADLINE}
            event.set_extra("_livinghello_reply_retry", state)

        async def wrapped():
            if not self.enabled:
                async for response in original():
                    yield response
                return
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                async for response in self.responses(event, runner, state):
                    yield response
            finally:
                self.tasks.discard(task)

        had_local = boundary in vars(runner)
        try:
            setattr(runner, boundary, wrapped)
        except (AttributeError, TypeError):
            return False
        self.bindings[id(runner)] = (runner, original, wrapped, had_local)
        owner = (event.get_extra(KEY) or {}).get("owner")
        if owner:
            owner.add_done_callback(lambda _: self.release(runner))
        return True

    def release(self, runner):
        binding = self.bindings.pop(id(runner), None)
        if not binding:
            return
        _, original, wrapped, had_local = binding
        if getattr(runner, "_iter_llm_responses_with_fallback", None) is wrapped:
            if had_local:
                runner._iter_llm_responses_with_fallback = original
            else:
                del runner._iter_llm_responses_with_fallback

    def stop_reason(self, event, runner, state, *, retrying=False):
        health = event.get_extra(KEY) or {}
        plan = event.get_extra("_at_guard_delivery_plan") or {}
        if state.get("state") == "cancelled":
            return state["reason"]
        if not self.enabled:
            return "retry_disabled"
        if health.get("ended") or self.clock() >= state["deadline"]:
            return "retry_deadline"
        if (health.get("sent") or health.get("failed") or plan.get("attempted")
                or event.get_extra("_livinghello_any_send_attempted")):
            return "send_already_attempted"
        if getattr(runner, "_is_stop_requested", lambda: False)():
            return "agent_aborted"
        stopped = getattr(event, "is_stopped", lambda: getattr(event, "stopped", False))()
        if stopped:
            return "event_stopped"
        if self.plugin._delivery().recalled(event):
            return "message_recalled"
        checker = event.get_extra("_livinghello_policy_send_check")
        reason = checker() if callable(checker) else ""
        if reason:
            return reason
        if self.plugin._judged_event(event):
            group = self.plugin._group_key(event)
            # 首次调用和工具后的正常调用允许后续聊天；仅失败重试复核原判定水位。
            advanced = getattr(self.plugin, "_context_advanced", None)
            if retrying and callable(advanced) and advanced(event, group):
                return "context_changed"
            reason = self.plugin._stale_reason(event, group)
            # 兼容旧版闸门把群聊上下文变化混在过期检查中的实现。
            if (reason == "context_changed" and not retrying
                    and event.get_message_type() == MessageType.GROUP_MESSAGE):
                return ""
            return reason
        return ""

    def account(self, event, started):
        elapsed = max(0, self.clock() - started)
        event.set_extra("_livinghello_retry_seconds",
                        (event.get_extra("_livinghello_retry_seconds", 0) or 0) + elapsed)

    async def wait(self, event, runner, state, seconds):
        # 每秒复核一次开关、撤回、权限与上下文；等待不延长普通生成期限。
        started = self.clock()
        try:
            remaining = seconds
            while remaining > 0:
                step = min(1, remaining)
                before = self.clock()
                await self.sleep(step)
                self.account(event, before)
                remaining -= step
                reason = self.stop_reason(event, runner, state, retrying=True)
                if reason:
                    return reason
            return ""
        finally:
            emit(event, "reply_retry_wait", "success",
                 ms=round((self.clock() - started) * 1000, 2))

    @staticmethod
    def failed(response):
        if response is None or getattr(response, "role", "") == "err":
            return True
        chain = getattr(getattr(response, "result_chain", None), "chain", None)
        return not (str(getattr(response, "completion_text", "") or "").strip() or
                    getattr(response, "tools_call_name", None) or
                    chain)

    def cancel(self, event, runner, state, reason):
        if state.get("state") == "cancelled":
            return  # 终态只记录一次，不能让宿主反复执行空步骤刷满诊断记录。
        metadata = {}
        if reason == "context_changed":
            group = self.plugin._group_key(event)
            judged = event.get_extra("_at_guard_judged_revision")
            if judged is None:
                # 只在旧闸门兼容路径出现：先留锚点，原有 TypeError 行为保持不变。
                rid = str(event.get_extra("_livinghello_request_id") or "")
                self.plugin.logger.warning(
                    "[ReplyFault] code=CONTEXT_CHANGED_TYPEERROR stage=hello "
                    "category=not_an_error disposition=not_an_error%s "
                    "reason=cancel(context_changed) 缺少 _at_guard_judged_revision，"
                    "下一行将抛 TypeError",
                    f" request_id={rid}" if rid else "",
                )
            bumps = int(event.get_extra("_at_guard_early_ack_bumps", 0) or 0)
            metadata = {"judged": judged, "before": judged + bumps,
                        "after": self.plugin.context_revision.get(group, 0),
                        "count": bumps, "source": "retry"}
        state.update(state="cancelled", reason=reason)
        # 正常保护取消不会触发“未回复”误报；期限耗尽保留失败收尾。
        if reason not in {"retry_deadline", "send_already_attempted"}:
            event.set_extra("_livinghello_intentional_silence", reason)
            event.set_extra("_at_guard_delivery_blocked", reason)
        emit(event, "reply_retry", "cancelled", reason=reason, **metadata)
        # 空生成器本身不会让宿主进入终态；必须通知当前runner结束，禁止重放Agent。
        runner.request_stop()

    async def responses(self, event, runner, state):
        candidates = [runner.provider, *getattr(runner, "fallback_providers", [])]
        if not getattr(runner.run_context, "messages", None):
            yield LLMResponse(role="err", completion_text="No messages remain for the LLM request.")
            return
        attempt = 0
        retrying = False
        while True:
            reason = self.stop_reason(event, runner, state, retrying=retrying)
            if reason:
                self.cancel(event, runner, state, reason)
                return
            candidate = candidates[attempt % len(candidates)]
            runner.provider = candidate
            self.plugin._reply_health().provider(event, candidate)
            started = self.clock()
            has_output, final, category, disposition, valid = (
                False, None, "empty_response", RETRY_THEN_ROTATE, False)
            previous_retries = runner.request_max_retries
            stream = None
            try:
                # 由本层统一预算；原生空输出循环被替换，SDK请求最多一次。
                runner.request_max_retries = 1
                stream = runner._iter_llm_responses(include_model=candidate is candidates[0])
                call_deadline = min(started + self.CALL_TIMEOUT, state["deadline"])
                while True:
                    try:
                        # 超时仅包住取模型结果，不能覆盖yield后的工具/平台处理。
                        response = await asyncio.wait_for(
                            anext(stream), timeout=max(0, call_deadline - self.clock()))
                    except StopAsyncIteration:
                        break
                    else:
                        if getattr(response, "is_chunk", False):
                            has_output = True
                            yield response
                            continue
                        final = response
                        if self.failed(response):
                            category, disposition = classify(
                                getattr(response, "completion_text", ""))
                            break
                        runner._sanitize_malformed_tool_calls(response)
                        valid = True
                        break
            except asyncio.CancelledError:
                self.cancel(event, runner, state, "agent_aborted")
                raise
            except Exception as exc:
                category, disposition = classify(exc)
            finally:
                runner.request_max_retries = previous_retries
                if stream is not None:
                    await stream.aclose()
            if valid:
                # 宿主收到最终结果会break；必须先还原预算、关闭provider生成器。
                state["state"] = "recovered" if state["retries"] or state["delayed"] else "completed"
                emit(event, "reply_retry", "success", calls=1, count=state["retries"])
                yield final
                return
            self.account(event, started)
            state.update(state="failed", category=category)
            emit(event, "reply_retry", "failed", calls=1, count=state["retries"], reason=category)
            if disposition == NOT_AN_ERROR:
                # 业务性取消不是故障：不重试、不告警，按主动静默收尾。
                self.cancel(event, runner, state, "no_reply")
                return
            if has_output:
                # 即使平台缓冲了片段也保守停止，不拼接重复的回复。
                break
            if disposition == FAIL_REPORT:
                # 无可用上游/程序缺陷：换候选没有意义，直接失败并上报。
                break
            retrying = True
            reason = self.stop_reason(event, runner, state, retrying=True)
            if reason:
                self.cancel(event, runner, state, reason)
                return
            if disposition == ROTATE_NOW_REPORT:
                # 认证/参数类错误重试不会变好：立即换候选，不占退避额度。
                await self.plugin._reply_health().failure(
                    "main_model", category, event=event)
                attempt += 1
                if attempt >= len(candidates):
                    break
                continue
            if state["retries"] < self.RETRIES:
                state["retries"] += 1
                delay = min(16, 2 ** (state["retries"] - 1)) + random.uniform(0, .25)
            elif not state["delayed"]:
                state.update(delayed=True, state="waiting")
                # 提醒未知结果不自动重发；无有效QQ/节流仍继续有界的最后尝试。
                notice_started = self.clock()
                try:
                    await asyncio.wait_for(self.plugin._reply_health().notify(
                        event=event, fallback=True,
                        message="【LivingHello 回复兜底】\n主模型已重试5次仍失败："
                                + ERROR_LABELS.get(category, category)
                                + "。等待1分钟后再尝试一次。"), timeout=10)
                except TimeoutError:
                    self.plugin._reply_health().notification = "send_unknown"
                finally:
                    self.account(event, notice_started)
                delay = self.COOLDOWN
            else:
                break
            reason = await self.wait(event, runner, state, delay)
            if reason:
                self.cancel(event, runner, state, reason)
                return
            attempt += 1
        row = event.get_extra(KEY)
        if row:
            row["fault"] = ("main_model", category)
        yield final if final is not None and getattr(final, "role", "") == "err" else LLMResponse(
            role="err", completion_text="LivingHello: main model retries exhausted.")

    async def close(self):
        self.closed = True
        tasks = [task for task in self.tasks
                 if task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for runner, *_ in list(self.bindings.values()):
            self.release(runner)
