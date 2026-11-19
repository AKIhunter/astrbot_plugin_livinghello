"""独立补答业务矩阵：真实恢复器、假原生发送，不调用外部模型或平台。"""
import asyncio
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock

from gate.guard_classifier_outage import BackfillStatus, OutageState
from gate.guard_recovery_replay import KEY
import test_classifier_outage_flow as recovery_fixtures
from test_reply_delivery import Event


class IndependentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def ready(self):
        self.guard, self.outage_event, self.work = recovery_fixtures.ClassifierRecoveryCallbackTests.ready(
            self, sessions=("1001",))
        self.guard._policy_send_reason = lambda _: ""
        self.service = self.guard._recovery_replay()
        self.source = Event()
        self.source.unified_msg_origin = self.work.sessions[0].session_id
        self.source.is_at_or_wake_command = False
        self.source.set_extra("_at_guard_recorded_at", time.monotonic())
        self.service.remember(self.source)
        self.service.native_reply = AsyncMock(return_value=True)
        return self.work.sessions[0].session_id

    async def run_recovery(self):
        await self.guard.resume_classifier_recovery()
        return self.outage_event.affected_sessions[self.work.sessions[0].session_id].backfill_status

    async def test_latest_original_message_only_and_real_send_receipt_completes(self):
        self.ready()
        old = self.source
        self.source = Event(mid="new")
        self.source.unified_msg_origin = old.unified_msg_origin
        self.source.set_extra("_at_guard_recorded_at", time.monotonic())
        self.service.remember(self.source)

        async def send(event):
            self.assertEqual(event.message_obj.message_id, "new")
            self.assertEqual(event.get_extra(KEY), self.work.event_id)
            event.set_extra("_at_guard_actual_sent", True)
            event.set_extra("_at_guard_sent_has_content", True)
            event.set_extra("_livinghello_reply_health", {"model_done": True, "sent": 1})
            return True

        self.service.native_reply.side_effect = send
        self.assertEqual(await self.run_recovery(), BackfillStatus.COMPLETED)
        self.assertEqual(self.guard._outage().state, OutageState.HEALTHY)
        self.assertEqual(self.service.messages, {})
        self.assertEqual(self.source.get_extra("_at_guard_actual_sent"), None)

    async def test_expired_message_is_no_reply_without_model_calls(self):
        self.ready()
        self.source.set_extra("_at_guard_recorded_at", time.monotonic() - 900)
        self.assertEqual(await self.run_recovery(), BackfillStatus.NO_REPLY)
        self.service.native_reply.assert_not_awaited()

    async def test_recalled_and_newly_blocked_messages_are_no_reply(self):
        for reason in ("message_recalled", "policy_changed"):
            with self.subTest(reason=reason):
                self.ready()
                self.guard._policy_send_reason = lambda _: reason
                self.assertEqual(await self.run_recovery(), BackfillStatus.NO_REPLY)
                self.service.native_reply.assert_not_awaited()

    async def test_native_silence_does_not_become_fake_delivery(self):
        self.ready()

        async def silent(event):
            event.set_extra("_livinghello_intentional_silence", "no_reply")
            return True

        self.service.native_reply.side_effect = silent
        self.assertEqual(await self.run_recovery(), BackfillStatus.NO_REPLY)

    async def test_generated_without_send_retries_after_cooldown(self):
        self.ready()
        self.assertEqual(await self.run_recovery(), BackfillStatus.RETRY_PENDING)
        self.assertFalse(await self.guard.resume_classifier_recovery())
        self.service.native_reply.assert_awaited_once()

    async def test_direct_error_text_is_not_formal_reply_completion(self):
        self.ready()

        async def error_text(event):
            event.set_extra("_at_guard_actual_sent", True)
            event.set_extra("_at_guard_sent_has_content", True)
            event.set_extra("_livinghello_any_send_attempted", True)
            event.set_extra("_livinghello_reply_health", {"model_done": False, "sent": 0})
            return True

        self.service.native_reply.side_effect = error_text
        self.assertEqual(await self.run_recovery(), BackfillStatus.SEND_UNKNOWN)

    async def test_failed_send_and_tool_side_effects_never_blindly_replay(self):
        for key in ("_livinghello_any_send_attempted", "_livinghello_recovery_tool_attempted"):
            with self.subTest(key=key):
                self.ready()

                async def uncertain(event):
                    event.set_extra(key, True)
                    raise RuntimeError("离线未知副作用")

                self.service.native_reply.side_effect = uncertain
                self.assertEqual(await self.run_recovery(), BackfillStatus.SEND_UNKNOWN)
                self.assertFalse(await self.guard.resume_classifier_recovery())
                self.service.native_reply.assert_awaited_once()

    async def test_native_attribute_error_after_send_attempt_stays_unknown(self):
        self.ready()

        async def uncertain(event):
            event.set_extra("_livinghello_any_send_attempted", True)
            raise AttributeError("离线宿主字段缺失")

        self.service.native_reply.side_effect = uncertain
        self.assertEqual(await self.run_recovery(), BackfillStatus.SEND_UNKNOWN)

    async def test_cancel_after_send_attempt_stays_unknown(self):
        self.ready()

        async def cancelled(event):
            event.set_extra("_livinghello_any_send_attempted", True)
            raise asyncio.CancelledError()

        self.service.native_reply.side_effect = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.run_recovery()
        session = self.outage_event.affected_sessions[self.work.sessions[0].session_id]
        self.assertEqual(session.backfill_status, BackfillStatus.SEND_UNKNOWN)

    async def test_missing_native_capability_is_unavailable(self):
        self.ready()
        self.service.native_reply.return_value = False
        self.assertEqual(await self.run_recovery(), BackfillStatus.UNAVAILABLE)

    async def test_concurrent_recovery_is_executed_once(self):
        self.ready()

        async def send(event):
            await asyncio.sleep(0)
            event.set_extra("_at_guard_actual_sent", True)
            event.set_extra("_at_guard_sent_has_content", True)
            event.set_extra("_livinghello_reply_health", {"model_done": True, "sent": 1})
            return True

        self.service.native_reply.side_effect = send
        await asyncio.gather(self.run_recovery(), self.run_recovery())
        self.service.native_reply.assert_awaited_once()

    async def test_recovery_event_bypasses_cooling_but_other_messages_do_not(self):
        self.ready()
        replay = self.service.replay_event(self.source, self.work)
        self.assertFalse(self.guard._handle_classifier_outage_cooling(replay, "enforce", "1001", []))
        # RECOVERING 本身不暂停新的正常请求；仅冷却阶段才拦普通消息。
        self.assertFalse(self.guard._handle_classifier_outage_cooling(Event(), "enforce", "1001", []))

    async def test_memory_is_bounded_and_cleared_on_close(self):
        self.ready()
        for index in range(300):
            event = Event(group=str(index))
            self.service.remember(event)
        self.assertEqual(len(self.service.messages), self.service.CAPACITY)
        self.service.close()
        self.assertEqual(self.service.messages, {})


class IndependenceSourceTests(unittest.TestCase):
    def test_no_retired_plugin_references_outside_changelog(self):
        root = Path(__file__).resolve().parents[1]
        tokens = ("cong" + "ling", "从" + "绫")
        violations = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".js", ".mjs", ".md", ".html", ".json", ".yaml", ".yml"}:
                continue
            if path.name == "CHANGELOG.md" or any(p.startswith(".") for p in path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8-sig").casefold()
            if any(token in text for token in tokens):
                violations.append(str(path.relative_to(root)))
        self.assertEqual(violations, [])
