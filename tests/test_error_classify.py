"""统一错误分类矩阵：状态码/异常类型/业务性取消/程序缺陷各一条断言。"""
import unittest

from guard_classifier_outage import FailureDisposition, classify_classifier_failure
from guard_reply_health import (
    FAIL_REPORT,
    NOT_AN_ERROR,
    RETRY_THEN_ROTATE,
    ROTATE_NOW_REPORT,
    classify,
    error_category,
)


class HttpError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__("must not be retained")


class APITimeoutError(RuntimeError):
    pass


class APIConnectionError(RuntimeError):
    pass


class ClassifyMatrixTests(unittest.TestCase):
    def assert_case(self, value, category, disposition):
        self.assertEqual(classify(value), (category, disposition))

    def test_http_status_matrix(self):
        cases = (
            (400, "configuration_error", ROTATE_NOW_REPORT),
            (401, "authentication_error", ROTATE_NOW_REPORT),
            (402, "quota_exhausted", ROTATE_NOW_REPORT),
            (403, "authentication_error", ROTATE_NOW_REPORT),
            (404, "configuration_error", ROTATE_NOW_REPORT),
            (408, "timeout", RETRY_THEN_ROTATE),
            (422, "configuration_error", ROTATE_NOW_REPORT),
            (429, "http_429", RETRY_THEN_ROTATE),
            (500, "http_5xx", RETRY_THEN_ROTATE),
            (502, "http_5xx", RETRY_THEN_ROTATE),
            (503, "http_5xx", RETRY_THEN_ROTATE),
            (529, "http_5xx", RETRY_THEN_ROTATE),
        )
        for status, category, disposition in cases:
            with self.subTest(status=status):
                self.assert_case(status, category, disposition)
                self.assert_case("HTTP %d" % status, category, disposition)

    def test_exception_type_matrix(self):
        cases = (
            (TimeoutError("slow"), "timeout", RETRY_THEN_ROTATE),
            (APITimeoutError("slow"), "timeout", RETRY_THEN_ROTATE),
            (ConnectionResetError("reset"), "connection_error", RETRY_THEN_ROTATE),
            (APIConnectionError("down"), "connection_error", RETRY_THEN_ROTATE),
            (HttpError(429), "http_429", RETRY_THEN_ROTATE),
            (HttpError(401), "authentication_error", ROTATE_NOW_REPORT),
        )
        for exc, category, disposition in cases:
            with self.subTest(exc=type(exc).__name__, status=getattr(exc, "status_code", None)):
                self.assert_case(exc, category, disposition)

    def test_quota_wins_over_status_code(self):
        for text in ("insufficient_quota", "403 Not enough quota",
                     "429 余额不足", "insufficient balance"):
            with self.subTest(text=text):
                self.assert_case(text, "quota_exhausted", ROTATE_NOW_REPORT)

    def test_429_and_503_share_the_same_path(self):
        rate = classify("HTTP 429")
        busy = classify("HTTP 503")
        self.assertEqual(rate[0], "http_429")
        self.assertEqual(busy[0], "http_5xx")
        self.assertEqual(rate[1], busy[1])
        self.assertEqual(rate[1], RETRY_THEN_ROTATE)
        # 配置类 4xx 必须走另一条路径，不能与限流混在一起重试。
        self.assertNotEqual(classify("HTTP 400")[1], rate[1])

    def test_business_cancellations_are_not_errors(self):
        for reason in ("message_recalled", "context_changed", "policy_changed",
                       "no_reply", "stale_context", "expired"):
            with self.subTest(reason=reason):
                self.assert_case(reason, NOT_AN_ERROR, NOT_AN_ERROR)

    def test_program_faults_fail_fast(self):
        for text in ("没有可调用的上游：无可用上游。", "no upstream available"):
            with self.subTest(text=text):
                self.assert_case(text, "provider_missing", FAIL_REPORT)

    def test_unknown_values_default_to_retryable(self):
        self.assert_case("provider unavailable", "model_execution_error", RETRY_THEN_ROTATE)
        self.assert_case("", "model_execution_error", RETRY_THEN_ROTATE)

    def test_old_error_category_contract_is_preserved(self):
        self.assertEqual(error_category("Error code: 403 forbidden"), "authentication_error")
        self.assertEqual(error_category("403 Not enough quota"), "quota_exhausted")
        self.assertEqual(error_category("protocol error: fixed enum"), "protocol_error")


class ClassifierOutageDispositionTests(unittest.TestCase):
    def test_status_codes_drive_cooldown_tier(self):
        for status in (408, 429, 500, 503, 529):
            with self.subTest(status=status):
                failure = classify_classifier_failure(exception=HttpError(status))
                self.assertEqual(failure.disposition, FailureDisposition.RECOVERABLE)
        for status in (400, 401, 402, 403, 404, 422):
            with self.subTest(status=status):
                failure = classify_classifier_failure(exception=HttpError(status))
                self.assertEqual(failure.disposition, FailureDisposition.NEEDS_ATTENTION)

    def test_429_and_503_use_the_same_tier(self):
        rate = classify_classifier_failure(exception=HttpError(429))
        busy = classify_classifier_failure(exception=HttpError(503))
        self.assertEqual(rate.category, "http_429")
        self.assertEqual(busy.category, "http_5xx")
        self.assertEqual(rate.disposition, busy.disposition)


if __name__ == "__main__":
    unittest.main()
