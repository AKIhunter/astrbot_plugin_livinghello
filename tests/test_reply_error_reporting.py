"""异常结果留痕、私聊通知与宿主直发错误的有界验证，无真实模型/消息。"""
from pathlib import Path
from types import SimpleNamespace
import unittest

from astrbot.api.message_components import At, Plain
from gate.guard_engine import DecisionLog
from reply.guard_reply_health import KEY, error_category, safe_excerpt
from test_reply_delivery import Chain
import test_reply_health


class ErrorReportingTests(test_reply_health.ReplyHealthTests):
    # 继承现有真实闭环fixture，同时执行已有成功/撤回/节流验证。
    async def test_failure_replaces_display_preserves_evidence_and_reload(self):
        rid = "a" * 32
        self.event.set_extra("_livinghello_request_id", rid)
        self.guard.engine.log = DecisionLog(self.guard.data_dir / "decisions.jsonl")
        self.guard.engine.log.add({
            "request_id": rid, "action": "reply", "src": "model", "reason": "engaged",
            "ts": 1, "gid": "1001", "category": "group", "ctx": "原判定上下文",
            "raw": '{"act":"reply","score":92}', "score": 92, "mdl": "judge-fixture"})
        before_stats = dict(self.guard.engine.stats)
        await self.health.failure("main_model", "quota_exhausted", event=self.event)
        latest = self.guard.engine.log.recent()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["action"], "error")
        self.assertEqual(latest[0]["decision_action"], "reply")
        self.assertEqual(latest[0]["ctx"], "原判定上下文")
        self.assertEqual(latest[0]["score"], 92)
        self.assertEqual(latest[0]["mdl"], "judge-fixture")
        self.assertEqual(self.guard.engine.stats, before_stats)
        reloaded = DecisionLog(self.guard.engine.log.path)
        self.assertEqual(reloaded.recent(query=rid)[0], latest[0])
        self.assertEqual(len(reloaded.recent(query="quota_exhausted", category="group")), 1)

    async def test_every_failure_logs_when_throttled_or_notifications_disabled(self):
        self.guard.engine.log = DecisionLog(self.guard.data_dir / "decisions.jsonl")
        for index in range(3):
            event = self.event_for()
            event.message_obj.message_id = "source-" + str(index)
            await self.health.failure("delivery", "send_unknown", event=event)
        self.assertEqual(len(self.guard.engine.log.recent()), 3)
        self.assertEqual(len(self.sent), 1)
        self.guard.config["classifier_outage_recovery_enabled"] = False
        event = self.event_for()
        self.assertIsNotNone(self.health.watch(event, expected=True))
        await self.health.finish(event)
        self.assertEqual(len(self.guard.engine.log.recent()), 4)
        self.assertEqual(len(self.sent), 1)

    async def test_group_notification_plain_private_has_source_and_safe_excerpt(self):
        self.event.message_str = "请看截图 token=very-private sk-test-secret /Users/example/key.pem"
        await self.health.failure("main_model", "quota_exhausted", event=self.event)
        route, chain = self.sent[0]
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertTrue(all(isinstance(item, Plain) for item in chain.chain))
        text = chain.chain[0].text
        self.assertIn("群 1001", text)
        self.assertIn("请求编号：" + self.event.get_extra("_livinghello_request_id"), text)
        self.assertIn("消息编号：m1", text)
        self.assertIn("请看截图", text)
        for secret in ("very-private", "sk-test-secret", "/Users/example"):
            self.assertNotIn(secret, text)

    async def test_native_direct_error_not_sent_to_group_and_quota_classified(self):
        self.guard.engine.log = DecisionLog(self.guard.data_dir / "decisions.jsonl")
        self.health.watch(self.event, expected=True)
        self.guard._delivery().patch(self.event)
        message = Chain([Plain("Error occurred while processing agent request: "
                               "Error code: 403 Not enough quota sk-secret")], llm=False)
        await self.event.send(message)
        self.assertEqual(self.event.sent, [])
        self.assertEqual(len(self.sent), 1)
        latest = self.guard.engine.log.recent()[0]
        self.assertEqual(latest["reply_error"]["category"], "quota_exhausted")
        self.assertNotIn("sk-secret", self.sent[0][1].chain[0].text)
        await self.health.finish(self.event)
        self.assertEqual(len(self.guard.engine.log.recent()), 1)

    async def test_typed_error_custom_native_error_and_ordinary_messages(self):
        self.health.watch(self.event, expected=True)
        ordinary = Chain([Plain("指令执行成功")], llm=False)
        self.assertFalse(await self.health.suppress_native_error(self.event, ordinary))
        typed = Chain([Plain("自定义错误文案")], llm=False)
        typed.result_content_type = SimpleNamespace(name="AGENT_RUNNER_ERROR")
        self.assertTrue(await self.health.suppress_native_error(self.event, typed))
        other = self.event_for()
        self.health.watch(other, expected=True)
        self.health.model_done(other, object())
        answer = Chain([Plain("抱歉，我不知道这个问题。")])
        self.assertFalse(await self.health.suppress_native_error(other, answer))
        self.assertTrue(await self.health.suppress_native_error(
            other, Chain([Plain("自定义错误")], llm=False), native_exception=TimeoutError()))

    async def test_native_except_custom_template_is_caught_at_send_boundary(self):
        self.health.watch(self.event, expected=True)
        self.guard._delivery().patch(self.event)
        # 源码文件标识及异常上下文与宿主except分支一致。
        namespace = {}
        exec(compile(
            "async def native(event, Chain, Plain):\n"
            "    try:\n"
            "        raise RuntimeError('Not enough quota')\n"
            "    except Exception:\n"
            "        await event.send(Chain([Plain('自定义错误提示')], llm=False))\n",
            "/synthetic/agent_sub_stages/internal.py", "exec"), namespace)
        await namespace["native"](self.event, Chain, Plain)
        self.assertEqual(self.event.sent, [])
        self.assertEqual(self.health.summary["category"], "quota_exhausted")

    async def test_provider_identity_distinct_from_judge_model(self):
        self.health.provider(self.event, SimpleNamespace(
            meta=lambda: SimpleNamespace(id="fixture/main-model"),
            get_model=lambda: "main-fixture"))
        self.guard.engine.log = DecisionLog(None)
        await self.health.failure("main_model", "quota_exhausted", event=self.event)
        row = self.guard.engine.log.recent()[0]
        self.assertEqual(row["reply_error"]["model"], "main-fixture")
        self.assertIn("本轮起始模型：main-fixture", self.sent[0][1].chain[0].text)

    async def test_cron_tool_failure_detail_is_recorded(self):
        self.guard.engine.log = DecisionLog(self.guard.data_dir / "decisions.jsonl")
        event = self.cron_event()
        self.health.watch(event, expected=True)
        self.health.model_done(event, object())
        self.health.tool_failed(
            event, "error: failed to find platform for session "
                   "qq-bot:FriendMessage:851911696 token=secret")
        self.assertTrue(await self.health.finish_cron(event))
        row = self.guard.engine.log.recent()[0]
        self.assertEqual(row["reason"], "tool_call_failed")
        # 失败原文进 decisions.jsonl，但凭据仍在入库前被脱敏。
        self.assertIn("failed to find platform", row["reply_error"]["detail"])
        self.assertNotIn("secret", row["reply_error"]["detail"])

    async def test_initialization_failure_before_agent_begin_and_command_survives(self):
        self.health.watch(self.event)
        ordinary = Chain([Plain("正常指令回复")], llm=False)
        self.assertFalse(await self.health.suppress_native_error(self.event, ordinary))
        self.assertTrue(await self.health.suppress_native_error(
            self.event, Chain([Plain("模型尚未初始化")], llm=False),
            native_exception=RuntimeError("provider missing")))
        self.assertEqual(self.health.summary["layer"], "main_model")
        self.assertEqual(len(self.sent), 1)

    def test_redaction_and_403_are_not_always_quota(self):
        text = safe_excerpt("api_key=secret password=hidden Bearer bearer-secret "
                            "https://example.invalid/?token=secret " + "A" * 100)
        for secret in ("secret", "hidden", "example.invalid", "A" * 100):
            self.assertNotIn(secret, text)
        self.assertEqual(error_category("Error code: 403 forbidden"), "authentication_error")
        self.assertEqual(error_category("403 Not enough quota"), "quota_exhausted")
