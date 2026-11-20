"""真实保存处理器的秒数配置与下一次调用读回；不接入模型或平台。"""
import unittest
from unittest.mock import AsyncMock, patch

from console import guard_webui
from reply.guard_reply_fallback import ReplyFallback
from test_gate import gate_config, plugin


class PrimaryTimeoutConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class Config(dict):
            def save_config(self):
                pass
        self.guard = plugin(gate_config())
        self.guard.config = Config(self.guard.config)
        self.guard.engine.config = self.guard.config
        self.retry = ReplyFallback(self.guard)

    async def save(self, values):
        async def body(**_):
            return values
        with patch.object(guard_webui.request, "json", body, create=True):
            return await guard_webui.handle_config(self.guard)

    async def test_save_receipt_memory_and_next_call_agree_without_catalog(self):
        key = "reply_primary_timeout_seconds"
        before = dict(self.guard.config)
        for seconds in (1, 8, 30, 60, 15):
            with self.subTest(seconds=seconds), \
                    patch.object(self.guard.config, "save_config") as write, \
                    patch.object(guard_webui, "_providers", AsyncMock(side_effect=AssertionError("models"))):
                result = await self.save({key: seconds})
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["data"]["config"], {key: seconds})
                self.assertEqual(self.retry.primary_first_output_timeout(), seconds)
                self.assertEqual({k: v for k, v in self.guard.config.items() if k != key},
                                 {k: v for k, v in before.items() if k != key})
                write.assert_called_once()
        with patch.object(guard_webui, "_providers", AsyncMock(return_value=[])):
            state = (await guard_webui.handle_state(self.guard))["data"]
        self.assertEqual(state["config"][key], 15)
        self.assertEqual(state["schema"][key]["slider"], {"min": 1, "max": 60, "step": 1})

    async def test_invalid_seconds_reject_entire_payload_without_write(self):
        for seconds in (0, 61, True, 1.5, "15", None):
            with self.subTest(seconds=seconds), \
                    patch.object(self.guard.config, "save_config") as write:
                before = dict(self.guard.config)
                result = await self.save({"fragment_wait_seconds": 4,
                                         "reply_primary_timeout_seconds": seconds})
                self.assertIsInstance(result, str)
                self.assertEqual(dict(self.guard.config), before)
                write.assert_not_called()

    async def test_old_config_without_key_reads_default_in_state_and_runtime(self):
        self.guard.config.pop("reply_primary_timeout_seconds", None)
        with patch.object(guard_webui, "_providers", AsyncMock(return_value=[])):
            state = (await guard_webui.handle_state(self.guard))["data"]
        self.assertEqual(state["config"]["reply_primary_timeout_seconds"], 15)
        self.assertEqual(self.retry.primary_first_output_timeout(), 15)
