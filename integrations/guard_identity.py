"""原生请求身份指纹；无可信配置时不复用缓存或开放历史补读。"""
from __future__ import annotations

import hashlib
import json


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def request_identity(plugin, event, request=None, *, include_system_prompt=True):
    try:
        origin = str(event.unified_msg_origin or "")
        if not origin:
            return None
        config = plugin.context.get_config(origin)
        if not isinstance(config, dict):
            return None
        if request is None:
            request = event.get_extra("_livinghello_provider_request")
        conversation = value(request, "conversation")
        runner = config.get("agent_runner", {})
        settings = runner.get("config", {})
        configured = (settings.get("persona", {}).get("persona_id")
                      if runner.get("runner_type") == "local"
                      else settings.get("persona_id"))
        # 宿主构建请求时写入最终人格（含会话选择）；配置与人格正文也纳入
        # 指纹，避免同 ID 改正文、换原生配置或切会话后继续读旧证据。
        selected = event.get_extra("_trace_persona_id")
        if selected is None:
            selected = value(conversation, "persona_id", configured)
        personas = getattr(getattr(plugin.context, "persona_manager", None), "personas_v3", [])
        identity = {
            "origin": origin, "sender": str(event.get_sender_id()),
            "cid": value(conversation, "cid"), "conversation_persona": value(conversation, "persona_id"),
            "selected": selected, "configured": configured,
            "persona": [row for row in personas if value(row, "name") == selected],
            "runner": runner, "providers": config.get("provider_settings", {}),
            "system_prompt": value(request, "system_prompt", "") if include_system_prompt else "",
        }
        return hashlib.sha256(json.dumps(
            identity, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    except Exception:
        # 宿主配置/人格读取失败时关闭复用，不能因未知异常泄漏旧会话资料。
        return None
