"""双插件真实门面闭环；显式运行，模型与平台均为替身。"""
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import test_image_generation as fixtures
from images.guard_image_settings import ImageSettings
from images.guard_image_tool import GenerateImageTool
from images.guard_image_failure import http_failure
from integrations.guard_livingemotion import image_result


class ImageResultHandoffTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImageGenerationTests.setUp

    async def asyncSetUp(self):
        from astrbot_plugin_livingemotion.tests.integration.test_application import application, Message
        from astrbot_plugin_livingemotion.contracts import Context, Relationship
        self.Message, self.Context = Message, Context
        self.app, _, _ = await application(Path(self.folder.name) / "emotion", {
            "relationship_image_refusal_close": 0})
        self.addAsyncCleanup(self.app.terminate)
        self.app.current_relationship = AsyncMock(return_value=Relationship(score=500))
        self.app.promise_actions_active = lambda: True
        self.app._preparation_guard = AsyncMock(return_value=True)
        self.plugin.config = {"coupling_livingemotion_enabled": True}
        self.plugin._livinghello_detached_enabled = True
        self.context.get_registered_star = lambda name: (
            SimpleNamespace(activated=True, star_cls=SimpleNamespace(
                api=self.app.api, application=self.app))
            if name == "astrbot_plugin_livingemotion" else None)
        self.tool = GenerateImageTool(service=self.service)
        self.event = await self.event_for("source")
        self.scope = self.Context.from_dict(self.event.extra["livingemotion:context"])

    async def event_for(self, mid):
        event = self.Message(mid, "现在把窗边的照片给我")
        event.get_extra = lambda key, default=None: event.extra.get(key, default)
        event.get_messages = lambda: []
        event.chain_result = lambda chain: SimpleNamespace(chain=chain)
        event.send = AsyncMock()
        await self.app.on_message(event)
        return event

    async def promise(self, description="生图：窗边的猫"):
        return (await self.app._promises().promises.create(
            self.scope, description=description, note=description,
            bucket="马上", due_at=self.app.store.clock() + 120))["promise_id"]

    async def run_image(self, pid=None):
        with patch("integrations.guard_livingemotion.API_TIMEOUT", 5):
            await self.tool.call(self.event, prompt="窗边的猫", **({"promise_id": pid} if pid else {}))
            await self.service._run_image_job(self.event, self.event.extra["_livinghello_image_job"])

    async def test_success_completes_original_and_next_turn_knows(self):
        pid, other = await self.promise(), await self.promise("生图：厨房的猫")
        await self.run_image(pid)
        store = self.app._promises().promises
        self.assertEqual((await store.get(pid))["status"], "sent")
        self.assertEqual([r["promise_id"] for r in await store.active()], [other])
        self.event.send.assert_awaited_once()
        self.service._request.assert_awaited_once()
        req = SimpleNamespace(extra_user_content_parts=[])
        next_event = await self.event_for("next")
        ImageSettings(self.plugin).attach_receipts(next_event, req)
        self.assertIn('"status": "sent"', req.extra_user_content_parts[0].text)
        await self.app.on_llm_request(next_event, req)
        self.assertTrue(any("已成功交付的图片事实" in p.text for p in req.extra_user_content_parts))

    async def test_send_rejection_keeps_original_without_duplicate(self):
        pid = await self.promise()
        self.event.send.return_value = False
        await self.run_image(pid)
        store = self.app._promises().promises
        self.assertEqual((await store.get(pid))["status"], "waiting")
        self.assertEqual([r["promise_id"] for r in await store.active()], [pid])
        self.service._request.assert_awaited_once()

    async def test_second_picture_does_not_complete_second_original(self):
        first, second = await self.promise(), await self.promise("生图：厨房的猫")
        with patch("integrations.guard_livingemotion.API_TIMEOUT", 5):
            await self.tool.call(self.event, prompt="窗边的猫", promise_id=first)
            self.assertIn("未执行", await self.tool.call(self.event, prompt="厨房的猫", promise_id=second))
            await self.service._run_image_job(self.event, self.event.extra["_livinghello_image_job"])
        self.assertEqual((await self.app._promises().promises.get(second))["status"], "scheduled")
        self.service._request.assert_awaited_once()

    async def test_unbound_success_keeps_old_task(self):
        pid = await self.promise()
        await self.run_image()
        self.assertEqual((await self.app._promises().promises.get(pid))["status"], "scheduled")
        self.assertEqual(self.service.receipts(self.event)[0]["status"], "sent")

    async def test_unknown_result_cannot_be_reclaimed(self):
        store = self.app._promises().promises
        pid = (await store.accept_image_handoff(self.scope, request_id="old-unknown",
            source_event_id="old-source", failure=dict(
                prompt="窗边的猫", kind="delivery_unknown", reason="等待核对",
                request_id="old-unknown", use_reference=False)))["promise_id"]
        await self.run_image(pid)
        self.assertEqual((await store.get(pid))["status"], "waiting")
        self.service._request.assert_not_awaited()

    async def test_content_failure_with_mapping_off_records_question_on_original(self):
        pid = await self.promise()
        self.service.values["prompt_guard_enabled"] = False
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        await self.run_image(pid)
        row = await self.app._promises().promises.get(pid)
        self.assertEqual(row["status"], "waiting")
        self.assertEqual(row["adjustment_notice_state"], "sent")
        self.assertIn("还是这次就算了", self.event.send.await_args.args[0].chain[0].text)

    async def test_lost_binding_receipt_never_charges(self):
        pid = await self.promise()

        async def lost(context, event, config, *, bind=False):
            result = await image_result(context, event, config, bind=bind)
            return None if bind else result

        with patch("images.guard_image_settings.image_result", side_effect=lost):
            await self.run_image(pid)
        self.assertEqual((await self.app._promises().promises.get(pid))["status"], "waiting")
        self.service._request.assert_not_awaited()

    def deferred_peer(self):
        fixtures.ImageGenerationTests.deferred_plugin(self)
        self.plugin.get_image_prompt_mapping_contract = lambda: (
            fixtures.Main.get_image_prompt_mapping_contract(self.plugin))
        self.plugin.prepare_image_prompt_repair = lambda prompt, proposal, **kwargs: (
            fixtures.Main.prepare_image_prompt_repair(self.plugin, prompt, proposal, **kwargs))
        self.app.bridge.discover = lambda _: self.plugin
        self.app.purpose_model = AsyncMock(return_value=json.dumps({
            "changes_scene": False, "substitutions": [{"from": "blouse", "to": "shirt"}]}))
        self.service.values["daily_limit"] = 10
        self.clock = [self.app.store.clock()]
        self.app.store.clock = lambda: self.clock[0]

    def deferred_event(self, context, row):
        event = self.Message("promise:" + row["promise_id"], row["description"],
                             account=context.account_id)
        event.get_extra = lambda key, default=None: event.extra.get(key, default)
        event.set_extra("livingemotion:context", {
            **self.event.extra["livingemotion:context"], "route": context.route})
        event.set_extra("livingemotion:promise_id", row["promise_id"])
        event.get_messages = lambda: []
        event.chain_result = lambda chain: SimpleNamespace(chain=chain)
        return event

    async def mapped_failure(self, pid):
        with patch("integrations.guard_livingemotion.API_TIMEOUT", 5):
            await self.tool.call(self.event, prompt="blue blouse", promise_id=pid)
            await self.service._run_image_job(self.event, self.event.extra["_livinghello_image_job"])

    async def test_real_facades_mapped_retry_delivers_original_and_learns(self):
        from astrbot_plugin_livingemotion.runtime.promise_actions import ImagePromiseAction
        self.deferred_peer()
        self.service._request.side_effect = [
            http_failure(400, '{"error":{"code":"content_policy_violation"}}'),
            (self.output, "blue shirt")]
        pid = await self.promise()
        await self.mapped_failure(pid)
        store = self.app._promises().promises
        row = await store.get(pid)
        self.assertEqual((row["status"], row["action_state"]), ("scheduled", "retry_ready"))
        self.assertEqual([r["promise_id"] for r in await store.active()], [pid])
        self.clock[0] += 301
        with patch.object(ImagePromiseAction, "make_event", side_effect=self.deferred_event):
            self.assertEqual(await self.app._promises().run_due(), 1)
            self.assertEqual(await self.app._promises().run_due(), 0)
        self.assertEqual((await store.get(pid))["status"], "sent")
        self.assertEqual(self.service._request.await_count, 2)
        self.context.send_message.assert_awaited_once()
        # 即时失败只说明一次；重试的图片走统一延迟投递出口。
        self.event.send.assert_awaited_once()
        self.assertIn("保留你要的画面", self.event.send.await_args.args[0].chain[0].text)
        self.assertEqual(self.app.purpose_model.await_count, 1)
        connection = self.service.connection(self.service.values)
        mapped = self.service.prompt_mapping.apply(self.service.values, connection, "red blouse")
        self.assertEqual(mapped["prompt"], "red shirt")
        self.assertFalse(await store.active())

    async def test_real_facades_retry_limit_survives_restart(self):
        from astrbot_plugin_livingemotion.runtime.promise_actions import ImagePromiseAction
        from astrbot_plugin_livingemotion.runtime.promise_service import PromiseService
        self.deferred_peer()
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        self.app.purpose_model.side_effect = [
            json.dumps({"changes_scene": False, "substitutions": [{"from": "blouse", "to": "shirt"}]}),
            json.dumps({"changes_scene": False, "substitutions": [{"from": "blue", "to": "azure"}]})]
        pid = await self.promise()
        await self.mapped_failure(pid)
        self.clock[0] += 301
        self.app.send_promise_notice = AsyncMock(return_value="sent")
        with patch.object(ImagePromiseAction, "make_event", side_effect=self.deferred_event):
            self.assertEqual(await self.app._promises().run_due(), 0)
        row = await self.app._promises().promises.get(pid)
        self.assertEqual((row["status"], row["action_state"]), ("waiting", "held"))
        self.assertEqual(json.loads(row["action_result_json"])["prompt_retry_count"], 2)
        self.assertEqual(self.service._request.await_count, 3)
        self.app.send_promise_notice.assert_awaited_once()
        self.context.send_message.assert_not_awaited()
        self.clock[0] += 3600
        restarted = PromiseService(self.app.store, self.app)
        self.assertEqual(await restarted.run_due(), 0)
        self.assertEqual(self.service._request.await_count, 3)
        self.assertEqual([r["promise_id"] for r in await restarted.promises.active()], [pid])

    async def test_rewrite_loses_authorization_before_second_request(self):
        from astrbot_plugin_livingemotion.runtime.promise_actions import ImagePromiseAction
        self.deferred_peer()
        self.service._request.side_effect = http_failure(
            400, '{"error":{"code":"content_policy_violation"}}')
        pid = await self.promise()
        await self.mapped_failure(pid)
        self.clock[0] += 301

        async def revoked(*args, **kwargs):
            self.app._preparation_guard.return_value = False
            return json.dumps({"changes_scene": False,
                               "substitutions": [{"from": "blouse", "to": "shirt"}]})
        self.app.purpose_model.side_effect = revoked
        with patch.object(ImagePromiseAction, "make_event", side_effect=self.deferred_event):
            self.assertEqual(await self.app._promises().run_due(), 0)
        self.assertEqual(self.service._request.await_count, 1)
        self.context.send_message.assert_not_awaited()
