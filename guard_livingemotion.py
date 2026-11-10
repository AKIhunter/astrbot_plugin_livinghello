"""Read-only livingEmotion v1 cooperation; no prompt writer or sender."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
try:
    from .guard_coupling import enabled as coupling_enabled
except ImportError:
    from guard_coupling import enabled as coupling_enabled

PLUGIN = "astrbot_plugin_livingemotion"
STATE_MARKER = "<!--livingemotion:state:v1-->"
API_TIMEOUT = 0.5


def is_state_line(text):
    return isinstance(text, str) and text.startswith(STATE_MARKER)


def _same_request(context, event, api, scope):
    latest = context.get_registered_star(PLUGIN)
    return (getattr(latest, "activated", False)
            and getattr(getattr(latest, "star_cls", None), "api", None) is api
            and event.get_extra("livingemotion:context") == scope)


async def _current(context, event, operation, config=None):
    """Never retain a plugin instance or accept a context derived from text."""
    try:
        if config is not None:
            if not coupling_enabled(context, config, "livingemotion"):
                return None, None
        meta = context.get_registered_star(PLUGIN)
        api = getattr(getattr(meta, "star_cls", None), "api", None)
        if not getattr(meta, "activated", False) or getattr(api, "api_version", None) != 1:
            return None, None
        scope = event.get_extra("livingemotion:context")
        if not isinstance(scope, Mapping):
            return None, None
        scope = dict(scope)
        group = str(event.get_group_id() or "")
        sender = str(event.get_sender_id() or "")
        route = str(getattr(event, "unified_msg_origin", "") or "")
        if (not route or not sender or scope.get("route") != route
                or scope.get("account_id") != sender
                or scope.get("scene_kind") != ("group" if group else "private")
                or scope.get("scene_id") != (group or sender)):
            return None, None
        # v1 增加可选范围参数，旧版无参数实现继续兼容。
        scoped = "context" in inspect.signature(api.get_capabilities).parameters
        caps = await asyncio.wait_for(api.get_capabilities(context=scope) if scoped
                                      else api.get_capabilities(), API_TIMEOUT)
        if (not isinstance(caps, dict) or caps.get("api_version") != 1
                or caps.get("status") != "ok" or caps.get("mode") != "active"
                or caps.get("enabled") is False
                or not caps.get("instance_id") or caps["instance_id"] != scope.get("instance_id")
                or operation not in caps.get("operations", [])):
            return None, None
        # Discovery may have awaited across a reload.
        if not _same_request(context, event, api, scope):
            return None, None
        if config is not None and not coupling_enabled(context, config, "livingemotion"):
            return None, None
        return api, scope
    except Exception:
        return None, None


async def availability_reason(context, event, *, unsolicited, config=None):
    """An unavailable provider is not permission; existing local gates still run."""
    api, scope = await _current(context, event, "get_availability", config)
    if api is None:
        return ""
    try:
        value = await asyncio.wait_for(api.get_availability(scope), API_TIMEOUT)
        if config is not None and not coupling_enabled(context, config, "livingemotion"):
            return ""
        if (not _same_request(context, event, api, scope)
                or not isinstance(value, dict) or value.get("api_version") != 1
                or value.get("status") != "ok"
                or not isinstance(value.get("available"), bool)
                or not isinstance(value.get("block_chat"), bool)):
            return ""
        # Only the trusted producer's real wake/admin handlers may set this marker.
        if event.get_extra("livingemotion:control_request") in {"wake", "admin"}:
            return ""
        if value["block_chat"]:
            return "livingemotion_block_chat"
        if unsolicited and not value["available"]:
            return "livingemotion_unavailable"
    except Exception:
        pass
    return ""


async def state_lines(context, event, req, config=None):
    api, _ = await _current(context, event, "get_bot_state", config)
    if api is None:
        return []
    # The producer owns injection; the gate consumes at most its first state block.
    for part in getattr(req, "extra_user_content_parts", []) or []:
        text = getattr(part, "text", "")
        if is_state_line(text):
            return [text[:6000]]
    return []
