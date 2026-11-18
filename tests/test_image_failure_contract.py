"""失败分类、普通/到期交接与本地 HTTP；不请求真实图片服务。"""
import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, patch

import test_image_generation as fixtures
from images.guard_image_failure import describe_failure, http_failure, REASONS
from main import Main


class FailureClassificationTests(unittest.TestCase):
    def test_content_policy_is_explicit(self):
        self.assertEqual(http_failure(400, '{"error":{"code":"content_policy_violation"}}').kind,
                         "content_rejected")

    def test_rate_limit_is_not_content_rejection(self):
        self.assertEqual(http_failure(429, '{"error":{"message":"content policy"}}').kind,
                         "rate_limited")

    def test_other_client_errors_require_settings_check(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.assertEqual(http_failure(status, "untrusted body").kind, "provider_error")

    def test_server_errors_remain_unknown(self):
        for body in ("not-json", "[]", '{"error":null}'):
            with self.subTest(body=body):
                self.assertEqual(http_failure(503, body).kind, "result_unknown")

    def test_timeout_and_pre_request_error_have_distinct_outcomes(self):
        self.assertEqual(describe_failure(asyncio.TimeoutError(), requested=True)[0], "result_unknown")
        self.assertEqual(describe_failure(ValueError(), requested=False)[0], "provider_error")
        self.assertTrue(all(not any(word in reason for word in ("任务", "接口", "待办", "服务"))
                            for reason in REASONS.values()))


class ImageFailureFlowTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImageGenerationTests.setUp

    async def test_deferred_content_rejection_returns_structure_without_sending(self):
        self.plugin._livinghello_detached_enabled = True
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        event = fixtures.Event()
        result = await Main.generate_deferred_image(self.plugin, event, "猫")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["kind"], "content_rejected")
        self.service._request.assert_awaited_once()
        event.send.assert_not_awaited()

    async def test_deferred_timeout_stays_unknown(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.service._request.side_effect = asyncio.TimeoutError()
        result = await Main.generate_deferred_image(self.plugin, fixtures.Event(), "猫")
        self.assertEqual(result, {"status": "unknown"})
        self.service._request.assert_awaited_once()

    async def test_synchronous_failure_notice_only_hands_off_once(self):
        event = fixtures.Event()
        event.result = event.chain_result([])
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        await self.service.generate(event, "猫")
        with patch("images.guard_image_settings.image_followup", AsyncMock(return_value=None)) as handoff:
            await Main.deliver_pending_image(self.plugin, event)
            await Main.deliver_pending_image(self.plugin, event)
        handoff.assert_awaited_once()
        event.send.assert_awaited_once()

    async def test_cron_failure_does_not_create_second_handoff(self):
        event = fixtures.Event()
        event.result = event.chain_result([])
        event.set_extra("cron_job", True)
        self.service._image_failure(event, dict(prompt="猫", use_reference=False), "content_rejected")
        with patch("images.guard_image_settings.image_followup", AsyncMock()) as handoff:
            await Main.deliver_pending_image(self.plugin, event)
        handoff.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_quota_prepare_is_classified_without_paid_request(self):
        self.plugin._livinghello_detached_enabled = True
        self.service.values["daily_limit"] = 0
        event = fixtures.Event()
        await self.service.generate(event, "猫")
        self.assertEqual(event.get_extra("_livinghello_image_job")["status"], "quota")
        self.assertEqual(event.get_extra("_livinghello_image_failure")["kind"], "quota_exhausted")
        self.service._request.assert_not_awaited()
        self.assertEqual(self.service.used(), 0)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "HTTP协议测试需宿主已有aiohttp")
class ImageFailureHttpTests(unittest.IsolatedAsyncioTestCase):
    # 只复用本地 HTTP 服务，不重复继承原回归用例。
    setUp = fixtures.ImageGenerationTests.setUp
    asyncSetUp = fixtures.ImageHttpTests.asyncSetUp
    asyncTearDown = fixtures.ImageHttpTests.asyncTearDown

    async def test_controlled_rejection_through_both_protocols(self):
        self.http_status = 400
        self.response_payload = {"error": {"code": "content_policy_violation",
                                         "message": "untrusted-upstream-body"}}
        self.service._request = lambda *args: fixtures.ImageSettings._request(self.service, *args)
        self.service.connection = lambda _: (self.base, "fixture-key", "image-model")
        for protocol in ("images", "chat_images"):
            event = fixtures.Event(mid=protocol)
            self.service.values["protocol"] = protocol
            response = await self.service.generate(event, "猫")
            self.assertEqual(event.get_extra("_livinghello_image_failure")["kind"], "content_rejected")
            self.assertNotIn("untrusted-upstream-body", response)
        self.assertEqual(len(self.received), 2)
