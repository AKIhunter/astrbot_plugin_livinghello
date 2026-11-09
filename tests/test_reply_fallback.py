"""无付费调用：真实闸门、事件包装与模型请求重试边界的组合测试。"""
import asyncio
import ast
from collections.abc import AsyncGenerator
from contextlib import nullcontext
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain
from guard_engine import DecisionLog
from guard_reply_health import KEY, ReplyHealth, safe_excerpt
from guard_reply_fallback import ReplyFallback
from guard_webui import EDITABLE, _coerce
from test_gate import FakeProvider, gate_config, plugin
from test_reply_delivery import Chain, Event


def response(text="成功回答", *, role="assistant", chunk=False):
    return SimpleNamespace(completion_text=text, role=role, is_chunk=chunk,
                           tools_call_name=[], result_chain=None)


class Runner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls, self.sanitized = [], []
        self.provider = SimpleNamespace(meta=lambda: SimpleNamespace(id="main"),
                                        get_model=lambda: "fixture-main")
        self.fallback_providers = []
        self.request_max_retries = 5
        self.run_context = SimpleNamespace(messages=[{"role": "user", "content": "test"}])
        self.aborted = False

    def _is_stop_requested(self):
        return self.aborted

    def request_stop(self):
        self.aborted = True

    def _sanitize_malformed_tool_calls(self, item):
        self.sanitized.append(item)

    async def _iter_llm_responses(self, **kwargs):
        self.calls.append((self.provider, self.request_max_retries, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = await outcome()
        for item in outcome if isinstance(outcome, list) else [outcome]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def _iter_llm_responses_with_fallback(self):
        async for item in self._iter_llm_responses():
            yield item


class ReplyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.guard = plugin(gate_config(reply_fallback_enabled=True))
        self.guard.early_ack_at = {}
        self.guard.config["reply_early_ack_seconds"] = 0
        self.guard.data_dir = Path(directory.name)
        self.guard.engine.log = DecisionLog(self.guard.data_dir / "decisions.jsonl")
        self.guard.context.get_config = lambda: {"admins_id": ["9001"]}
        self.guard.context.send_message = AsyncMock(return_value=True)
        self.health = self.guard.reply_health = ReplyHealth(self.guard)
        self.now, self.waits = [time.monotonic()], []

        async def sleep(seconds):
            self.waits.append(seconds)
            self.now[0] += seconds
            await asyncio.sleep(0)
        self.retry = self.guard.reply_fallback = ReplyFallback(
            self.guard, clock=lambda: self.now[0], sleep=sleep)
        self.addAsyncCleanup(self.health.close)
        self.addAsyncCleanup(self.retry.close)
        self.event = self.make_event()

    async def asyncTearDown(self):
        await self.retry.close()
        await self.health.close()
        trace = getattr(self.guard, "request_trace", None)
        if trace is not None:
            await trace.close()
            if trace.writer is not None:
                await trace.writer

    def make_event(self):
        event = Event()
        event.unified_msg_origin = "qq-bot:GroupMessage:1001"
        event.get_platform_id = lambda: "qq-bot"
        event.get_platform_name = lambda: "aiocqhttp"
        event.set_extra("_livinghello_reply_approved", True)
        self.health.watch(event, expected=True)
        return event

    async def run_model(self, outcomes):
        runner = Runner(outcomes)
        self.assertTrue(self.retry.bind(self.event, runner))
        # 离线框架LLMResponse是object；仅替换构造器，生产重试代码保持原样。
        with patch("guard_reply_fallback.LLMResponse", side_effect=lambda **kw: response(
                kw["completion_text"], role=kw["role"])):
            result = [item async for item in runner._iter_llm_responses_with_fallback()]
        return runner, result

    async def deliver(self, result):
        await self.guard.trace_agent_done(self.event, None, result)
        packet = Chain([Plain(result.completion_text)])
        self.guard._delivery().patch(self.event)
        self.health.arm(self.event, packet)
        await self.event.send(packet)
        await self.health.finish(self.event)

    async def test_five_retries_then_notify_before_minute_and_final_success(self):
        self.event.message_str = '{"api_key": "fixture_hidden_1234"}'
        # 只开兜底，不开分类器恢复；独立开关仍应通知。
        self.assertFalse(self.health.enabled)
        notices = []
        self.guard.context.send_message.side_effect = lambda *args: notices.append(
            (len(self.waits), args)) or True
        runner, result = await self.run_model([TimeoutError()] * 6 + [response()])
        self.assertEqual(len(runner.calls), 7)
        self.assertTrue(all(item[1] == 1 for item in runner.calls))
        self.assertEqual(runner.request_max_retries, 5)
        self.assertEqual(len(notices), 1)
        notified_after = notices[0][0]
        self.assertAlmostEqual(sum(self.waits[notified_after:]), 60)
        route, chain = notices[0][1]
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertNotIn("fixture_hidden_1234", chain.chain[0].text)
        self.assertEqual(self.event.get_extra("_livinghello_reply_retry")["retries"], 5)
        await self.deliver(result[-1])
        self.assertEqual(self.health.summary["state"], "healthy")
        self.assertEqual(len(self.event.sent), 1)

    async def test_exhaustion_is_bounded_and_final_failure_still_recorded(self):
        runner, result = await self.run_model([ConnectionError("upstream")] * 7)
        self.assertEqual(len(runner.calls), 7)
        self.health.model_done(self.event, result[-1])
        await self.health.finish(self.event)
        self.assertEqual(self.health.summary["state"], "failed")
        self.assertEqual(self.health.summary["layer"], "main_model")
        self.assertEqual(len(self.guard.context.send_message.call_args_list), 1)
        self.assertEqual(self.guard.engine.log.recent()[0]["action"], "error")

    async def test_unknown_admin_notification_does_not_retry_notification_or_block_last_call(self):
        self.health.notify = AsyncMock(side_effect=TimeoutError())
        runner, result = await self.run_model([TimeoutError()] * 6 + [response()])
        self.assertEqual(len(runner.calls), 7)
        self.assertEqual(result[-1].role, "assistant")
        self.assertEqual(self.health.notify.await_count, 1)
        self.assertEqual(self.health.notification, "send_unknown")

    async def test_fast_recovery_does_not_notify_and_keeps_tool_results(self):
        tool_result = {"role": "tool", "content": "side effect already completed"}
        runner = Runner([TimeoutError(), response()])
        runner.run_context.messages.append(tool_result)
        self.retry.bind(self.event, runner)
        result = [item async for item in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(len(runner.calls), 2)
        self.assertIs(runner.run_context.messages[-1], tool_result)
        self.guard.context.send_message.assert_not_called()
        await self.deliver(result[-1])
        self.assertEqual(len(self.event.sent), 1)

    async def test_stream_prefix_never_replayed(self):
        runner, result = await self.run_model([
            [response("partial", chunk=True), TimeoutError()], response("duplicate")])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result[0].completion_text, "partial")
        self.assertEqual(result[-1].role, "err")
        self.assertEqual(self.waits, [])

    async def test_empty_and_error_roles_retry_but_tool_calls_and_no_reply_do_not(self):
        runner, result = await self.run_model([
            response(""), response("   "), response("provider unavailable", role="err"), response()])
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(result[-1].role, "assistant")
        for item in (response("[[ASTRBOT_NO_REPLY]]"), response("")):
            if not item.completion_text:
                item.tools_call_name = ["existing_tool"]
            self.event = self.make_event()
            runner, result = await self.run_model([item])
            self.assertEqual(len(runner.calls), 1)

    async def test_unknown_send_is_not_replayed_or_hidden_as_silence(self):
        self.event.set_extra("_livinghello_any_send_attempted", True)
        self.event.get_extra(KEY)["failed"] = True
        runner, result = await self.run_model([response()])
        self.assertEqual(runner.calls, [])
        self.assertEqual(result, [])
        self.assertFalse(self.event.get_extra("_livinghello_intentional_silence"))
        await self.health.finish(self.event)
        self.assertEqual(self.health.summary["state"], "failed")

    async def test_recall_policy_context_disable_and_abort_cancel_during_wait(self):
        for reason in ("message_recalled", "blacklist_user", "context_changed",
                       "retry_disabled", "agent_aborted"):
            self.guard.config["reply_fallback_enabled"] = True
            self.event = self.make_event()
            runner = Runner([TimeoutError(), response()])
            self.retry.bind(self.event, runner)

            async def cancel(_):
                if reason == "message_recalled":
                    self.event.set_extra("_at_guard_source_recalled", True)
                elif reason == "blacklist_user":
                    self.event.set_extra("_livinghello_policy_send_check", lambda: reason)
                elif reason == "context_changed":
                    self.event.set_extra("_at_guard_proactive", True)
                    self.event.set_extra("_at_guard_judged_revision", 0)
                    self.guard.context_revision["1001"] = 1
                elif reason == "retry_disabled":
                    self.guard.config["reply_fallback_enabled"] = False
                else:
                    runner.aborted = True
                await asyncio.sleep(0)
            self.retry.sleep = cancel
            result = [item async for item in runner._iter_llm_responses_with_fallback()]
            self.assertEqual(result, [])
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(self.event.get_extra("_livinghello_reply_retry")["reason"], reason)
            await self.health.finish(self.event)
        self.guard.context.send_message.assert_not_called()

    async def test_failed_wait_time_discount_does_not_erase_generation_expiry(self):
        self.event.set_extra("_at_guard_recorded_at", self.now[0])
        self.event.set_extra("_at_guard_proactive", True)
        self.event.set_extra("_at_guard_judged_revision", 0)
        import main
        with patch.object(main, "time", SimpleNamespace(
                monotonic=lambda: self.now[0], time=time.time)):
            _, result = await self.run_model([TimeoutError()] * 6 + [response()])
            self.assertEqual(result[-1].role, "assistant")
            self.assertEqual(self.guard._stale_reason(self.event, "1001"), "")
            self.now[0] += 31  # 成功生成耗时未计入兜底扣除。
            self.assertEqual(self.guard._stale_reason(self.event, "1001"), "message_expired")

    async def test_per_call_timeout_and_close_cancel_restore_instance(self):
        self.retry.CALL_TIMEOUT = .01
        slow = asyncio.Event()
        async def hang():
            await slow.wait()
        runner, result = await self.run_model([hang] * 7)
        self.assertEqual(len(runner.calls), 7)
        self.assertEqual(result[-1].role, "err")
        runner2 = Runner([hang])
        event2 = self.make_event()
        self.retry.CALL_TIMEOUT = 60
        self.retry.bind(event2, runner2)
        async def consume():
            return [item async for item in runner2._iter_llm_responses_with_fallback()]
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await self.retry.close()
        self.assertTrue(task.cancelled())
        self.assertNotIn("_iter_llm_responses_with_fallback", vars(runner2))
        self.assertEqual(runner2.request_max_retries, 5)

    async def test_retry_budget_shared_between_agent_steps_and_capacity(self):
        runner, _ = await self.run_model([TimeoutError()] * 5 + [response()])
        runner.outcomes = [TimeoutError(), response()]
        result = [item async for item in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(len(runner.calls), 8)  # 下一步仅原调用+尚未消费的延后尝试。
        self.assertEqual(result[-1].role, "assistant")
        self.retry.CAPACITY = len(self.retry.bindings)
        self.assertFalse(self.retry.bind(self.make_event(), Runner([response()])))

    async def test_default_off_and_unapproved_leave_native_boundary(self):
        runner = Runner([response()])
        self.guard.config.pop("reply_fallback_enabled")
        self.assertFalse(self.retry.bind(self.event, runner))
        self.guard.config["reply_fallback_enabled"] = True
        self.event.set_extra("_livinghello_reply_approved", False)
        self.assertFalse(self.retry.bind(self.event, runner))
        self.assertNotIn("_iter_llm_responses_with_fallback", vars(runner))
        self.assertIn("reply_fallback_enabled", EDITABLE)
        self.assertIs(_coerce("reply_fallback_enabled", True), True)
        self.assertEqual(_coerce("outage_notify_admin_qq", "9001, 9002"), ["9001", "9002"])

    async def test_real_host_pipeline_completion_keeps_normal_gate_silence(self):
        root = os.environ.get("ASTRBOT_HOST_SOURCE")
        if not root:
            self.skipTest("设置ASTRBOT_HOST_SOURCE验证实际宿主静默收尾")
        path = Path(root) / "astrbot/core/pipeline/scheduler.py"
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "PipelineScheduler")
        namespace = {
            "active_event_registry": SimpleNamespace(register=lambda _: None, unregister=lambda _: None),
            "AsyncGenerator": AsyncGenerator, "cast": lambda _, value: value,
            "log_context": lambda *_: nullcontext(), "event_context": lambda *_: nullcontext(),
            "trace_execution": lambda *_: nullcontext(), "logger": self.guard.logger,
            "WebChatMessageEvent": type("WebChatMessageEvent", (), {}),
            "WecomAIBotMessageEvent": type("WecomAIBotMessageEvent", (), {}),
        }
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
            ast.alias(name="annotations")], level=0), cls], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
        self.guard.config["classifier_outage_recovery_enabled"] = True
        event = Event()
        event.is_at_or_wake_command = False
        event.is_stopped = lambda: event.stopped
        event.unified_msg_origin = "qq-bot:GroupMessage:1001"
        event.get_platform_id = lambda: "qq-bot"
        event.get_platform_name = lambda: "aiocqhttp"
        event.trace = SimpleNamespace()
        event.cleanup_temporary_local_files = lambda: None
        self.guard.context.provider = FakeProvider(json.dumps({
            "act": "silent", "conf": 20, "understood": True,
            "reason": "conversation_ended", "ground": "none"}))
        async def process(current):
            await self.guard.guard_llm_request(current, SimpleNamespace(
                image_urls=[], contexts=[], extra_user_content_parts=[]))
            self.assertIs(current.get_extra(KEY)["owner"], asyncio.current_task())
        scheduler = object.__new__(namespace["PipelineScheduler"])
        scheduler.stages = [SimpleNamespace(process=process)]
        await asyncio.create_task(scheduler.execute(event))
        await asyncio.sleep(0)
        pending = [task for task in self.health.tasks if not task.done()
                   and task is not event.get_extra(KEY)["watchdog"]
                   and task is not self.event.get_extra(KEY)["watchdog"]]
        await asyncio.gather(*pending)
        self.assertTrue(event.get_extra(KEY)["ended"])
        self.assertNotEqual(self.health.summary.get("state"), "failed")
        self.assertEqual(self.guard.engine.log.recent()[0]["action"], "silence")
        self.guard.context.send_message.assert_not_called()
        self.assertIsNone(self.health.claimed_at)

    async def test_real_gate_silences_do_not_log_errors_or_claim_admin_quota(self):
        for case in ("classifier", "mentions_other", "duplicate", "cooldown", "inflight"):
            event = self.make_event()
            event.extra.pop("_livinghello_reply_health")
            event.extra.pop("_livinghello_reply_approved")
            event.is_at_or_wake_command = False
            self.guard.context.provider = FakeProvider(json.dumps({
                "act": "silent", "conf": 20, "understood": True,
                "reason": "conversation_ended", "ground": "none"}))
            if case == "mentions_other":
                event._mentions = [At(qq="7001")]
            if case == "duplicate":
                self.guard.config["duplicate_window_seconds"] = 60
                self.guard.replied_fingerprints[("1001", self.guard._fingerprint(
                    event.message_str))] = time.monotonic()
            if case == "cooldown":
                self.guard.config["proactive_cooldown_seconds"] = 60
                self.guard.last_proactive_reply["1001"] = time.monotonic()
            if case == "inflight":
                self.guard.proactive_inflight_until["1001"] = time.monotonic() + 60
            await self.guard.guard_llm_request(event, SimpleNamespace(
                extra_user_content_parts=[], image_urls=[], contexts=[]))
            self.assertTrue(event.stopped)
            await self.health.finish(event)
            self.assertFalse(event.get_extra(KEY)["expected"])
            self.assertTrue(all(row["action"] != "error"
                                for row in self.guard.engine.log.recent()))
            self.guard.replied_fingerprints.clear()
            self.guard.last_proactive_reply.clear()
            self.guard.proactive_inflight_until.clear()
            self.guard.config.update(duplicate_window_seconds=0, proactive_cooldown_seconds=0)
        self.guard.context.send_message.assert_not_called()
        self.assertIsNone(self.health.claimed_at)

    async def test_nonproactive_no_reply_is_normal_terminal(self):
        item = response("[[ASTRBOT_NO_REPLY]]")
        await self.guard.suppress_no_reply_and_track(self.event, item)
        self.health.model_done(self.event, item)
        await self.health.finish(self.event)
        self.assertEqual(item.completion_text, "")
        self.guard.context.send_message.assert_not_called()
        self.assertEqual(self.guard.engine.log.recent(), [])

    async def test_quoted_credentials_hidden_in_notifications_and_new_disk_record(self):
        for text in ('{"api_key":"fixture_hidden_1234","password":"fixture_password_1234"}',
                     "{'API-KEY': 'fixture hidden 1234', 'password'='fixture password 1234'}",
                     '{"authorization":"Bearer fixture hidden 1234"}',
                     '{"password":"fixture \\"hidden\\" 1234"}',
                     'token=fixture_hidden_1234', '"password": "fixture unclosed 1234'):
            self.assertNotIn("fixture", safe_excerpt(text))
        self.guard.config["classifier_outage_recovery_enabled"] = True
        self.event.message_str = '{"api_key":"fixture_hidden_1234","password":"fixture_password_1234"}'
        await self.health.failure("main_model", "quota_exhausted", event=self.event)
        self.assertNotIn("fixture", self.guard.context.send_message.call_args[0][1].chain[0].text)
        stored = DecisionLog(self.guard.engine.log.path).recent()
        self.assertNotIn("fixture", json.dumps(stored))

    async def test_real_host_request_methods_and_hook_binding_under_pipeline(self):
        """加载完整方法AST；依赖与发送器替身，无付费API。"""
        root = os.environ.get("ASTRBOT_HOST_SOURCE")
        if not root:
            self.skipTest("设置ASTRBOT_HOST_SOURCE执行实际宿主方法验证")
        root = Path(root)
        runner_path = root / "astrbot/core/agent/runners/tool_loop_agent_runner.py"
        scheduler_path = root / "astrbot/core/pipeline/scheduler.py"
        namespace = {
            "active_event_registry": SimpleNamespace(register=lambda _: None, unregister=lambda _: None),
            "asyncio": asyncio, "time": time, "override": lambda method: method,
            "AgentState": SimpleNamespace(IDLE="idle", RUNNING="running"),
            "logger": self.guard.logger, "AsyncGenerator": AsyncGenerator,
            "cast": lambda _, value: value, "log_context": lambda *_: nullcontext(),
            "event_context": lambda *_: nullcontext(), "trace_execution": lambda *_: nullcontext(),
            "WebChatMessageEvent": type("WebChatMessageEvent", (), {}),
            "WecomAIBotMessageEvent": type("WecomAIBotMessageEvent", (), {}),
        }
        def load(path, name, methods=None):
            tree = ast.parse(path.read_text())
            cls = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == name)
            cls.bases, cls.decorator_list = [], []
            if methods:
                cls.body = [node for node in cls.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name in methods]
            module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
                ast.alias(name="annotations")], level=0), cls], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
            return namespace[name]
        native = load(runner_path, "ToolLoopAgentRunner",
                      {"step", "request_stop", "_await_or_stop", "_iter_llm_responses",
                       "_iter_llm_responses_with_fallback"})
        scheduler_cls = load(scheduler_path, "PipelineScheduler")
        event = self.event
        event.extra.pop("_livinghello_reply_approved")
        event.extra.pop(KEY)
        event.is_stopped = lambda: event.stopped
        event.trace = SimpleNamespace()
        event.cleanup_temporary_local_files = lambda: None
        runner = native()
        outputs = [TimeoutError()] * 6 + [response()]
        calls = []
        async def chat(**payload):
            calls.append(payload)
            item = outputs.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        runner.provider = SimpleNamespace(text_chat=chat, meta=lambda: SimpleNamespace(id="native"),
                                          get_model=lambda: "native-fixture")
        runner.req = SimpleNamespace(session_id=event.unified_msg_origin, conversation=None,
                                     extra_user_content_parts=[], model="native-fixture",
                                     image_urls=[], contexts=[])
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event),
                                              messages=[{"role": "user", "content": "synthetic"}])
        runner.request_max_retries, runner.fallback_providers, runner.streaming = 5, [], False
        runner._abort_signal = asyncio.Event()
        runner._is_stop_requested = runner._abort_signal.is_set
        runner._sanitize_contexts_for_provider = lambda messages: messages
        runner._func_tool_for_provider = lambda: None
        runner._sanitize_malformed_tool_calls = lambda _: None
        runner._close_executor = lambda stream: stream.aclose()
        runner._step_budget_max, runner._state = None, "idle"
        runner._transition_state = lambda _: None
        runner._simple_print_message_role = lambda *_: None
        runner.request_context_manager = SimpleNamespace(process=AsyncMock(return_value=None))
        runner._finalize_aborted_step = AsyncMock(return_value="synthetic-aborted-stage")
        runner.agent_hooks = SimpleNamespace(on_agent_begin=lambda ctx:
            self.guard.trace_agent_begin(event, ctx))
        # 先调用真实step的完整on_agent_begin路径，再使用其真实模型请求方法。
        async def flow(current):
            await self.guard.guard_llm_request(current, runner.req)
            await self.guard.fit_final_request_images(current, runner.req)
            self.assertTrue(current.get_extra("_livinghello_reply_approved"))
            await anext(runner.step())
            self.assertIn("_iter_llm_responses_with_fallback", vars(runner))
            self.assertIs(current.get_extra(KEY)["owner"], asyncio.current_task())
            async for item in runner._iter_llm_responses_with_fallback():
                self.assertEqual(item.role, "assistant")
                # 模拟宿主最终结果后break，预算必须已经还原。
                self.assertEqual(runner.request_max_retries, 5)
                await self.deliver(item)
                break
        scheduler = object.__new__(scheduler_cls)
        scheduler.stages = [SimpleNamespace(process=flow)]
        with patch("guard_reply_fallback.LLMResponse", side_effect=lambda **kw:
                   response(kw["completion_text"], role=kw["role"])):
            await asyncio.create_task(scheduler.execute(event))
        await asyncio.sleep(0)
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(payload["request_max_retries"] == 1 for payload in calls))
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.guard.context.send_message.await_count, 1)
        self.assertNotIn("_iter_llm_responses_with_fallback", vars(runner))
