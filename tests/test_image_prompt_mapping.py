"""局部映射、热更新、模型隔离、成功学习及请求前熔断；无收费请求。"""
import copy
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from images.guard_image_prompt import ImagePromptMapping, PromptReviewError, substitutions
from images.guard_image_settings import DEFAULTS, ImageSettings
from reply.guard_native_reply import NativeReplyError
from test_image_generation import Event


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 宿主的 astrbot.api.logger 不是 logging.Logger，assertLogs 只认后者；
        # 这里显式传标准库 logger，断言内容与生产无关。
        self.logger = logging.getLogger("lh-image-prompt-mapping-test")
        self.mapping = ImagePromptMapping(self.root, logger=self.logger)
        self.values = {**DEFAULTS, "provider_id": "image-native"}
        self.connection = ("https://example.invalid/v1", "never-persist-this-key", "image-one")

    def apply(self, prompt, **kwargs):
        return self.mapping.apply(self.values, self.connection, prompt, **kwargs)

    def table(self, rules, **changes):
        data = {"version": 1, "rules": rules, **changes}
        (self.root / "image_prompt_rules.json").write_text(json.dumps(data))

    def repair(self, source="blouse", target="shirt", prompt="blue blouse"):
        return self.mapping.prepare_repair(self.values, self.connection, prompt, {
            "changes_scene": False, "substitutions": [{"from": source, "to": target}]})

    def test_missing_table_uses_builtin_and_preserves_original(self):
        result = self.apply("blue neckline, garden")
        self.assertEqual(result["original"], "blue neckline, garden")
        self.assertEqual(result["prompt"], "blue collar, garden")
        self.assertTrue(self.mapping.warning)

    def test_tokens_have_word_boundaries_and_ignore_case(self):
        self.assertEqual(self.apply("NECKLINE necklineish")["prompt"], "collar necklineish")

    def test_token_array_is_literal_longest_first_and_does_not_cascade(self):
        self.table([
            {"id": "aliases", "type": "token", "from": ["cat", "orange cat", "c.t"], "to": "kitty"},
            {"id": "next", "type": "token", "from": "kitty", "to": "feline"},
        ])
        self.assertEqual(self.apply("ORANGE CAT catfish c.t kitty")["prompt"],
                         "kitty catfish kitty feline")
        self.assertEqual(self.mapping.rules(self.values)["rules"][0]["from"],
                         ["cat", "orange cat", "c.t"])

    def test_token_array_condition_and_scene_check(self):
        self.table([{"id": "shirt", "type": "token", "from": ["blouse", "top"],
                     "to": "shirt", "when": "garden"}])
        self.assertEqual(self.apply("blue blouse")["prompt"], "blue blouse")
        self.assertEqual(self.apply("blue blouse in garden")["prompt"], "blue shirt in garden")
        self.table([{"id": "pose", "type": "token", "from": ["standing", "kneeling"],
                     "to": "sitting"}])
        with self.assertRaises(PromptReviewError):
            self.apply("standing cat")

    def test_invalid_token_arrays_fail_whole_table(self):
        for source in ([], ["cat", None], ["cat", "CAT"], [" "], ["cat"] * 21,
                       {"cat": "kitty"}, ["x" * 101]):
            with self.subTest(source=source), self.assertRaises(ValueError):
                ImagePromptMapping.validate_table({"version": 1, "rules": [
                    {"id": "bad", "type": "token", "from": source, "to": "kitty"}]})

    def test_regex_keeps_next_clause(self):
        self.table([{"id": "short", "type": "regex", "pattern": r"extra detail[^,.;\n]*", "to": "detail"}])
        self.assertEqual(self.apply("extra detail many words, garden; cat")["prompt"], "detail, garden; cat")

    def test_injection_is_idempotent(self):
        self.table([{"id": "body", "type": "inject_if_missing", "pattern": "完整身体", "to": "完整身体"}])
        first = self.apply("猫")["prompt"]
        self.assertEqual(self.apply(first)["prompt"], first)
        self.assertEqual(first.count("完整身体"), 1)

    def test_blocklist_does_not_delete_or_charge(self):
        self.table([], blocklist=["forbidden"])
        with self.assertRaises(PromptReviewError) as error:
            self.apply("cat forbidden garden")
        self.assertEqual(error.exception.kind, "prompt_adjustment_required")

    def test_pose_rule_waits_for_confirmation(self):
        self.table([{"id": "pose", "type": "token", "from": "symmetrical", "to": "asymmetrical",
                     "requires_confirmation": True, "notice": "建议换成非对称站姿，需要确认。"}])
        with self.assertRaisesRegex(PromptReviewError, "调整") as error:
            self.apply("symmetrical standing cat")
        self.assertIn("非对称", error.exception.notice)
        self.table([{"id": "pose", "type": "token", "from": "symmetrical", "to": "asymmetrical"}])
        with self.assertRaises(PromptReviewError):
            self.apply("symmetrical standing cat")

    def test_advisory_preserves_scene(self):
        self.table([{"id": "pose", "type": "advisory", "pattern": "对称站姿", "notice": "站姿需要确认。"}])
        with self.assertRaises(PromptReviewError):
            self.apply("对称站姿")

    def test_hot_reload_and_corrupt_file_keep_good_rules(self):
        self.table([{"id": "one", "type": "token", "from": "cat", "to": "kitty"}])
        self.assertEqual(self.apply("cat")["prompt"], "kitty")
        (self.root / "image_prompt_rules.json").write_text("{broken")
        self.assertEqual(self.apply("cat")["prompt"], "kitty")
        self.assertTrue(self.mapping.warning)
        self.table([{"id": "two", "type": "token", "from": "cat", "to": "feline"}])
        self.assertEqual(self.apply("cat")["prompt"], "feline")

    def test_unsafe_regex_is_rejected(self):
        for regex in ("(a+)+$", r"a+a+a+$", r"(foo)\1"):
            with self.subTest(regex=regex), self.assertRaises(ValueError):
                ImagePromptMapping.validate_table({"version": 1, "rules": [
                    {"id": "bad", "type": "regex", "pattern": regex, "to": "cat"}]})

    def test_length_limit_is_explicit_and_can_be_disabled(self):
        with self.assertRaises(PromptReviewError) as error:
            self.apply("猫" * 501)
        self.assertEqual(error.exception.kind, "prompt_too_long")
        self.values["prompt_guard_enabled"] = False
        self.assertEqual(self.apply("猫" * 4000)["prompt"], "猫" * 4000)

    def test_proposals_do_not_learn_before_success(self):
        recipe = self.repair()
        self.assertEqual(recipe["status"], "ready")
        result = self.apply("blue blouse", repair=recipe)
        self.assertEqual(result["prompt"], "blue shirt")
        self.assertEqual(self.mapping.learned(result["scope"]), [])

    def test_success_is_persistent_idempotent_and_model_scoped(self):
        result = self.apply("blue blouse", repair=self.repair())
        self.mapping.observe("r1", result, "success")
        self.mapping.observe("r1", result, "success")
        restored = ImagePromptMapping(self.root)
        self.assertEqual(restored.learned(result["scope"])[0]["successes"], 1)
        self.assertEqual(restored.apply(self.values, self.connection, "red blouse")["prompt"], "red shirt")
        other = (self.connection[0], self.connection[1], "image-two")
        self.assertEqual(restored.apply(self.values, other, "red blouse")["prompt"], "red blouse")
        another_api = ("https://other.invalid/v1", self.connection[1], "image-one")
        self.assertEqual(restored.apply(self.values, another_api, "red blouse")["prompt"], "red blouse")
        self.assertNotIn(self.connection[1].encode(), self.mapping.ledger.read_bytes())

    def test_rejection_retires_failed_learned_pair(self):
        first = self.apply("blue blouse", repair=self.repair())
        self.mapping.observe("ok", first, "success")
        second = self.apply("red blouse")
        self.mapping.observe("bad", second, "content_rejected")
        self.assertEqual(self.apply("red blouse")["prompt"], "red blouse")

    def test_failure_timeout_and_delivery_do_not_learn(self):
        result = self.apply("blue blouse", repair=self.repair())
        for status in ("content_rejected", "unknown", "timeout", "delivery_unknown"):
            self.mapping.observe(status, result, status)
        self.assertEqual(self.mapping.learned(result["scope"]), [])

    def test_model_change_rejects_prepared_candidate(self):
        recipe = self.repair()
        self.connection = (self.connection[0], self.connection[1], "image-two")
        with self.assertRaises(PromptReviewError):
            self.apply("blue blouse", repair=recipe)

    def test_meaning_change_deletion_and_malformed_proposals_are_not_auto_applied(self):
        proposals = [
            {"changes_scene": True, "substitutions": []},
            {"changes_scene": False, "substitutions": [{"from": "blouse", "to": ""}]},
            {"changes_scene": False, "substitutions": [{"from": "standing", "to": "sitting"}]},
            {"changes_scene": False, "substitutions": [{"from": "blouse", "to": "naked"}]},
        ]
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                result = self.mapping.prepare_repair(self.values, self.connection,
                                                     "standing blue blouse", proposal)
                self.assertNotEqual(result["status"], "ready")

    def test_shortening_has_no_reusable_rule(self):
        recipe = self.mapping.prepare_repair(self.values, self.connection, "猫" * 501,
                                            {"changes_scene": False, "shortened_prompt": "猫"})
        self.assertEqual((recipe["prompt"], recipe["candidates"]), ("猫", []))

    def test_simultaneous_replacements_do_not_cascade(self):
        self.assertEqual(substitutions("cat kitty", [{"from": "cat", "to": "kitty"},
                                                     {"from": "kitty", "to": "feline"}]), "kitty feline")

    def test_manual_and_repair_use_the_same_scene_decision(self):
        cases = [
            ("lying on her back", "lying face up", True),
            ("nude", "beige", True, "a nude color dress"),
            ("sad", "unhappy", True),
            ("smiling", "smile", True),
            ("kneeling", "reclining", False),
            ("kneel", "recline", False),
            ("lying on her back", "lying face down", False),
            ("standing", "sitting", False),
            ("18", "19", False, "an 18 year old adult"),
            ("abdomen", "navel", False),
            ("nude", "beige", False, "a nude figure"),
        ]
        for case in cases:
            source, target, allowed, *context = case
            prompt = context[0] if context else source
            with self.subTest(source=source, target=target, prompt=prompt):
                self.table([{"id": "test", "type": "token", "from": source, "to": target}])
                if allowed:
                    self.assertEqual(self.apply(prompt)["prompt"], prompt.replace(source, target))
                else:
                    with self.assertRaises(PromptReviewError):
                        self.apply(prompt)
                repair = self.repair(source, target, prompt)
                self.assertEqual(repair["status"], "ready" if allowed else "review")
                if allowed:
                    self.assertEqual(self.apply(prompt, repair=repair)["prompt"], prompt.replace(source, target))

    def test_shortening_cannot_drop_protected_scene_details(self):
        for detail in ("standing", "kneeling", "18 year old", "not nude"):
            with self.subTest(detail=detail):
                prompt = detail + " 猫" * 260
                repair = self.mapping.prepare_repair(self.values, self.connection, prompt,
                    {"changes_scene": False, "shortened_prompt": "cat"})
                self.assertEqual(repair["status"], "review")

    def test_legacy_learned_pose_change_is_not_reused_or_deleted(self):
        scope = self.mapping.scope(self.values, self.connection)
        # 模拟升级前已经写入的成功词对，检查升级后真实 SQLite 复用路径。
        db = self.mapping.connect()
        with db:
            db.execute("INSERT INTO mappings VALUES (?,?,?,?,?,?)",
                       (scope["key"], "kneeling", "reclining", 1, 0, 1))
        db.close()
        self.assertEqual(self.apply("kneeling")["prompt"], "kneeling")
        self.assertEqual(len(self.mapping.learned(scope)), 1)

    def test_injection_cannot_add_a_pose_without_confirmation(self):
        self.table([{"id": "pose", "type": "inject_if_missing", "pattern": "standing", "to": "standing"}])
        with self.assertRaises(PromptReviewError):
            self.apply("cat")

    def test_load_status_reports_real_source_and_effective_count(self):
        self.table([{"id": "one", "type": "token", "from": "cat", "to": "kitty"},
                    {"id": "two", "type": "token", "from": "blouse", "to": "shirt"}])
        with self.assertLogs(self.logger, level="INFO") as logs:
            policy = self.mapping.policy(self.values, self.connection)
            self.mapping.policy(self.values, self.connection)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((policy["load_status"], policy["rules_source"], policy["rule_count"]),
                         ("loaded", "file", 2))
        self.assertEqual(policy["rules_file"], "image_prompt_rules.json")
        (self.root / "image_prompt_rules.json").write_text("{broken")
        with self.assertLogs(self.logger, level="WARNING") as logs:
            policy = self.mapping.policy(self.values, self.connection)
            self.mapping.policy(self.values, self.connection)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((policy["load_status"], policy["rules_source"], policy["rule_count"]),
                         ("invalid", "previous", 2))
        self.assertIn("JSON语法错误", policy["warning"])
        self.values["prompt_guard_enabled"] = False
        self.assertEqual(self.mapping.policy(self.values, self.connection)["rule_count"], 0)

    def test_invalid_first_table_falls_back_with_safe_rule_diagnostic(self):
        private = "sk-fixture-do-not-display"
        self.table([{"id": "bad", "type": "regex", "pattern": "(" + private + ")", "to": "cat"}])
        with self.assertLogs(self.logger, level="WARNING") as logs:
            policy = self.mapping.policy(self.values, self.connection)
        self.assertEqual((policy["rules_source"], policy["rule_count"]), ("builtin", 1))
        self.assertIn("第1条规则", policy["warning"])
        self.assertNotIn(private, policy["warning"])
        self.assertNotIn(private, "".join(logs.output))

    def test_malformed_limits_and_encoding_do_not_break_reload(self):
        for limits in (None, [], "wrong", True):
            with self.subTest(limits=limits):
                self.table([], limits=limits)
                self.assertEqual(self.mapping.policy(self.values, self.connection)["load_status"], "invalid")
        for kind in ([], {}, None):
            with self.subTest(kind=kind):
                self.table([{"id": "bad", "type": kind}])
                policy = self.mapping.policy(self.values, self.connection)
                self.assertEqual(policy["load_status"], "invalid")
                self.assertIn("第1条规则", policy["warning"])
        (self.root / "image_prompt_rules.json").write_bytes(b"\xff\xfe")
        policy = self.mapping.policy(self.values, self.connection)
        self.assertEqual(policy["load_status"], "invalid")
        self.assertIn("UTF-8", policy["warning"])

    def test_missing_file_warns_once_and_recovery_switches_to_current_table(self):
        with self.assertLogs(self.logger, level="WARNING") as logs:
            self.mapping.policy(self.values, self.connection)
            policy = self.mapping.policy(self.values, self.connection)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((policy["load_status"], policy["rules_source"], policy["rule_count"]),
                         ("missing", "builtin", 1))
        self.table([])
        policy = self.mapping.policy(self.values, self.connection)
        self.assertEqual((policy["load_status"], policy["rules_source"], policy["rule_count"]),
                         ("loaded", "file", 0))
        self.assertEqual(policy["warning"], "")


class MappingGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        native = SimpleNamespace(provider_config={"id": "image", "type": "openai",
            "api_base": "https://example.invalid/v1", "key": "offline-only"}, get_model=lambda: "image-one")
        plugin = SimpleNamespace(context=SimpleNamespace(get_all_providers=lambda: [native]),
            data_dir=self.root, logger=logging.getLogger("image-prompt-test"),
            _delivery=lambda: SimpleNamespace(recalled=lambda _: False),
            _judged_event=lambda _: False, _gate_mode=lambda: "enforce")
        self.service = ImageSettings(plugin)
        self.service.values.update(enabled=True, provider_id="image")
        output = self.root / "cat.png"
        output.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.service._request = AsyncMock(return_value=(output, ""))

    async def test_long_description_is_rejected_before_quota(self):
        event = Event()
        await self.service.generate(event, "猫" * 501, _execute=True)
        self.assertEqual(event.get_extra("_livinghello_image_failure")["kind"], "prompt_too_long")
        self.assertEqual(self.service.used(), 0)
        self.service._request.assert_not_awaited()

    async def test_repaired_generation_records_only_valid_success(self):
        recipe = self.service.prepare_prompt_repair("blue blouse",
            {"changes_scene": False, "substitutions": [{"from": "blouse", "to": "shirt"}]},
            self.service.prompt_policy()["scope"])
        event = Event()
        event.set_extra("_livinghello_image_prompt_repair", recipe)
        await self.service.generate(event, "blue blouse", _execute=True)
        self.assertEqual(self.service.prompt_policy()["learned_count"], 1)
        pending = event.get_extra("_livinghello_pending_image")
        self.assertEqual((pending["original_prompt"], pending["effective_prompt"]), ("blue blouse", "blue shirt"))
        self.assertEqual(self.service.used(), 1)

    async def test_style_total_limit_keeps_entire_description(self):
        self.service.values["style_prompt"] = "风" * 1000
        event = Event()
        await self.service.generate(event, "猫" * 500, _execute=True)
        guarded = event.get_extra("_livinghello_prompt_guard")
        self.assertEqual(len(guarded["styled_prompt"]), 900)
        self.assertTrue(guarded["styled_prompt"].startswith("猫" * 500 + "\n"))

    async def test_trace_lengths_use_actual_style_after_truncation(self):
        self.service.values["style_prompt"] = "风" * 1000
        event = Event()
        with patch("images.guard_image_settings.emit") as trace:
            await self.service.generate(event, "猫" * 500, _execute=True)
        entries = [c.kwargs for c in trace.call_args_list
                   if len(c.args) > 1 and c.args[1] == "prompt_guard"]
        self.assertEqual(len(entries), 1)
        self.assertEqual({k: entries[0][k] for k in ("desc_len", "style_len", "time_len", "total_len")},
                         {"desc_len": 500, "style_len": 399, "time_len": 0, "total_len": 900})
        self.assertNotIn("prompt", entries[0])
        self.assertEqual(self.service.used(), 1)

    async def test_time_overhead_reduces_shortening_budget_before_quota(self):
        self.service.values["follow_server_time"] = True
        (self.root / "image_prompt_rules.json").write_text(json.dumps({
            "version": 1, "rules": [],
            "limits": {"desc_soft": 100, "desc_hard": 100, "total_hard": 100}}))
        policy = self.service.prompt_policy()
        budget = 100 - len(self.service.timed_prompt("", True))
        self.assertEqual(policy["limits"]["desc_hard"], budget)
        event = Event()
        await self.service.generate(event, "猫" * 100, _execute=True)
        self.assertEqual(event.get_extra("_livinghello_image_failure")["kind"], "prompt_too_long")
        self.assertEqual(self.service.used(), 0)
        recipe = self.service.prepare_prompt_repair("猫" * 100,
            {"changes_scene": False, "shortened_prompt": "猫" * budget}, policy["scope"])
        self.assertEqual(recipe["status"], "ready")
        retry = Event()
        retry.set_extra("_livinghello_image_prompt_repair", recipe)
        await self.service.generate(retry, "猫" * 100, _execute=True)
        self.assertEqual(len(retry.get_extra("_livinghello_prompt_guard")["styled_prompt"]), 100)
        self.assertEqual(self.service.used(), 1)

    async def test_retry_config_has_same_save_and_load_limits(self):
        for value in (-1, 11, True, 1.5):
            with self.subTest(value=value), self.assertRaises(NativeReplyError):
                ImageSettings._validate({**DEFAULTS, "prompt_guard_retry_limit": value})
        for value in (0, 3, 10):
            ImageSettings._validate({**DEFAULTS, "prompt_guard_retry_limit": value})
            self.service.values["prompt_guard_retry_limit"] = value
            self.assertEqual(self.service.prompt_mapping.policy(
                self.service.values, self.service.connection(self.service.values))["retry_limit"], value)
        old = {k: v for k, v in DEFAULTS.items() if not k.startswith("prompt_guard_")}
        self.service.path.write_text(json.dumps(old))
        self.assertTrue(ImageSettings._load(self.service.path)["prompt_guard_enabled"])

    async def test_ready_le_notice_defers_adjustment_question_until_retry_exhaustion(self):
        event = Event()
        job = {"prompt": "cat", "use_reference": False, "emotion": None}
        failure = self.service._image_failure(event, job, "content_rejected")
        with patch("images.guard_image_settings.image_prompt_retry_ready", AsyncMock(return_value=True)), \
                patch("images.guard_image_settings.image_followup", AsyncMock(return_value={"task_status": "scheduled"})):
            await self.service._notify_image_failure(event, job, failure)
        self.assertFalse(failure["adjustment_asked"])
        self.assertEqual(job["followup"]["task_status"], "scheduled")
        self.assertEqual(event.send.await_count, 1)
