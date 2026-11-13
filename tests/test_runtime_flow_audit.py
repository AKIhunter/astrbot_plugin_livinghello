"""Synthetic requests exercise actual hooks; no external models or platform sends."""
import json
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from images.guard_image_history import ImageHistory
from integrations.guard_livingemotion import STATE_MARKER
from reply.guard_segments import ReplySegments
from images.image_history import SUMMARY_PREFIX, TOOL_NAME, reference_id
from test_context_policy import SequenceProvider, output
from test_fragment_wait import request
from test_gate import gate_config, plugin
from test_livingemotion_handoff import API, PLUGIN, STATE, meta, scope
from test_proactive_group_judge import JudgeEvent
from test_reply_delivery import Chain, Event
from astrbot.api.message_components import Plain


class RuntimeFlowAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guards = []

    async def asyncTearDown(self):
        for guard in self.guards:
            await guard.terminate()

    def guard(self, **settings):
        guard = plugin(gate_config(
            expression_takeover_enabled=True, **settings),
            SequenceProvider(output()))
        guard.segments = ReplySegments(guard)
        # Independent generation tests cover this service; keep this fixture in memory.
        guard._image_generation = lambda: SimpleNamespace(attach=lambda *_: None)
        self.guards.append(guard)
        return guard

    async def test_inflight_group_message_still_reaches_participation_judgement(self):
        guard = self.guard(proactive_group_judge="all")
        event = JudgeEvent("新的问题需要回复")
        guard.proactive_inflight_until["1001"] = time.monotonic() + 60
        with patch("main.time", SimpleNamespace(monotonic=lambda: 100)):
            triggered = [item async for item in guard.coalesce_message_fragments(event)]
        self.assertEqual(triggered, [("llm", "新的问题需要回复")])
        await guard.guard_llm_request(event, request())
        self.assertIn("inflight", event.get_extra("_at_guard_observation_rules"))
        self.assertIsNotNone(event.get_extra("_at_guard_decision"))

    async def test_cooldown_and_disabled_proactive_entry_still_do_not_trigger(self):
        for config, cooling in (({"proactive_group_judge": "off"}, False),
                                ({"proactive_cooldown_seconds": 8}, True)):
            guard = self.guard(**config)
            if cooling:
                guard.last_proactive_reply["1001"] = 98
            with patch("main.time", SimpleNamespace(monotonic=lambda: 100)):
                self.assertEqual([item async for item in guard.coalesce_message_fragments(
                    JudgeEvent("新的问题需要回复"))], [])

    async def test_private_without_participation_still_saves_current_image_summary(self):
        guard = self.guard(apply_to_private=False)
        event = Event(group="")
        event.set_extra("_at_guard_understanding",
                        SimpleNamespace(vision_summary="合成图片：一只橙色的猫"))
        req = request()
        await guard.guard_llm_request(event, req)
        await guard.attach_reply_contract(event, req)
        summaries = [p for p in req.extra_user_content_parts
                     if p.text.startswith(SUMMARY_PREFIX)]
        self.assertEqual(len(summaries), 1)
        self.assertIn("橙色的猫", summaries[0].text)
        self.assertFalse(getattr(summaries[0], "no_save", False))
        self.assertFalse(event.stopped)
        self.assertIsNone(event.get_extra("_at_guard_decision"))
        guard.context.provider.prompts.clear()
        # Hook repetition must not append a second persisted caption.
        await guard.attach_reply_contract(event, req)
        self.assertEqual(sum(p.text.startswith(SUMMARY_PREFIX)
                             for p in req.extra_user_content_parts), 1)

    async def test_blacklist_cannot_persist_image_summary(self):
        guard = self.guard(message_blacklist_qq=["123"])
        event, req = Event(group=""), request()
        event.get_platform_name = lambda: "aiocqhttp"
        event.set_extra("_at_guard_understanding",
                        SimpleNamespace(vision_summary="不应保存的图片观察"))
        await guard.attach_reply_contract(event, req)
        self.assertFalse(any(p.text.startswith(SUMMARY_PREFIX)
                             for p in req.extra_user_content_parts))

    async def test_private_old_image_tool_is_available_without_participation(self):
        guard = self.guard(apply_to_private=False)
        event, req = Event(group=""), request()
        url = "https://example.invalid/synthetic-cat.png"
        req.contexts = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}}]},
            {"role": "user", "content": "后续合成文字一"},
            {"role": "user", "content": "后续合成文字二"}]
        req.conversation = SimpleNamespace(cid="synthetic-conversation")
        class ToolSet:
            def __init__(self):
                self.tools = []
            def add_tool(self, tool):
                self.tools.append(tool)
        class HistoryTool:
            name = TOOL_NAME
            def __init__(self, service, scope):
                self.service, self.scope = service, scope
        module = types.ModuleType("astrbot.core.agent.tool")
        module.ToolSet = ToolSet
        wrapper = types.ModuleType("images.guard_history_image_tool")
        wrapper.ReadHistoryImageTool = HistoryTool
        with patch.dict(sys.modules, {
            "astrbot.core.agent.tool": module,
            "images.guard_history_image_tool": wrapper,
        }):
            await guard.attach_reply_contract(event, req)
        self.assertIn(TOOL_NAME, [t.name for t in req.func_tool.tools])
        self.assertEqual(event.get_extra("_livinghello_history_image_scope")["references"],
                         {reference_id(url): url})

    async def test_four_optional_plugin_combinations_keep_private_reply_alive(self):
        for memory_on, emotion_on in ((False, False), (True, False),
                                     (False, True), (True, True)):
            with self.subTest(memory=memory_on, emotion=emotion_on):
                guard = self.guard(coupling_livingmemory_enabled=memory_on,
                                   coupling_livingemotion_enabled=emotion_on)
                event, req = Event(group=""), request()
                api = API()
                memory = SimpleNamespace(
                    initializer=SimpleNamespace(memory_engine=object()),
                    search_memories_for_event=AsyncMock(return_value=[
                        SimpleNamespace(content="合成用户喜欢橙色",
                                        metadata={"persona_id": "synthetic"})]))
                registry = {
                    PLUGIN: meta(api),
                    "astrbot_plugin_livingmemory": SimpleNamespace(
                        activated=True, star_cls=memory),
                }
                guard.context.get_registered_star = registry.get
                event.set_extra("livingemotion:context", scope(event))
                req.extra_user_content_parts = [SimpleNamespace(text=STATE),
                    SimpleNamespace(text="<RAG-Faiss-Memory>synthetic")]
                req.func_tool = SimpleNamespace(tools=[
                    SimpleNamespace(name="recall_long_term_memory"),
                    SimpleNamespace(name="memorize_long_term_memory"),
                    SimpleNamespace(name="knowledge_search")])
                shared = req.func_tool
                await guard.listen_livingemotion(event, req)
                await guard.listen_livingmemory(event, req)
                await guard.guard_llm_request(event, req)
                await guard.attach_reply_contract(event, req)
                self.assertFalse(event.stopped)
                texts = [p.text for p in req.extra_user_content_parts]
                self.assertEqual(sum(t.startswith(STATE_MARKER) for t in texts),
                                 int(emotion_on))
                self.assertEqual(any(t.startswith("<RAG-Faiss-Memory>") for t in texts),
                                 memory_on)
                names = [t.name for t in req.func_tool.tools]
                self.assertEqual("recall_long_term_memory" in names, memory_on)
                self.assertIn("memorize_long_term_memory", names)
                self.assertEqual(len(shared.tools), 3)
                self.assertIsNone(event.get_extra("_at_guard_decision"))
                memory.search_memories_for_event.assert_not_awaited()
                self.assertEqual(api.get_availability.await_count, int(emotion_on))
                event.result = Chain([Plain("合成最终回复")])
                await guard.recheck_before_send(event)
                await event.send(event.result)
                self.assertEqual(len(event.sent), 1)
                self.assertEqual(event.sent[0].chain[-1].text, "合成最终回复")

    async def test_four_combinations_require_real_memory_for_history_grounding(self):
        for memory_on, emotion_on in ((False, False), (True, False),
                                     (False, True), (True, True)):
            with self.subTest(memory=memory_on, emotion=emotion_on):
                guard = self.guard(coupling_livingmemory_enabled=memory_on,
                                   coupling_livingemotion_enabled=emotion_on)
                guard.context.provider = SequenceProvider(
                    output("silent", 30, False, "context_missing", "memory"),
                    output(score=85))
                api = API()
                memory = SimpleNamespace(
                    initializer=SimpleNamespace(memory_engine=object()),
                    search_memories_for_event=AsyncMock(return_value=[
                        SimpleNamespace(content="昨天的合成任务是整理花园",
                                        metadata={"persona_id": "synthetic"})]))
                registry = {PLUGIN: meta(api),
                    "astrbot_plugin_livingmemory": SimpleNamespace(
                        activated=True, star_cls=memory)}
                guard.context.get_registered_star = registry.get
                event, req = Event(), request()
                event.message_str = "昨天那个任务怎么样"
                event.is_at_or_wake_command = False
                event.set_extra("livingemotion:context", scope(event))
                req.extra_user_content_parts = [SimpleNamespace(text=STATE)]
                utils = SimpleNamespace(get_persona_id=AsyncMock(return_value="synthetic"))
                await guard.listen_livingemotion(event, req)
                with patch("main.importlib.import_module", return_value=utils):
                    await guard.guard_llm_request(event, req)
                self.assertEqual(event.stopped, not memory_on)
                self.assertEqual(len(guard.context.provider.prompts), 2 if memory_on else 1)
                self.assertEqual(memory.search_memories_for_event.await_count, int(memory_on))
                if memory_on:
                    parts = [p.text for p in req.extra_user_content_parts]
                    self.assertTrue(any("整理花园" in p for p in parts))
                if emotion_on:
                    self.assertIn(STATE, [p.text for p in req.extra_user_content_parts])

    async def test_missing_optional_plugins_degrade_without_reading_emotion_or_memory(self):
        guard = self.guard(coupling_livingmemory_enabled=True,
                           coupling_livingemotion_enabled=True)
        guard.context.get_registered_star = lambda _: None
        event, req = Event(group=""), request()
        req.extra_user_content_parts = [SimpleNamespace(text=STATE),
                                      SimpleNamespace(text="<RAG-Faiss-Memory>old")]
        await guard.listen_livingemotion(event, req)
        await guard.listen_livingmemory(event, req)
        await guard.guard_llm_request(event, req)
        await guard.attach_reply_contract(event, req)
        self.assertFalse(event.stopped)
        self.assertFalse(any(p.text.startswith((STATE_MARKER, "<RAG-Faiss-Memory>"))
                             for p in req.extra_user_content_parts))

    async def test_emotion_sleep_blocks_even_private_without_participation(self):
        guard = self.guard(coupling_livingemotion_enabled=True)
        api = API()
        api.get_availability.return_value["block_chat"] = True
        guard.context.get_registered_star = lambda name: meta(api) if name == PLUGIN else None
        event, req = Event(group=""), request()
        event.set_extra("livingemotion:context", scope(event))
        req.extra_user_content_parts = [SimpleNamespace(text=STATE)]
        await guard.listen_livingemotion(event, req)
        await guard.guard_llm_request(event, req)
        self.assertTrue(event.stopped)
        self.assertIsNone(event.get_extra("_at_guard_decision"))


class HistoryAuthorizationAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = Event(group="")
        self.block = ""
        self.policy = SimpleNamespace(revision="v1", black_active=False)
        self.provider = SimpleNamespace(text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text="合成历史图片结果")))
        self.resolve = AsyncMock(return_value=(self.provider, {}))
        self.plugin = SimpleNamespace(
            config={}, _policy=lambda: self.policy,
            _policy_send_reason=lambda _: self.block,
            _understanding=lambda: SimpleNamespace(resolve=self.resolve))
        self.service = ImageHistory(self.plugin)
        self.scope = {
            "event": self.event, "origin": self.event.unified_msg_origin, "cid": "synthetic",
            "revision": "v1", "references": {"hist-img-test": "https://example.invalid/test.png"},
            "used": False,
        }
        self.event.set_extra("_livinghello_history_image_scope", self.scope)

    async def test_authorization_change_while_resolving_prevents_model_call(self):
        async def resolve(*_):
            self.policy.revision = "v2"
            return self.provider, {}
        self.resolve.side_effect = resolve
        with patch("images.guard_image_history.image_target",
                   return_value=SimpleNamespace(provider=self.provider, model="fixture")):
            result = await self.service.read(self.event, self.scope, "hist-img-test")
        self.provider.text_chat.assert_not_awaited()
        self.assertIn("失效", result)

    async def test_authorization_change_while_model_runs_discards_old_result(self):
        for change in ("policy", "recall", "scope"):
            with self.subTest(change=change):
                self.setUp()
                async def respond(**_):
                    if change == "policy":
                        self.policy.revision = "v2"
                    elif change == "recall":
                        self.block = "recalled"
                    else:
                        self.event.set_extra("_livinghello_history_image_scope", {})
                    return SimpleNamespace(completion_text="不得回填的旧图片结果")
                self.provider.text_chat.side_effect = respond
                with patch("images.guard_image_history.image_target",
                           return_value=SimpleNamespace(provider=self.provider, model="fixture")):
                    result = await self.service.read(self.event, self.scope, "hist-img-test")
                self.assertIn("失效", result)
                self.assertNotIn("不得回填", result)
                self.assertTrue(self.scope["used"])

    async def test_valid_authorization_returns_bounded_result_once(self):
        with patch("images.guard_image_history.image_target",
                   return_value=SimpleNamespace(provider=self.provider, model="fixture")):
            result = json.loads(await self.service.read(self.event, self.scope, "hist-img-test"))
        self.assertTrue(result["untrusted"])
        self.assertEqual(result["summary"], "合成历史图片结果")
        self.assertIn("额度", await self.service.read(self.event, self.scope, "hist-img-test"))
        self.provider.text_chat.assert_awaited_once()
