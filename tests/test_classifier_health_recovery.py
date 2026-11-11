"""健康恢复必须有成功探测证据，且不能覆盖其他层或更新的故障。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from guard_reply_health import ReplyHealth
import guard_webui


class ClassifierRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.health = ReplyHealth(SimpleNamespace(data_dir=Path(folder.name), config={}),
                                  clock=lambda: 1200.0)
        self.addAsyncCleanup(self.health.close)
        self.health.summary = {"state": "failed", "layer": "classifier",
                               "category": "protocol_error", "failed_at": 1000.0,
                               "recovered_at": None}
        self.health.store.save(self.health.summary)

    async def test_success_persists_recovery_and_keeps_failure_evidence(self):
        self.assertTrue(await self.health.classifier_recovered(1100.0))
        stored, _ = self.health.store.read()
        self.assertEqual(stored["state"], "healthy")
        self.assertEqual(stored["failed_at"], 1000.0)
        self.assertEqual(stored["category"], "protocol_error")
        self.assertEqual(stored["recovery_source"], "classifier_probe")

    async def test_old_probe_cannot_clear_a_newer_failure(self):
        self.health.summary["failed_at"] = 1150.0
        self.assertFalse(await self.health.classifier_recovered(1100.0))
        self.assertEqual(self.health.summary["state"], "failed")

    async def test_classifier_probe_cannot_clear_other_layers(self):
        for layer in ("main_model", "delivery", "preparation", "pipeline"):
            self.health.summary["layer"] = layer
            self.assertFalse(await self.health.classifier_recovered(1100.0))
        self.assertEqual(self.health.summary["state"], "failed")

    async def test_storage_failure_does_not_claim_recovery(self):
        def broken(_):
            self.health.store.health = "unavailable"
        with patch.object(self.health.store, "save", broken):
            self.assertFalse(await self.health.classifier_recovered(1100.0))
        self.assertEqual(self.health.summary["state"], "failed")

    async def test_closed_or_invalid_probe_does_not_recover(self):
        for started in (True, None, float("nan"), float("inf")):
            self.assertFalse(await self.health.classifier_recovered(started))
        self.health.closed = True
        self.assertFalse(await self.health.classifier_recovered(1100.0))

    async def test_probe_endpoint_success_isolated_from_messages_and_backfill(self):
        plugin = SimpleNamespace(engine=SimpleNamespace(probe_classifier=AsyncMock(return_value=None)),
            _resolve_classifier_probe_provider=AsyncMock(), _reply_health=lambda: self.health,
            _policy=Mock(side_effect=AssertionError("不得读取用户消息")),
            context=SimpleNamespace(send_message=Mock(side_effect=AssertionError("不得发送消息"))))
        with patch.object(guard_webui, "request", SimpleNamespace(json=AsyncMock(return_value={"health_check": True}))), \
             patch.object(guard_webui, "_response", lambda payload: payload):
            result = await guard_webui.handle_probe(plugin)
        self.assertTrue(result["probe_passed"])
        self.assertTrue(result["recovered"])
        plugin.engine.probe_classifier.assert_awaited_once_with(
            plugin._resolve_classifier_probe_provider, diagnostics={})
        self.assertEqual(result["platform_sends"], 0)
        plugin._policy.assert_not_called()
        plugin.context.send_message.assert_not_called()

    async def test_failed_probe_does_not_clear_health(self):
        plugin = SimpleNamespace(engine=SimpleNamespace(probe_classifier=AsyncMock(
            return_value=SimpleNamespace(category="protocol_error"))),
            _resolve_classifier_probe_provider=AsyncMock(), _reply_health=lambda: self.health)
        with patch.object(guard_webui, "request", SimpleNamespace(json=AsyncMock(return_value={"health_check": True}))), \
             patch.object(guard_webui, "_response", lambda payload: payload):
            result = await guard_webui.handle_probe(plugin)
        self.assertFalse(result["probe_passed"])
        self.assertFalse(result["recovered"])
        self.assertEqual(result["health_state"], "failed")
