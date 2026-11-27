"""受控失败类别与说明；不把上游正文、地址或密钥交给用户/模型。"""
from __future__ import annotations

import asyncio
import json
import re

try:
    from ..reply.guard_native_reply import NativeReplyError
except ImportError:
    from reply.guard_native_reply import NativeReplyError


class ImageRequestError(NativeReplyError):
    def __init__(self, status, kind, *, code="", error_type=""):
        super().__init__(f"生图接口HTTP {status}", status)
        self.kind = kind
        self.code = code
        self.error_type = error_type


def _error_identifier(value):
    """仅保留短错误标识；正文、地址及凭据形态不能进入日志。"""
    if (not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) is None
            or value.lower().startswith(("sk-", "sk_"))):
        return ""
    return value


def payload_failure(status, payload):
    """HTTP 成功不代表生图成功；两种响应分支共用错误体分类。"""
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = _error_identifier(error.get("code"))
    error_type = _error_identifier(error.get("type"))
    if code.lower() == "content_policy_violation" or status == 451:
        # 451 = 部分上游（如 gpt-image-2.6-flare）用「Unavailable For Legal Reasons」
        # 表达内容审核拒绝；与 content_policy_violation 同类，走等义词改词重试。
        kind = "content_rejected"
    elif status == 429 or code.lower() in {"rate_limit_exceeded", "rate_limited", "too_many_requests"}:
        kind = "rate_limited"
    elif (400 <= status < 500 or code.lower() in {
            "invalid_api_key", "authentication_error", "permission_denied", "model_not_found",
            "invalid_request_error", "invalid_parameter", "unsupported_model",
    } or error_type.lower() in {"invalid_request_error", "authentication_error", "permission_error"}):
        kind = "provider_error"
    else:
        # 未知上游错误仍可能已执行，不能凭 HTTP 200/5xx 自动重新收费。
        kind = "result_unknown"
    return ImageRequestError(status, kind, code=code, error_type=error_type)


def http_failure(status, detail):
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        payload = None
    return payload_failure(status, payload)


REASONS = {
    "prompt_too_long": "描述太长，我先保留画面要点再整理一下。",
    "prompt_adjustment_required": "这张的画面需要你确认调整，我先停在这里。",
    "content_rejected": "这张没能弄好，有些内容我暂时做不了。",
    "rate_limited": "这会儿还做不了，这张还没开始。",
    "provider_error": "这次没能开始，我先停一下，看看哪里出了岔子。",
    "result_unknown": "这次还没拿到图片，我先看看有没有做好。",
    "delivery_unknown": "图片已经弄好了，不过我还不确定你有没有收到，先确认一下。",
    "quota_exhausted": "今天的图片数量已经到上限了，这张得等一等。",
    "quota_busy": "这会儿暂时接不了新图，这张还没开始。",
}


def describe_failure(exc, *, requested):
    kind = getattr(exc, "kind", None)
    if kind not in REASONS:
        kind = "result_unknown" if requested else "provider_error"
    if isinstance(exc, asyncio.TimeoutError) and requested:
        kind = "result_unknown"
    return kind, REASONS[kind]
