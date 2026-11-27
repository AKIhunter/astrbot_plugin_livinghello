"""Issue #1 的有限重试、混合预算和灰名单静默；仅假上游，无付费请求。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from gate.guard_message_policy import MessagePolicy
from images.guard_image_failure import http_failure, payload_failure
from images.guard_image_settings import IMAGE_EVENT
import test_image_generation as fixtures


class ImageIssueTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImageGenerationTests.setUp

    async def test_failure_matrix_keeps_request_and_quota_bounds(self):
        cases = (
            (http_failure(400, '{"error":{"code":"content_policy_violation"}}'), 3),
            (http_failure(429, "{}"), 3),
            (http_failure(401, "{}"), 1),
            (payload_failure(200, {"error": {"type": "upstream_error"}}), 3),
            (payload_failure(200, {"error": {"message": "unknown"}}), 1),
            (asyncio.TimeoutError(), 3),
        )
        for index, (failure, attempts) in enumerate(cases):
            with self.subTest(failure=type(failure).__name__, attempts=attempts):
                self.service.values["daily_limit"] = 10
                self.service._request.reset_mock()
                self.service._request.side_effect = failure
                event = fixtures.Event(mid=f"failure-{index}")
                with patch("images.guard_image_settings.asyncio.sleep", new=AsyncMock()):
                    await self.service.generate(event, "cat", _execute=True)
                self.assertEqual(self.service._request.await_count, attempts)
                self.assertEqual(self.service.used(), 0)
                self.assertIsNone(self.service.quota_reason())
                event.send.assert_not_awaited()
                self.assertIsNone(event.get_extra("_livinghello_pending_image"))

    async def test_mixed_retry_stops_at_five_and_succeeds_without_new_task(self):
        rejection = http_failure(400, '{"error":{"code":"content_policy_violation"}}')
        self.service._request.side_effect = [
            rejection, asyncio.TimeoutError(), rejection, asyncio.TimeoutError(),
            (self.output, "cat"),
        ]
        event = fixtures.Event()
        with patch("images.guard_image_settings.asyncio.sleep", new=AsyncMock()):
            await self.service.generate(event, "cat", _execute=True)
        self.assertEqual(self.service._request.await_count, 5)
        self.assertEqual(self.service.used(), 0)
        self.assertIsNotNone(event.get_extra("_livinghello_pending_image"))
        self.assertIsNone(event.get_extra("_livinghello_image_failure"))
        await fixtures.ImageGenerationTests.deliver(self, event)
        self.assertEqual(self.service.used(), 1)

    async def test_cancel_propagates_without_retry(self):
        self.service._request.side_effect = asyncio.CancelledError()
        event = fixtures.Event()
        token = IMAGE_EVENT.set(event)
        try:
            with patch("images.guard_image_settings.asyncio.sleep", new=AsyncMock()) as delay:
                with self.assertRaises(asyncio.CancelledError):
                    await self.service._request_with_retry(
                        self.service.values, self.service.connection(self.service.values), "cat", None)
            self.service._request.assert_awaited_once()
            delay.assert_not_awaited()
            event.send.assert_not_awaited()
        finally:
            IMAGE_EVENT.reset(token)

    async def test_gray_group_failure_guard_keeps_silent(self):
        policy = MessagePolicy({"message_graylist_groups": ["123"]})
        result = policy.check(platform="aiocqhttp", session_kind="group",
                              sender_id="456", group_id="123", explicit_call=False)
        self.assertEqual(result["reason"], "gray_silent")
        self.assertFalse(result["reply_allowed"])
        event = fixtures.Event()
        event.set_extra("_at_guard_delivery_blocked", not result["reply_allowed"])
        job = {"prompt": "cat", "use_reference": False}
        failure = self.service._image_failure(event, job, "provider_error")
        with patch("images.guard_image_settings.image_followup", new=AsyncMock()) as followup:
            await self.service._notify_image_failure(event, job, failure)
        event.send.assert_not_awaited()
        followup.assert_not_awaited()
        self.assertFalse(failure["notified"])
        self.service._request.assert_not_awaited()
