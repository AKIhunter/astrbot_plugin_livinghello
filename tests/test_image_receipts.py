"""实际发送、重启与隔离矩阵：真实 SQLite，替身上游和平台。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import test_image_generation as fixtures
from images.guard_image_settings import ImageSettings
from images.guard_image_tool import GenerateImageTool
from observability.guard_request_trace import RequestTrace, ID_KEY


class ImageReceiptTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImageGenerationTests.setUp

    def event(self, mid="receipt-one"):
        event = fixtures.Event(mid)
        event.set_extra("_livinghello_image_persona", "persona-one")
        return event

    async def sent(self, event):
        await self.service.generate(event, "窗边的橙色猫")
        pending = dict(event.get_extra("_livinghello_pending_image"))
        await fixtures.ImageGenerationTests.deliver(self, event)
        return pending

    async def test_sent_survives_restart_disabled_tool_and_quota(self):
        event = self.event()
        pending = await self.sent(event)
        for changes in ({"enabled": False}, {"daily_limit": 0}, {}):
            restarted = ImageSettings(self.plugin)
            restarted.values.update(changes)
            req = SimpleNamespace(extra_user_content_parts=[], system_prompt="")
            restarted.attach(self.event("next"), req)
            text = req.extra_user_content_parts[0].text
            self.assertIn('"status": "sent"', text)
            self.assertIn(pending["request_id"], text)
            self.assertIn("一只橙色猫", text)
            self.assertIn("未经视觉检查", text)
            self.assertTrue(req.extra_user_content_parts[0].no_save)
        self.service._request.assert_awaited_once()

    async def test_background_success_updates_actual_trace(self):
        event = self.event()
        trace = RequestTrace(self.plugin, self.plugin.data_dir)
        self.addAsyncCleanup(trace.close)
        trace.begin(event)
        self.plugin._livinghello_detached_enabled = True
        await self.service.prepare_image_job(event, "窗边的猫", False)
        job = event.get_extra("_livinghello_image_job")
        await self.service._run_image_job(event, job)
        self.assertEqual(job["status"], "sent")
        row = await trace.get(event.get_extra(ID_KEY))
        self.assertEqual(row["features"]["image_delivery"]["status"], "success")
        self.assertEqual(self.service.receipts(event)[0]["status"], "sent")

    async def test_send_false_and_exception_never_claim_sent(self):
        for index, value in enumerate((False, OSError("isolated uncertain"))):
            with self.subTest(value=type(value).__name__):
                event = self.event(str(index))
                event.send = AsyncMock(
                    side_effect=value if isinstance(value, Exception) else None, return_value=False)
                await self.service.generate(event, "猫")
                await fixtures.ImageGenerationTests.deliver(self, event)
                self.assertEqual(self.service.receipts(event)[0]["status"], "unknown")

    async def test_restart_marks_interruption_without_regeneration(self):
        event = self.event()
        await self.service.generate(event, "猫")
        await self.service.reconcile_receipts(self.event("live"))
        self.assertEqual(self.service.receipts(event)[0]["status"], "generated")
        restarted = ImageSettings(self.plugin)
        await restarted.reconcile_receipts(self.event("next"))
        self.assertEqual(restarted.receipts(event)[0]["status"], "unknown")
        event.send.assert_not_awaited()
        self.service._request.assert_awaited_once()

    async def test_late_failure_does_not_downgrade_success(self):
        event = self.event()
        pending = await self.sent(event)
        self.service.save_receipt(event, pending, "failed")
        self.service.mark(pending["request_id"], "unknown")
        self.assertEqual(self.service.receipts(event)[0]["status"], "sent")
        with self.service._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls").fetchone()[0], "sent")

    async def test_user_conversation_persona_and_unresolved_are_isolated(self):
        await self.sent(self.event())
        for field in ("sender", "conversation", "persona", "unresolved"):
            with self.subTest(field=field):
                other = self.event("next")
                if field == "sender":
                    other.get_sender_id = lambda: "other-user"
                elif field == "conversation":
                    other.unified_msg_origin = "test:group:two"
                else:
                    other.set_extra("_livinghello_image_persona",
                                    None if field == "unresolved" else "other")
                self.assertEqual(self.service.receipts(other), [])

    async def test_attachment_is_temporary_deduplicated_and_removes_stale(self):
        event = self.event()
        await self.sent(event)
        req = SimpleNamespace(extra_user_content_parts=[])
        for _ in range(3):
            self.service.attach_receipts(event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        other = self.event("other")
        other.get_sender_id = lambda: "other-user"
        self.service.attach_receipts(other, req)
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_different_picture_or_promise_in_same_turn_is_not_executed(self):
        self.plugin._livinghello_detached_enabled = True
        event = self.event()
        tool = GenerateImageTool(service=self.service)
        await tool.call(event, prompt="厨房的猫")
        self.assertIn("未执行", await tool.call(event, prompt="露台的猫"))
        self.assertIn("未执行", await tool.call(
            event, prompt="厨房的猫", promise_id="other-task"))
        self.assertEqual(event.get_extra("_livinghello_image_job")["prompt"], "厨房的猫")
        self.service._request.assert_not_awaited()

    async def test_settlement_timeout_replays_receipt_only(self):
        event = self.event()
        await self.service.generate(event, "猫")
        pending = dict(event.get_extra("_livinghello_pending_image"), promise_id="original")
        with patch("images.guard_image_settings.image_result", AsyncMock(return_value=None)):
            await self.service.finish_image(event, pending, "sent")
        restarted = ImageSettings(self.plugin)
        callback = AsyncMock(return_value={"status": "sent"})
        with patch("images.guard_image_settings.image_result", callback):
            await restarted.reconcile_receipts(self.event("next"))
            await restarted.reconcile_receipts(self.event("next2"))
        callback.assert_awaited_once()
        self.service._request.assert_awaited_once()
        event.send.assert_not_awaited()

    async def test_unbound_success_does_not_settle_other_tasks(self):
        with patch("images.guard_image_settings.image_result", AsyncMock()) as hook:
            await self.sent(self.event())
        hook.assert_not_awaited()

    async def test_deferred_delivery_persists_platform_acceptance(self):
        event = self.event()
        self.plugin.get_deferred_image_contract = lambda: fixtures.Main.get_deferred_image_contract(self.plugin)
        artifact = await fixtures.Main.generate_deferred_image(self.plugin, event, "猫")
        self.context.send_message = AsyncMock(return_value=True)
        result = await fixtures.Main.deliver_deferred_image(self.plugin, event, artifact)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.service.receipts(event)[0]["status"], "sent")

    async def test_sync_second_picture_is_not_generated(self):
        event = self.event()
        await self.service.generate(event, "厨房的猫")
        self.assertIn("未执行", await self.service.generate(event, "露台的猫"))
        self.service._request.assert_awaited_once()
