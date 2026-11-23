"""工具循环护栏：相同调用只执行一次，只读工具异常时本地按原参数重试。

宿主每拿到一批工具结果就会再请求一次大模型。模型偶尔会把同一个工具、同一组
参数连续调用多次；如果工具本身还会失败，模型就会「失败 -> 原样再调」地空转，
每转一圈都是一次完整的大模型请求。这里在工具执行器外面加一层薄代理：

1. 同一条用户消息内，工具名与参数完全相同的调用只真正执行一次，后续直接复用
   结果，并明确告诉模型「已经执行过，不要重复」；
2. 只读类工具抛异常或返回 isError 时，由代码按同一参数在本地重试，不再把
   失败结果丢回模型让它重新发一次相同调用；
3. 发送、生图、写入、记忆变更等有副作用的工具不缓存也不自动重试，避免重复
   发送、重复扣费或重复写入。

代理只替换当前 runner 的 ``tool_executor``，不改宿主源码；runner 与事件一一对应，
随本轮结束自然释放。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import mcp.types
from astrbot.api import logger

try:
    from ..observability.guard_request_trace import emit
    from ..reply.guard_reply_fallback import find_current_runner
except ImportError:  # 兼容单文件调试与测试入口
    from observability.guard_request_trace import emit
    from reply.guard_reply_fallback import find_current_runner

CACHE_KEY = "_livinghello_tool_result_cache"
STATS_KEY = "_livinghello_tool_loop_stats"

DEFAULT_ATTEMPTS = 3
CACHE_LIMIT = 64
BASE_DELAY = 0.5
MAX_DELAY = 2.0

# 有副作用的工具：结果仍去重（避免重复发送/重复建任务），但绝不自动重试。
SIDE_EFFECT_TOOLS = frozenset({
    "send_message_to_user",
    "livinghello_generate_image",
    "memorize_long_term_memory",
    "forget_long_term_memory",
    "update_user_profile",
    "future_task",
    "bili_action",
    "bili_block_user",
    "bili_owner_feedback",
    "watch_and_share_video_private",
    "transfer_to_ops_assistant",
    "watch_video",
    "bili_watch_videos",
})
SIDE_EFFECT_PREFIXES = (
    "send_", "write_", "edit_", "delete_", "create_", "upload_",
    "install_", "run_", "execute_", "python", "transfer_",
    "share_", "schedule_", "remind_",
)

# 这些异常重试也不会变好，直接交回宿主。
NO_RETRY_MARKERS = (
    "parameter mismatch",
    "unknown method",
    "not found",
    "缺少",
    "无权",
    "权限",
)


def _event_of(run_context):
    return getattr(getattr(run_context, "context", None), "event", None)


def _tool_name(tool) -> str:
    return str(getattr(tool, "name", "") or "")


def _cache_key(tool, tool_args: dict[str, Any]) -> str:
    payload = json.dumps(tool_args, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_tool_name(tool)}:{digest}"


def _idempotent(name: str) -> bool:
    if not name or name in SIDE_EFFECT_TOOLS:
        return False
    return not name.startswith(SIDE_EFFECT_PREFIXES)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    # 宿主的中断异常只在内部流转，不能吞掉。
    if type(exc).__name__ in {"_ToolExecutionInterrupted", "ToolExecutionInterrupted"}:
        return False
    text = str(exc).lower()
    return not any(marker in text for marker in NO_RETRY_MARKERS)


def _stopped(event) -> bool:
    checker = getattr(event, "is_stopped", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(event, "stopped", False))


def _is_error(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, mcp.types.CallToolResult):
            continue
        if getattr(item, "isError", False):
            return True
        for content in getattr(item, "content", None) or []:
            text = getattr(content, "text", "")
            if isinstance(text, str) and text.lstrip().lower().startswith("error:"):
                return True
    return False


def _text_notice(text: str, *, is_error: bool = False) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _with_notice(items: list[Any], text: str) -> list[Any]:
    """在复用结果后追加一句提示；保留原有图片/文本内容。"""
    out: list[Any] = []
    added = False
    for item in items:
        if isinstance(item, mcp.types.CallToolResult):
            content = list(getattr(item, "content", None) or [])
            content.append(mcp.types.TextContent(type="text", text=text))
            out.append(mcp.types.CallToolResult(
                content=content, isError=bool(getattr(item, "isError", False))))
            added = True
        else:
            out.append(item)
    if not added:
        out.append(_text_notice(text, is_error=False))
    return out


def _remember(cache: dict[str, Any], key: str, items: list[Any], failed: bool) -> None:
    if len(cache) >= CACHE_LIMIT and key not in cache:
        cache.pop(next(iter(cache)), None)
    cache[key] = {"items": list(items), "failed": failed}


def _bump(event, field: str) -> None:
    stats = event.get_extra(STATS_KEY)
    if not isinstance(stats, dict):
        stats = {}
        event.set_extra(STATS_KEY, stats)
    stats[field] = int(stats.get(field, 0) or 0) + 1


class GuardedToolExecutor:
    """包住宿主执行器，只对当前 runner 生效。"""

    def __init__(self, original, plugin):
        self.original = original
        self.plugin = plugin

    def _attempts(self) -> int:
        config = getattr(self.plugin, "config", None) or {}
        value = config.get("tool_local_retry_attempts")
        if type(value) is int and 1 <= value <= 5:
            return value
        return DEFAULT_ATTEMPTS

    async def execute(self, tool, run_context, **tool_args):
        event = _event_of(run_context)
        name = _tool_name(tool)
        if event is None:
            async for item in self.original.execute(
                    tool=tool, run_context=run_context, **tool_args):
                yield item
            return

        # 所有工具都去重；只有只读工具允许本地自动重试。
        allow_retry = _idempotent(name)
        key = _cache_key(tool, tool_args)
        cache = event.get_extra(CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            event.set_extra(CACHE_KEY, cache)
        cached = cache.get(key)
        if cached is not None:
            _bump(event, "duplicates")
            emit(event, "tool_loop", "duplicate", tool=name,
                 failed=bool(cached.get("failed")))
            if cached.get("failed"):
                yield _text_notice(
                    "同一工具和参数在本轮已经执行过并失败，不要再重复调用；"
                    "换参数、换工具，或直接说明拿不到结果。", is_error=True)
            elif cached.get("items"):
                for item in _with_notice(
                        list(cached["items"]),
                        "\n\n[duplicate suppressed] 同一工具和参数在本轮已执行过，"
                        "以上为上次结果，不要再次调用该工具。"):
                    yield item
            return

        attempts = self._attempts()
        delay = BASE_DELAY
        for attempt in range(1, attempts + 1):
            buffered: list[Any] = []
            try:
                async for item in self.original.execute(
                        tool=tool, run_context=run_context, **tool_args):
                    buffered.append(item)
                failed = _is_error(buffered)
                if failed and allow_retry and attempt < attempts and not _stopped(event):
                    _bump(event, "retries")
                    emit(event, "tool_loop", "retry", tool=name,
                         attempt=attempt, reason="tool_error")
                    logger.warning(
                        "[LivingHello] 工具 %s 返回错误，本地第 %d 次重试", name, attempt)
                    await asyncio.sleep(delay)
                    delay = min(MAX_DELAY, delay * 2)
                    continue
                # 成功结果一律缓存；失败结果只缓存只读工具，副作用工具留给模型决定。
                if not failed or allow_retry:
                    _remember(cache, key, buffered, failed)
                for item in buffered:
                    yield item
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if allow_retry and attempt < attempts and _retryable(exc) and not _stopped(event):
                    _bump(event, "retries")
                    emit(event, "tool_loop", "retry", tool=name,
                         attempt=attempt, reason=type(exc).__name__)
                    logger.warning(
                        "[LivingHello] 工具 %s 异常（%s），本地第 %d 次重试",
                        name, type(exc).__name__, attempt)
                    await asyncio.sleep(delay)
                    delay = min(MAX_DELAY, delay * 2)
                    continue
                if allow_retry:
                    _remember(cache, key, [_text_notice(f"error: {exc!s}", is_error=True)], True)
                raise


def bind_current(event, run_context, plugin) -> bool:
    """在 on_agent_begin 里替换当前 runner 的执行器。

    每个 runner（含子代理）各自绑定一次；runner 与本轮事件一一对应，
    不把执行器缓存到全局，避免跨会话串线。
    """
    runner = find_current_runner(run_context)
    executor = getattr(runner, "tool_executor", None) if runner is not None else None
    if executor is None:
        if event is not None:
            emit(event, "tool_loop", "uncollected", reason="unsupported_runner")
        return False
    if isinstance(executor, GuardedToolExecutor):
        return True
    runner.tool_executor = GuardedToolExecutor(executor, plugin)
    return True
