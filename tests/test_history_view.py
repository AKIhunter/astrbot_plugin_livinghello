"""历史发送副本的隔离验收；真实宿主消息与发送方法，不调用网络。"""
import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import typing
import unittest
from unittest.mock import AsyncMock

from input.guard_history_view import HistoryView, KEY, STATE_HEADER
from reply.guard_reply_fallback import find_current_runner
from test_gate import gate_config, plugin
from test_reply_delivery import Event

SOURCE = Path(os.environ.get(
    "ASTRBOT_NATIVE_SOURCE",
    str(Path(__file__).resolve().parents[3] / ".livingemotion-host-t1/AstrBot/astrbot"),
))


def state(number=1):
    return STATE_HEADER + json.dumps({
        "daily_emotions": {"joy": number / 10},
        "relationship": {"raw_score": number, "note": "保持完整的关系字段"},
        "night": {"phase": "awake", "energy": 0.8},
    }, ensure_ascii=False)


def history():
    return [
        {"role": "user", "content": [
            {"type": "text", "text": "原始问题"},
            {"type": "text", "text": state()},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
        {"role": "assistant", "content": "原回复"},
    ]


def native_messages():
    name = "_livinghello_history_native_messages"
    if name not in sys.modules:
        module = ModuleType(name)
        sys.modules[name] = module
        path = SOURCE / "core/agent/message.py"
        exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return sys.modules[name]


def native_runner_class():
    path = SOURCE / "core/agent/runners/tool_loop_agent_runner.py"
    owner = next(n for n in ast.parse(path.read_text()).body
                 if isinstance(n, ast.ClassDef) and n.name == "ToolLoopAgentRunner")
    names = {"_iter_llm_responses", "_sanitize_contexts_for_provider", "step",
             "_iter_llm_responses_with_fallback", "_resolve_tool_exec",
             "_build_tool_subset", "_has_meaningful_assistant_reply",
             "_build_tool_requery_context", "_func_tool_for_provider"}
    owner.bases, owner.decorator_list = [], []
    owner.body = [n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name in names]
    for method in owner.body:
        method.decorator_list = [d for d in method.decorator_list
                                 if isinstance(d, ast.Name) and d.id == "staticmethod"]
    namespace = {
        "T": typing, "AgentState": SimpleNamespace(IDLE="idle", RUNNING="running"),
        "logger": SimpleNamespace(warning=lambda *_, **__: None, debug=lambda *_, **__: None),
        "copy": copy,
    }
    from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential
    namespace.update(
        AsyncRetrying=AsyncRetrying, retry_if_exception_type=retry_if_exception_type,
        stop_after_attempt=stop_after_attempt, wait_exponential=wait_exponential,
        EmptyModelOutputError=type("EmptyModelOutputError", (Exception,), {}),
    )
    if "astrbot.core.agent.tool" in sys.modules:
        namespace["ToolSet"] = sys.modules["astrbot.core.agent.tool"].ToolSet
    modality_path = SOURCE / "core/provider/modalities.py"
    modality_ast = ast.parse(modality_path.read_text())
    modality_ast.body = [
        n for n in modality_ast.body
        if not (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("astrbot"))
    ]
    modality_module = ModuleType("_livinghello_history_native_modalities")
    sys.modules[modality_module.__name__] = modality_module
    modality_module.__dict__.update(Message=native_messages().Message, logger=namespace["logger"])
    exec(compile(modality_ast, str(modality_path), "exec"), modality_module.__dict__)
    namespace.update(
        sanitize_contexts_by_modalities=modality_module.sanitize_contexts_by_modalities,
        log_context_sanitize_stats=modality_module.log_context_sanitize_stats,
    )
    tree = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        owner], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace["ToolLoopAgentRunner"]


class HistoryViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = plugin(gate_config())
        self.guard._policy_send_reason = lambda _: ""
        self.event = Event(group="")
        self.request = SimpleNamespace(contexts=history(), conversation=SimpleNamespace(cid="c1"))
        self.service = self.guard.history_view = HistoryView(self.guard)
        self.scope = self.service.prepare(self.event, self.request)

    async def asyncTearDown(self):
        await self.service.close()

    def test_old_state_removed_only_from_copy_and_current_state_retained(self):
        original = copy.deepcopy(self.request.contexts)
        current = {"role": "user", "content": [
            {"type": "text", "text": "当前问题"},
            {"type": "text", "text": state(), "_no_save": True}]}
        contexts = self.request.contexts + [current]
        view = self.service.for_provider(self.event, contexts)
        self.assertEqual(self.request.contexts, original)
        self.assertEqual(len(view[0]["content"]), 2)
        self.assertEqual(view[0]["content"][0]["text"], "原始问题")
        self.assertEqual(view[0]["content"][1]["type"], "image_url")
        self.assertEqual(view[-1], current)

    def test_user_quote_string_format_and_unknown_state_are_preserved(self):
        quote = {"role": "user", "content": [
            {"type": "text", "text": state()},
            {"type": "text", "text": "<!--livingemotion:memory:v2-->记忆"}]}
        contexts = [quote, {"role": "user", "content": "请解释 " + state()},
                    {"role": "user", "content": [
                        {"type": "text", "text": "正文"},
                        {"type": "text", "text": STATE_HEADER + "未知历史"}]}]
        self.assertEqual(self.service.for_provider(self.event, contexts), contexts)

    def test_policy_sender_persona_and_conversation_changes_expire_scope(self):
        mutations = [
            lambda: setattr(self.event, "get_sender_id", lambda: "another"),
            lambda: self.event.set_extra("congling:persona_token", "new"),
            lambda: setattr(self.request.conversation, "cid", "other"),
            lambda: setattr(self.guard, "_policy_send_reason", lambda _: "blocked"),
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.setUp()
                original = self.request.contexts
                change()
                self.assertIs(self.service.for_provider(self.event, original), original)

    @unittest.skipUnless((SOURCE / "core/agent/message.py").is_file(),
                         "需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
    def test_same_event_freezes_full_state_and_absence_does_not_restore_it(self):
        native = native_messages()
        first = native.TextPart(text=state())
        req = SimpleNamespace(extra_user_content_parts=[first])
        self.guard._freeze_emotion_snapshot(self.event, req)
        req.extra_user_content_parts = [native.TextPart(text=state(2))]
        self.guard._freeze_emotion_snapshot(self.event, req)
        self.assertIs(req.extra_user_content_parts[0], first)
        self.assertEqual(first.text, state())
        self.assertTrue(first._no_save)
        req.extra_user_content_parts = []
        self.guard._freeze_emotion_snapshot(self.event, req)
        self.assertEqual(req.extra_user_content_parts, [])

    @unittest.skipUnless((SOURCE / "core/agent/message.py").is_file(),
                         "需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
    def test_real_message_binding_and_persistence_preserve_original_history(self):
        native = native_messages()
        current = native.TextPart(text=state()).mark_as_temp()
        raw = self.request.contexts + [{"role": "user", "content": [
            {"type": "text", "text": "当前正文"}, current.model_dump_for_context()]}]
        messages = native.bind_checkpoint_messages(raw)
        before = native.dump_messages_with_checkpoints(messages)
        view = self.service.for_provider(self.event, messages)
        self.assertEqual(len(view[0].content), 2)
        self.assertEqual(view[-1].content[-1].text, current.text)
        self.assertTrue(view[-1].content[-1]._no_save)
        saved = native.dump_messages_with_checkpoints(messages)
        self.assertEqual(saved, before)
        self.assertEqual(saved[0]["content"][1]["text"], state())
        self.assertEqual(saved[-1]["content"], [{"type": "text", "text": "当前正文"}])

    @unittest.skipUnless((SOURCE / "core/agent/runners/tool_loop_agent_runner.py").is_file(),
                         "需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
    async def test_real_runner_sends_view_for_stream_and_non_stream_and_releases(self):
        cls = native_runner_class()
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                runner = cls()
                calls = []

                async def chat(**payload):
                    calls.append(payload)
                    return SimpleNamespace(completion_text="离线正文", is_chunk=False)

                async def stream(**payload):
                    yield await chat(**payload)

                runner.provider = SimpleNamespace(provider_config={}, text_chat=chat,
                                                  text_chat_stream=stream)
                runner.req = self.request
                self.request.session_id, self.request.model = "fixture", "fake"
                self.request.extra_user_content_parts = []
                runner.run_context = SimpleNamespace(
                    context=SimpleNamespace(event=self.event),
                    messages=copy.deepcopy(self.request.contexts) + [
                        {"role": "user", "content": "当前正文"}])
                runner.streaming, runner.request_max_retries = streaming, 1
                runner._abort_signal = asyncio.Event()
                runner._await_or_stop = lambda awaitable: awaitable
                runner._close_executor = lambda generator: generator.aclose()
                runner._func_tool_for_provider = lambda: None
                original = copy.deepcopy(runner.run_context.messages)
                self.assertTrue(self.service.bind(self.event, runner))
                self.assertTrue(self.service.bind(self.event, runner))
                responses = [r async for r in runner._iter_llm_responses()]
                self.assertEqual(len(responses), 1)
                self.assertEqual(len(calls[0]["contexts"][0]["content"]), 2)
                self.assertEqual(runner.run_context.messages, original)
                self.service.release(runner)
                self.assertNotIn("_sanitize_contexts_for_provider", vars(runner))
                self.service.prepare(self.event, self.request)

    @unittest.skipUnless((SOURCE / "core/agent/runners/tool_loop_agent_runner.py").is_file(),
                         "需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
    async def test_true_step_locates_same_runner_without_retry_enabled(self):
        cls = native_runner_class()
        runner = cls()
        runner.req, runner._state, runner._step_budget_max = self.request, "idle", None
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=self.event),
                                             messages=self.request.contexts)
        located = []

        async def begin(context):
            located.append(find_current_runner(context))
            self.service.bind(self.event, located[-1])

        runner.agent_hooks = SimpleNamespace(on_agent_begin=begin)
        runner._transition_state = lambda _: None
        runner._simple_print_message_role = lambda *_: None
        runner._await_or_stop = lambda awaitable: awaitable
        runner.request_context_manager = SimpleNamespace(process=AsyncMock(return_value=None))
        runner._finalize_aborted_step = AsyncMock(return_value="cancelled")
        self.request.conversation.token_usage = 0
        self.assertEqual(await anext(runner.step()), "cancelled")
        self.assertIs(located[0], runner)
        self.assertIn(id(runner), self.service.bindings)
        await self.service.close()
        self.assertFalse(self.service.bindings)

    async def test_task_completion_cancellation_and_exception_release_instance(self):
        for ending in ("normal", "cancel", "exception"):
            with self.subTest(ending=ending):
                ready = asyncio.Event()
                finish = asyncio.Event()
                runner = SimpleNamespace(
                    req=self.request, run_context=SimpleNamespace(
                        context=SimpleNamespace(event=self.event)),
                    _sanitize_contexts_for_provider=lambda rows: rows)
                original = runner._sanitize_contexts_for_provider

                async def work():
                    self.service.prepare(self.event, self.request)
                    self.assertTrue(self.service.bind(self.event, runner))
                    ready.set()
                    await finish.wait()
                    if ending == "exception":
                        raise ValueError("offline exception")

                task = asyncio.create_task(work())
                await ready.wait()
                if ending == "cancel":
                    task.cancel()
                else:
                    finish.set()
                await asyncio.gather(task, return_exceptions=True)
                self.assertIs(runner._sanitize_contexts_for_provider, original)
                self.assertFalse(self.service.bindings)
                self.assertIsNone(self.event.get_extra(KEY))

    def test_unsupported_runner_or_signature_does_not_change_messages(self):
        for sanitize in (None, lambda rows, required: rows):
            runner = SimpleNamespace(
                req=self.request, run_context=SimpleNamespace(
                    context=SimpleNamespace(event=self.event)),
                _sanitize_contexts_for_provider=sanitize)
            self.assertFalse(self.service.bind(self.event, runner))
            self.assertIs(runner._sanitize_contexts_for_provider, sanitize)
        self.assertFalse(self.service.bind(self.event, None))
