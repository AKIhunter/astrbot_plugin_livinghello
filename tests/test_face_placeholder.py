"""纯 QQ 表情占位：只补正文，不改有文字消息和灰名单的行为。"""
import unittest

from test_gate import gate_config, plugin
from test_reply_delivery import Event


class Face:
    """替身：宿主 Face 组件只有 id，类名就是 Face。"""

    def __init__(self, face_id):
        self.id = face_id


class Mface:
    """替身：部分适配器会把 mface 也放进消息链。"""

    def __init__(self, face_id=""):
        self.id = face_id


class QQEvent(Event):
    """名单校验需要真实平台名，否则会先命中 source_unknown。"""

    def get_platform_name(self):
        return "aiocqhttp"


class FacePlaceholderTest(unittest.IsolatedAsyncioTestCase):
    def test_pure_face_gets_placeholder_and_text_is_untouched(self):
        guard = plugin(gate_config())

        face = Event()
        face.message_str = ""
        face.message_obj.message = [Face(14)]
        self.assertEqual(guard._fragment_text(face), "[表情:14]")
        self.assertEqual(guard._judged_snapshot(face)["text"], "[表情:14]")

        plain = Event()
        plain.message_str = "在吗"
        plain.message_obj.message = []
        self.assertEqual(guard._fragment_text(plain), "在吗")
        self.assertEqual(guard._judged_snapshot(plain)["text"], "在吗")

    def test_face_without_id_still_gets_plain_placeholder(self):
        guard = plugin(gate_config())
        event = Event()
        event.message_str = ""
        event.message_obj.message = [Mface()]
        self.assertEqual(guard._fragment_text(event), "[表情]")

    async def test_graylist_silence_still_wins_over_placeholder(self):
        guard = plugin(gate_config(message_graylist_groups=["1001"]))
        event = QQEvent()
        event.message_str = ""
        event.message_obj.message = [Face(1)]
        await guard.check_incoming_message_policy(event)
        await guard.stop_readonly_message_policy(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.get_extra("_at_guard_delivery_blocked"), "gray_silent")
        await guard.terminate()
