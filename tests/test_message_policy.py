"""名单规范化、决策矩阵和配置事务；只使用合成合法 QQ 标识。"""
import asyncio
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At
from gate.guard_message_policy import FIELDS, MessagePolicy, validate_policy_config
from console import guard_webui
from test_gate import gate_config, plugin
from test_reply_delivery import Event, recall


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


class PokePolicyTest(unittest.IsolatedAsyncioTestCase):
    poke = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke"}

    @staticmethod
    def event(group, raw):
        event = Event(group=group, notice=dict(raw))
        event.get_platform_name = lambda: "aiocqhttp"
        event.message_str = ""
        event.is_at_or_wake_command = False
        return event

    async def test_group_and_private_poke_leave_peer_listener_and_reply_untouched(self):
        for group in ("1001", ""):
            for policy in ({}, {"message_blacklist_qq": ["123"]},
                           {"message_graylist_qq": ["123"]}, {"message_whitelist_qq": ["456"]}):
                with self.subTest(group=bool(group), policy=policy):
                    guard = plugin(gate_config(apply_to_private=True, expression_takeover_enabled=True, **policy))
                    event = self.event(group, self.poke)
                    original_send = event.send
                    for hook in (guard.check_incoming_message_policy, guard.observe_message_fragments,
                                 guard.prepare_reply_delivery, guard.parse_reply_input):
                        await hook(event)
                        self.assertFalse(event.stopped)
                    if group:
                        self.assertEqual([r async for r in guard.apply_hard_group_rules(event)], [])
                    await guard.stop_readonly_message_policy(event)
                    self.assertFalse(event.stopped)
                    self.assertEqual([r async for r in guard.coalesce_message_fragments(event)], [])

                    # 后续插件可以反戳，并显式提交自己的补话请求。
                    direct_reply = event.result.derive(event.result.chain)
                    await event.send(direct_reply)
                    req = SimpleNamespace(prompt="戳一戳插件自己的提示", extra_user_content_parts=[])
                    response = SimpleNamespace(completion_text="戳一戳插件自己的回复")
                    original_chain = list(event.result.chain)
                    with patch.object(guard.engine, "decide", new_callable=AsyncMock) as judge:
                        for hook in (guard.check_request_message_policy, guard.understand_reply_input,
                                     guard.listen_livingemotion, guard.listen_livingmemory,
                                     guard.guard_llm_request, guard.attach_reply_contract):
                            await hook(event, req)
                            self.assertFalse(event.stopped)
                        judge.assert_not_awaited()
                    await guard.trace_agent_begin(event, None)
                    tool_args = {"text": "插件自己的工具参数"}
                    await guard.trace_tool_start(event, None, tool_args)
                    await guard.remember_tool_evidence(event, None, tool_args, "工具完成")
                    await guard.suppress_no_reply_and_track(event, response)
                    await guard.trace_agent_done(event, None, response)
                    await guard.deduplicate_before_decoration(event)
                    await guard.recheck_before_send(event)
                    self.assertFalse(event.stopped)
                    await event.send(event.result)
                    await guard.track_sent_message(event)
                    self.assertEqual(event.send, original_send)
                    self.assertEqual(event.sent, [direct_reply, event.result])
                    self.assertEqual(event.result.chain, original_chain)
                    self.assertEqual(vars(req), {"prompt": "戳一戳插件自己的提示", "extra_user_content_parts": []})
                    self.assertEqual(tool_args, {"text": "插件自己的工具参数"})
                    self.assertEqual(response.completion_text, "戳一戳插件自己的回复")
                    self.assertEqual(event.extra, {})
                    self.assertEqual(dict(guard.recent_messages), {})

    async def test_poke_exemption_does_not_expand_to_chat_other_notices_or_recall(self):
        for group in ("1001", ""):
            guard = plugin(gate_config(message_blacklist_qq=["123"]))
            for raw in ({"post_type": "message"}, {**self.poke, "post_type": "message"},
                        {**self.poke, "notice_type": "other"}, {**self.poke, "sub_type": "honor"}):
                with self.subTest(group=bool(group), raw=raw):
                    event = self.event(group, raw)
                    await guard.check_incoming_message_policy(event)
                    self.assertTrue(event.stopped)
                    self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), "blacklist")
            event = recall(self.event(group, self.poke))
            await guard.check_incoming_message_policy(event)
            self.assertTrue(event.stopped)
            self.assertIsNone(event.get_extra("_at_guard_delivery_blocked"))


class ReadonlyArchivePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_message_reaches_archive_before_silent_stop(self):
        for group in ("1001", ""):
            cases = (
                ({"message_graylist_qq": ["123"]}, False, True, False),
                ({"message_whitelist_qq": ["456"]}, False, True, False),
                ({"message_graylist_qq": ["123"]}, True, False, False),
                ({"message_blacklist_qq": ["123"]}, False, False, True),
            )
            for config, wake, readonly, blocked in cases:
                with self.subTest(group=bool(group), config=config, wake=wake):
                    guard = plugin(gate_config(apply_to_private=True, expression_takeover_enabled=True, **config))
                    event = Event(group=group)
                    event.get_platform_name = lambda: "aiocqhttp"
                    event.get_self_id = lambda: "999"
                    event._mentions = [At(qq="999")] if wake else []
                    await guard.check_incoming_message_policy(event)
                    self.assertEqual(event.stopped, blocked)
                    if blocked:
                        self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), "blacklist")
                        continue
                    self.assertEqual(event.get_extra("_livinghello_policy_readonly"), readonly)
                    await guard.prepare_reply_delivery(event)
                    # 归档监听器在两个 LH 钩子之间，事件在到达前不能被停止。
                    self.assertFalse(event.stopped)
                    self.assertEqual(event.get_extra("_at_guard_delivery_prepared", False), not readonly)
                    await guard.stop_readonly_message_policy(event)
                    self.assertEqual(event.stopped, readonly)


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
