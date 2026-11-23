"""工具循环护栏离线测试：去重、只读重试、副作用工具不缓存。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types

from integrations.guard_tool_loop import GuardedToolExecutor


class Event:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def is_stopped(self):
        return False


def outcome(text="ok", *, error=False):
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)], isError=error)


def text_of(items):
    parts = []
    for item in items:
        if isinstance(item, mcp.types.CallToolResult):
            for content in item.content:
                parts.append(getattr(content, "text", ""))
    return "\n".join(parts)


class OriginalExecutor:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def execute(self, tool, run_context, **tool_args):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item


class ToolLoopGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = Event()
        self.context = SimpleNamespace(
            context=SimpleNamespace(event=self.event))
        self.plugin = SimpleNamespace(config={})
        self.tool = SimpleNamespace(name="web_search_baidu")

    async def collect(self, executor, tool=None, **args):
        return [item async for item in executor.execute(
            tool=tool or self.tool, run_context=self.context, **args)]

    async def test_identical_call_runs_once_and_reuses_result(self):
        original = OriginalExecutor([outcome("first")])
        executor = GuardedToolExecutor(original, self.plugin)
        first = await self.collect(executor, query="今天天气")
        second = await self.collect(executor, query="今天天气")
        self.assertEqual(original.calls, 1)
        self.assertEqual(text_of(first), "first")
        self.assertIn("duplicate suppressed", text_of(second))

    async def test_side_effect_tool_deduplicates_but_never_retries(self):
        original = OriginalExecutor([outcome("sent"), outcome("sent-again")])
        executor = GuardedToolExecutor(original, self.plugin)
        tool = SimpleNamespace(name="send_message_to_user")
        first = await self.collect(executor, tool=tool, text="hi")
        second = await self.collect(executor, tool=tool, text="hi")
        # 相同参数只发一次；第二次直接复用结果，不再执行副作用。
        self.assertEqual(original.calls, 1)
        self.assertIn("sent", text_of(first))
        self.assertIn("duplicate suppressed", text_of(second))

    async def test_side_effect_tool_failure_is_not_retried(self):
        original = OriginalExecutor([RuntimeError("boom")])
        executor = GuardedToolExecutor(original, self.plugin)
        tool = SimpleNamespace(name="future_task")
        with self.assertRaises(RuntimeError):
            await self.collect(executor, tool=tool, text="明天提醒我")
        self.assertEqual(original.calls, 1)

    async def test_read_only_tool_retries_exception_then_succeeds(self):
        original = OriginalExecutor([RuntimeError("boom"), outcome("ok")])
        executor = GuardedToolExecutor(original, self.plugin)
        with patch("integrations.guard_tool_loop.asyncio.sleep", new=self.fast_sleep):
            items = await self.collect(executor, query="x")
        self.assertEqual(original.calls, 2)
        self.assertEqual(text_of(items), "ok")

    async def test_read_only_tool_retries_error_result_then_succeeds(self):
        original = OriginalExecutor([outcome("upstream unavailable", error=True), outcome("ok")])
        executor = GuardedToolExecutor(original, self.plugin)
        with patch("integrations.guard_tool_loop.asyncio.sleep", new=self.fast_sleep):
            items = await self.collect(executor, query="x")
        self.assertEqual(original.calls, 2)
        self.assertEqual(text_of(items), "ok")

    async def test_parameter_mismatch_is_not_retried(self):
        original = OriginalExecutor([RuntimeError("Tool handler parameter mismatch")])
        executor = GuardedToolExecutor(original, self.plugin)
        with self.assertRaises(RuntimeError):
            await self.collect(executor, query="x")
        self.assertEqual(original.calls, 1)

    async def test_failed_result_is_cached_as_failure(self):
        self.plugin.config["tool_local_retry_attempts"] = 1
        # 正文不带 error:，必须靠协议错误位识别；缓存回执也不能丢掉该位。
        original = OriginalExecutor([outcome("upstream unavailable", error=True)])
        executor = GuardedToolExecutor(original, self.plugin)
        first = await self.collect(executor, query="x")
        second = await self.collect(executor, query="x")
        self.assertEqual(original.calls, 1)
        self.assertTrue(first[-1].model_dump(by_alias=True)["isError"])
        self.assertTrue(second[-1].model_dump(by_alias=True)["isError"])
        self.assertIn("已经执行过并失败", text_of(second))

    async def fast_sleep(self, _seconds):
        return None


if __name__ == "__main__":
    unittest.main()
