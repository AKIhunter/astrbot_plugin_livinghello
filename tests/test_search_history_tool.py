"""百度旧结果摘录与本轮原文补读；只执行隔离工具和消息契约。"""
import copy
import json
import os
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from input.guard_history_view import (
    collect_search_candidates, compact_baidu_result, KEY, MIN_SEARCH_RESULT_CHARS,
    TOOL_NAME,
)
import test_history_view as history_fixtures

SOURCE = history_fixtures.SOURCE
native_messages = history_fixtures.native_messages


def search_round(number=1, *, raw=None, name="web_search_baidu"):
    call_id = f"call-{number}"
    raw = raw or json.dumps({"results": [
        {"title": "原题", "url": "https://example.test/page?a=1&b=2",
         "index": "abcd.1", "snippet": "搜索材料。后半段关键参数为接口USB4。" * 200},
        {"title": "另一个来源", "url": "https://example.test/other",
         "index": "abcd.2", "snippet": "短摘要保持原样。"},
    ]}, ensure_ascii=False)
    return [
        {"role": "user", "content": "原查询"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": '{"query":"原查询"}'}}]},
        {"role": "tool", "tool_call_id": call_id, "content": raw},
        {"role": "assistant", "content": "根据已查资料回答"},
    ]


def complete_rounds(count=2):
    result = []
    for i in range(count):
        result += [{"role": "user", "content": f"后续问题{i}"},
                   {"role": "assistant", "content": f"后续回答{i}"}]
    return result


@unittest.skipUnless((SOURCE / "core/agent/tool.py").is_file(),
                     "需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
class SearchHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = history_fixtures.HistoryViewTests.asyncTearDown

    def setUp(self):
        history_fixtures.HistoryViewTests.setUp(self)
        from host_fixtures import install_tools
        with patch.dict(os.environ, {"ASTRBOT_NATIVE_SOURCE": str(SOURCE)}):
            install_tools()

    def prepare_search(self, rows=None):
        self.event.set_extra(KEY, None)
        self.request.contexts = rows if rows is not None else search_round() + complete_rounds()
        self.request.func_tool = None
        self.scope = self.service.prepare(self.event, self.request)
        return self.scope

    def test_two_recent_rounds_and_current_tool_cycle_are_never_shortened(self):
        for count in (0, 1, 2, 3):
            rows = search_round() + complete_rounds(count)
            scope = self.prepare_search(rows)
            self.assertEqual(bool(scope["search"]), count >= 2)
        rows = search_round() + complete_rounds() + search_round(2)[:-1]
        self.prepare_search(rows)
        view = self.service.for_provider(self.event, rows)
        self.assertNotEqual(view[2]["content"], rows[2]["content"])
        self.assertEqual(view[-1], rows[-1])
        # 当前轮再追加内部 user 指令，不得重新计算旧候选窗口。
        rows.append({"role": "user", "content": "仅补齐工具参数"})
        self.assertEqual(self.service.for_provider(self.event, rows)[-2], rows[-2])

    def test_urls_titles_indices_and_pairing_remain_exact(self):
        self.prepare_search()
        original = copy.deepcopy(self.request.contexts)
        view = self.service.for_provider(self.event, self.request.contexts)
        compact = json.loads(view[2]["content"])
        raw = json.loads(original[2]["content"])
        self.assertEqual(view[1], original[1])
        self.assertEqual(view[2]["tool_call_id"], original[2]["tool_call_id"])
        for short, full in zip(compact["results"], raw["results"], strict=True):
            for key in ("url", "title", "index"):
                self.assertEqual(short[key], full[key])
        self.assertEqual(compact["results"][1]["snippet"], raw["results"][1]["snippet"])
        self.assertLessEqual(len(compact["results"][0]["snippet"]), 160)
        self.assertEqual(compact["verification"], "search_snippets_only")
        self.assertIsNone(compact["published_at"])
        self.assertIsNone(compact["retrieved_at"])
        self.assertEqual(self.request.contexts, original)
        self.assertLess(len(view[2]["content"]), len(original[2]["content"]))

    def test_bad_payload_unknown_fields_failures_and_other_tools_keep_original(self):
        good = search_round()[2]["content"]
        changed = json.loads(good)
        changed["results"][0]["published_at"] = "unknown"
        for raw in ("Error: search failed", "{broken" * 500, '{"results":[]}',
                    json.dumps(changed), json.dumps({"other": "x" * 3000})):
            self.prepare_search(search_round(raw=raw) + complete_rounds())
            self.assertFalse(self.scope["search"])
        self.prepare_search(search_round(name="fetch_url") + complete_rounds())
        self.assertFalse(self.scope["search"])
        call = search_round()[1]["tool_calls"][0]
        self.assertIsNone(compact_baidu_result(" " * (MIN_SEARCH_RESULT_CHARS - 1), call)[0])
        long_url = json.loads(good)
        long_url["results"][0]["url"] = "https://example.test/" + "x" * 5000
        long_url["results"][0]["snippet"] = "短"
        raw = json.dumps(long_url, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(compact_baidu_result(raw, call)[1], "no_saving")

    def test_duplicate_or_hanging_calls_are_not_eligible(self):
        for mutate in (
            lambda rows: rows.insert(3, copy.deepcopy(rows[2])),
            lambda rows: rows.pop(2),
            lambda rows: rows[1]["tool_calls"].append(copy.deepcopy(rows[1]["tool_calls"][0])),
            lambda rows: rows[1]["tool_calls"][0].update(id=["invalid"]),
        ):
            rows = search_round()
            mutate(rows)
            candidates, _ = collect_search_candidates(rows + complete_rounds())
            self.assertFalse(candidates)
        self.prepare_search()
        # 循环中突然出现同ID工具，也不能替换一个无法唯一配对的结果。
        rows = self.request.contexts + [copy.deepcopy(self.request.contexts[2])]
        self.assertEqual(self.service.for_provider(self.event, rows)[2], rows[2])

    async def test_read_tool_validates_schema_and_returns_full_original_once(self):
        self.prepare_search()
        tool = next(t for t in self.request.func_tool.tools if t.name == TOOL_NAME)
        self.assertEqual(tool.parameters["required"], ["tool_call_id"])
        self.assertFalse(tool.parameters["additionalProperties"])
        import jsonschema
        jsonschema.Draft7Validator.check_schema(tool.parameters)
        jsonschema.validate({"tool_call_id": "call-1"}, tool.parameters)
        for arguments in ({}, {"tool_call_id": 1},
                          {"tool_call_id": "call-1", "conversation_id": "outside"}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(arguments, tool.parameters)
        context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        self.assertIn("tool_call_id", await tool.call(context))
        self.assertEqual(await tool.call(context, tool_call_id="call-1"),
                         self.request.contexts[2]["content"])
        self.assertIn("直接复用", await tool.call(context, tool_call_id="call-1"))
        self.assertIn("不能读取", await tool.call(context, tool_call_id="outside"))
        self.event.get_sender_id = lambda: "another"
        self.assertIn("失效", await tool.call(context, tool_call_id="call-1"))

    def test_active_blacklist_does_not_expand_group_history_read_access(self):
        self.event.get_group_id = lambda: "group-1"
        policy = self.guard._policy()
        self.guard._policy = lambda: SimpleNamespace(revision=policy.revision, black_active=True)
        self.prepare_search()
        self.assertFalse(self.scope["search"])
        self.assertIsNone(self.request.func_tool)
        self.assertEqual(self.service.for_provider(self.event, self.request.contexts)[2],
                         self.request.contexts[2])

    async def test_close_clears_unbound_search_scope_and_rejects_existing_tool(self):
        self.prepare_search()
        tool = self.request.func_tool.tools[-1]
        await self.service.close()
        self.assertFalse(self.scope["search"])
        self.assertFalse(self.service.scopes)
        self.assertIsNone(self.event.get_extra(KEY))
        context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        self.assertIn("失效", await tool.call(context, tool_call_id="call-1"))

    def test_toolset_is_copied_and_reader_failure_keeps_long_result(self):
        self.prepare_search()
        from astrbot.core.agent.tool import ToolSet
        shared = ToolSet()
        self.event.set_extra(KEY, None)
        self.request.func_tool = shared
        self.service.prepare(self.event, self.request)
        self.assertEqual(shared.tools, [])
        self.assertIsNot(self.request.func_tool, shared)
        self.event.set_extra(KEY, None)
        with patch("input.guard_search_history_tool.ReadSearchHistoryTool",
                   side_effect=ValueError("fixture")):
            scope = self.service.prepare(self.event, self.request)
        self.assertFalse(scope["search"])
        self.assertEqual(self.service.for_provider(self.event, self.request.contexts)[2],
                         self.request.contexts[2])

    async def test_reasoning_retry_sends_same_excerpt_without_mutating_history(self):
        from reply.guard_reasoning_retry import ReasoningRetry, REQUEST_KEY
        from test_reasoning_retry import response
        self.prepare_search()
        self.event.set_extra(REQUEST_KEY, self.request)
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=response(text="离线补答")))
        self.guard.context.get_provider_by_id = lambda _: provider
        self.guard._delivery = lambda: SimpleNamespace(recalled=lambda _: False)
        retry = ReasoningRetry(self.guard)
        original = copy.deepcopy(self.request.contexts)
        await retry.recover(self.event, SimpleNamespace(messages=[]), response())
        payload = provider.text_chat.await_args.kwargs
        self.assertEqual(json.loads(payload["contexts"][2]["content"])["tool_call_id"], "call-1")
        self.assertEqual(self.request.contexts, original)

    async def test_real_messages_keep_full_search_for_history_and_full_readback_in_loop(self):
        self.prepare_search()
        native = native_messages()
        messages = native.bind_checkpoint_messages(self.request.contexts)
        saved_before = native.dump_messages_with_checkpoints(messages)
        view = self.service.for_provider(self.event, messages)
        self.assertIn("search_excerpt", view[2].content)
        self.assertEqual(native.dump_messages_with_checkpoints(messages), saved_before)
        full = await self.service.read(self.event, self.scope, "call-1")
        messages += native.bind_checkpoint_messages([
            {"role": "user", "content": "当前问细节"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read-1", "type": "function", "function": {
                    "name": TOOL_NAME, "arguments": '{"tool_call_id":"call-1"}'}}]},
            {"role": "tool", "tool_call_id": "read-1", "content": full}])
        self.assertEqual(self.service.for_provider(self.event, messages)[-1].content, full)
        self.service._expire(self.scope)
        self.assertFalse(self.scope["search"])
        self.assertIsNone(self.event.get_extra(KEY))

    async def test_native_fallback_and_argument_requery_keep_current_state_and_old_excerpt(self):
        rows = search_round() + complete_rounds()
        rows[0] = history_fixtures.history()[0]
        self.prepare_search(rows)
        native = native_messages()
        current = native.TextPart(text=history_fixtures.state()).mark_as_temp()
        messages = native.bind_checkpoint_messages(rows + [{
            "role": "user", "content": [
                {"type": "text", "text": "当前继续问接口"}, current.model_dump_for_context()]}])
        cls = history_fixtures.native_runner_class()
        runner = cls()
        calls = []

        async def chat(**payload):
            calls.append(payload)
            return SimpleNamespace(role="assistant", completion_text="离线回复",
                                   is_chunk=False, tools_call_name=[])

        async def failed_chat(**payload):
            calls.append(payload)
            return SimpleNamespace(role="err", completion_text="unavailable",
                                   is_chunk=False, tools_call_name=[])

        provider = SimpleNamespace(provider_config={}, text_chat=chat)
        runner.provider = SimpleNamespace(provider_config={}, text_chat=failed_chat)
        runner.fallback_providers = [provider]
        runner.req = self.request
        self.request.session_id, self.request.model = "fixture", "fake"
        self.request.extra_user_content_parts = [current]
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=self.event),
                                             messages=messages)
        runner.streaming, runner.request_max_retries = False, 1
        runner._abort_signal = asyncio.Event()
        runner._is_stop_requested = lambda: False
        runner._await_or_stop = lambda awaitable: awaitable
        runner._sanitize_malformed_tool_calls = lambda _: None
        runner.EMPTY_OUTPUT_RETRY_ATTEMPTS = 1
        runner.EMPTY_OUTPUT_RETRY_WAIT_MIN_S = runner.EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 0
        runner.SKILLS_LIKE_REQUERY_INSTRUCTION_TEMPLATE = "调用 {tool_names} 或解释"
        runner.SKILLS_LIKE_REQUERY_REPAIR_INSTRUCTION = "补齐参数"
        original = native.dump_messages_with_checkpoints(messages)
        self.assertTrue(self.service.bind(self.event, runner))
        responses = [r async for r in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(len(responses), 1)
        self.assertEqual(len(calls), 2)
        for payload in calls:
            self.assertIn("search_excerpt", payload["contexts"][2].content)
            self.assertEqual(len(payload["contexts"][0].content), 2)
            self.assertEqual(payload["contexts"][-1].content[-1].text, current.text)
        from astrbot.core.agent.tool import ToolSet
        runner._tool_schema_param_set = ToolSet()
        runner._tool_schema_param_set.add_tool(self.request.func_tool.tools[-1])
        # 首次参数补查无正文，强提示补查再返回正文，两个实际payload都要保留本轮状态。
        async def requery_chat(**payload):
            result = await chat(**payload)
            if len(calls) == 3:
                result.completion_text = ""
            return result
        provider.text_chat = requery_chat
        await runner._resolve_tool_exec(SimpleNamespace(tools_call_name=[TOOL_NAME]))
        self.assertEqual(len(calls), 4)
        for payload in calls[2:]:
            self.assertIn("search_excerpt", payload["contexts"][2]["content"])
            self.assertEqual(len(payload["contexts"][0]["content"]), 2)
            self.assertEqual(payload["contexts"][-2]["content"][-1]["text"], current.text)
        self.assertEqual(native.dump_messages_with_checkpoints(messages), original)
        # 后备模型没有工具能力时不可承诺补读，搜索回到全文。
        provider.provider_config = {"modalities": ["text"]}
        projected = runner._sanitize_contexts_for_provider(messages)
        self.assertTrue(any(m["content"] == "[Tool result]\n" + messages[2].content
                            for m in projected))

    def test_multiple_tools_checkpoints_and_native_private_metadata_survive(self):
        rows = search_round()
        second = search_round(2)
        rows[1]["tool_calls"] += second[1]["tool_calls"]
        rows.insert(3, second[2])
        rows.insert(3, {"role": "_checkpoint", "content": {"id": "history-checkpoint"}})
        self.prepare_search(rows + complete_rounds())
        self.assertEqual(set(self.scope["search"]), {"call-1", "call-2"})
        native = native_messages()
        messages = native.bind_checkpoint_messages(self.request.contexts)
        before = native.dump_messages_with_checkpoints(messages)
        projected = self.service.for_provider(self.event, messages)
        self.assertEqual(projected[2]._checkpoint_after.id, "history-checkpoint")
        self.assertEqual(native.dump_messages_with_checkpoints(messages), before)
