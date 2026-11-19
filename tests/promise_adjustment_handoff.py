"""双插件实际门面验收；显式执行，独立 LH CI 不依赖相邻 LE 仓库。"""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_image_generation as fixtures
from images.guard_image_failure import http_failure


class RealAdjustmentHandoffTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImageGenerationTests.setUp

    async def test_real_handoff_keeps_lh_question_and_le_does_not_repeat(self):
        # 本例验证立即询问调整；显式关闭自动改词，避免默认重试许可改变场景。
        self.service.values["prompt_guard_retry_limit"] = 0
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from astrbot_plugin_livingemotion.tests.integration.test_application import application, Message
        from astrbot_plugin_livingemotion.contracts import Relationship
        # 只固定隔离夹具的概率，确保每次都真正走到内容拒绝与实际门面交接。
        app, host, _ = await application(Path(self.folder.name) / "emotion", {
            "relationship_image_refusal_close": 0,
        })
        try:
            event = Message("user-source", "请画窗边的猫")
            event.get_messages = lambda: []
            event.get_extra = lambda key, default=None: event.extra.get(key, default)
            event.chain_result = lambda chain: SimpleNamespace(chain=chain)
            event.send = AsyncMock()
            await app.on_message(event)
            app.current_relationship = AsyncMock(return_value=Relationship(score=500))
            app.promise_actions_active = lambda: True
            app._preparation_guard = AsyncMock(return_value=True)
            app.send_promise_notice = AsyncMock()
            self.plugin.config = {"coupling_livingemotion_enabled": True}
            self.plugin._livinghello_detached_enabled = True
            self.context.get_registered_star = lambda name: (
                SimpleNamespace(activated=True, star_cls=SimpleNamespace(api=app.api, application=app))
                if name == "astrbot_plugin_livingemotion" else None)
            self.service._request.side_effect = http_failure(
                400, '{"error":{"code":"content_policy_violation"}}')
            job = dict(prompt="窗边的猫", use_reference=False, status="running")
            # 本例验证交接内容与去重；隔离冷启动不用生产 0.5s 的性能预算。
            with patch("integrations.guard_livingemotion.API_TIMEOUT", 5.0):
                await self.service._run_image_job(event, job)
            self.service._request.assert_awaited_once()
            self.assertEqual(event.extra["_livinghello_image_failure"]["kind"], "content_rejected")
            self.assertEqual(job["followup"]["task_status"], "waiting")
            pid = job["followup"]["promise_id"]
            self.assertEqual((await app._promises().promises.get(pid))["adjustment_notice_state"], "sent")
            await app._promises().run_due()
            app.send_promise_notice.assert_not_awaited()
            self.assertEqual(event.send.await_count, 1)
            self.assertIn("还是这次就算了？", event.send.await_args_list[0].args[0].chain[0].text)
            self.assertEqual(host.sends, 0)
        finally:
            await app.terminate()
