import asyncio
import time
import unittest
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest import mock

from astrbot.api.message_components import Image
from astrbot.core.platform.message_type import MessageType

from guard_engine import GateEngine
from guard_livingemotion import STATE_MARKER
from main import Main


class TurnEvent:
    """水位测试用事件：只实现真正会碰到的接口。"""

    def __init__(self, *, text="hi", private=True, origin="friend:u1",
                 received_at=None, group="g1"):
        self.message_str = text
        self.unified_msg_origin = origin
        self.extra = {}
        self.stopped = False
        self.private = private
        self.group = group
        self.message_obj = SimpleNamespace(raw_message={}, message_id="m1")
        self.result = None
        self.messages = []
        if received_at is not None:
            self.extra["_at_guard_received_at"] = received_at

    def get_message_type(self):
        return MessageType.FRIEND_MESSAGE if self.private else MessageType.GROUP_MESSAGE

    def get_group_id(self):
        return "" if self.private else self.group

    def get_messages(self):
        return self.messages

    def get_sender_id(self):
        return "u1"

    def get_self_id(self):
        return "bot"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_result(self):
        return self.result

    def stop_event(self):
        self.stopped = True


class DeliveryStub:
    @staticmethod
    def recalled(_event):
        return False

    @staticmethod
    def block(_event):
        raise AssertionError("水位测试不应走撤回分支")


def plugin(config=None):
    instance = Main.__new__(Main)
    base = {
        "expression_takeover_enabled": True,
        "gate_mode": "enforce",
        "gate_reply_max_age_seconds": 30,
        "private_watermark_window_seconds": 15,
        "watermark_scope": "private",
        "apply_to_private": False,
        "reply_recall_private": False,
        "reply_recall_group": False,
    }
    base.update(config or {})
    instance.config = base
    instance.data_dir = None
    instance.active_reply_turns = {}
    instance.context_revision = defaultdict(int)
    instance.proactive_inflight_until = {}
    instance.recent_messages = defaultdict(lambda: deque(maxlen=30))
    instance.participation_stats_path = None
    instance.participation_stats = instance._empty_participation_stats()
    instance.logger = SimpleNamespace(
        debug=lambda *_a, **_k: None,
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )
    instance.engine = GateEngine(instance.config, None, instance.logger)
    instance.context = SimpleNamespace()
    instance._delivery = lambda: DeliveryStub()
    return instance


class WatermarkTurnTest(unittest.TestCase):
    def test_new_message_inside_window_supersedes_old(self):
        now = time.monotonic()
        instance = plugin()
        old = TurnEvent(received_at=now)
        new = TurnEvent(received_at=now + 8)

        instance._note_reply_turn(old)
        instance._note_reply_turn(new)

        self.assertTrue(old.get_extra("_at_guard_superseded"))
        self.assertEqual(instance._stale_reason(old, "friend:u1"),
                         "superseded_by_new_message")
        # 新消息自己不被标记。
        self.assertFalse(new.get_extra("_at_guard_superseded"))

    def test_first_message_image_is_registered_before_understanding(self):
        now = time.monotonic()
        instance = plugin()
        old = TurnEvent(received_at=now)
        old.messages = [Image(url="https://example.com/a.png")]
        seen = {}

        async def parse(event):
            row = instance.active_reply_turns.get("friend:u1") or {}
            seen["registered"] = row.get("event") is event

        instance._event_policy = lambda _event: {
            "read_allowed": True, "reply_allowed": True}
        instance._understanding = lambda: SimpleNamespace(parse=parse)

        asyncio.run(instance.parse_reply_input(old))

        self.assertTrue(seen.get("registered"), "视觉理解前必须已登记本轮")
        instance._note_reply_turn(TurnEvent(received_at=now + 3))
        self.assertTrue(old.get_extra("_at_guard_superseded"))

    def test_new_message_outside_window_keeps_old_reply(self):
        now = time.monotonic()
        instance = plugin()
        old = TurnEvent(received_at=now - 100)
        new = TurnEvent(received_at=now)

        instance._note_reply_turn(old)
        instance._note_reply_turn(new)

        self.assertFalse(old.get_extra("_at_guard_superseded"))
        self.assertEqual(instance._stale_reason(old, "friend:u1"), "")

    def test_new_message_keeps_old_context_after_supersede(self):
        now = time.monotonic()
        instance = plugin()
        instance.context_revision["friend:u1"] = 7
        old = TurnEvent(text="第一条", received_at=now)
        instance._note_reply_turn(old)
        new = TurnEvent(text="第二条", received_at=now + 5)

        instance._note_reply_turn(new)

        # 顶替只影响发送：旧消息仍在会话上下文里，上下文版本不被水位改动。
        self.assertTrue(old.get_extra("_at_guard_superseded"))
        self.assertEqual(instance.context_revision["friend:u1"], 7)
        self.assertIs(instance.active_reply_turns["friend:u1"]["event"], new)
        # 新消息本身不被连坐，轮到自己复核时照常发送。
        self.assertEqual(instance._stale_reason(new, "friend:u1"), "")

    def test_reply_max_age_wins_over_superseded(self):
        now = time.monotonic()
        instance = plugin()
        old = TurnEvent(received_at=now - 400)
        old.set_extra("_at_guard_recorded_at", now - 400)
        old.set_extra("_at_guard_superseded", True)

        self.assertEqual(instance._stale_reason(old, "friend:u1"), "message_expired")

    def test_zero_window_disables_supersede(self):
        now = time.monotonic()
        instance = plugin({"private_watermark_window_seconds": 0})
        old = TurnEvent(received_at=now)
        new = TurnEvent(received_at=now + 1)

        instance._note_reply_turn(old)
        instance._note_reply_turn(new)

        self.assertFalse(old.get_extra("_at_guard_superseded"))
        self.assertEqual(instance._stale_reason(old, "friend:u1"), "")

    def test_group_is_untouched_by_default(self):
        now = time.monotonic()
        instance = plugin()
        old = TurnEvent(private=False, origin="group:g1", received_at=now)
        new = TurnEvent(private=False, origin="group:g1", received_at=now + 2)

        instance._note_reply_turn(old)
        instance._note_reply_turn(new)

        self.assertFalse(old.get_extra("_at_guard_superseded"))
        self.assertEqual(instance.active_reply_turns, {})

    def test_scope_all_applies_watermark_to_group(self):
        now = time.monotonic()
        instance = plugin({"watermark_scope": "all"})
        old = TurnEvent(private=False, origin="group:g1", received_at=now)
        new = TurnEvent(private=False, origin="group:g1", received_at=now + 2)

        instance._note_reply_turn(old)
        instance._note_reply_turn(new)

        self.assertTrue(old.get_extra("_at_guard_superseded"))

    def test_recheck_drops_chain_and_stops_event(self):
        instance = plugin()
        event = TurnEvent(received_at=time.monotonic())
        event.set_extra("_at_guard_superseded", True)
        event.set_extra("_at_guard_judged_revision", 0)
        event.result = SimpleNamespace(chain=[SimpleNamespace(text="旧回复")])
        trace = SimpleNamespace(
            finish=lambda *_a, **_k: None,
            identity=lambda _event: {"request_id": "r1"},
        )
        instance._policy_send_reason = lambda _event: ""
        instance._reply_health = lambda: SimpleNamespace(
            suppress_native_error=_async_false)
        instance._trace = lambda event=None: trace
        instance._message_meta = lambda _event: {}

        asyncio.run(instance.recheck_before_send(event))

        self.assertEqual(event.result.chain, [])
        self.assertTrue(event.stopped)
        self.assertEqual(event.get_extra("_at_guard_delivery_blocked"),
                         "superseded_by_new_message")
        self.assertNotIn("friend:u1", instance.active_reply_turns)


class VisionBatchTest(unittest.TestCase):
    def test_superseded_stops_vision_calls(self):
        from guard_understanding import Observation, Understanding

        instance = plugin()
        instance._policy_send_reason = lambda _event: ""
        understanding = Understanding(instance)
        event = TurnEvent(received_at=time.monotonic())
        event.set_extra("_at_guard_superseded", True)
        observation = Observation(images=["a", "b", "c"])

        with mock.patch("guard_understanding.report_model_fault") as report:
            state, calls = asyncio.run(
                understanding._enrich_images(event, observation, object(), {}))

        self.assertEqual(calls, 0)
        self.assertEqual(state, "failed")
        report.assert_not_called()
        # 已完成的批次没有，摘要仍要标明未读取，不能猜测内容。
        self.assertIn("未能读取", observation.vision_summary)


class EmotionSnapshotTest(unittest.TestCase):
    def test_same_turn_reuses_first_state_block(self):
        instance = plugin()
        event = TurnEvent()
        first = SimpleNamespace(text=STATE_MARKER + '\n{"emotion":"calm"}')
        req = SimpleNamespace(extra_user_content_parts=[first])

        instance._freeze_emotion_snapshot(event, req)
        self.assertIs(event.get_extra("_at_guard_emotion_snapshot"), first)

        fresh = SimpleNamespace(text=STATE_MARKER + '\n{"emotion":"angry"}')
        req.extra_user_content_parts[:] = [fresh]
        instance._freeze_emotion_snapshot(event, req)

        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIs(req.extra_user_content_parts[0], first)

    def test_no_fresh_state_does_not_reinject_snapshot(self):
        instance = plugin()
        event = TurnEvent()
        first = SimpleNamespace(text=STATE_MARKER + "\n{}")
        req = SimpleNamespace(extra_user_content_parts=[first])
        instance._freeze_emotion_snapshot(event, req)

        other = SimpleNamespace(text="<RAG-Faiss-Memory>memory")
        req.extra_user_content_parts[:] = [other]
        instance._freeze_emotion_snapshot(event, req)

        self.assertEqual([part.text for part in req.extra_user_content_parts],
                         ["<RAG-Faiss-Memory>memory"])

    def test_snapshot_is_per_event(self):
        instance = plugin()
        first_event, second_event = TurnEvent(), TurnEvent()
        first = SimpleNamespace(text=STATE_MARKER + '\n{"emotion":"calm"}')
        second = SimpleNamespace(text=STATE_MARKER + '\n{"emotion":"sad"}')

        req = SimpleNamespace(extra_user_content_parts=[first])
        instance._freeze_emotion_snapshot(first_event, req)
        req.extra_user_content_parts[:] = [second]
        instance._freeze_emotion_snapshot(second_event, req)

        self.assertIs(second_event.get_extra("_at_guard_emotion_snapshot"), second)
        self.assertIs(first_event.get_extra("_at_guard_emotion_snapshot"), first)


async def _async_false(*_args, **_kwargs):
    return False


if __name__ == "__main__":
    unittest.main()
