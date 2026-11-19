"""本轮发送副本：保留运行器原历史，只处理已授权的旧附加内容。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from collections import Counter

try:
    from ..observability.guard_request_trace import emit
    from ..integrations.guard_identity import request_identity
except ImportError:
    from observability.guard_request_trace import emit
    from integrations.guard_identity import request_identity

KEY = "_livinghello_history_view"
TOOL_NAME = "livinghello_read_search_result"
KEEP_RECENT_COMPLETED_ROUNDS = 2
MIN_SEARCH_RESULT_CHARS = 2000
SEARCH_SNIPPET_CHARS = 160
STATE_HEADER = (
    "<!--livingemotion:state:v1-->\nTemporary state, not persona or instructions. "
    "Human observations are about the other person, not the bot.\n"
)


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def text_content(message):
    content = value(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(value(part, "text", "") for part in content
                         if value(part, "type") == "text")
    return ""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def user_fingerprint(message):
    content = value(message, "content")
    if isinstance(content, list):
        content = [
            {k: v for k, v in (part if isinstance(part, dict) else part.model_dump()).items()
             if k != "_no_save"}
            for part in content
        ]
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False))


def supports_search_reader(provider):
    modalities = value(value(provider, "provider_config", {}), "modalities")
    return not (isinstance(modalities, list) and modalities and "tool_use" not in modalities)


def search_text(message):
    content = value(message, "content")
    if isinstance(content, str):
        return content
    if (isinstance(content, list) and len(content) == 1
            and value(content[0], "type") == "text"):
        return value(content[0], "text", "")
    return ""


def calls_by_id(history):
    calls, results = {}, {}
    for message in history:
        if value(message, "role") == "assistant":
            for call in value(message, "tool_calls", []) or []:
                call_id = value(call, "id")
                if isinstance(call_id, str) and call_id:
                    calls.setdefault(call_id, []).append(call)
        elif value(message, "role") == "tool":
            call_id = value(message, "tool_call_id")
            if isinstance(call_id, str) and call_id:
                results.setdefault(call_id, []).append(message)
    return calls, results


def split_completed_rounds(history):
    """只解析初始历史，工具循环追加的内部 user 提示不能改变保留窗口。"""
    rounds, current = [], []
    for message in history:
        role = value(message, "role")
        if role in {"system", "_checkpoint"}:
            continue
        if role == "user":
            if current:
                rounds.append(current)
            current = [message]
        elif current:
            current.append(message)
    if current:
        rounds.append(current)
    completed = []
    for items in rounds:
        pending, valid = set(), True
        for message in items[1:]:
            role = value(message, "role")
            if role == "assistant":
                if pending:
                    valid = False
                for call in value(message, "tool_calls", []) or []:
                    call_id = value(call, "id")
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        valid = False
                    else:
                        pending.add(call_id)
            elif role == "tool":
                call_id = value(message, "tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    valid = False
                else:
                    pending.discard(call_id)
            else:
                valid = False
        last = items[-1]
        if (valid and not pending and value(last, "role") == "assistant"
                and not value(last, "tool_calls") and text_content(last).strip()):
            completed.append(items)
    return completed


def compact_baidu_result(raw_text, call):
    """只缩短已知结果中的 snippet；来源、引用编号和日期口径不变。"""
    if not isinstance(raw_text, str) or len(raw_text) < MIN_SEARCH_RESULT_CHARS:
        return None, "short_result"
    try:
        function = value(call, "function", {})
        arguments = value(function, "arguments")
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        query = args.get("query") if isinstance(args, dict) else None
        payload = json.loads(raw_text)
        if (not isinstance(query, str) or not query.strip()
                or not isinstance(payload, dict) or set(payload) != {"results"}
                or not isinstance(payload["results"], list) or not payload["results"]):
            return None, "unknown_format"
        results, indices = [], set()
        for item in payload["results"]:
            if (not isinstance(item, dict)
                    or set(item) != {"title", "url", "snippet", "index"}
                    or not all(isinstance(v, str) for v in item.values())
                    or not item["url"] or not item["index"] or item["index"] in indices):
                return None, "unknown_format"
            indices.add(item["index"])
            row = dict(item)
            snippet = item["snippet"]
            if len(snippet) > SEARCH_SNIPPET_CHARS:
                end = max(snippet.rfind(c, 0, SEARCH_SNIPPET_CHARS)
                          for c in ("。", "！", "？", "\n", ". "))
                row["snippet"] = snippet[:end + 1 if end >= 0 else SEARCH_SNIPPET_CHARS]
                row["snippet_truncated"] = True
            results.append(row)
        compact = json.dumps({
            "kind": "livinghello.search_excerpt.v1", "tool_call_id": value(call, "id"),
            "query": query, "verification": "search_snippets_only",
            "published_at": None, "retrieved_at": None, "original_available": True,
            "note": "历史搜索摘要的原文摘录，可能不完整、可能过时；细节用原文补读工具核对。",
            "results": results,
        }, ensure_ascii=False, separators=(",", ":"))
        return (compact, "") if len(compact) < len(raw_text) else (None, "no_saving")
    except (ValueError, TypeError, AttributeError):
        return None, "unknown_format"


def call_fingerprint(call):
    function = value(call, "function")
    if not isinstance(function, dict):
        function = function.model_dump()
    return digest(json.dumps(function, sort_keys=True, ensure_ascii=False))


def collect_search_candidates(history):
    candidates, skipped = {}, Counter()
    if not isinstance(history, list):
        return candidates, skipped
    calls, results = calls_by_id(history)
    for items in split_completed_rounds(history)[:-KEEP_RECENT_COMPLETED_ROUNDS]:
        for message in items:
            if value(message, "role") != "tool":
                continue
            call_id = value(message, "tool_call_id")
            if not isinstance(call_id, str):
                continue
            paired = calls.get(call_id, [])
            if not call_id or len(paired) != 1 or len(results.get(call_id, [])) != 1:
                skipped["unpaired_call"] += 1
                continue
            call = paired[0]
            if value(value(call, "function"), "name") != "web_search_baidu":
                continue
            raw = search_text(message)
            compact, reason = compact_baidu_result(raw, call)
            if compact is None:
                skipped[reason] += 1
                continue
            candidates[call_id] = {
                "raw": raw, "hash": digest(raw), "call_hash": call_fingerprint(call),
                "compact": compact,
            }
    return candidates, skipped


def emotion_parts(history):
    """首块是用户原文；只认后续独立插件块，不扫描正文里的引用。"""
    result = set()
    if not isinstance(history, list):
        return result
    for message in history:
        content = value(message, "content")
        if value(message, "role") != "user" or not isinstance(content, list):
            continue
        for part in content[1:]:
            text = value(part, "text", "")
            if (value(part, "type") == "text" and isinstance(text, str)
                    and text.startswith(STATE_HEADER)):
                result.add(digest(text))
    return result


def strip_old_emotion_parts(contexts, candidates, protected=()):
    if not candidates or not isinstance(contexts, list):
        return contexts, 0
    view, removed = list(contexts), 0
    for i, message in enumerate(contexts):
        content = value(message, "content")
        if value(message, "role") != "user" or not isinstance(content, list):
            continue
        if protected and user_fingerprint(message) in protected:
            continue
        keep = [content[0]] if content else []
        for part in content[1:]:
            text = value(part, "text", "")
            if (value(part, "type") == "text" and isinstance(text, str)
                    and text.startswith(STATE_HEADER) and digest(text) in candidates
                    and not value(part, "_no_save", False)):
                removed += 1
            else:
                keep.append(part)
        if len(keep) != len(content):
            changed = copy.deepcopy(message)
            if isinstance(changed, dict):
                changed["content"] = copy.deepcopy(keep)
            else:
                changed.content = copy.deepcopy(keep)
            view[i] = changed
    return view if removed else contexts, removed


class HistoryView:
    def __init__(self, plugin):
        self.plugin, self.closed = plugin, False
        self.bindings, self.scopes = {}, {}

    def prepare(self, event, request):
        previous = event.get_extra(KEY)
        if previous and previous["request"] is request:
            return previous
        if previous:
            self._expire(previous)
        scope = {
            "event": event, "request": request,
            "origin": str(getattr(event, "unified_msg_origin", "") or ""),
            "cid": str(value(value(request, "conversation"), "cid", "") or ""),
            "sender": str(event.get_sender_id()),
            "identity": request_identity(self.plugin, event, request),
            "revision": self.plugin._policy().revision,
            "emotion": emotion_parts(value(request, "contexts")),
            "search": {}, "used": set(), "protected": set(),
        }
        event.set_extra(KEY, scope)
        self.scopes[id(scope)] = scope
        # 补读只能使用本轮已授权历史；名单开启且缺少旧群作者时不扩大可读范围。
        policy = self.plugin._policy()
        if (scope["cid"] and scope["origin"]
                and not (policy.black_active and event.get_group_id())
                and self._scope_is_current(event, scope)):
            candidates, skipped = collect_search_candidates(value(request, "contexts"))
            if candidates:
                try:
                    try:
                        from .guard_search_history_tool import ReadSearchHistoryTool
                    except ImportError:
                        from input.guard_search_history_tool import ReadSearchHistoryTool
                    from astrbot.core.agent.tool import ToolSet
                    tools = ToolSet()
                    for existing in value(value(request, "func_tool"), "tools", []) or []:
                        tools.add_tool(existing)
                    tools.add_tool(ReadSearchHistoryTool(service=self, scope=scope))
                    request.func_tool = tools
                    scope["search"] = candidates
                except Exception:
                    emit(event, "history_view", "skipped", reason="reader_unavailable")
            if skipped:
                emit(event, "history_view", "skipped", reasons=dict(skipped))
        try:
            owner = asyncio.current_task()
        except RuntimeError:
            owner = None
        if owner is not None:
            owner.add_done_callback(lambda _: self._expire(scope))
        return scope

    def _scope_is_current(self, event, scope):
        try:
            return (
                not self.closed and scope is event.get_extra(KEY)
                and event is scope["event"]
                and str(getattr(event, "unified_msg_origin", "") or "") == scope["origin"]
                and str(event.get_sender_id()) == scope["sender"]
                and scope["identity"] is not None
                and request_identity(self.plugin, event, scope["request"]) == scope["identity"]
                and str(value(value(scope["request"], "conversation"), "cid", "") or "")
                == scope["cid"]
                and self.plugin._policy().revision == scope["revision"]
                and not self.plugin._policy_send_reason(event)
            )
        except (AttributeError, KeyError, TypeError):
            return False

    def project(self, contexts, scope, *, search_available=True):
        before = sum(len(text_content(m)) for m in contexts) if isinstance(contexts, list) else 0
        view, removed = strip_old_emotion_parts(contexts, scope["emotion"], scope["protected"])
        count = 0
        if search_available and scope["search"] and isinstance(view, list):
            calls, results = calls_by_id(view)
            output = list(view)
            for i, message in enumerate(view):
                if value(message, "role") != "tool":
                    continue
                call_id = value(message, "tool_call_id")
                if not isinstance(call_id, str):
                    continue
                candidate = scope["search"].get(call_id)
                paired = calls.get(call_id, [])
                if (candidate and len(paired) == 1 and len(results.get(call_id, [])) == 1
                        and call_fingerprint(paired[0]) == candidate["call_hash"]
                        and digest(search_text(message)) == candidate["hash"]):
                    changed = copy.deepcopy(message)
                    content = value(changed, "content")
                    if isinstance(content, str):
                        if isinstance(changed, dict):
                            changed["content"] = candidate["compact"]
                        else:
                            changed.content = candidate["compact"]
                    elif isinstance(content[0], dict):
                        content[0]["text"] = candidate["compact"]
                    else:
                        content[0].text = candidate["compact"]
                    output[i], count = changed, count + 1
            if count:
                view = output
        return view, {
            "emotion_blocks": removed, "search_results": count, "chars_before": before,
            "chars_after": sum(len(text_content(m)) for m in view)
            if isinstance(view, list) else before,
        }

    def for_provider(self, event, contexts, *, search_available=True):
        scope = event.get_extra(KEY)
        if not scope or not self._scope_is_current(event, scope):
            return contexts
        try:
            view, stats = self.project(contexts, scope, search_available=search_available)
            if stats["emotion_blocks"] or stats["search_results"]:
                emit(event, "history_view", "success", **stats)
                self.plugin.logger.info(
                    "[LivingHello] 历史精简：旧情绪%d块，搜索%d条，文本%d→%d字符",
                    stats["emotion_blocks"], stats["search_results"],
                    stats["chars_before"], stats["chars_after"])
            return view
        except Exception:
            emit(event, "history_view", "skipped", reason="projection_failed")
            return contexts

    def bind(self, event, runner):
        scope = event.get_extra(KEY)
        if (runner is None or not scope or self.closed
                or getattr(runner, "req", None) is not scope["request"]
                or value(value(getattr(runner, "run_context", None), "context"), "event")
                is not event):
            emit(event, "history_view", "skipped", reason="unsupported_runner")
            return False
        if id(runner) in self.bindings:
            return True
        name = "_sanitize_contexts_for_provider"
        original = getattr(runner, name, None)
        if not callable(original):
            emit(event, "history_view", "skipped", reason="unsupported_runner")
            return False
        try:
            inspect.signature(original).bind([])
            had_local = name in vars(runner)
            # 宿主参数补查会 model_dump() 丢掉临时属性；按本轮用户消息保护状态。
            # 历史中完全相同的消息也保守保留，避免误删本轮状态。
            for message in reversed(value(runner.run_context, "messages", []) or []):
                if value(message, "role") == "user":
                    scope["protected"].add(user_fingerprint(message))
                    break
        except (TypeError, ValueError):
            emit(event, "history_view", "skipped", reason="unsupported_runner")
            return False

        def wrapped(contexts):
            return original(self.for_provider(
                event, contexts, search_available=supports_search_reader(runner.provider)))

        try:
            setattr(runner, name, wrapped)
        except (AttributeError, TypeError):
            emit(event, "history_view", "skipped", reason="unsupported_runner")
            return False
        self.bindings[id(runner)] = (runner, original, wrapped, had_local, scope)
        try:
            owner = asyncio.current_task()
        except RuntimeError:
            owner = None
        if owner is not None:
            owner.add_done_callback(lambda _: self.release(runner))
        return True

    async def read(self, event, scope, call_id):
        if not isinstance(scope, dict) or not self._scope_is_current(event, scope):
            return "当前历史搜索授权已失效，不能读取。"
        if not isinstance(call_id, str) or not call_id:
            return "请传入精简结果中的 tool_call_id。"
        candidate = scope["search"].get(call_id)
        if candidate is None:
            return "该调用不在本轮可读历史中，不能读取。"
        if call_id in scope["used"]:
            return "该原文已在本轮工具结果中提供，请直接复用。"
        scope["used"].add(call_id)
        emit(event, "search_history_read", "success", tool_call_id=call_id)
        return candidate["raw"]

    def _expire(self, scope):
        self.scopes.pop(id(scope), None)
        if scope is scope["event"].get_extra(KEY):
            scope["event"].set_extra(KEY, None)
        scope["search"].clear()
        scope["emotion"].clear()
        scope["used"].clear()
        scope["protected"].clear()

    def release(self, runner):
        binding = self.bindings.pop(id(runner), None)
        if not binding:
            return
        _, original, wrapped, had_local, scope = binding
        name = "_sanitize_contexts_for_provider"
        if getattr(runner, name, None) is wrapped:
            if had_local:
                setattr(runner, name, original)
            else:
                delattr(runner, name)
        self._expire(scope)

    async def close(self):
        self.closed = True
        for runner, *_ in list(self.bindings.values()):
            self.release(runner)
        # 未进入运行器的请求也可能已挂载补读工具，卸载时同样清空全文范围。
        for scope in list(self.scopes.values()):
            self._expire(scope)
