"""实际钩子和发送包装的名单副作用验证；合成 QQ 事件，无真实发送。"""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import At, Plain, Reply, Image
from test_gate import plugin, gate_config
from test_reply_delivery import Event, Chain, recall


class QQEvent(Event):
    def __init__(self, *, sender="123", group="456", text="普通聊天", components=None):
        super().__init__(group=group)
        self.sender, self.message_str = sender, text
        self.unified_msg_origin = f"fixture:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
        self.message_obj.message = components if components is not None else [Plain(text)]
        self.is_at_or_wake_command = False
    def get_platform_name(self):
        return "aiocqhttp"
    def get_sender_id(self):
        return self.sender
    def get_self_id(self):
        return "999"
    def get_messages(self):
        return self.message_obj.message


class ReadSpyEvent(QQEvent):
    """Count actual reads so the diagnostic exception handler cannot hide them."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.body_reads = 0
        self.get_messages = Mock(return_value=self.message_obj.message)

    @property
    def message_str(self):
        self.body_reads = getattr(self, "body_reads", 0) + 1
        return self._body

    @message_str.setter
    def message_str(self, value):
        self._body = value


class RuntimePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_quote_metadata_and_images_use_actual_origin_before_body(self):
        guard = plugin(gate_config(message_blacklist_qq=["789"], message_blacklist_groups=["888"]))
        nested = Reply(sender_id="789", message_str="BLACK_BODY", chain=[Plain("BLACK_BODY")])
        outer = Reply(sender_id="555", message_str="允许原文 BLACK_BODY",
                      chain=[Plain("允许原文"), nested, Image.fromURL("https://invalid.test/picture")])
        event = QQEvent(components=[Plain("当前消息"), outer])
        meta = guard._message_meta(event)
        self.assertNotIn("BLACK_BODY", str(meta))
        outer.group_id = "888"
        self.assertEqual(guard._event_images(event), [])
        observation = await guard._understanding().parse(event)
        self.assertNotIn("允许原文", observation.text)
        self.assertEqual(observation.images, [])
        await guard.terminate()

    async def test_gray_bypass_preserves_explicit_evidence_without_fake_score(self):
        guard = plugin(gate_config(message_graylist_qq=["123"]))
        event = QQEvent(components=[At(qq="999"), Plain("帮我")], text="帮我")
        await guard.check_incoming_message_policy(event)
        guard._record_bypass(event, "gray_explicit")
        record = guard.engine.log.recent(1)[0]
        self.assertIsNone(record["score"])
        self.assertTrue(record["chat_data"]["current_message"]["explicit_call"])
        await guard.terminate()

    async def test_blacklist_stops_before_reading_body_or_components(self):
        for field, group, blocked in (
                ("message_blacklist_qq", "456", "123"),
                ("message_blacklist_qq", "", "123"),
                ("message_blacklist_groups", "456", "456")):
            with self.subTest(field=field, group=group):
                guard = plugin(gate_config(**{field: [blocked]}))
                event = ReadSpyEvent(group=group, text="BLACK_BODY")
                guard._understanding = Mock()
                await guard.check_incoming_message_policy(event)
                self.assertTrue(event.stopped)
                self.assertEqual(event.body_reads, 0)
                event.get_messages.assert_not_called()
                guard._understanding.assert_not_called()
                self.assertFalse(guard.recent_messages)
                self.assertFalse(getattr(guard, "recent_images", {}))
                self.assertFalse(getattr(guard, "fragment_wait", None))
                self.assertFalse(hasattr(guard, "request_trace"))
                self.assertEqual(guard.engine.snapshot()["judged"], 0)
                await guard.terminate()

    async def test_blacklist_rechecks_close_existing_trace_without_reading(self):
        for hook in ("check_incoming_message_policy", "check_request_message_policy",
                     "guard_llm_request", "recheck_before_send"):
            for field, group, blocked in (
                    ("message_blacklist_qq", "456", "123"),
                    ("message_blacklist_qq", "", "123"),
                    ("message_blacklist_groups", "456", "456")):
                with self.subTest(hook=hook, field=field, group=group):
                    guard = plugin(gate_config())
                    event = ReadSpyEvent(group=group, text="BLACK_BODY")
                    trace = guard._trace()
                    request_id = trace.begin(event)
                    guard.config[field] = [blocked]
                    args = (event, SimpleNamespace()) if hook in (
                        "check_request_message_policy", "guard_llm_request") else (event,)
                    await getattr(guard, hook)(*args)
                    self.assertTrue(event.stopped)
                    self.assertEqual(event.body_reads, 0)
                    event.get_messages.assert_not_called()
                    self.assertEqual(event.result.chain, [])
                    row = await trace.get(request_id)
                    self.assertEqual(row["state"], "silent")
                    self.assertEqual(row["events"][-1]["reason"],
                                     event.get_extra("_at_guard_delivery_blocked"))
                    self.assertNotIn("BLACK_BODY", str(row))
                    self.assertNotIn("judged_input", row)
                    await guard.terminate()

    async def test_readonly_records_basic_background_and_stops_after_archive(self):
        for field in ("message_graylist_qq", "message_whitelist_qq"):
            guard = plugin(gate_config(**{field: ["123" if "gray" in field else "789"]}))
            event = QQEvent(components=[Plain("普通聊天"), Image.fromURL("https://invalid.test/picture")])
            guard._understanding = Mock(side_effect=AssertionError("只读不能调用理解"))
            await guard.check_incoming_message_policy(event)
            await guard.observe_message_fragments(event)
            await guard.parse_reply_input(event)
            results = [r async for r in guard.apply_hard_group_rules(event)]
            self.assertFalse(results)
            self.assertFalse(event.stopped)
            self.assertEqual(sum(map(len, guard.recent_messages.values())), 1)
            self.assertFalse(getattr(guard, "fragment_wait", None))
            await guard.stop_readonly_message_policy(event)
            self.assertTrue(event.stopped)
            trace_id = event.get_extra("_livinghello_request_id")
            row = await guard._trace().get(trace_id)
            self.assertEqual(row["judged_input"]["text"], "普通聊天")
            self.assertEqual(guard.engine.snapshot()["judged"], 0)
            await guard.terminate()

    async def test_gray_actual_at_and_name_bypass_model_even_private(self):
        for group, components, text in (
                ("456", [At(qq="999"), Plain("帮我")], "帮我"),
                ("456", [Plain("恰比 帮我")], "恰比 帮我"),
                ("", [Plain("恰比 帮我")], "恰比 帮我")):
            provider = SimpleNamespace(text_chat=AsyncMock(side_effect=AssertionError("灰名单唤醒不能参与打分")))
            guard = plugin(gate_config(message_graylist_qq=["123"], bot_names=["恰比"]), provider)
            event = QQEvent(group=group, text=text, components=components)
            await guard.check_incoming_message_policy(event)
            req = SimpleNamespace(extra_user_content_parts=[], image_urls=[])
            await guard.guard_llm_request(event, req)
            self.assertFalse(event.stopped)
            provider.text_chat.assert_not_called()
            await guard.terminate()

    async def test_gray_group_reply_survives_ordinary_and_explicit_new_messages(self):
        for field in ("message_graylist_qq", "message_graylist_groups"):
            for mode in ("enforce", "shadow"):
                for scope in ("private", "all"):
                    for wake in ("at", "name"):
                        with self.subTest(field=field, mode=mode, scope=scope, wake=wake):
                            guard = plugin(gate_config(
                                **{field: ["123" if field.endswith("qq") else "456"]},
                                gate_mode=mode, watermark_scope=scope,
                                expression_takeover_enabled=True,
                                reply_typing_chars_per_second=0, bot_names=["恰比"],
                            ))
                            try:
                                old = QQEvent(
                                    text="帮我" if wake == "at" else "恰比 帮我",
                                    components=[At(qq="999"), Plain("帮我")]
                                    if wake == "at" else None,
                                )
                                await guard.check_incoming_message_policy(old)
                                await guard.parse_reply_input(old)
                                await guard.guard_llm_request(old, SimpleNamespace(
                                    extra_user_content_parts=[], image_urls=[]))
                                # 同一用户续话、其他群友插话和新的明确呼叫都不撤销旧任务。
                                for sender, explicit in (("123", False), ("789", False),
                                                         ("123", True), ("789", True)):
                                    newer = QQEvent(
                                        sender=sender, text="补充消息",
                                        components=[At(qq="999"), Plain("补充消息")]
                                        if explicit else None,
                                    )
                                    await guard.check_incoming_message_policy(newer)
                                    await guard.parse_reply_input(newer)
                                await guard.recheck_before_send(old)
                                checker = old.get_extra("_at_guard_send_check")
                                self.assertTrue(callable(checker))
                                self.assertEqual(checker(), "")
                                self.assertFalse(old.get_extra("_at_guard_superseded"))
                                self.assertFalse(old.stopped)
                                await old.send(old.result)
                                self.assertTrue(old.sent)
                            finally:
                                await guard.terminate()

    async def test_gray_group_reply_rechecks_expiry_without_emotion_plugin(self):
        guard = plugin(gate_config(
            message_graylist_groups=["456"], expression_takeover_enabled=True,
            gate_reply_max_age_seconds=300,
        ))
        try:
            event = QQEvent(components=[At(qq="999"), Plain("帮我")])
            await guard.check_incoming_message_policy(event)
            await guard.guard_llm_request(event, SimpleNamespace(
                extra_user_content_parts=[], image_urls=[]))
            event.set_extra("_at_guard_recorded_at", time.monotonic() - 400)
            await guard.recheck_before_send(event)
            self.assertTrue(event.stopped)
            self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), "message_expired")
            self.assertFalse(event.result.chain)
            self.assertFalse(event.sent)
        finally:
            await guard.terminate()

    async def test_gray_group_reply_rechecks_protections_between_packets(self):
        for case in ("recall", "expired", "blacklist", "policy_changed"):
            with self.subTest(case=case):
                guard = plugin(gate_config(
                    message_graylist_groups=["456"], expression_takeover_enabled=True,
                    reply_recall_group=True, reply_typing_chars_per_second=0,
                    gate_reply_max_age_seconds=300,
                ))
                try:
                    event = QQEvent(components=[At(qq="999"), Plain("帮我")])
                    event.result = Chain([Plain("第一段"), Plain("第二段")])
                    event.set_extra("_at_guard_fragment_ticket", SimpleNamespace(
                        batch=SimpleNamespace(deadline=time.monotonic() + 60,
                                              clock=time.monotonic)))
                    await guard.check_incoming_message_policy(event)
                    guard._record_incoming(event, guard._group_key(event))
                    await guard.guard_llm_request(event, SimpleNamespace(
                        extra_user_content_parts=[], image_urls=[]))
                    await guard.recheck_before_send(event)
                    parts = list(event.result.chain)
                    await event.send(event.result.derive([parts[0]]))
                    self.assertEqual(len(event.sent), 1)
                    if case == "recall":
                        notice = recall(event)
                        notice.unified_msg_origin = event.unified_msg_origin
                        guard._delivery().notice(notice)
                    elif case == "expired":
                        event.set_extra("_at_guard_recorded_at", time.monotonic() - 400)
                    elif case == "blacklist":
                        guard.config["message_blacklist_qq"] = ["123"]
                    else:
                        guard.config["message_blacklist_qq"] = ["789"]
                    await event.send(event.result.derive([parts[1]]))
                    self.assertEqual(len(event.sent), 1)
                    expected = {"recall": "message_recalled", "expired": "message_expired",
                                "blacklist": "blacklist", "policy_changed": "policy_changed"}[case]
                    self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), expected)
                finally:
                    await guard.terminate()

    async def test_only_other_all_and_quoted_at_do_not_wake(self):
        for components in ([At(qq="555")], [At(qq="all")], [Reply(sender_id="789", chain=[At(qq="999")])]):
            guard = plugin(gate_config(message_graylist_qq=["123"]))
            event = QQEvent(components=components)
            await guard.check_incoming_message_policy(event)
            self.assertTrue(event.get_extra("_livinghello_policy_readonly"))

    async def test_direct_and_streaming_send_cancel_after_policy_change(self):
        for mode in ("enforce", "shadow"):
            guard = plugin(gate_config(gate_mode=mode))
            event = QQEvent()
            await guard.check_incoming_message_policy(event)
            guard.config["message_blacklist_qq"] = ["123"]
            await event.send(Chain([Plain("不得发送")], llm=False))
            self.assertFalse(event.sent)
            self.assertTrue(event.stopped)
            await guard.terminate()

    async def test_old_generation_invalidated_even_if_sender_still_allowed(self):
        guard = plugin(gate_config())
        event = QQEvent()
        await guard.check_request_message_policy(event, SimpleNamespace())
        guard.config["message_blacklist_qq"] = ["789"]
        await event.send(Chain([Plain("可能引用了789的旧资料")]))
        self.assertFalse(event.sent)
        self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), "policy_changed")
        await guard.terminate()
