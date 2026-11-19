"""异常私聊的完整事件边界；所有 Provider、下载和平台发送均为替身。"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Image, Plain, Record, Reply
from gate.guard_engine import DecisionLog
from input.guard_understanding import Observation
from reply import guard_reply_health as health_module
from reply.guard_reply_health import KEY, report_model_fault
PRIVATE_FAULT_KEY = getattr(health_module, "PRIVATE_FAULT_KEY", None)
from test_gate import gate_config, plugin
from test_reply_delivery import Chain, Event


@unittest.skipUnless(PRIVATE_FAULT_KEY, "历史终止式故障草稿；现版保留媒体降级与独立管理员通知")
class PrivateFaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.guards = []
        self.guard = self.make_guard()
        self.event = self.event_for()
        self.addAsyncCleanup(self.close_services)

    def make_guard(self, **overrides):
        guard = plugin(gate_config(
            expression_takeover_enabled=True,
            understand_image_enabled=True,
            understand_image_provider_id="vision",
            understand_voice_enabled=True,
            understand_voice_provider_id="stt",
            classifier_outage_recovery_enabled=True,
            **overrides))
        guard.data_dir = Path(self.directory.name) / str(len(self.guards))
        guard.data_dir.mkdir()
        guard.context.get_config = lambda: {"admins_id": ["9001"]}
        guard.context.send_message = AsyncMock(return_value=True)
        guard.engine.log = DecisionLog(guard.data_dir / "decisions.jsonl")
        self.guards.append(guard)
        return guard

    @staticmethod
    def event_for(group="1001", mid="m1"):
        event = Event(group=group, mid=mid)
        event.unified_msg_origin = f"qq-bot:{'GroupMessage' if group else 'FriendMessage'}:{group or '123'}"
        event.get_platform_id = lambda: "qq-bot"
        event.get_platform_name = lambda: "aiocqhttp"
        return event

    async def drain(self, guard=None):
        guard = guard or self.guard
        tasks = list(getattr(guard, "_model_fault_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks)

    async def close_services(self):
        for guard in self.guards:
            await self.drain(guard)
            for name in ("reply_health", "reply_delivery", "image_generation", "request_trace"):
                service = getattr(guard, name, None)
                if service:
                    await service.close()

    def assert_owner_only(self, guard=None):
        guard = guard or self.guard
        guard.context.send_message.assert_awaited_once()
        route, message = guard.context.send_message.await_args.args
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertTrue(all(isinstance(c, Plain) for c in message.chain))
        self.assertNotIn("sk-private-secret", message.chain[0].text)

    async def image_request(self, guard=None, event=None, error=None):
        guard, event = guard or self.guard, event or self.event
        provider = SimpleNamespace(
            get_model=lambda: "qwen3-vl",
            provider_config={"modalities": ["image"]},
            text_chat=AsyncMock(side_effect=error,
                return_value=SimpleNamespace(completion_text="画面中是一只猫")))
        guard.context.get_provider_by_id = Mock(return_value=provider)
        event.message_obj.message = [Reply(id="other-person"), Image(url="https://example.invalid/image")]
        req = SimpleNamespace(extra_user_content_parts=[])
        guard._delivery().patch(event)
        await guard.understand_reply_input(event, req)
        return req, provider

    async def test_http400_stops_request_before_notification_task_and_never_group_send(self):
        self.guard._trace(self.event)
        self.guard._reply_health().watch(self.event, expected=True)
        req, provider = await self.image_request(error=RuntimeError("HTTP 400 sk-private-secret"))
        self.assertTrue(self.event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(self.event.result.chain, [])
        provider.text_chat.assert_awaited_once()
        await self.guard.attach_reply_contract(self.event, req)
        await self.event.send(Chain([Reply(id="other-person"), Plain("图没有加载出来，主人…")]))
        await self.drain()
        await self.guard._reply_health().finish(self.event)
        self.assertEqual(self.event.sent, [])
        self.assert_owner_only()
        records = self.guard.engine.log.recent()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reply_error"]["category"], "configuration_error")
        rid = self.event.get_extra("_livinghello_request_id")
        row = self.guard.request_trace.rows[rid]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["events"][-1]["reason"], "configuration_error")

    async def test_fault_is_independent_of_all_quote_and_mention_combinations(self):
        for quote in (False, True):
            for mention in (False, True):
                with self.subTest(quote=quote, mention=mention):
                    guard = self.make_guard(reply_quote_enabled=quote, reply_mention_enabled=mention)
                    event = self.event_for(mid=f"{quote}-{mention}")
                    await self.image_request(guard, event, TimeoutError())
                    await event.send(Chain([Plain("异常解释")]))
                    await self.drain(guard)
                    self.assertEqual(event.sent, [])
                    self.assert_owner_only(guard)

    async def test_disabled_watch_missing_admin_and_failed_private_send_never_fallback(self):
        for mode in ("disabled", "watch_off", "missing_admin", "private_failed"):
            with self.subTest(mode=mode):
                guard = self.make_guard()
                if mode == "disabled":
                    guard.config["classifier_outage_recovery_enabled"] = False
                elif mode == "watch_off":
                    guard.config["outage_watch_understand_image"] = False
                elif mode == "missing_admin":
                    guard.context.get_config = lambda: {"admins_id": []}
                else:
                    guard.context.send_message.side_effect = RuntimeError("offline private send failure")
                event = self.event_for(mid=mode)
                await self.image_request(guard, event, ConnectionError())
                await event.send(Chain([Plain("不能私聊就发群里")]))
                await self.drain(guard)
                self.assertTrue(event.stopped)
                self.assertEqual(event.sent, [])
                self.assertEqual(len(guard.engine.log.recent()), 1)
                if mode != "private_failed":
                    guard.context.send_message.assert_not_awaited()
                else:
                    self.assert_owner_only(guard)
                    self.assertEqual(guard.reply_health.notification, "send_unknown")

    async def test_native_error_from_other_users_private_chat_only_notifies_owner(self):
        event = self.event_for(group="")
        self.guard._reply_health().watch(event, expected=True)
        self.guard._delivery().patch(event)
        await event.send(Chain([Plain("Error occurred while processing agent request: "
                                      "HTTP 403 insufficient balance sk-private-secret")], llm=False))
        await event.send(Chain([Plain("第二条异常")]))
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, [])
        self.assert_owner_only()
        self.assertEqual(self.guard.reply_health.summary["category"], "quota_exhausted")

    async def test_earlier_fragment_fault_propagates_to_final_request(self):
        first = self.event_for(mid="image-first")
        first.message_obj.message = [Image(url="https://example.invalid/image")]
        self.event.message_obj.message = [Plain("帮我看看前面的图")]
        self.event.set_extra("_at_guard_fragment_sources", (first, self.event))
        provider = SimpleNamespace(
            get_model=lambda: "qwen3-vl",
            provider_config={"modalities": ["image"]},
            text_chat=AsyncMock(side_effect=RuntimeError("HTTP 400")))
        self.guard.context.get_provider_by_id = Mock(return_value=provider)
        await self.guard.understand_reply_input(
            self.event, SimpleNamespace(extra_user_content_parts=[]))
        self.guard._delivery().patch(self.event)
        await self.event.send(Chain([Plain("不再发送故障解释")]))
        await self.drain()
        self.assertTrue(first.stopped)
        self.assertTrue(self.event.stopped)
        self.assertIs(first.get_extra(PRIVATE_FAULT_KEY), self.event.get_extra(PRIVATE_FAULT_KEY))
        self.assertEqual(self.event.sent, [])
        self.assert_owner_only()
        provider.text_chat.assert_awaited_once()
        self.assertEqual(len(self.guard.engine.log.recent()), 1)

    async def test_voice_failure_stops_request_and_notifies_owner(self):
        self.guard.context.get_provider_by_id = Mock(return_value=SimpleNamespace(
            get_text=AsyncMock(side_effect=ConnectionError())))
        self.event.message_obj.message = [Record(url="offline.wav")]
        req = SimpleNamespace(extra_user_content_parts=[])
        await self.guard.understand_reply_input(self.event, req)
        await self.drain()
        self.assertTrue(self.event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assert_owner_only()

    async def test_actual_image_tool_failure_stops_remaining_bubbles_without_retry(self):
        self.guard._trace(self.event)
        self.guard._reply_health().watch(self.event, expected=True)
        self.guard._delivery().patch(self.event)
        await self.event.send(Chain([Plain("已发送的第一条")]))
        service = self.guard._image_generation()
        service.values.update(enabled=True, daily_limit=5)
        self.guard._judged_event = lambda _: False
        service.connection = Mock(return_value=("offline", "offline", "offline"))
        service._request = AsyncMock(side_effect=TimeoutError())
        result = await service.generate(self.event, "离线画猫")
        self.assertIn("生图未完成", result)
        await self.guard.trace_agent_done(self.event, object(), SimpleNamespace(completion_text="失败解释"))
        await self.guard.recheck_before_send(self.event)
        await self.event.send(Chain([Plain("未发送的第二条")]))
        await self.drain()
        self.assertEqual(len(self.event.sent), 1)
        self.assertEqual(self.event.sent[0].chain[0].text, "已发送的第一条")
        service._request.assert_awaited_once()
        self.assert_owner_only()
        row = self.guard.request_trace.rows[self.event.get_extra("_livinghello_request_id")]
        self.assertEqual(row["state"], "failed")

    async def test_fault_during_typing_blocks_packet_after_wait(self):
        await self.guard.prepare_reply_delivery(self.event)
        await self.guard.recheck_before_send(self.event)
        async def fail_during_wait(*_):
            report_model_fault(self.guard, "image_gen", "timeout", event=self.event)
            return True
        with patch.object(self.guard._delivery(), "typing_pause", new=fail_during_wait):
            await self.event.send(self.event.result)
        await self.drain()
        self.assertEqual(self.event.sent, [])
        self.assert_owner_only()

    async def test_normal_text_and_successful_media_are_not_blocked(self):
        await self.image_request()
        await self.drain()
        self.assertFalse(self.event.stopped)
        self.assertIsNone(self.event.get_extra(PRIVATE_FAULT_KEY))
        await self.event.send(Chain([Plain("我看到用户写着加载失败，但图片读取成功。")]))
        self.assertEqual(len(self.event.sent), 1)
        self.guard.context.send_message.assert_not_awaited()

    async def test_budget_policy_and_superseded_are_not_technical_faults(self):
        reader = self.guard._understanding()
        provider = SimpleNamespace(text_chat=AsyncMock())
        for reason in ("budget", "call_budget", "policy", "superseded"):
            with self.subTest(reason=reason):
                event = self.event_for(mid=reason)
                observation = Observation(images=["https://example.invalid/one"])
                if reason == "call_budget":
                    event.set_extra("_livinghello_vision_call_budget", {"remaining": 0})
                elif reason == "superseded":
                    event.set_extra("_at_guard_superseded", True)
                with patch.object(self.guard, "_policy_send_reason",
                                  return_value="policy_changed" if reason == "policy" else ""), \
                     patch("images.guard_image_budget.fit_sync",
                           return_value=([], 1) if reason == "budget" else (observation.images, 0)):
                    await reader._enrich_images(event, observation, provider, {})
                self.assertIsNone(event.get_extra(PRIVATE_FAULT_KEY))
                self.assertFalse(event.stopped)
        provider.text_chat.assert_not_awaited()
        self.guard.context.send_message.assert_not_awaited()

    async def test_hard_timeout_is_private_fault(self):
        reader = self.guard._understanding()
        reader.parse = AsyncMock(return_value=Observation(images=["offline"]))
        async def cancelled_enrich(*_, **__):
            raise asyncio.TimeoutError()
        reader.enrich = cancelled_enrich
        await reader._observe_source(self.event)
        await self.drain()
        self.assertTrue(self.event.stopped)
        self.assertEqual(self.event.get_extra(PRIVATE_FAULT_KEY)["category"], "timeout")
        self.assert_owner_only()

    async def test_duplicate_model_fault_records_and_notifies_once_after_finish(self):
        health = self.guard._reply_health()
        health.watch(self.event, expected=True)
        report_model_fault(self.guard, "understand_image", "timeout", event=self.event)
        # finish 可以先于后台提醒；不能把原因改成 model_reply_missing。
        await health.finish(self.event)
        report_model_fault(self.guard, "understand_image", "timeout", event=self.event)
        await self.drain()
        await health.model_fault("understand_image", "timeout", event=self.event)
        self.assertEqual(len(self.guard.engine.log.recent()), 1)
        self.assert_owner_only()
        self.assertTrue(self.event.get_extra(KEY)["ended"])
