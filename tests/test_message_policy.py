"""名单规范化、决策矩阵和配置事务；只使用合成合法 QQ 标识。"""
import asyncio
import itertools
import unittest
from unittest.mock import patch

from gate.guard_message_policy import FIELDS, MessagePolicy, validate_policy_config
from console import guard_webui
from test_gate import gate_config, plugin


class PolicyTest(unittest.TestCase):
    def test_normalize_separators_precision_and_dedup(self):
        result = validate_policy_config({"message_blacklist_qq": "００１２３，123、456;７８９\n12345678901234567890"})
        self.assertTrue(result["valid"])
        self.assertEqual(result["normalized_values"]["message_blacklist_qq"],
                         ["123", "456", "789", "12345678901234567890"])
        same = validate_policy_config({"message_blacklist_qq": ["123", 456, "789", "12345678901234567890"]})
        self.assertEqual(result["revision"], same["revision"])

    def test_reject_all_invalid_types_and_limits(self):
        for bad in (True, 1.0, -1, 0, "000", "1e4", "1.5", "张三", "@全体", "https://qq.com", {}, [[]], "1"*21):
            with self.subTest(bad=bad):
                self.assertFalse(validate_policy_config({"message_blacklist_qq": [bad]})["valid"])
        self.assertFalse(validate_policy_config({"message_blacklist_qq": list(range(1, 502))})["valid"])
        self.assertFalse(validate_policy_config({"message_blacklist_qq": " "*131073})["valid"])

    def test_all_same_type_pairs_conflict_but_cross_type_allowed(self):
        for kind in ("qq", "groups"):
            for a, b in itertools.combinations(("black", "gray", "white"), 2):
                result = validate_policy_config({f"message_{a}list_{kind}": ["123"],
                                                 f"message_{b}list_{kind}": ["00123"]})
                self.assertEqual({e["field"] for e in result["errors"]},
                                 {f"message_{a}list_{kind}", f"message_{b}list_{kind}"})
        self.assertTrue(validate_policy_config({"message_blacklist_qq": ["123"], "message_whitelist_groups": ["123"]})["valid"])

    def test_matrix(self):
        cases = [
            ({}, True, True, False),
            ({"message_blacklist_qq": ["123"], "message_whitelist_groups": ["456"]}, False, False, False),
            ({"message_blacklist_groups": ["456"], "message_whitelist_qq": ["123"]}, False, False, False),
            ({"message_graylist_qq": ["123"]}, True, False, True),
            ({"message_graylist_groups": ["456"]}, True, False, True),
            ({"message_whitelist_qq": ["789"]}, True, False, False),
            ({"message_whitelist_groups": ["456"]}, True, True, False),
            ({"message_whitelist_qq": ["123"]}, True, True, False),
            ({"message_graylist_qq": ["123"], "message_whitelist_groups": ["456"]}, True, False, True),
            ({"message_graylist_groups": ["456"], "message_whitelist_qq": ["123"]}, True, False, True),
            ({"message_graylist_qq": ["123"], "message_whitelist_groups": ["789"]}, True, False, False),
        ]
        for config, read, reply, wakes in cases:
            for explicit in (False, True):
                with self.subTest(config=config, explicit=explicit):
                    result = MessagePolicy(config).check(platform="aiocqhttp", session_kind="group",
                        sender_id="123", group_id="456", explicit_call=explicit)
                    self.assertEqual(result["read_allowed"], read)
                    self.assertEqual(result["reply_allowed"], reply or wakes and explicit)
                    other = MessagePolicy(config).check(platform="telegram", session_kind="group", sender_id="123", group_id="456")
                    self.assertTrue(other["reply_allowed"])

    def test_private_and_unknown_identity(self):
        policy = MessagePolicy({"message_graylist_qq": ["123"]})
        self.assertFalse(policy.check(platform="qq", session_kind="private", sender_id="123")["reply_allowed"])
        self.assertTrue(policy.check(platform="qq", session_kind="private", sender_id="123", explicit_call=True)["reply_allowed"])
        policy = MessagePolicy({"message_blacklist_qq": ["123"]})
        self.assertFalse(policy.check(platform="qq", session_kind="group", group_id="456")["read_allowed"])
        self.assertFalse(policy.check(platform="", session_kind="private", sender_id="456")["read_allowed"])

    def test_invalid_cold_start_and_old_snapshot_protection(self):
        policy = MessagePolicy({"message_blacklist_qq": ["坏号"]})
        self.assertFalse(policy.healthy)
        self.assertFalse(policy.check(platform="qq", session_kind="private", sender_id="456")["read_allowed"])
        policy = MessagePolicy({"message_blacklist_qq": ["123"]})
        revision = policy.revision
        policy.refresh({"message_blacklist_qq": ["坏号"]})
        self.assertFalse(policy.healthy)
        self.assertEqual(policy.revision, revision)
        self.assertFalse(policy.check(platform="qq", session_kind="private", sender_id="123")["read_allowed"])
        with self.assertRaises(TypeError):
            policy.values["message_blacklist_qq"] = ()


class SavePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Config(dict):
            fail = False
            def save_config(self):
                if self.fail:
                    raise OSError("isolated disk failure")
        self.guard = plugin(gate_config(message_blacklist_qq=["123"]))
        self.guard.config = Config(self.guard.config)
        self.guard.engine.config = self.guard.config
        self.revision = self.guard.get_message_policy_snapshot()["revision"]
        self.providers = patch.object(guard_webui, "_providers", return_value=[])
        self.providers.start()
        self.addCleanup(self.providers.stop)
        # 保留 HTTP 状态及字段错误供断言。
        self.errors = patch.object(guard_webui, "error_response",
            side_effect=lambda message, status_code=400, data=None: {"status":"error", "code":status_code, "message":message, "data":data})
        self.errors.start()
        self.addCleanup(self.errors.stop)

    async def save(self, values):
        async def body(**_):
            return values
        with patch.object(guard_webui.request, "json", body, create=True):
            return await guard_webui.handle_config(self.guard)

    async def test_merge_conflict_rejected_then_atomic_move(self):
        before = dict(self.guard.config)
        result = await self.save({"message_graylist_qq": ["123"], "message_policy_expected_revision":self.revision})
        self.assertEqual(result["status"], "error")
        self.assertEqual(dict(self.guard.config), before)
        result = await self.save({"message_blacklist_qq": [], "message_graylist_qq":["123"],
                                  "message_policy_expected_revision":self.revision})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.guard.config["message_blacklist_qq"], [])

    async def test_revision_collision_and_disk_failure_restore_all(self):
        result = await self.save({"message_blacklist_qq":[], "message_policy_expected_revision":"stale"})
        self.assertEqual(result["code"], 409)
        before = dict(self.guard.config)
        self.guard.config.fail = True
        await self.save({"message_blacklist_qq":[], "fragment_wait_seconds":4,
                         "message_policy_expected_revision":self.revision})
        self.assertEqual(dict(self.guard.config), before)
        self.assertEqual(self.guard.get_message_policy_snapshot()["revision"], self.revision)
        self.guard.config.fail = False
        await self.save({"fragment_wait_seconds":4})
        self.assertEqual(self.guard.config["message_blacklist_qq"], ["123"])

    async def test_validation_is_pure_and_old_companion_cannot_enable(self):
        before = dict(self.guard.config)
        async def body(**_):
            return {"values": {key: before.get(key, []) for key in FIELDS}, "expected_revision":self.revision}
        with patch.object(guard_webui.request, "json", body, create=True):
            result = await guard_webui.handle_message_policy_validate(self.guard)
        self.assertTrue(result["data"]["valid"])
        self.assertEqual(dict(self.guard.config), before)
        with patch.object(self.guard, "message_policy_companion", return_value={"supported":False}):
            result = await self.save({"message_blacklist_qq":["456"], "message_policy_expected_revision":self.revision})
        self.assertEqual(result["status"], "error")
        self.assertEqual(dict(self.guard.config), before)
