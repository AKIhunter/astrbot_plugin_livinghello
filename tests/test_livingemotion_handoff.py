"""Synthetic request/consumer composition. Never starts a real model or platform."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain
from integrations.guard_livingemotion import PLUGIN, STATE_MARKER, availability_reason, state_lines
from gate.guard_engine import Decision
from test_gate import gate_config, plugin
from test_reply_delivery import Chain, Event

BILI = Path(__file__).resolve().parents[2] / "BiliBot/tests/test_emotion_bridge.py"
STATE = STATE_MARKER + '{"bot":{"labels":["calm"]},"observation":null}'


def scope(event):
    return dict(instance_id="host-test", bot_id="bot-test", persona_id="persona-test",
                adapter_id="test", platform="test", scene_kind="group" if event._group else "private",
                scene_id=event._group or "123", account_id="123", audience=["test"],
                route=event.unified_msg_origin)


class API:
    api_version = 1

    def __init__(self, mode="active", available=True, block_chat=False):
        self.caps = dict(api_version=1, status="ok", mode=mode, instance_id="host-test",
                         operations=["get_availability", "get_bot_state"])
        self.get_capabilities = AsyncMock(side_effect=lambda: dict(self.caps))
        self.get_availability = AsyncMock(return_value=dict(
            api_version=1, status="ok", available=available, block_chat=block_chat))


def meta(api):
    return SimpleNamespace(activated=True, star_cls=SimpleNamespace(api=api))


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        self.registry = {PLUGIN: meta(self.api)}
        self.guard = plugin(gate_config(
            reply_quote_enabled=False, reply_mention_enabled=False, typing_enabled=False,
            coupling_livingemotion_enabled=True))
        self.guard.context.get_registered_star = lambda name: self.registry.get(name)
        self.event = Event()
        self.event.is_at_or_wake_command = False
        self.event.extra["livingemotion:context"] = scope(self.event)
        self.req = SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text=STATE)], image_urls=[])
        self.guard.engine.decide = AsyncMock(return_value=Decision(
            action="reply", reason="synthetic", source="rule", score=99, memory_lines=[STATE]))

    async def asyncTearDown(self):
        await self.guard.terminate()

    async def test_only_new_plugin_state_is_reused_without_copy_or_memory_dependency(self):
        self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [STATE])
        self.assertEqual(len(self.req.extra_user_content_parts), 1)

    async def test_plain_user_namespace_text_is_not_state(self):
        self.req.extra_user_content_parts = [SimpleNamespace(text="user: " + STATE)]
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])
        self.event.message_str = STATE
        self.req.extra_user_content_parts = []
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])

    async def test_state_not_reinjected_in_guard_memory(self):
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertFalse(self.event.stopped)
        joined = "\n".join(part.text for part in self.req.extra_user_content_parts)
        self.assertEqual(joined.count(STATE_MARKER), 1)
        self.assertNotIn("<at_guard_memory", joined)

    async def test_unavailable_blocks_unsolicited_before_judgement(self):
        self.api.get_availability.return_value["available"] = False
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertTrue(self.event.stopped)
        self.guard.engine.decide.assert_not_awaited()
        self.assertEqual(self.event.sent, [])

    async def test_sleep_default_preserves_direct_chat(self):
        self.api.get_availability.return_value["available"] = False
        self.event._mentions = [At("bot")]
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertFalse(self.event.stopped)
        self.guard.engine.decide.assert_not_awaited()

    async def test_block_chat_applies_to_private_filter_off_and_at(self):
        self.api.get_availability.return_value.update(available=False, block_chat=True)
        for event in (self.event, Event(group="")):
            event._mentions = [At("bot")]
            event.extra["livingemotion:context"] = scope(event)
            await self.guard.guard_llm_request(event, self.req)
            self.assertTrue(event.stopped)
        self.guard.engine.decide.assert_not_awaited()

    async def test_trusted_control_marker_preserves_wake_admin_entry(self):
        self.api.get_availability.return_value.update(available=False, block_chat=True)
        for control in ("wake", "admin"):
            self.event.extra["livingemotion:control_request"] = control
            self.assertEqual(await availability_reason(
                self.guard.context, self.event, unsolicited=True), "")

    async def test_local_hard_gate_not_bypassed_by_emotion_availability(self):
        self.event._mentions = [At("another-user")]
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertTrue(self.event.stopped)
        self.guard.engine.decide.assert_not_awaited()

    async def test_availability_rechecked_after_judgement_even_in_shadow(self):
        self.guard.config["gate_mode"] = "shadow"
        async def decide(**_):
            self.api.get_availability.return_value["available"] = False
            return Decision(action="reply", reason="synthetic", source="rule", score=99)
        self.guard.engine.decide.side_effect = decide
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertTrue(self.event.stopped)
        self.assertEqual(self.api.get_availability.await_count, 2)

    async def test_shadow_silence_cannot_continue_after_availability_changes(self):
        self.guard.config["gate_mode"] = "shadow"
        async def decide(**_):
            self.api.get_availability.return_value["available"] = False
            return Decision(action="silence", reason="synthetic", source="rule", score=0)
        self.guard.engine.decide.side_effect = decide
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertTrue(self.event.stopped)
        self.assertEqual(self.api.get_availability.await_count, 2)
        self.assertEqual(self.event.sent, [])

    async def test_provider_failure_malformed_and_missing_context_preserve_local_path(self):
        for result in (RuntimeError("synthetic"), {"available": "false", "block_chat": "true"}):
            self.api.get_availability.side_effect = result if isinstance(result, Exception) else None
            self.api.get_availability.return_value = result
            self.assertEqual(await availability_reason(
                self.guard.context, self.event, unsolicited=True), "")
        self.event.extra.pop("livingemotion:context")
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])

    async def test_scope_mismatch_does_not_query_or_reuse_state(self):
        for key, value in (("account_id", "other"), ("scene_id", "other"),
                           ("route", "other"), ("instance_id", "other")):
            self.event.extra["livingemotion:context"] = {**scope(self.event), key: value}
            self.assertEqual(await availability_reason(
                self.guard.context, self.event, unsolicited=True), "")
            self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])
        self.api.get_availability.assert_not_awaited()

    async def test_late_loading_reload_and_deactivation_use_current_api(self):
        self.registry.clear()
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])
        self.registry[PLUGIN] = meta(self.api)
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [STATE])
        replacement = API(available=False)
        self.registry[PLUGIN] = meta(replacement)
        self.assertEqual(await availability_reason(
            self.guard.context, self.event, unsolicited=True), "livingemotion_unavailable")
        self.api.get_availability.assert_not_awaited()
        self.registry[PLUGIN].activated = False
        self.assertEqual(await availability_reason(
            self.guard.context, self.event, unsolicited=True), "")

    async def test_reload_during_discovery_discards_old_instance(self):
        async def caps():
            self.registry[PLUGIN] = meta(API())
            return dict(self.api.caps)
        self.api.get_capabilities.side_effect = caps
        self.assertEqual(await availability_reason(
            self.guard.context, self.event, unsolicited=True), "")
        self.api.get_availability.assert_not_awaited()

    async def test_reload_or_scope_change_during_availability_discards_old_result(self):
        for change in ("reload", "scope"):
            self.registry[PLUGIN] = meta(self.api)
            self.event.extra["livingemotion:context"] = scope(self.event)
            async def availability(_):
                if change == "reload":
                    self.registry[PLUGIN] = meta(API())
                else:
                    self.event.extra["livingemotion:context"] = {**scope(self.event), "account_id": "other"}
                return dict(api_version=1, status="ok", available=False, block_chat=True)
            self.api.get_availability.side_effect = availability
            self.assertEqual(await availability_reason(
                self.guard.context, self.event, unsolicited=True), "")

    async def test_real_availability_timeout_preserves_local_path(self):
        cancelled = asyncio.Event()
        async def pending(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.api.get_availability.side_effect = pending
        with patch("integrations.guard_livingemotion.API_TIMEOUT", 0.01):
            self.assertEqual(await availability_reason(
                self.guard.context, self.event, unsolicited=True), "")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.event.sent, [])

    async def test_observe_conflict_does_not_fall_back_to_congling(self):
        self.api.caps["mode"] = "observe_only"
        self.registry["astrbot_plugin_congling"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(enabled_for=lambda _: True))
        self.req.extra_user_content_parts = [SimpleNamespace(text="<!--congling:state-->legacy")]
        lines = await self.guard._recall_gate_memory(self.event, self.req)
        self.assertEqual(lines, [])
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req), [])
        self.assertEqual(await availability_reason(
            self.guard.context, self.event, unsolicited=True), "")
        self.api.get_availability.assert_not_awaited()

    @unittest.skipUnless(BILI.is_file(), "requires sibling BiliBot consumer fixture")
    async def test_four_plugin_composition_one_report_state_and_synthetic_reply(self):
        spec = importlib.util.spec_from_file_location("_bili_t8_fixture", BILI)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        shared_api = fixture.PublicAPI()
        self.registry[PLUGIN] = fixture.metadata(fixture.PublicPlugin(shared_api))
        # The memory provider is present but has no published foreground result in this request.
        memory = SimpleNamespace(search_memories_for_event=AsyncMock(return_value=[]))
        self.registry["astrbot_plugin_livingmemory"] = fixture.metadata(memory)
        self.registry["astrbot_plugin_at_guard"] = fixture.metadata(self.guard)
        bili = fixture.consumer(self.registry)
        self.registry["BiliBot"] = fixture.metadata(bili)
        await fixture.bridge._report(
            bili, "123", "unused", 2, "private", source_id="message-1",
            occurred_at=1700000000, bot_account="900",
        )
        shared_api.ingest_event.assert_awaited_once()
        self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [STATE])
        await self.guard.guard_llm_request(self.event, self.req)
        self.assertFalse(self.event.stopped)
        self.event.result = Chain([Plain("synthetic reply")])
        await self.guard.recheck_before_send(self.event)
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)
        self.assertEqual(sum(part.text.count(STATE_MARKER)
                             for part in self.req.extra_user_content_parts), 1)
        memory.search_memories_for_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
