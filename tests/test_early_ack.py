"""用离线群聊事件和发送替身验证垫场轮换。"""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from astrbot.api.message_components import At

from console.guard_webui import _coerce
from test_gate import FakeProvider, GateEvent, gate_config, plugin


def schema_texts():
    schema = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    return json.loads(schema.read_text(encoding="utf-8"))["reply_early_ack_texts"]["default"]


class EarlyAckTest(unittest.IsolatedAsyncioTestCase):
    async def test_at_and_wake_share_group_rotation_and_wrap_after_twenty(self):
        provider = FakeProvider("不应触发参与判定")
        guard = plugin(gate_config(
            reply_early_ack_texts=["我看看"],
            reply_early_ack_seconds=0.001,
            reply_early_ack_cooldown_seconds=0,
        ), provider)
        guard.context.send_message = send = AsyncMock()

        async def trigger(group, at):
            event = GateEvent(mentions=[At(qq="bot")] if at else [], group=group)
            event.is_at_or_wake_command = not at  # 宿主已经匹配到唤醒词。
            req = SimpleNamespace(extra_user_content_parts=[], contexts=[])
            await guard.guard_llm_request(event, req)
            self.assertFalse(event.stopped)
            self.assertEqual(guard.engine.log.recent(1)[0]["reason"], "at_call" if at else "wake")
            task = event.get_extra("_livinghello_early_ack")
            self.assertIsNotNone(task)
            await task
            route, chain = send.await_args.args
            self.assertEqual(route, event.unified_msg_origin)
            return chain.chain[0].text

        sent = []
        for index in range(20):
            sent.append(await trigger("1001", at=index % 2 == 0))
            if index == 6:
                self.assertEqual(await trigger("1002", at=False), sent[0])

        self.assertEqual(len(set(sent)), 20)
        self.assertEqual(sent, schema_texts())
        self.assertEqual(await trigger("1001", at=True), sent[0])
        self.assertEqual(await trigger("1002", at=True), sent[1])
        self.assertEqual(send.await_count, 23)
        self.assertEqual(provider.calls, 0)

    def test_old_defaults_expand_but_custom_texts_still_win(self):
        expected = schema_texts()
        cases = (
            (None, expected),
            (" 我看看 ", expected),
            (["稍等，我想一下。", "我看一下，马上说。", "等一下，我确认下。"], expected),
            (expected[:12], expected),
            (["稍等我看下"], ["稍等我看下"]),
            ("先别急 | 我先确认一下", ["先别急", "我先确认一下"]),
        )
        for saved, sequence in cases:
            with self.subTest(saved=saved):
                guard = plugin(gate_config(reply_early_ack_texts=saved))
                self.assertEqual(
                    [guard._early_ack_text("1001") for _ in range(len(sequence) + 1)],
                    [*sequence, sequence[0]],
                )

    def test_save_validation_accepts_full_default_and_reset(self):
        key = "reply_early_ack_texts"
        self.assertEqual(_coerce(key, schema_texts()), schema_texts())
        self.assertEqual(_coerce(key, []), [])
        self.assertEqual(_coerce(key, "先别急 | 我先确认一下\n稍等哈"), [
            "先别急", "我先确认一下", "稍等哈",
        ])
