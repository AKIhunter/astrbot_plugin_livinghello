"""隔离回复闭环与提醒时间窗；禁止真实模型和平台调用。"""
import asyncio
from contextlib import nullcontext
import itertools
from pathlib import Path
import random
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import At, Plain
from reply.guard_reply_health import KEY, ReminderStore, ReplyHealth, qq_ids, tool_result_delivered
from console.guard_webui import _coerce, _outage_status
from test_gate import plugin, gate_config
from test_reply_delivery import Event, Chain


class PipelineScheduler:
    async def execute(self, callback):
        await callback()


class ReplyHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.guard = plugin(gate_config(classifier_outage_recovery_enabled=True))
        self.guard.data_dir = Path(directory.name)
        self.guard.last_bot_line_at = {}
        self.guard.context_revision = {}
        self.guard.context.get_config = lambda: {"admins_id": ["9001", "opaque-admin", "9001"]}
        self.sent = []

        async def send(route, chain):
            self.sent.append((route, chain))
            return True
        self.guard.context.send_message = send
        self.now = [1000.0]
        self.health = ReplyHealth(self.guard, clock=lambda: self.now[0])
        self.guard.reply_health = self.health
        self.addAsyncCleanup(self.health.close)
        self.event = self.event_for()

    @staticmethod
    def event_for():
        event = Event()
        event.unified_msg_origin = "qq-bot:GroupMessage:1001"
        event.get_platform_id = lambda: "qq-bot"
        event.get_platform_name = lambda: "aiocqhttp"
        return event

    async def complete(self, *, response=True, content=True, fail=False, direct=False):
        event = self.event
        self.health.watch(event, expected=True)
        self.guard._delivery().patch(event)
        if direct:
            await event.send(Chain([Plain("工具提示或者错误信息")], llm=False))
        if response:
            self.health.model_done(event, object())
        result = Chain([Plain("正式回答")] if content else [At(qq="123")])
        self.health.arm(event, result)
        if fail:
            # 包装前捕获原始发送返回；显式失败不得记作投递成功。
            self.health.sent(event, result, False)
        else:
            await event.send(result.derive(result.chain))
        await self.health.finish(event)

    async def test_final_success_and_fallback_suppress_reminder(self):
        self.health.classifier_failed(self.event, "timeout")
        await self.complete(direct=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.health.summary["state"], "healthy")

    async def test_main_failure_error_prompt_is_not_final(self):
        await self.complete(response=False, direct=True)
        self.assertEqual(self.health.summary["layer"], "main_model")
        self.assertEqual(len(self.sent), 1)
        route, chain = self.sent[0]
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertEqual([str(c.qq) for c in chain.chain if isinstance(c, At)], [])
        self.assertNotIn("123", [str(c.qq) for c in chain.chain if isinstance(c, At)])
        self.assertEqual(self.event.get_extra(KEY)["sent"], 0)

    async def test_native_task_completion_detects_swallowed_error(self):
        async def flow():
            self.health.watch(self.event, expected=True)
            self.guard._delivery().patch(self.event)
            await self.event.send(Chain([Plain("error sk-secret")], llm=False))
        await asyncio.create_task(PipelineScheduler().execute(flow))
        await asyncio.sleep(0)  # 宿主 await 返回先于后注册的 done callback。
        await asyncio.gather(*list(self.health.tasks - {self.event.get_extra(KEY)["watchdog"]}))
        self.assertTrue(self.event.get_extra(KEY)["ended"])
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("sk-secret", str(self.sent[0][1].chain))

    async def test_real_host_pipeline_task_contract(self):
        try:
            import astrbot.core.pipeline.scheduler as native
        except ImportError:
            from host_fixtures import scheduler
            try:
                native = scheduler()
            except ImportError as exc:
                self.skipTest(str(exc))
        scheduler = object.__new__(native.PipelineScheduler)
        async def flow(_):
            self.health.watch(self.event, expected=True)
        scheduler._process_stages = flow
        self.event.get_platform_name = lambda: "aiocqhttp"
        self.event.trace = SimpleNamespace()
        self.event.cleanup_temporary_local_files = lambda: None
        with patch.object(native, "log_context", lambda *_: nullcontext()), \
             patch.object(native, "event_context", lambda *_: nullcontext(), create=True), \
             patch.object(native, "trace_execution", lambda *_: nullcontext(), create=True), \
             patch.object(native, "active_event_registry",
                          SimpleNamespace(register=lambda _: None, unregister=lambda _: None),
                          create=True):
            await asyncio.create_task(scheduler.execute(self.event))
        await asyncio.sleep(0)
        await asyncio.gather(*list(self.health.tasks - {self.event.get_extra(KEY)["watchdog"]}))
        self.assertTrue(self.event.get_extra(KEY)["ended"])
        self.assertEqual(len(self.sent), 1)

    async def test_false_and_negative_receipts_and_partial_packets(self):
        for receipt in (False, {"status": "failed"}, {"retcode": 100}, {"success": False}):
            event = self.event_for()
            self.health.watch(event, expected=True)
            self.health.model_done(event, object())
            event.send = AsyncMock(side_effect=[None, receipt])
            self.guard._delivery().patch(event)
            result = Chain([Plain("第一段"), Plain("第二段")])
            self.health.arm(event, result)
            await event.send(result.derive([result.chain[0]]))
            await event.send(result.derive([result.chain[1]]))
            await self.guard.track_sent_message(event)  # 宿主仍会调用此回调。
            await self.health.finish(event)
            self.assertEqual(self.health.summary["state"], "failed")
            self.assertTrue(event.get_extra(KEY)["failed"])
        self.assertEqual(len(self.sent), 1)

    async def test_final_exception_even_after_partial_success(self):
        self.health.watch(self.event, expected=True)
        self.health.model_done(self.event, object())
        self.event.send = AsyncMock(side_effect=[None, TimeoutError()])
        self.guard._delivery().patch(self.event)
        result = Chain([Plain("一"), Plain("二")])
        self.health.arm(self.event, result)
        await self.event.send(result.derive([result.chain[0]]))
        with self.assertRaises(TimeoutError):
            await self.event.send(result.derive([result.chain[1]]))
        await self.health.finish(self.event)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.health.summary["category"], "send_unknown")

    async def test_empty_or_only_mention_is_not_reply(self):
        await self.complete(content=False)
        self.assertEqual(self.health.summary["layer"], "delivery")
        self.assertEqual(len(self.sent), 1)

    async def test_stream_full_completion_and_broken_stream(self):
        self.health.watch(self.event, expected=True)
        self.guard._delivery().patch(self.event)
        async def stream():
            yield Chain([Plain("完整回答")])
            self.health.model_done(self.event, object())
        await self.event.send_streaming(stream())
        await self.health.finish(self.event)
        self.assertEqual(self.sent, [])
        other = self.event_for()
        self.health.watch(other, expected=True)
        self.guard._delivery().patch(other)
        async def broken():
            yield Chain([Plain("半截")])
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            await other.send_streaming(broken())
        await self.health.finish(other)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(other.sent, [])

    async def test_unknown_notification_no_retry_until_exact_boundary(self):
        self.guard.context.send_message = AsyncMock(side_effect=TimeoutError())
        await self.health.failure("delivery", "send_unknown", event=self.event)
        self.assertEqual(self.health.notification, "send_unknown")
        self.now[0] += 599
        await self.health.failure("main_model", "model_reply_missing", event=self.event)
        self.assertEqual(self.guard.context.send_message.await_count, 1)
        self.now[0] += 1
        await self.health.failure("classifier", "timeout", event=self.event)
        self.assertEqual(self.guard.context.send_message.await_count, 2)

    async def test_concurrency_reload_crash_and_clock_rollback(self):
        rng = random.Random(20261106)
        await asyncio.gather(*(self.health.failure(rng.choice(["delivery", "main_model"]),
                                                  "send_unknown", event=self.event) for _ in range(32)))
        self.assertEqual(len(self.sent), 1)
        reloaded = ReplyHealth(self.guard, clock=lambda: self.now[0])
        self.addAsyncCleanup(reloaded.close)
        self.now[0] -= 30
        await reloaded.failure("classifier", "timeout", event=self.event)
        self.assertEqual(len(self.sent), 1)
        self.now[0] = 1600
        await reloaded.failure("classifier", "timeout", event=self.event)
        self.assertEqual(len(self.sent), 2)
        store = ReminderStore(self.guard.data_dir)
        self.assertFalse(store.claim(1601))  # 模拟认领后崩溃，无回执也不放回额度。

    async def test_intentional_silence_policy_and_recall_do_not_alert(self):
        for reason in ("no_reply", "message_recalled", "context_changed", "message_expired", "policy_changed"):
            event = self.event_for()
            self.health.watch(event, expected=True)
            event.set_extra("_at_guard_delivery_blocked", reason)
            await self.health.finish(event)
        self.assertEqual(self.sent, [])
        event = self.event_for()
        self.health.watch(event)
        event.set_extra("_livinghello_message_policy", {"reply_allowed": False, "reason": "gray_silent"})
        trace = SimpleNamespace(finish=Mock())
        event.set_extra("_livinghello_trace", trace)
        await self.health.finish(event)
        trace.finish.assert_called_once_with(event, "silent", "gray_silent")
        self.assertEqual(self.sent, [])

    async def test_private_targets_and_invalid_admins(self):
        self.event._group = ""
        await self.health.failure("main_model", "model_reply_missing", event=self.event)
        self.assertEqual(self.sent[0][0], "qq-bot:FriendMessage:9001")
        self.guard.config["outage_notify_admin_qq"] = ["opaque-admin"]
        self.now[0] += 600
        await self.health.failure("main_model", "model_reply_missing", event=self.event)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.health.notification, "missing_admin_qq")
        self.assertEqual(_coerce("outage_notify_admin_qq", ""), [])
        with self.assertRaises(ValueError):
            _coerce("outage_notify_admin_qq", "all")
        self.assertEqual(qq_ids([True, "0", "9001", "9001"]), ["9001"])

    async def test_watchdog_reports_unknown_without_cancelling_owner(self):
        self.health.watch(self.event, expected=True)
        await self.health.finish(self.event, timed_out=True)
        self.assertEqual(self.health.summary["category"], "completion_timeout")
        self.assertEqual(len(self.sent), 1)

    def cron_event(self):
        event = self.event_for()
        event.get_platform_name = lambda: "cron"
        event.unified_msg_origin = "cron:active_agent"
        return event

    async def test_cron_tool_delivery_completes_without_waiting_timeout(self):
        event = self.cron_event()
        trace = SimpleNamespace(finish=Mock(), end=Mock(), record=Mock(),
                                identity=lambda _event: {"request_id": "r1", "message_id": ""})
        event.set_extra("_livinghello_trace", trace)
        self.health.watch(event, expected=True)
        self.health.model_done(event, object())
        self.assertTrue(self.health.tool_delivered(
            event, target="qq-bot:FriendMessage:9001", elapsed=0.12))
        self.assertTrue(await self.health.finish_cron(event))
        row = event.get_extra(KEY)
        await asyncio.sleep(0)  # 让被取消的 watchdog 真正结束。
        self.assertTrue(row["ended"])
        # 工具送达即收尾：watchdog 必须被取消，不能再等 900 秒。
        self.assertTrue(row["watchdog"].cancelled())
        self.assertEqual(self.health.summary["state"], "healthy")
        trace.finish.assert_called_once_with(event, "completed", "tool_delivery_confirmed")

    async def test_cron_without_tool_reports_final_message_missing_immediately(self):
        event = self.cron_event()
        trace = SimpleNamespace(finish=Mock(), end=Mock(), record=Mock(),
                                identity=lambda _event: {"request_id": "r2", "message_id": ""})
        event.set_extra("_livinghello_trace", trace)
        self.health.watch(event, expected=True)
        self.health.model_done(event, object())
        self.assertTrue(await self.health.finish_cron(event))
        self.assertTrue(event.get_extra(KEY)["ended"])
        self.assertEqual(self.health.summary["state"], "failed")
        self.assertEqual(self.health.summary["category"], "tool_not_called")
        trace.finish.assert_called_once_with(event, "failed", "tool_not_called")

    async def test_non_cron_tool_delivery_is_not_final_send(self):
        # 回归保护：普通消息的 send_message_to_user 不能代替带 token 的最终回复包。
        self.health.watch(self.event, expected=True)
        self.health.model_done(self.event, object())
        self.health.tool_delivered(self.event, target="qq-bot:GroupMessage:1001", elapsed=0.1)
        # finish_cron 只处理 cron 事件，普通消息仍走原有时序。
        self.assertFalse(await self.health.finish_cron(self.event))
        self.assertFalse(self.event.get_extra(KEY)["ended"])
        await self.health.finish(self.event)
        self.assertEqual(self.health.summary["state"], "failed")
        self.assertEqual(self.health.summary["category"], "final_message_missing")
        # 没有最终回复包时，watchdog 仍会把普通消息判成流程超时。
        other = self.event_for()
        self.health.watch(other, expected=True)
        self.health.model_done(other, object())
        self.health.tool_delivered(other, target="qq-bot:GroupMessage:1001", elapsed=0.1)
        await self.health.finish(other, timed_out=True)
        self.assertEqual(self.health.summary["category"], "completion_timeout")

    async def test_tool_result_delivery_classification(self):
        def result(text, is_error=False):
            return SimpleNamespace(isError=is_error,
                                   content=[SimpleNamespace(text=text)])

        self.assertTrue(tool_result_delivered(
            "send_message_to_user", result("Message sent to session qq-bot:FriendMessage:9001"))[0])
        self.assertFalse(tool_result_delivered(
            "send_message_to_user", result("error: failed to find platform for session x"))[0])
        self.assertFalse(tool_result_delivered(
            "send_message_to_user", result("boom", is_error=True))[0])
        self.assertFalse(tool_result_delivered("send_message_to_user", None)[0])
        # 只认这一个工具，泛化到其他工具等于放开普通消息判定。
        self.assertFalse(tool_result_delivered(
            "send_message", result("Message sent to session x"))[0])

    async def test_close_does_not_alert_or_cancel_real_task(self):
        release = asyncio.Event()
        async def flow():
            self.health.watch(self.event, expected=True)
            await release.wait()
        task = asyncio.create_task(PipelineScheduler().execute(flow))
        await asyncio.sleep(0)
        await self.health.close()
        self.assertFalse(task.cancelled())
        release.set()
        await task
        self.assertEqual(self.sent, [])

    async def test_bounded_interleaving_matrix(self):
        # 32种闭环组合，覆盖直接包、末包缺失、成功后失败和流程异常。
        self.guard.context.get_config = lambda: {"admins_id": []}
        for model, final, failed, direct, pipeline_error in itertools.product((False, True), repeat=5):
            event = self.event_for()
            self.health.watch(event, expected=True)
            self.guard._delivery().patch(event)
            if direct:
                await event.send(Chain([Plain("工具返回")], llm=False))
            if model:
                self.health.model_done(event, object())
            result = Chain([Plain("模型回答")])
            self.health.arm(event, result)
            if final:
                await event.send(result)
            if failed:
                self.health.sent(event, result, False)
            await self.health.finish(event, pipeline_error=pipeline_error)
            expected = "healthy" if model and final and not failed and not pipeline_error else "failed"
            self.assertEqual(self.health.summary["state"], expected,
                             (model, final, failed, direct, pipeline_error))
            self.now[0] += 1
