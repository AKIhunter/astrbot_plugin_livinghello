"""LivingEmotion 准备耗时与 LH 发送保护；不用真实模型或群消息。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.message_components import Plain
from console.guard_webui import EDITABLE, _coerce, _load_schema_subset, _state
from test_gate import GateEvent, gate_config, plugin


class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.guard = plugin(gate_config(gate_reply_max_age_seconds=30))
        self.event = GateEvent("这梗编得挺完整")
        self.event.set_extra("_at_guard_recorded_at", 100)
        self.event.set_extra("_at_guard_judged_revision", 0)
        self.event.set_extra("_at_guard_proactive", True)
        self.event.set_extra("_at_guard_group_key", "1001")

    def reason(self, age=60):
        with patch("main.time.monotonic", return_value=100 + age):
            return self.guard._stale_reason(self.event, "1001")

    def test_preparation_allows_reply_but_later_generation_expires(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(60), "")
        self.assertEqual(self.reason(80), "message_expired")

    def test_exact_deadline_and_submillisecond_overrun(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(75), "")
        self.assertEqual(self.reason(75.00001), "message_expired")

    def test_disabling_catchup_accounting_uses_original_age(self):
        self.guard.config["integrate_livingemotion_catchup"] = False
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "message_expired")

    def test_invalid_preparation_never_extends_deadline(self):
        for value in (None, "", 0, -1, True, False, "bad", [], {},
                      float("nan"), float("inf"), -float("inf"), 61, 10 ** 1000):
            with self.subTest(value=repr(value)[:30]):
                self.event.set_extra("livingemotion:wake_prepare_seconds", value)
                self.assertEqual(self.reason(), "message_expired")

    def test_missing_preparation_preserves_original_rule(self):
        self.assertEqual(self.reason(), "message_expired")

    def test_numeric_string_is_accepted(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", "45.0")
        self.assertEqual(self.reason(), "")

    def test_preparation_is_isolated_per_event(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "")
        self.event.extra.pop("livingemotion:wake_prepare_seconds")
        self.assertEqual(self.reason(), "message_expired")

    def test_missing_recorded_timestamp_does_not_create_timeout(self):
        self.event.extra.pop("_at_guard_recorded_at")
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "")
        self.guard.context_revision["1001"] = 1
        # 新设计：判定后群友继续说话只刷新上下文，不撤销已生成的回复。
        self.assertEqual(self.reason(), "")
        self.assertTrue(self.guard._context_advanced(self.event, "1001"))

    def test_new_message_marks_context_advanced_without_revoking_prepared_reply(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        self.guard.context_revision["1001"] = 1
        self.assertEqual(self.reason(), "")
        self.assertTrue(self.guard._context_advanced(self.event, "1001"))

    def test_generation_hook_keeps_generated_group_reply_but_blocks_expiry(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        for age, revision, expected in ((80, 0, "message_expired"), (60, 1, None)):
            with self.subTest(expected=expected):
                self.event.extra.pop("_at_guard_delivery_blocked", None)
                self.guard.context_revision["1001"] = revision
                response = SimpleNamespace(completion_text="旧回复", result_chain=None,
                                           tools_call_args=[], tools_call_name=[])
                with patch("main.time.monotonic", return_value=100 + age):
                    asyncio.run(self.guard.suppress_no_reply_and_track(self.event, response))
                self.assertEqual(response.completion_text, "" if expected else "旧回复")
                self.assertEqual(self.event.get_extra("_at_guard_delivery_blocked"), expected)

    def test_final_send_recheck_keeps_prepared_reply_when_new_context_arrives(self):
        self.event.set_extra("livingemotion:wake_prepare_seconds", 45)
        result = SimpleNamespace(chain=[Plain("当前答复")])
        self.event.get_result = lambda: result
        with patch("main.time.monotonic", return_value=160):
            asyncio.run(self.guard.recheck_before_send(self.event))
            self.assertTrue(result.chain)
            self.guard.context_revision["1001"] = 1
            asyncio.run(self.guard.recheck_before_send(self.event))
        # 新设计：复核时群友插话只打标记，不清空 chain、不停止事件。
        self.assertTrue(result.chain)
        self.assertFalse(self.event.stopped)
        self.assertTrue(self.event.get_extra("_at_guard_context_advanced"))


class PreparationSettingsTest(unittest.TestCase):
    def setUp(self):
        self.guard = plugin(gate_config())

    def test_schema_and_editable_fields_include_switch(self):
        key = "integrate_livingemotion_catchup"
        self.assertIn(key, EDITABLE)
        self.assertIs(_load_schema_subset()[key]["default"], True)
        self.assertIs(_coerce(key, "false"), False)
        self.assertIs(_coerce(key, False), False)
        self.assertIs(_coerce(key, "true"), True)

    def test_console_state_exposes_saved_switch(self):
        self.guard.context = SimpleNamespace(get_registered_star=lambda _: None,
                                             get_all_providers=lambda: [])
        self.guard.config["integrate_livingemotion_catchup"] = False
        state = asyncio.run(_state(self.guard))
        self.assertFalse(state["config"]["integrate_livingemotion_catchup"])


if __name__ == "__main__":
    unittest.main()
