"""主动调整追问与确认回执，全部使用隔离事件和假图片服务。"""
import unittest
from unittest.mock import AsyncMock, patch

import test_image_generation as fixtures
from images.guard_image_failure import http_failure


class AdjustmentNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ImageGenerationTests.setUp(self)
        # 本组验证停止后询问用户；自动改词和重试由单独矩阵覆盖。
        self.service.values["prompt_guard_retry_limit"] = 0

    async def test_content_rejection_question_and_receipt_share_one_send(self):
        event = fixtures.Event()
        job = dict(prompt="窗边的猫", use_reference=False, status="running")
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        timeline = []
        async def handoff(*_):
            data = event.extra["_livinghello_image_failure"]
            self.assertTrue(data["notified"])
            self.assertTrue(data["adjustment_asked"])
            self.assertIn("还是这次就算了？", event.send.await_args.args[0].chain[0].text)
            timeline.append("handoff")
            return dict(status="accepted", task_status="waiting", promise_id="one")
        with patch("images.guard_image_settings.image_followup", side_effect=handoff) as hook:
            await self.service._run_image_job(event, job)
            await self.service._notify_image_failure(event, job, event.extra["_livinghello_image_failure"])
        self.assertEqual(timeline, ["handoff"])
        self.assertEqual(hook.await_count, 1)
        self.assertEqual(event.send.await_count, 1)
        self.assertFalse(any(word in event.send.await_args.args[0].chain[0].text
                             for word in ("任务", "接口", "待办", "服务")))
        # 明确拒绝保留初次请求+2次原句重试；只验证假服务的调用计数。
        self.assertEqual(self.service._request.await_count, 3)

    async def test_question_still_exists_when_le_unavailable(self):
        event = fixtures.Event()
        job = dict(prompt="窗边的猫", use_reference=False)
        data = self.service._image_failure(event, job, "content_rejected")
        with patch("images.guard_image_settings.image_followup", AsyncMock(return_value=None)):
            await self.service._notify_image_failure(event, job, data)
        event.send.assert_awaited_once()
        self.assertIn("还是这次就算了？", event.send.await_args.args[0].chain[0].text)
        self.assertNotIn("记下", event.send.await_args.args[0].chain[0].text)

    async def test_non_content_failure_does_not_ask_adjustment(self):
        for kind in ("rate_limited", "provider_error", "result_unknown", "delivery_unknown"):
            event = fixtures.Event(mid=kind)
            job = dict(prompt="窗边的猫", use_reference=False)
            data = self.service._image_failure(event, job, kind)
            with patch("images.guard_image_settings.image_followup", AsyncMock(return_value=None)):
                await self.service._notify_image_failure(event, job, data)
            self.assertFalse(data["adjustment_asked"])
            self.assertNotIn("改成什么", event.send.await_args.args[0].chain[0].text)
            self.assertFalse(any(word in event.send.await_args.args[0].chain[0].text
                                 for word in ("任务", "接口", "待办", "服务")))

    async def test_repeated_rejection_explains_and_offers_choice_without_jargon(self):
        event = fixtures.Event()
        job = dict(prompt="花园里的猫", use_reference=False)
        data = self.service._image_failure(event, job, "content_rejected")
        data["adjustment_round"] = 1
        with patch("images.guard_image_settings.image_followup", AsyncMock(return_value={
                "status": "accepted", "task_status": "waiting", "promise_id": "internal-id"})):
            await self.service._notify_image_failure(event, job, data)
        event.send.assert_awaited_once()
        text = event.send.await_args.args[0].chain[0].text
        self.assertIn("按你刚才改的想法又试了一下", text)
        self.assertIn("再调整一下", text)
        self.assertIn("还是这次就算了？", text)
        self.assertFalse(any(word in text for word in ("任务", "接口", "待办", "服务", "internal-id")))

    async def test_notice_not_confirmed_never_records_asked_or_hands_off(self):
        for outcome in (False, OSError("uncertain")):
            event = fixtures.Event()
            if isinstance(outcome, Exception):
                event.send.side_effect = outcome
            else:
                event.send.return_value = outcome
            job = dict(prompt="窗边的猫", use_reference=False)
            data = self.service._image_failure(event, job, "content_rejected")
            with patch("images.guard_image_settings.image_followup", AsyncMock()) as handoff:
                await self.service._notify_image_failure(event, job, data)
                await self.service._notify_image_failure(event, job, data)
            self.assertFalse(data["notified"])
            self.assertFalse(data.get("adjustment_asked", False))
            handoff.assert_not_awaited()
            event.send.assert_awaited_once()

    async def test_failed_delivery_plan_cannot_claim_asked(self):
        event = fixtures.Event()
        event.set_extra("_at_guard_delivery_plan", {"failed": True})
        job = dict(prompt="窗边的猫", use_reference=False)
        data = self.service._image_failure(event, job, "content_rejected")
        with patch("images.guard_image_settings.image_followup", AsyncMock()) as handoff:
            await self.service._notify_image_failure(event, job, data)
        self.assertFalse(data["notified"])
        handoff.assert_not_awaited()

    async def test_photo_and_drawing_notices_use_neutral_result_wording(self):
        for prompt in ("拍一张自拍", "拍一张照片", "画一张猫"):
            event = fixtures.Event(mid=prompt)
            job = dict(prompt=prompt, use_reference=False)
            data = self.service._image_failure(event, job, "content_rejected")
            data["adjustment_round"] = 1
            with patch("images.guard_image_settings.image_followup", AsyncMock(return_value=None)):
                await self.service._notify_image_failure(event, job, data)
            text = event.send.await_args.args[0].chain[0].text
            self.assertIn("还是没能弄好", text)
            self.assertIn("再调整一下", text)
            self.assertIn("还是这次就算了？", text)
            self.assertNotIn("画", text)
