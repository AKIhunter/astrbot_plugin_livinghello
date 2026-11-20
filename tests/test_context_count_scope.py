"""核对两个配置的实际模型输入范围；虚拟时间、替身Provider，无真实调用。"""
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain
from console import guard_webui as api
from gate.guard_engine import GateEngine
from test_context_policy import output
from test_gate import gate_config, plugin
from test_message_policy_flow import QQEvent


class CaptureProvider:
    def __init__(self, response=None, during_call=None):
        self.response = response or output()
        self.during_call = during_call
        self.payloads = []

    async def text_chat(self, **kwargs):
        data_line = next(line for line in kwargs["prompt"].splitlines() if line.startswith("{"))
        self.payloads.append(json.loads(data_line))
        if self.during_call:
            self.during_call()
        return SimpleNamespace(completion_text=self.response)


class MemoryConfig(dict):
    """只模拟宿主保存回执，不触碰本地或生产配置文件。"""
    def __init__(self, values):
        super().__init__(values)
        self.saved = []
        self.fail = False

    def save_config(self):
        if self.fail:
            raise OSError("fixture-save-failed")
        self.saved.append(dict(self))


class ContextScopeFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 虚拟业务时间不修改事件循环计时，避免超时和慢任务诊断跟着漂移。
        self.loop_clock = patch.object(asyncio.get_running_loop(), "time", time.monotonic)
        self.loop_clock.start()
        self.clock = patch("main.time.monotonic", return_value=1000.0)
        self.clock.start()
        self.guards = []

    async def asyncTearDown(self):
        for guard in self.guards:
            await guard.terminate()
        self.clock.stop()
        self.loop_clock.stop()

    def make_guard(self, provider=None, **config):
        guard = plugin(gate_config(**config), provider or CaptureProvider())
        self.guards.append(guard)
        guard._policy()
        return guard

    def seed(self, guard, event, count=20, text=None):
        key = guard._group_key(event)
        for index in range(count):
            self.add_row(guard, event, 1000 - count + index,
                         text or f"HISTORY_{index:02d}")

    @staticmethod
    def add_row(guard, event, stamp, text, sender=None):
        meta = guard._message_meta(event)
        if sender is not None:
            meta["sender_id"] = sender
        guard.recent_messages[guard._group_key(event)].append(
            (stamp, json.dumps({"text": text, **meta}, ensure_ascii=False)))

    @staticmethod
    def request():
        return SimpleNamespace(extra_user_content_parts=[], image_urls=[],
                               prompt="NATIVE_CURRENT", contexts=["NATIVE_HISTORY"])

    async def run_hook(self, guard, event):
        req = self.request()
        await guard.guard_llm_request(event, req)
        self.assertEqual((req.prompt, req.contexts),
                         ("NATIVE_CURRENT", ["NATIVE_HISTORY"]))
        return req

    def reply_rows(self, req):
        for part in req.extra_user_content_parts:
            if '<at_guard_topic untrusted="true">' in part.text:
                data = part.text.split('<at_guard_topic untrusted="true">\n')[1].split(
                    "\n</at_guard_topic>")[0]
                history, current = data.split("\n当前消息: ", 1)
                rows = history.split("历史（旧→新）:\n", 1)[1].splitlines()
                self.assertTrue(part.no_save)
                return [line for line in rows if line != "（无）"], current
            if '<recent_group_context untrusted="true">' in part.text:
                data = part.text.split('<recent_group_context untrusted="true">\n')[1].split(
                    "\n</recent_group_context>")[0].splitlines()[1:]
                self.assertTrue(part.no_save)
                return data, None
        return [], None

    async def assert_counts(self, gate_count, reply_count, mode="enforce", silent=False):
        provider = CaptureProvider(output("silent", 20, True, "no_value") if silent else output())
        guard = self.make_guard(provider, gate_context_messages=gate_count,
                                participation_context_messages=reply_count, gate_mode=mode)
        event = QQEvent(text="CURRENT_UNIQUE")
        self.seed(guard, event)
        req = await self.run_hook(guard, event)
        self.assertFalse(event.stopped)
        self.assertEqual(len(provider.payloads), 1)
        gate_rows = provider.payloads[0]["recent_messages_old_to_new"]
        rows, current = self.reply_rows(req)
        self.assertEqual((len(gate_rows), len(rows)), (gate_count, reply_count))
        self.assertNotIn("CURRENT_UNIQUE", str(gate_rows) + str(rows))
        self.assertEqual(provider.payloads[0]["current_message"]["text"], "CURRENT_UNIQUE")
        if not silent:
            self.assertIn("CURRENT_UNIQUE", current)
        for selected, count in ((gate_rows, gate_count), (rows, reply_count)):
            for index, row in enumerate(selected):
                self.assertIn(f"HISTORY_{20-count+index:02d}", row)


class ContextCountScopeTests(ContextScopeFixture):
    async def test_enforce_reply_reads_independent_counts_and_current_once(self):
        for counts in ((8, 12), (12, 8), (0, 12)):
            with self.subTest(counts=counts):
                await self.assert_counts(*counts)

    async def test_shadow_reply_reads_independent_counts(self):
        await self.assert_counts(8, 12, mode="shadow")

    async def test_shadow_silence_reads_reply_count_even_when_gate_count_zero(self):
        for counts in ((8, 12), (12, 8), (0, 12)):
            with self.subTest(counts=counts):
                await self.assert_counts(*counts, mode="shadow", silent=True)

    async def test_enforced_silence_does_not_inject_reply_context(self):
        provider = CaptureProvider(output("silent", 20, True, "no_value"))
        guard = self.make_guard(provider, participation_context_messages=12)
        event = QQEvent()
        self.seed(guard, event)
        req = await self.run_hook(guard, event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(provider.payloads[0]["recent_messages_old_to_new"]), 8)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(guard.engine.snapshot()["gen_skipped"], 1)

    async def test_private_with_gate_enabled_uses_its_own_history(self):
        guard = self.make_guard(apply_to_private=True, participation_context_messages=12)
        event = QQEvent(group="")
        self.seed(guard, event)
        self.add_row(guard, QQEvent(group="777"), 990, "OTHER_CONVERSATION")
        req = await self.run_hook(guard, event)
        payload = guard.context.provider.payloads[0]
        rows, current = self.reply_rows(req)
        self.assertFalse(event.stopped)
        self.assertEqual(payload["scene"], "私聊")
        self.assertEqual((len(payload["recent_messages_old_to_new"]), len(rows)), (8, 12))
        self.assertNotIn("OTHER_CONVERSATION", str(payload) + str(rows))
        self.assertIn("QQ私聊", "\n".join(part.text for part in req.extra_user_content_parts))

    async def test_default_private_keeps_native_path(self):
        guard = self.make_guard(participation_context_messages=12)
        event = QQEvent(group="")
        self.seed(guard, event)
        req = await self.run_hook(guard, event)
        self.assertFalse(event.stopped)
        self.assertEqual(guard.context.provider.payloads, [])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_direct_at_wake_and_gray_explicit_keep_native_path(self):
        for kind in ("at", "wake", "gray_at", "gray_name"):
            with self.subTest(kind=kind):
                config = {"message_graylist_qq": ["123"], "bot_names": ["助手"]} if kind.startswith("gray") else {}
                guard = self.make_guard(participation_context_messages=12, **config)
                event = QQEvent(text="助手 帮我" if kind == "gray_name" else "帮我",
                                components=[At(qq="999"), Plain("帮我")] if "at" in kind else None)
                event.is_at_or_wake_command = kind == "wake"
                self.seed(guard, event)
                req = await self.run_hook(guard, event)
                self.assertFalse(event.stopped)
                self.assertEqual(guard.context.provider.payloads, [])
                self.assertEqual(req.extra_user_content_parts, [])

    async def test_policy_and_hard_rule_blocks_do_not_inject_context(self):
        for kind in ("black", "whitelist", "other_at"):
            with self.subTest(kind=kind):
                config = {"message_blacklist_qq": ["123"]} if kind == "black" else (
                    {"message_whitelist_qq": ["789"]} if kind == "whitelist" else {})
                guard = self.make_guard(**config)
                event = QQEvent(components=[At(qq="555"), Plain("帮我")] if kind == "other_at" else None)
                req = await self.run_hook(guard, event)
                self.assertTrue(event.stopped)
                self.assertEqual(guard.context.provider.payloads, [])
                self.assertEqual(req.extra_user_content_parts, [])

    async def test_age_and_blacklist_filters_apply_to_both_inputs(self):
        guard = self.make_guard(gate_context_messages=8, participation_context_messages=12,
                                message_blacklist_qq=["789"], gate_context_max_age_seconds=300)
        event = QQEvent()
        for stamp, body, sender in ((500, "EXPIRED", "123"), (990, "ALLOWED_1", "123"),
                                    (991, "BLACK_BODY", "789"), (992, "ALLOWED_2", "123")):
            self.add_row(guard, event, stamp, body, sender)
        req = await self.run_hook(guard, event)
        gate_rows = guard.context.provider.payloads[0]["recent_messages_old_to_new"]
        reply_rows, _ = self.reply_rows(req)
        self.assertEqual((len(gate_rows), len(reply_rows)), (2, 2))
        self.assertNotIn("EXPIRED", str(gate_rows) + str(reply_rows))
        self.assertNotIn("BLACK_BODY", str(gate_rows) + str(reply_rows))

    async def test_age_zero_preserves_independent_limits(self):
        guard = self.make_guard(gate_context_messages=8, participation_context_messages=12,
                                gate_context_max_age_seconds=0)
        event = QQEvent()
        for index in range(20):
            self.add_row(guard, event, -4000 + index, f"OLD_HISTORY_{index:02d}")
        req = await self.run_hook(guard, event)
        self.assertEqual((len(guard.context.provider.payloads[0]["recent_messages_old_to_new"]),
                          len(self.reply_rows(req)[0])), (8, 12))

    async def test_character_budget_remains_an_upper_limit(self):
        guard = self.make_guard(gate_context_messages=30, participation_context_messages=30)
        event = QQEvent()
        self.seed(guard, event, count=20, text="&" * 400)
        req = await self.run_hook(guard, event)
        for rows in (guard.context.provider.payloads[0]["recent_messages_old_to_new"],
                     self.reply_rows(req)[0]):
            self.assertEqual(sum(map(len, rows)), 2400)
            self.assertLess(len(rows), 30)
            self.assertTrue(all(len(row) <= 610 for row in rows))

    async def test_cache_capacity_includes_current_record(self):
        guard = self.make_guard(gate_context_messages=30, participation_context_messages=30)
        event = QQEvent(text="CURRENT_UNIQUE")
        event.get_sender_name = lambda: ""
        self.seed(guard, event, count=40)
        req = await self.run_hook(guard, event)
        for rows in (guard.context.provider.payloads[0]["recent_messages_old_to_new"],
                     self.reply_rows(req)[0]):
            self.assertEqual(len(rows), 29)
            self.assertIn("HISTORY_11", rows[0])
            self.assertIn("HISTORY_39", rows[-1])
            self.assertNotIn("CURRENT_UNIQUE", str(rows))

    async def test_messages_arriving_during_judgment_do_not_change_reply_snapshot(self):
        for mode, silent in (("enforce", False), ("shadow", True)):
            with self.subTest(mode=mode):
                provider = CaptureProvider(output("silent", 20, True, "no_value") if silent else output())
                guard = self.make_guard(provider, gate_mode=mode, participation_context_messages=12)
                event = QQEvent()
                self.seed(guard, event)
                def incoming():
                    future = QQEvent(text="ARRIVED_DURING_JUDGMENT")
                    guard._record_incoming(future, guard._group_key(future))
                provider.during_call = incoming
                req = await self.run_hook(guard, event)
                rows, _ = self.reply_rows(req)
                self.assertFalse(event.stopped)
                self.assertEqual(len(rows), 12)
                self.assertIn("HISTORY_08", rows[0])
                self.assertNotIn("ARRIVED_DURING_JUDGMENT", str(rows))

    async def test_successful_saves_affect_next_request_independently(self):
        guard = self.make_guard(participation_context_messages=12)
        guard.config = MemoryConfig(guard.config)
        guard.engine.config = guard.config
        for updates, expected in (({"gate_context_messages": 4}, (4, 12)),
                                  ({"participation_context_messages": 6}, (4, 6)),
                                  ({"gate_context_messages": 0}, (0, 6))):
            with self.subTest(updates=updates), patch.object(
                    api.request, "json", AsyncMock(return_value=updates), create=True):
                result = await api.handle_config(guard)
                self.assertEqual(result["data"]["config"], updates)
                self.assertTrue(result["data"]["saved"])
                self.assertEqual(guard.config.saved[-1], dict(guard.config))
                self.assertEqual((guard.engine.context_messages(), guard.engine.reply_context_messages()), expected)
                event = QQEvent()
                guard.recent_messages.clear()
                self.seed(guard, event)
                req = await self.run_hook(guard, event)
                self.assertEqual((len(guard.context.provider.payloads[-1]["recent_messages_old_to_new"]),
                                  len(self.reply_rows(req)[0])), expected)

    async def test_invalid_or_failed_save_keeps_both_previous_values(self):
        guard = self.make_guard(participation_context_messages=12)
        guard.config = MemoryConfig(guard.config)
        guard.engine.config = guard.config
        before = dict(guard.config)
        for updates in ({"gate_context_messages": 4, "participation_context_messages": 0},
                        {"gate_context_messages": 31, "participation_context_messages": 6},
                        {"gate_context_messages": float("nan")},
                        {"participation_context_messages": float("inf")},
                        {"gate_context_messages": "bad"},
                        {"gate_context_messages": 4, "participation_context_messages": 6}):
            guard.config.fail = updates == {"gate_context_messages": 4, "participation_context_messages": 6}
            with self.subTest(updates=updates), patch.object(
                    api.request, "json", AsyncMock(return_value=updates), create=True), \
                    patch.object(api.logger, "exception"), \
                    patch.object(api, "_failure", side_effect=lambda message, status_code=400: {"status_code": status_code}):
                result = await api.handle_config(guard)
                self.assertEqual(result["status_code"], 500 if guard.config.fail else 400)
                self.assertEqual(guard.config, before)
                self.assertEqual(guard.config.saved, [])
                self.assertEqual((guard.engine.context_messages(), guard.engine.reply_context_messages()), (8, 12))


class ContextCountBoundsTests(unittest.TestCase):
    def test_defaults_bounds_and_invalid_runtime_fallbacks(self):
        engine = GateEngine({}, None, None)
        for value, expected in ((None, (8, 12)), ("", (8, 12)), ("bad", (8, 12)),
                                (float("nan"), (8, 12)), (-1, (0, 2)), (31, (30, 30)),
                                ("6", (6, 6)), (8, (8, 8))):
            with self.subTest(value=value):
                engine.config = {"gate_context_messages": value, "participation_context_messages": value}
                self.assertEqual((engine.context_messages(), engine.reply_context_messages()), expected)
        engine.config = {}
        self.assertEqual((engine.context_messages(), engine.reply_context_messages()), (8, 12))

    def test_schema_backend_and_page_ranges_agree(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text())
        page = (root / "pages/guard/index.html").read_text()
        for key, low, default in (("gate_context_messages", 0, 8),
                                  ("participation_context_messages", 2, 12)):
            with self.subTest(key=key):
                self.assertEqual(schema[key]["default"], default)
                self.assertEqual(schema[key]["slider"], {"min": low, "max": 30, "step": 1})
                self.assertEqual(api.NUMERIC_FIELDS[key], ("int", low, 30))
                self.assertIn(f'key: "{key}", label:', page)
                definition = next(line for line in page.splitlines() if f'key: "{key}", label:' in line)
                self.assertIn(f'min: {low}, max: 30, step: 1', definition)
                for value in (low, default, 30):
                    self.assertEqual(api._coerce(key, value), value)
