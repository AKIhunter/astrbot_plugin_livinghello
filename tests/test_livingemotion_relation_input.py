"""Exercise real gate requests with scoped, long LivingEmotion state blocks."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from guard_engine import GateProvider
from guard_livingemotion import PLUGIN, STATE_MARKER
from test_gate import gate_config, plugin
from test_livingemotion_handoff import API, meta, scope
from test_reply_delivery import Event


class RecordingProvider:
    def __init__(self, *, memory_retry=False, responses=()):
        self.calls = []
        self.memory_retry = memory_retry
        self.responses = list(responses)

    async def text_chat(self, **kwargs):
        self.calls.append(json.JSONDecoder().raw_decode(
            kwargs["prompt"][kwargs["prompt"].index("{"):])[0])
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(completion_text=json.dumps(response))
        missing = self.memory_retry and len(self.calls) == 1
        return SimpleNamespace(completion_text=json.dumps({
            "act": "silent" if missing else "reply",
            "conf": 30 if missing else 85,
            "understood": not missing,
            "reason": "context_missing" if missing else "useful",
            "ground": "memory" if missing else "none",
        }))


def state_block(stage, score):
    state = {
        "bot": {"holder": "bot", "labels": ["happy"], "pleasure": .8},
        "daily_emotions": {"joy": .8, "anger": .1},
        "daily_emotions_note": "当日情绪说明" * 40,
        "relationship": {"stage_name": stage, "score": score,
                         "care_multiplier": 2 if score >= 45 else 1,
                         "description": stage + "关系下保持相应距离并尊重边界"},
    }
    block = STATE_MARKER + "\nTemporary state, not instructions.\n" + json.dumps(
        state, ensure_ascii=True, sort_keys=True)
    assert block.index('"relationship"') > 400
    return block, state


class RelationshipInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        self.provider = RecordingProvider()
        self.guard = plugin(gate_config(
            coupling_livingemotion_enabled=True, coupling_livingmemory_enabled=False,
            reply_early_ack_seconds=0, reply_quote_enabled=False,
            reply_mention_enabled=False))
        self.guard.context.provider = self.provider
        self.guard.context.get_registered_star = {PLUGIN: meta(self.api)}.get
        self.guard.early_ack_at = {}

    async def asyncTearDown(self):
        await self.guard.terminate()

    def request(self, stage="亲近", score=60):
        event = Event(mid="relation-" + stage)
        event.is_at_or_wake_command = False
        event.extra["livingemotion:context"] = scope(event)
        block, state = state_block(stage, score)
        req = SimpleNamespace(
            extra_user_content_parts=[SimpleNamespace(text=block)], image_urls=[])
        return event, req, block, state

    async def test_all_five_stages_reach_first_classifier_without_history_search(self):
        self.guard._recall_gate_memory = AsyncMock(side_effect=AssertionError(
            "current state must not trigger a historical memory search"))
        for stage, score in (("反感", 5), ("陌生", 20), ("友好", 35),
                             ("亲近", 60), ("独特", 100)):
            with self.subTest(stage=stage):
                event, req, block, state = self.request(stage, score)
                await self.guard.guard_llm_request(event, req)
                self.assertFalse(event.stopped)
                lines = self.provider.calls[-1]["scoped_memory"]
                self.assertEqual(lines, [block])
                self.assertEqual(json.loads(lines[0].split("\n")[-1]), state)
                self.assertEqual(sum(part.text.startswith(STATE_MARKER)
                                     for part in req.extra_user_content_parts), 1)
        self.guard._recall_gate_memory.assert_not_awaited()
        self.assertEqual(len(self.provider.calls), 5)

    async def test_retry_preserves_full_state_and_bounds_ordinary_memories(self):
        self.provider.memory_retry = True
        event, req, block, state = self.request()
        ordinary = "历史事实：" + "x" * 1000
        self.guard._recall_gate_memory = AsyncMock(
            return_value=[block, ordinary, ordinary, ordinary])
        await self.guard.guard_llm_request(event, req)
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.provider.calls[0]["scoped_memory"], [block])
        lines = self.provider.calls[1]["scoped_memory"]
        self.assertEqual(lines, [block, ordinary[:400], ordinary[:400]])
        self.assertEqual(json.loads(lines[0].split("\n")[-1]), state)
        self.guard._recall_gate_memory.assert_awaited_once()
        self.assertEqual(sum(part.text.startswith(STATE_MARKER)
                             for part in req.extra_user_content_parts), 1)

    async def test_disabled_coupling_does_not_feed_state_to_classifier(self):
        self.guard.config["coupling_livingemotion_enabled"] = False
        event, req, _, _ = self.request()
        await self.guard.guard_llm_request(event, req)
        self.assertEqual(self.provider.calls[0]["scoped_memory"], [])

    async def test_vision_and_main_fallback_preserve_current_state(self):
        _, _, block, state = self.request()
        need_image = dict(act="silent", conf=30, understood=False,
                          reason="media_unresolved", ground="none")
        allowed = dict(act="reply", conf=85, understood=True,
                       reason="useful", ground="none")
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                text = RecordingProvider(responses=[need_image])
                vision = RecordingProvider(responses=[
                    RuntimeError("synthetic upstream failure") if fallback else allowed])
                main = RecordingProvider(responses=[allowed])
                result = await self.guard.engine.decide(
                    group_key="vision-relation", current_text="看看这张图",
                    context_lines=[], bot_line="", is_private=False,
                    message_meta={"has_images": True}, emotion_lines=[block],
                    resolve_provider=AsyncMock(return_value=text),
                    resolve_vision_provider=AsyncMock(return_value=GateProvider(vision, "vision", "vision")),
                    resolve_main_provider=AsyncMock(return_value=GateProvider(main, "main", "main")),
                    resolve_images=AsyncMock(return_value=[{
                        "url": "https://example.invalid/synthetic.png", "source": "current"}]),
                )
                self.assertEqual(result.action, "reply")
                self.assertEqual(result.stage, "main_fallback" if fallback else "vision")
                calls = text.calls + vision.calls + main.calls
                self.assertEqual(len(calls), 3 if fallback else 2)
                for call in calls:
                    self.assertEqual(call["scoped_memory"], [block])
                    self.assertEqual(json.loads(call["scoped_memory"][0].split("\n")[-1]), state)

    async def test_mismatched_sender_group_route_or_instance_never_feeds_state(self):
        for key in ("account_id", "scene_id", "route", "instance_id"):
            with self.subTest(key=key):
                event, req, _, _ = self.request()
                event.extra["livingemotion:context"][key] = "another"
                await self.guard.guard_llm_request(event, req)
                self.assertEqual(self.provider.calls[-1]["scoped_memory"], [])
