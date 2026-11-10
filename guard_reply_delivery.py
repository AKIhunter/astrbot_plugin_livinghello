"""发送节奏与撤回保护。只包装当前事件，不修改平台或其他会话。"""
from __future__ import annotations

import asyncio
import copy
import sys
import math
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from astrbot.api.message_components import At, Plain, Reply
try:
    from .guard_request_trace import emit, TRACE_KEY
    from .guard_reply_health import KEY as HEALTH_KEY, send_accepted
except ImportError:
    from guard_request_trace import emit, TRACE_KEY
    from guard_reply_health import KEY as HEALTH_KEY, send_accepted
try:
    from .guard_thinking_filter import strip_thinking_chain
except ImportError:
    from guard_thinking_filter import strip_thinking_chain

# 兼容用的历史标识：congling/core/rhythm.py 仍按这个键名和 "at_guard" 值判断
# 回复归属，改名会让从绫把 LivingHello 的回复当成自己的。不要改。
OWNER_KEY = "_at_guard_reply_owner"
RECALLED_KEY = "_at_guard_source_recalled"


@dataclass
class RecallRecord:
    created: float
    recalled: asyncio.Event
    batch: object = None

    def __getitem__(self, key):
        return getattr(self, key)


def number(config, key, default, low, high):
    try:
        value = float(config.get(key, default))
        if not math.isfinite(value):
            return default
        return max(low, min(high, value))
    except (TypeError, ValueError, OverflowError):
        return default


def enabled(config, key):
    return config.get(key, False) is True


class ReplyDelivery:
    TTL = 3600.0
    CAPACITY = 4096

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        # key=(完整会话, 平台原消息ID)，notice中的随机事件ID不能用于取消。
        self.records = OrderedDict()
        # 容量清理仅清理索引；仍被事件持有的记录继续接收撤回，不漏取消。
        self.active_records = weakref.WeakValueDictionary()
        self.tasks = set()
        self.closed = False

    @staticmethod
    def raw(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if isinstance(raw, Mapping) else {}

    def key(self, event, message_id=None):
        if message_id is None:
            message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
        scope = str(getattr(event, "unified_msg_origin", "") or "")
        mid = str(message_id or "").strip()
        return (scope, mid) if scope and mid and mid not in {"0", "None", "null"} else None

    def _record(self, key):
        now = time.monotonic()
        while self.records:
            first, row = next(iter(self.records.items()))
            if now - row["created"] <= self.TTL:
                break
            self.records.pop(first)
        if key not in self.records:
            row = self.active_records.get(key)
            self.records[key] = row if row is not None else RecallRecord(now, asyncio.Event())
            self.active_records[key] = self.records[key]
        while len(self.records) > self.CAPACITY:
            self.records.popitem(last=False)
        return self.records[key]

    def notice(self, event):
        raw = self.raw(event)
        if raw.get("post_type") != "notice" or raw.get("notice_type") not in {
            "group_recall", "friend_recall",
        }:
            return False
        if not self.recall_enabled(event, raw.get("notice_type") == "group_recall"):
            # 通知始终停止，不把撤回通知送入人格或主模型。
            event.stop_event()
            return True
        key = self.key(event, raw.get("message_id"))
        if key:
            row = self.active_records.get(key)
            # 只认监听后且安静期截止前的撤回，不让生成/打字延迟延长保护。
            if row and row.batch is not None and row.batch.clock() < row.batch.deadline:
                row["recalled"].set()
        # 撤回通知不是一条新用户消息，不交给人格/记忆/主模型。
        event.stop_event()
        return True

    def recall_enabled(self, event, group=None):
        group = bool(event.get_group_id()) if group is None else group
        return enabled(self.config, "reply_recall_group" if group else "reply_recall_private")

    def owns_expression(self):
        return self.config.get("expression_takeover_enabled", True) is True

    def recalled(self, event):
        if event.get_extra(RECALLED_KEY, False):
            return True
        # 合并输入的任意原消息撤回，都必须取消整个尚未发送的回复。
        for source in (event, *event.get_extra("_at_guard_fragment_sources", ())):
            row = source.get_extra("_at_guard_recall_record")
            if source.get_extra(RECALLED_KEY, False) or (row and row["recalled"].is_set()):
                event.set_extra(RECALLED_KEY, True)
                return True
        return False

    def block(self, event):
        emit(event, "delivery", "cancelled", reason="message_recalled")
        service = event.get_extra(TRACE_KEY)
        if service:
            service.finish(event, "cancelled", "message_recalled")
        event.set_extra(RECALLED_KEY, True)
        event.set_extra("_at_guard_delivery_blocked", "message_recalled")
        result = event.get_result()
        if result is not None:
            result.chain = []
            if hasattr(result, "async_stream"):
                result.async_stream = None
        event.stop_event()

    async def pause(self, event, seconds):
        if self.closed:
            raise asyncio.CancelledError
        if not self.policy_allows(event):
            return False
        if self.recalled(event):
            self.block(event)
            return False
        if seconds > 0:
            rows = [source.get_extra("_at_guard_recall_record") for source in
                    (event, *event.get_extra("_at_guard_fragment_sources", ()))]
            waits = [asyncio.create_task(row["recalled"].wait()) for row in rows if row]
            if waits:
                try:
                    await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
            else:
                await asyncio.sleep(seconds)
        if self.closed:
            raise asyncio.CancelledError
        if not self.policy_allows(event):
            return False
        if self.recalled(event):
            self.block(event)
            return False
        return True

    @staticmethod
    def policy_allows(event):
        checker = event.get_extra("_livinghello_policy_send_check")
        reason = checker() if callable(checker) else ""
        if not reason:
            return True
        event.set_extra("_at_guard_delivery_blocked", reason)
        result = event.get_result()
        if result is not None:
            result.chain = []
            if hasattr(result, "async_stream"):
                result.async_stream = None
        event.stop_event()
        emit(event, "delivery", "cancelled", reason=reason)
        return False

    async def prepare(self, event):
        if self.notice(event):
            return False
        if self.raw(event).get("post_type") == "notice":
            return True
        if event.get_extra("_at_guard_received_at") is None:
            event.set_extra("_at_guard_received_at", time.monotonic())
        if event.get_extra("_at_guard_delivery_prepared", False):
            return not self.recalled(event)
        recall = self.recall_enabled(event)
        style = self.owns_expression()
        segments = getattr(self, "segments", None)
        segmenting = bool(segments and segments.active(event))
        if not recall and not style and not segmenting:
            return True
        event.set_extra("_at_guard_delivery_prepared", True)
        if recall and (key := self.key(event)):
            row = self._record(key)
            row.batch = getattr(event.get_extra("_at_guard_fragment_ticket"), "batch", None)
            event.set_extra("_at_guard_recall_record", row)
        event.set_extra("_at_guard_expression_received", time.monotonic())
        # 直接event.send只复核撤回；主模型最终输出才执行表达计划。
        if self.patch(event) and style:
            event.set_extra(OWNER_KEY, "at_guard")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await self.pause(event, 0)
        finally:
            self.tasks.discard(task)

    @staticmethod
    def derive(message, chain):
        derive = getattr(message, "derive", None)
        if callable(derive):
            return derive(list(chain))
        result = copy.copy(message)
        result.chain = list(chain)
        return result

    def typing_seconds(self, chain, event=None):
        # 速度 0 即关闭模拟；不再单独维护开关，避免「开关开着但速度 0」的歧义。
        speed = number(self.config, "reply_typing_chars_per_second", 0, 0, 20)
        if speed <= 0 or not self.owns_expression():
            return 0.0
        text = "".join(c.text for c in chain if isinstance(c, Plain))
        chars = len(re.sub(r"\s", "", text))
        return chars / speed

    async def typing_pause(self, event, seconds):
        started = time.monotonic()
        previous = event.get_extra("_at_guard_typing_seconds", 0)
        event.set_extra("_at_guard_typing_started", started)
        emit(event, "typing", "running" if seconds else "disabled")
        try:
            return await self.pause(event, seconds)
        finally:
            event.set_extra("_at_guard_typing_seconds", previous + max(0, time.monotonic() - started))
            event.set_extra("_at_guard_typing_started", None)
            emit(event, "typing", "cancelled" if self.recalled(event) else "success" if seconds else "disabled",
                 ms=round((time.monotonic() - started) * 1000, 2))

    def arm(self, event, result, *, streaming=False):
        """登记结果身份；派生链继承局部标记，宿主可自由创建Plain或转换媒体。"""
        if event.get_extra(OWNER_KEY) != "at_guard" or not (
                streaming or getattr(result, "is_llm_result", lambda: False)()):
            return
        if event.get_extra("_at_guard_delivery_plan"):
            return
        original_derive = getattr(result, "derive", None)
        if not callable(original_derive):
            # 未知宿主保留原生完整结果，不认领旧全文或吞正文。
            return
        token = object()
        try:
            def owned_derive(chain=None):
                message = original_derive(chain)
                message._at_guard_delivery_token = token
                return message
            result.derive = owned_derive
            result._at_guard_delivery_token = token
        except (AttributeError, TypeError):
            self.logger.warning("[LivingHello] 当前结果不能登记发送策略，保留原生回复")
            return
        event.set_extra("_at_guard_delivery_plan", {
            "token": token, "semantic_headers": {
                id(c): c for c in result.chain or [] if isinstance(c, (Reply, At))},
            "attempted": {}, "header_sent": False, "failed": False,
        })
        event.set_extra("_at_guard_actual_sent", False)
        event.set_extra("_at_guard_sent_has_content", False)
        event.set_extra("_at_guard_sent_text", "")

    def patch(self, event):
        if event.get_extra("_at_guard_send_patched", False):
            return True
        original = getattr(event, "send", None)
        if not callable(original):
            return False
        # 事件局部锁也挡住同一回复的并行发送，不产生全局群聊锁。
        lock = asyncio.Lock()

        async def wrapped(message, *args, **kwargs):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                _p = event.get_extra("_at_guard_delivery_plan") or {}
                self.logger.warning(
                    "[LH-probe] wrapped enter: chain=%d token_ok=%r plan_failed=%r "
                    "attempted=%d",
                    len(getattr(message, "chain", None) or []),
                    getattr(message, "_at_guard_delivery_token", None) is _p.get("token"),
                    bool(_p.get("failed")),
                    len(_p.get("attempted") or {}),
                )
                async with lock:
                    if not await self.pause(event, 0):
                        return
                    health = event.get_extra(HEALTH_KEY)
                    if health:
                        # 原生内部Agent在except里直接event.send，自定义错误文案也须拦截。
                        caller_frame = sys._getframe(1)
                        caller = caller_frame.f_code.co_filename.replace("\\", "/")
                        native_error = (sys.exc_info()[1] if caller.endswith(
                            "/agent_sub_stages/internal.py") else None)
                        if native_error is not None:
                            runner = caller_frame.f_locals.get("agent_runner")
                            failed_provider = getattr(runner, "provider", None)
                            if failed_provider is not None:
                                health["service"].provider(event, failed_provider)
                                health["provider_evidence"] = "native_error_runner"
                        del caller_frame
                        if await health["service"].suppress_native_error(
                                event, message, native_exception=native_error):
                            return
                    plan = event.get_extra("_at_guard_delivery_plan")
                    incoming = getattr(message, "chain", []) or []
                    belongs = bool(plan and getattr(message, "_at_guard_delivery_token", None)
                                   is plan["token"])
                    if not belongs:
                        expression = getattr(self, "expression", None)
                        if expression and event.get_extra(OWNER_KEY) == "at_guard":
                            # 失败提示等原生直发也可能带引用；只移除私聊当前消息头。
                            chain = expression.private_headers(event, incoming)
                            if chain != list(incoming):
                                message = self.derive(message, chain)
                                incoming = chain
                        if incoming:
                            event.set_extra("_livinghello_any_send_attempted", True)
                        try:
                            sent = await original(message, *args, **kwargs)
                        except BaseException as exc:
                            self.health_sent(event, message, False)
                            self.trace_failure(event, exc)
                            raise
                        if not send_accepted(sent):
                            self.health_sent(event, message, False)
                            self.trace_failure(event, RuntimeError("negative_send_receipt"))
                            return sent
                        self.health_sent(event, message, True)
                        self.trace_sent(event, incoming, "direct")
                        if not plan:
                            await self.note_sent(event, incoming)
                        return sent
                    if plan["failed"]:
                        return
                    # 记录实际发送组件（保留强引用防止ID复用）；重复包只消费未尝试正文。
                    body = [c for c in incoming if not isinstance(c, (Reply, At))]
                    pending = [c for c in body if id(c) not in plan["attempted"]]
                    if not pending:
                        return
                    pending_ids = {id(c) for c in pending}
                    chain = [c for c in incoming if isinstance(c, (Reply, At)) or id(c) in pending_ids]
                    plan["attempted"].update({id(c): c for c in pending})
                    event.set_extra("_livinghello_any_send_attempted", True)
                    expression = getattr(self, "expression", None)
                    checker = event.get_extra("_at_guard_send_check")
                    reason = checker() if callable(checker) else ""
                    if reason:
                        emit(event, "delivery", "cancelled", reason=reason)
                        service = event.get_extra(TRACE_KEY)
                        if service:
                            service.finish(event, "cancelled", reason)
                        event.set_extra("_at_guard_delivery_blocked", reason)
                        plan["failed"] = True
                        return
                    if not await self.typing_pause(event, self.typing_seconds(pending, event)):
                        plan["failed"] = True
                        return
                    reason = checker() if callable(checker) else ""
                    if reason:
                        emit(event, "delivery", "cancelled", reason=reason)
                        service = event.get_extra(TRACE_KEY)
                        if service:
                            service.finish(event, "cancelled", reason)
                        event.set_extra("_at_guard_delivery_blocked", reason)
                        plan["failed"] = True
                        return
                    if expression:
                        chain = expression.before_send(event, chain, plan)
                    try:
                        sent = await original(self.derive(message, chain), *args, **kwargs)
                    except BaseException as exc:
                        plan["failed"] = True
                        self.health_sent(event, message, False)
                        self.trace_failure(event, exc)
                        raise
                    if not send_accepted(sent):
                        plan["failed"] = True
                        self.health_sent(event, message, False)
                        self.trace_failure(event, RuntimeError("negative_send_receipt"))
                        return sent
                    self.health_sent(event, message, True)
                    self.trace_sent(event, chain, "reply_packet")
                    if any(not isinstance(c, (Reply, At)) and type(c).__name__ != "Record"
                           for c in chain):
                        plan["header_sent"] = True
                    await self.note_sent(event, chain)
                    return sent
            finally:
                self.tasks.discard(task)

        try:
            event.send = wrapped
            event.set_extra("_at_guard_send_patched", True)
        except (AttributeError, TypeError):
            self.logger.warning("[LivingHello] 当前事件无法包装发送，表达保留宿主行为")
            return False

        original_streaming = getattr(event, "send_streaming", None)
        if callable(original_streaming):
            async def buffered_stream(stream, *args, **kwargs):
                # 开启接管/撤回保护时等待生成完成，避免边生成边发送越过撤回检查。
                task = asyncio.current_task()
                self.tasks.add(task)
                try:
                    if event.get_extra("_at_guard_stream_consumed", False):
                        return
                    event.set_extra("_at_guard_stream_consumed", True)
                    chain, template = [], None
                    async for chunk in stream:
                        if not await self.pause(event, 0):
                            return
                        if getattr(chunk, "type", "") in {"break", "reasoning", "tool_call", "agent_stats"}:
                            continue
                        template = chunk
                        chain.extend(getattr(chunk, "chain", []) or [])
                    if template is None or not chain:
                        return
                    full = self.derive(template, chain)
                    # 流式 delta.content 里的思考段不会被 core 剥离，
                    # 必须在合并成整段后、切段之前清掉，否则会被切成多条气泡发出。
                    try:
                        merged = list(getattr(full, 'chain', None) or [])
                        before_len = sum(len(c.text) for c in merged
                                        if isinstance(c, Plain))
                        if strip_thinking_chain(merged):
                            full.chain = merged
                            after_len = sum(len(c.text) for c in merged
                                           if isinstance(c, Plain))
                            emit(event, 'thinking_filter', 'success',
                                 removed=before_len - after_len)
                            if not full.chain:
                                return
                    except Exception as exc:
                        self.logger.warning(
                            '[LivingHello] thinking 清洗失败: %r', exc)
                    health_row = event.get_extra(HEALTH_KEY)
                    if health_row and health_row["model_done"]:
                        # 生成器完全耗尽之后才认领；工具直接发送不继承该标记。
                        full._livinghello_reply_token = health_row["token"]
                    if event.get_extra(OWNER_KEY) == "at_guard":
                        expression = getattr(self, "expression", None)
                        if expression:
                            expression.clean_chain(full, event)
                        if not full.chain:
                            return
                        plan = event.get_extra("_at_guard_delivery_plan")
                        if plan is None:
                            self.arm(event, full, streaming=True)
                            plan = event.get_extra("_at_guard_delivery_plan")
                        if plan:
                            full._at_guard_delivery_token = plan["token"]
                    if health_row and health_row["model_done"]:
                        health_row["service"].arm(event, full, streaming=True)
                    segments = getattr(self, "segments", None)
                    packets = segments.stream_packets(event, full) if segments else [full]
                    for packet in packets:
                        await wrapped(packet)
                finally:
                    self.tasks.discard(task)
            event.send_streaming = buffered_stream
        return True

    @staticmethod
    def health_sent(event, message, accepted):
        row = event.get_extra(HEALTH_KEY)
        if row:
            row["service"].sent(event, message, accepted)

    async def note_sent(self, event, chain):
        if not chain:
            return
        event.set_extra("_at_guard_actual_sent", True)
        event.set_extra("_at_guard_sent_has_content", True)
        previous = event.get_extra("_at_guard_sent_text", "")
        event.set_extra("_at_guard_sent_text", previous + "".join(
            c.text for c in chain if isinstance(c, Plain)))

    @staticmethod
    def trace_sent(event, chain, source):
        if not chain:
            return
        service = event.get_extra(TRACE_KEY)
        if service:
            service.increment(event, "sent_packets")
        if source == "reply_packet" and event.get_extra("_livinghello_agent_done"):
            event.set_extra("_livinghello_final_packet_sent", True)
        emit(event, "send", "success", source=source, types=[type(c).__name__ for c in chain])
        # 原生转换只能证明已发送此组件，不据此推造TTS/文本转图内部执行。
        if any(type(c).__name__ == "Record" for c in chain):
            emit(event, "native_tts", "success", reason="record_sent")
        if any(type(c).__name__ == "Image" for c in chain):
            emit(event, "native_text_image", "uncollected", reason="image_origin_not_proven")

    @staticmethod
    def trace_failure(event, exc):
        state = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        emit(event, "send", state, error_type=type(exc).__name__)
        service = event.get_extra(TRACE_KEY)
        if service:
            service.finish(event, state, "send_exception")

    async def close(self):
        self.closed = True
        tasks = [t for t in self.tasks if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.records.clear()
        self.active_records.clear()
