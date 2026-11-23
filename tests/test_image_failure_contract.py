"""失败分类、普通/到期交接与本地 HTTP；不请求真实图片服务。"""
import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, patch

import test_image_generation as fixtures
from images.guard_image_failure import describe_failure, http_failure, payload_failure, REASONS
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

    def test_success_status_error_body_keeps_failure_kind(self):
        cases = (
            ({"code": "content_policy_violation"}, "content_rejected"),
            ({"code": "rate_limit_exceeded"}, "rate_limited"),
            ({"code": "model_not_found", "type": "invalid_request_error"}, "provider_error"),
            ({"code": "upstream_timeout"}, "result_unknown"),
        )
        for status in (200, 201):
            for error, expected in cases:
                with self.subTest(status=status, error=error):
                    with self.assertRaises(fixtures.NativeReplyError) as raised:
                        fixtures.ImageSettings.extract({"error": error}, status=status)
                    self.assertEqual(describe_failure(raised.exception, requested=True)[0], expected)
                    self.assertEqual(raised.exception.code, error["code"])
                    self.assertEqual(raised.exception.status, status)

    def test_error_diagnostics_never_include_body_or_credentials(self):
        for value in ("sk-fixture-sensitive", "https://example.invalid/secret", "raw\nbody", "x" * 81):
            with self.subTest(value=value):
                failure = payload_failure(200, {"error": {
                    "code": value, "type": value, "message": value}})
                self.assertEqual(failure.code, "")
                self.assertEqual(failure.error_type, "")
                self.assertNotIn(value, str(failure))


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
        # 明确拒绝沿用原描述最多请求三次，最后只交接一次失败。
        self.assertEqual(self.service._request.await_count, 3)
        event.send.assert_not_awaited()

    async def test_deferred_timeout_stays_unknown(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.service._request.side_effect = asyncio.TimeoutError()
        result = await Main.generate_deferred_image(self.plugin, fixtures.Event(), "猫")
        self.assertEqual(result["status"], "unknown")
        # 未知结果也必须携带诊断，供 LE 停止自动重试并提示核对上游。
        self.assertEqual(result["failure"]["kind"], "result_unknown")
        self.assertTrue(result["failure"]["requested"])
        self.assertEqual(result["failure"]["exception_type"], "TimeoutError")
        # 超时在本地按原描述重试 3 次；仍失败才进入未知结果。
        self.assertEqual(self.service._request.await_count, 3)

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

    async def test_background_502_with_accepted_handoff_sends_one_notice(self):
        event = fixtures.Event()
        self.service._request.side_effect = http_failure(502, "offline-upstream-error")
        job = dict(prompt="猫", use_reference=False, emotion=None, status="running")
        receipt = {"status": "accepted", "task_status": "waiting"}
        with patch("images.guard_image_settings.image_followup",
                   AsyncMock(return_value=receipt)) as handoff:
            await self.service._run_image_job(event, job)
            await self.service._notify_image_failure(
                event, job, event.get_extra("_livinghello_image_failure"))
        # 502 属于可重试失败：同一任务内共请求 3 次，只发一条失败通知。
        self.assertEqual(self.service._request.await_count, 3)
        event.send.assert_awaited_once()
        handoff.assert_awaited_once()
        self.assertEqual(job["status"], "unknown")
        self.assertEqual(job["followup"], receipt)

    async def test_all_failure_kinds_keep_one_notice_after_handoff(self):
        for kind in REASONS:
            for receipt in (None, {"status": "accepted", "task_status": "waiting"},
                            {"status": "accepted", "task_status": "scheduled"}):
                with self.subTest(kind=kind, receipt=receipt):
                    event = fixtures.Event()
                    job = dict(prompt="猫", use_reference=False, emotion=None)
                    failure = self.service._image_failure(event, job, kind)
                    with patch("images.guard_image_settings.image_followup",
                               AsyncMock(return_value=receipt)) as handoff:
                        await self.service._notify_image_failure(event, job, failure)
                        await self.service._notify_image_failure(event, job, failure)
                    event.send.assert_awaited_once()
                    handoff.assert_awaited_once()
                    self.assertTrue(failure["notified"])
                    self.assertEqual(failure["adjustment_asked"], kind in {
                        "content_rejected", "prompt_too_long", "prompt_adjustment_required"})
                    # 确认回执必须与用户实际收到的询问一致，其他失败不声称重拍。
                    notice = event.send.await_args.args[0].chain[0].text
                    self.assertEqual("还是这次就算了？" in notice, failure["adjustment_asked"])
                    self.assertNotIn("重新给你", notice)
        self.service._request.assert_not_awaited()

    async def test_unaccepted_failure_notice_never_hands_off_or_resends(self):
        for result in (False, OSError("offline-send")):
            with self.subTest(result=type(result).__name__):
                event = fixtures.Event()
                if isinstance(result, Exception):
                    event.send.side_effect = result
                else:
                    event.send.return_value = result
                job = dict(prompt="猫", use_reference=False, emotion=None)
                failure = self.service._image_failure(event, job, "result_unknown")
                with patch("images.guard_image_settings.image_followup", AsyncMock()) as handoff:
                    await self.service._notify_image_failure(event, job, failure)
                    await self.service._notify_image_failure(event, job, failure)
                event.send.assert_awaited_once()
                handoff.assert_not_awaited()
                self.assertFalse(failure["notified"])

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
        self.response_payload = {"error": {"code": "content_policy_violation",
                                         "message": "untrusted-upstream-body"}}
        self.service.values["daily_limit"] = 10
        self.service._request = lambda *args: fixtures.ImageSettings._request(self.service, *args)
        self.service.connection = lambda _: (self.base, "fixture-key", "image-model")
        for status in (200, 201, 400):
            self.http_status = status
            for protocol in ("images", "chat_images"):
                with self.subTest(status=status, protocol=protocol):
                    event = fixtures.Event(mid=f"{status}-{protocol}")
                    self.service.values["protocol"] = protocol
                    with patch("images.guard_image_settings.emit") as trace:
                        response = await self.service.generate(event, "猫")
                    self.assertEqual(event.get_extra("_livinghello_image_failure")["kind"],
                                     "content_rejected")
                    api_states = [call.args[2] for call in trace.call_args_list
                                  if len(call.args) > 2 and call.args[1] == "image_api"]
                    self.assertIn("failed", api_states)
                    self.assertNotIn("success", api_states)
                    self.assertNotIn("untrusted-upstream-body", response)
        # 两个协议×三个回合，明确内容拒绝每回合按原描述最多请求三次。
        self.assertEqual(len(self.received), 18)
        # 每个任务只预占一次本地额度。
        self.assertEqual(self.service.used(), 6)
