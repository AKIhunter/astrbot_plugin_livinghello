"""受控失败类别与说明；不把上游正文、地址或密钥交给用户/模型。"""
from __future__ import annotations

import asyncio
import json

try:
    from ..reply.guard_native_reply import NativeReplyError
except ImportError:
    from reply.guard_native_reply import NativeReplyError


class ImageRequestError(NativeReplyError):
    def __init__(self, status, kind):
        super().__init__(f"生图接口HTTP {status}", status)
        self.kind = kind


def http_failure(status, detail):
    try:
        error = json.loads(detail).get("error", {})
        code = error.get("code") if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        code = ""
    if code == "content_policy_violation":
        kind = "content_rejected"
    elif status == 429:
        kind = "rate_limited"
    elif 400 <= status < 500:
        kind = "provider_error"
    else:
        kind = "result_unknown"
    return ImageRequestError(status, kind)


REASONS = {
    "content_rejected": "这张没能弄好，有些内容我暂时做不了。",
    "rate_limited": "这会儿还做不了，这张还没开始。",
    "provider_error": "这次没能开始，我先停一下，看看哪里出了岔子。",
    "result_unknown": "这次还没拿到图片，我先看看有没有做好。",
    "delivery_unknown": "图片已经弄好了，不过我还不确定你有没有收到，先确认一下。",
    "quota_exhausted": "今天的图片数量已经到上限了，这张得等一等。",
}


def describe_failure(exc, *, requested):
    kind = getattr(exc, "kind", None)
    if kind not in REASONS:
        kind = "result_unknown" if requested else "provider_error"
    if isinstance(exc, asyncio.TimeoutError) and requested:
        kind = "result_unknown"
    return kind, REASONS[kind]
