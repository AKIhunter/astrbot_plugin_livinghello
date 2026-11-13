"""主模型只产出思考没有正文时，用关闭思考的模型按原请求补一次正文。"""
from __future__ import annotations

import asyncio
import time

from astrbot.core.agent.message import TextPart

try:
    from ..observability.guard_request_trace import emit
except ImportError:
    from observability.guard_request_trace import emit

KEY = "_livinghello_reasoning_retry"
REQUEST_KEY = "_livinghello_provider_request"
DEFAULT_PROVIDER = "deepseek/deepseek-flash-quiet"
DEFAULT_TIMEOUT = 120


class ReasoningRetry:
    def __init__(self, plugin):
        self.plugin = plugin
        self.config = plugin.config

    def enabled(self):
        return self.config.get("reasoning_retry_enabled", True) is True

    def provider_id(self):
        value = self.config.get("reasoning_retry_provider_id", DEFAULT_PROVIDER)
        return str(value or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER

    def timeout(self):
        try:
            value = float(self.config.get("reasoning_retry_timeout_seconds", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        return max(10.0, min(300.0, value))

    @staticmethod
    def needs_retry(response):
        """只有“有思考、无正文”才补答；真正的空响应交给宿主重试。"""
        text = str(getattr(response, "completion_text", "") or "").strip()
        reasoning = str(getattr(response, "reasoning_content", "") or "").strip()
        return not text and bool(reasoning)

    def _provider(self):
        try:
            return self.plugin.context.get_provider_by_id(self.provider_id())
        except Exception:
            return None

    def _blocked(self, event):
        if self.plugin._delivery().recalled(event):
            return "recalled"
        if event.get_extra("_at_guard_delivery_blocked"):
            return "blocked"
        if self.plugin._policy_send_reason(event):
            return "policy"
        return ""

    @staticmethod
    def _payload(request):
        """复用原请求的上下文与临时块，但不重放工具调用结果之外的行为。"""
        payload = {
            "prompt": str(getattr(request, "prompt", "") or ""),
            "contexts": list(getattr(request, "contexts", None) or []),
            "system_prompt": getattr(request, "system_prompt", None),
            "request_max_retries": 1,
        }
        for name in ("image_urls", "audio_urls", "extra_user_content_parts"):
            found = getattr(request, name, None)
            if found:
                payload[name] = list(found)
        tool = getattr(request, "func_tool", None)
        if tool is not None:
            payload["func_tool"] = tool
        return payload

    @staticmethod
    def _repair_history(run_context, text):
        """宿主已先写入只含思考的assistant消息，补上正文避免下一轮回放空回复。"""
        try:
            messages = list(getattr(run_context, "messages", None) or [])
            if not messages:
                return
            last = messages[-1]
            content = getattr(last, "content", None)
            if getattr(last, "role", "") != "assistant" or not isinstance(content, list):
                return
            for part in content:
                kind = getattr(part, "type", "") or (
                    "text" if isinstance(part, TextPart) else "")
                if kind == "text" and str(getattr(part, "text", "") or "").strip():
                    return
            content.append(TextPart(text=text))
        except (AttributeError, TypeError):
            pass

    async def recover(self, event, run_context, response):
        """返回是否真的补到正文；任何异常都保留原响应，不覆盖宿主失败处理。"""
        if not self.enabled() or response is None or event.get_extra(KEY):
            return False
        if not self.needs_retry(response):
            return False
        request = event.get_extra(REQUEST_KEY)
        if request is None:
            return False
        reason = self._blocked(event)
        if reason:
            event.set_extra(KEY, "skipped")
            emit(event, "main_model_retry", "skipped", reason=reason)
            return False
        event.set_extra(KEY, "started")
        provider = self._provider()
        if provider is None:
            event.set_extra(KEY, "provider_missing")
            emit(event, "main_model_retry", "failed", reason="provider_missing")
            return False
        started = time.monotonic()
        try:
            retry = await asyncio.wait_for(
                provider.text_chat(**self._payload(request)), self.timeout())
        except asyncio.TimeoutError:
            event.set_extra(KEY, "timeout")
            emit(event, "main_model_retry", "failed", reason="timeout")
            return False
        except Exception as exc:
            event.set_extra(KEY, "failed")
            emit(event, "main_model_retry", "failed", error_type=type(exc).__name__)
            return False
        elapsed = round((time.monotonic() - started) * 1000)
        text = str(getattr(retry, "completion_text", "") or "").strip()
        if not text:
            event.set_extra(KEY, "empty")
            emit(event, "main_model_retry", "failed", reason="empty_response", ms=elapsed)
            return False
        response.completion_text = text
        if not getattr(response, "reasoning_content", None):
            response.reasoning_content = getattr(retry, "reasoning_content", None)
        self._repair_history(run_context, text)
        event.set_extra(KEY, "recovered")
        emit(event, "main_model_retry", "success", ms=elapsed, model=self.provider_id())
        return True
