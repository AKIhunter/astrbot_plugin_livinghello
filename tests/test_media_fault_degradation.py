"""现行媒体故障边界：保留证据、继续回复、管理员纯文本私聊。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from input.guard_understanding import Observation
from reply.guard_reply_health import report_model_fault
from test_gate import gate_config, plugin
from test_reply_delivery import Event


class MediaFaultDegradationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.guard = plugin(gate_config(classifier_outage_recovery_enabled=True))
        self.guard.data_dir = Path(self.temp.name)
        self.guard.context.get_config = lambda: {"admins_id": ["9001"]}
        self.guard.context.send_message = AsyncMock(return_value=True)
        self.event = Event()
        self.event.get_platform_id = lambda: "qq-bot"
        self.event.get_platform_name = lambda: "aiocqhttp"

    async def asyncTearDown(self):
        await self.drain()
        await self.guard.terminate()
        self.temp.cleanup()

    async def drain(self):
        import asyncio
        tasks = list(getattr(self.guard, "_model_fault_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks)

    async def test_notification_is_private_plain_and_does_not_stop_reply(self):
        report_model_fault(self.guard, "understand_image", "timeout",
                           "sk-private-secret", self.event)
        await self.drain()
        self.assertFalse(self.event.stopped)
        self.guard.context.send_message.assert_awaited_once()
        route, message = self.guard.context.send_message.await_args.args
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertTrue(all(type(p).__name__ == "Plain" for p in message.chain))
        self.assertNotIn("sk-private-secret", message.chain[0].text)

    async def test_disabled_notice_does_not_stop_reply_or_send(self):
        self.guard.config["classifier_outage_recovery_enabled"] = False
        report_model_fault(self.guard, "understand_image", "timeout", event=self.event)
        await self.drain()
        self.assertFalse(self.event.stopped)
        self.guard.context.send_message.assert_not_awaited()

    async def test_image_timeout_preserves_text_and_unread_marker(self):
        reader = self.guard._understanding()
        observation = Observation(text="原始文字", prompt="原始文字", images=["offline.png"])
        provider = SimpleNamespace(text_chat=AsyncMock(side_effect=TimeoutError()))
        from unittest.mock import patch
        with patch("images.guard_image_budget.fit_sync",
                   side_effect=lambda urls: (urls, 0)):
            await reader._enrich_images(self.event, observation, provider, {})
        await self.drain()
        self.assertIn("原始文字", observation.prompt)
        self.assertIn("勿猜测", observation.prompt)
        self.assertFalse(self.event.stopped)
