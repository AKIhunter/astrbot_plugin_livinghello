"""私聊复用开关：最终发送包、原生失败提示与群聊隔离。"""
import unittest

from astrbot.api.message_components import At, Plain, Reply
from console.guard_webui import EDITABLE, _coerce, _load_schema_subset
from test_gate import gate_config, plugin
from test_reply_delivery import Chain, Event


class PrivateReplyTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, *, group="", reuse=None, native=True, quote=True, llm=True,
                   takeover=True, semantic=False):
        config = gate_config(expression_takeover_enabled=takeover,
                             reply_quote_enabled=quote, reply_mention_enabled=True)
        if reuse is not None:
            config["expression_private_reuse"] = reuse
        guard = plugin(config)
        event = Event(group=group)
        event.result = Chain([Plain("原文😊")], llm=llm)
        if semantic:
            event.result.chain.insert(0, Reply(id="m1"))
        delivery = guard._delivery()
        await delivery.prepare(event)
        await guard.recheck_before_send(event)
        heads = [Reply(id="m1"), At(qq="123")] if native else []
        await event.send(event.result.derive(heads + event.result.chain))
        return event.sent[0].chain

    async def test_missing_and_false_default_strip_native_current_headers(self):
        for reuse in (None, False):
            chain = await self.send(reuse=reuse)
            self.assertFalse(any(isinstance(c, (Reply, At)) for c in chain))
            self.assertEqual(chain[0].text, "原文😊")

    async def test_enabled_reuses_quote_setting_and_cleanup_without_private_at(self):
        for quote in (False, True):
            chain = await self.send(reuse=True, quote=quote)
            self.assertEqual(sum(isinstance(c, Reply) for c in chain), int(quote))
            self.assertFalse(any(isinstance(c, At) for c in chain))
            self.assertEqual(chain[-1].text, "原文")

    async def test_group_keeps_quote_and_cleanup_for_both_switch_values(self):
        for reuse in (False, True):
            chain = await self.send(group="1001", reuse=reuse)
            self.assertEqual(sum(isinstance(c, Reply) for c in chain), 1)
            self.assertEqual(sum(isinstance(c, At) for c in chain), 1)
            self.assertEqual(chain[-1].text, "原文")

    async def test_private_native_failure_prompt_also_sends_without_current_quote(self):
        chain = await self.send(llm=False)
        self.assertFalse(any(isinstance(c, (Reply, At)) for c in chain))
        self.assertEqual(chain[0].text, "原文😊")

    async def test_current_quote_removed_even_when_present_before_decoration(self):
        chain = await self.send(semantic=True, native=False)
        self.assertFalse(any(isinstance(c, Reply) for c in chain))

    async def test_disabled_management_preserves_native_reply(self):
        chain = await self.send(takeover=False)
        self.assertEqual(sum(isinstance(c, Reply) for c in chain), 1)
        self.assertEqual(chain[-1].text, "原文😊")

    async def test_other_reference_and_media_metadata_preserved(self):
        guard = plugin(gate_config(expression_takeover_enabled=True))
        event = Event(group="")
        reference = Reply(id="other-history")
        event.result = Chain([reference, Plain("保留历史引用")])
        event.result.use_t2i_ = True
        await guard._delivery().prepare(event)
        await guard.recheck_before_send(event)
        await event.send(event.result.derive([Reply(id="m1"), *event.result.chain]))
        self.assertEqual(event.sent[0].chain, [reference, event.result.chain[-1]])
        self.assertTrue(event.sent[0].use_t2i_)


class PrivateReplySettingsTests(unittest.TestCase):
    def test_schema_and_console_share_false_boolean_default(self):
        self.assertIn("expression_private_reuse", EDITABLE)
        field = _load_schema_subset()["expression_private_reuse"]
        self.assertEqual(field["type"], "bool")
        self.assertIs(field["default"], False)
        for value in (False, True):
            self.assertIs(_coerce("expression_private_reuse", value), value)
        self.assertIs(_coerce("expression_private_reuse", "false"), False)


if __name__ == "__main__":
    unittest.main()
