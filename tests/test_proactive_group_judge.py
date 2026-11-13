"""普通群消息主动判定（proactive_group_judge）的离线回归。"""
import time
import unittest

from astrbot.api.message_components import At, Plain
from input.guard_fragments import FragmentWait

from test_context_policy import SequenceProvider, output
from test_fragment_wait import Clock, FragmentEvent, request
from test_gate import gate_config, plugin


class JudgeEvent(FragmentEvent):
    """补齐名单判定需要的平台元信息。"""

    def get_platform_name(self):
        return "aiocqhttp"


def group_config(**overrides):
    config = gate_config(fragment_wait_seconds=0, bot_names=["恰比"])
    config.update(overrides)
    return config


class ProactiveGroupJudgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = SequenceProvider(output())
        self.guard = plugin(group_config(), self.provider)

    async def trigger(self, event):
        await self.guard.observe_message_fragments(event)
        return [item async for item in self.guard.coalesce_message_fragments(event)]

    async def judge(self, event):
        req = request()
        await self.guard.guard_llm_request(event, req)
        return req

    async def test_plain_group_message_triggers_when_whitelist_empty(self):
        event = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(event), [("llm", "这个任务怎么做")])
        self.assertTrue(event.get_extra("_at_guard_proactive_judge_requested"))

    async def test_plain_group_message_skips_when_whitelist_not_empty(self):
        self.guard = plugin(group_config(message_whitelist_groups=["1001"]), self.provider)
        event = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(event), [])
        self.assertFalse(event.get_extra("_at_guard_proactive_judge_requested"))

    async def test_all_mode_ignores_whitelist_but_still_skips_blacklist(self):
        self.guard = plugin(group_config(
            proactive_group_judge="all", message_whitelist_groups=["1001"]), self.provider)
        self.assertEqual(await self.trigger(JudgeEvent("这个任务怎么做")),
                         [("llm", "这个任务怎么做")])

        self.guard = plugin(group_config(
            proactive_group_judge="all", message_blacklist_groups=["1001"]), self.provider)
        blocked = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(blocked), [])
        self.assertFalse(blocked.get_extra("_at_guard_proactive_judge_requested"))

    async def test_blacklist_group_never_triggers(self):
        self.guard = plugin(group_config(message_blacklist_groups=["1001"]), self.provider)
        event = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(event), [])

    async def test_graylist_plain_message_skips_but_explicit_at_passes_through(self):
        self.guard = plugin(group_config(message_graylist_groups=["1001"]), self.provider)
        plain = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(plain), [])
        self.assertFalse(plain.get_extra("_at_guard_proactive_judge_requested"))

        explicit = JudgeEvent(
            "现在回我", components=[At(qq="bot"), Plain("现在回我")], explicit=True)
        self.assertEqual(await self.trigger(explicit), [])
        await self.judge(explicit)
        self.assertFalse(explicit.stopped)
        self.assertEqual(explicit.extra["_livinghello_message_policy"]["reason"], "gray_explicit")
        self.assertEqual(self.provider.prompts, [])

    async def test_bot_own_message_and_private_do_not_trigger(self):
        own = JudgeEvent("这个任务怎么做", sender="bot")
        self.assertEqual(await self.trigger(own), [])

        private = JudgeEvent("这个任务怎么做", group="")
        self.assertEqual(await self.trigger(private), [])

    async def test_off_mode_keeps_host_only_behavior(self):
        self.guard = plugin(group_config(proactive_group_judge="off"), self.provider)
        event = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(event), [])

    async def test_silent_decision_stops_generation(self):
        self.guard.context.provider = SequenceProvider(output("silent", 20, True, "others_talking"))
        event = JudgeEvent("这个任务怎么做")
        self.assertEqual(await self.trigger(event), [("llm", "这个任务怎么做")])
        await self.judge(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.extra["_at_guard_decision"]["action"], "silence")
        self.assertEqual(event.extra["_at_guard_decision"]["reason"], "others_talking")
        self.assertTrue(event.get_extra("_at_guard_proactive"))
        self.assertEqual(self.guard.engine.stats["gen_skipped"], 1)

    async def test_reply_decision_keeps_request_alive(self):
        self.guard.context.provider = SequenceProvider(output())
        event = JudgeEvent("这个任务怎么做")
        await self.trigger(event)
        await self.judge(event)
        self.assertFalse(event.stopped)
        self.assertTrue(event.get_extra("_at_guard_proactive"))
        self.assertEqual(event.extra["_at_guard_decision"]["action"], "reply")

    async def test_cooldown_skips_but_inflight_keeps_new_question(self):
        self.guard = plugin(group_config(proactive_cooldown_seconds=60), self.provider)
        self.guard.last_proactive_reply["1001"] = time.monotonic()
        self.assertEqual(await self.trigger(JudgeEvent("这个任务怎么做")), [])

        self.guard = plugin(group_config(), self.provider)
        self.guard.proactive_inflight_until["1001"] = time.monotonic() + 30
        self.assertEqual(await self.trigger(JudgeEvent("这个任务怎么做")),
                         [("llm", "这个任务怎么做")])

    async def test_fragment_batch_triggers_only_once_with_merged_text(self):
        clock = Clock()
        guard = plugin(gate_config(fragment_wait_seconds=3, bot_names=["恰比"]), self.provider)
        guard.fragment_wait = FragmentWait(clock)
        first = JudgeEvent("我想问")
        second = JudgeEvent("这个任务怎么做", mid="2")

        clock.now = 0
        await guard.observe_message_fragments(first)
        clock.now = 2
        await guard.observe_message_fragments(second)
        clock.now = first.extra["_at_guard_fragment_ticket"].batch.deadline
        self.assertEqual(
            [item async for item in guard.coalesce_message_fragments(second)],
            [("llm", "我想问\n这个任务怎么做")])
        self.assertTrue(second.get_extra("_at_guard_proactive_judge_requested"))
        self.assertFalse(first.get_extra("_at_guard_proactive_judge_requested"))


if __name__ == "__main__":
    unittest.main()
