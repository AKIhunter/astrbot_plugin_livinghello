"""保存确认与读取解耦、模型目录缓存回归；均为内存替身。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from console import guard_webui
from gate.guard_message_policy import FIELDS
from test_gate import gate_config, plugin


class SaveReceiptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class Config(dict):
            def save_config(self):
                pass
        self.guard = plugin(gate_config())
        self.guard.config = Config(self.guard.config)
        self.guard.engine.config = self.guard.config

    async def save(self, values):
        async def body(**_):
            return values
        with patch.object(guard_webui.request, "json", body, create=True):
            return await guard_webui.handle_config(self.guard)

    async def test_plain_save_never_waits_for_state_or_models(self):
        with patch.object(guard_webui, "_state", AsyncMock(side_effect=AssertionError("slow state"))), \
             patch.object(guard_webui, "_providers", AsyncMock(side_effect=AssertionError("slow models"))):
            result = await self.save({"fragment_wait_seconds": 4})
        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertTrue(data["saved"])
        self.assertEqual(data["config"], {"fragment_wait_seconds": 4.0})
        self.assertEqual(data["changed"], ["fragment_wait_seconds"])
        self.assertFalse(set(data) & {"providers", "today", "decisions", "schema", "listeners"})
        self.assertNotIn("normalized_values", data["message_policy"])

    async def test_empty_model_selection_needs_no_directory(self):
        with patch.object(guard_webui, "_providers", AsyncMock(side_effect=AssertionError("models"))):
            result = await self.save({"vision_provider_id": ""})
        self.assertEqual(result["status"], "ok")

    async def test_nonempty_model_still_checks_availability_and_capability(self):
        models = AsyncMock(return_value=[{"id": "text", "supports_images": False}])
        with patch.object(guard_webui, "_providers", models):
            self.assertIn("不在可用列表", await self.save({"classifier_provider_id": "missing"}))
            self.assertIn("无法确认支持图片", await self.save({"vision_provider_id": "text"}))
            result = await self.save({"classifier_provider_id": "text"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(models.await_count, 3)

    async def test_shared_vision_field_is_editable_and_can_be_cleared(self):
        # 不走 handle_config：宿主版本差异会让响应类型不再是 dict，这里只验证白名单与取值。
        self.assertIn("vision_shared_provider_id", guard_webui.EDITABLE)
        self.assertEqual(
            guard_webui._coerce("vision_shared_provider_id", "multi_api_router:upstream_5"),
            "multi_api_router:upstream_5")
        # 清空共用项必须合法，否则无法回到「各自跟随会话模型」。
        self.assertEqual(guard_webui._coerce("vision_shared_provider_id", ""), "")

    async def test_canonical_policy_receipt_and_state_are_consistent(self):
        revision = self.guard.get_message_policy_snapshot()["revision"]
        values = {key: [] for key in FIELDS}
        values["message_blacklist_qq"] = "００１２３，123、456"
        result = await self.save({**values, "message_policy_expected_revision": revision})
        self.assertEqual(result["data"]["config"]["message_blacklist_qq"], ["123", "456"])
        with patch.object(guard_webui, "_providers", AsyncMock(return_value=[])):
            state = (await guard_webui.handle_state(self.guard))["data"]
        self.assertEqual(result["data"]["message_policy"]["revision"], state["message_policy"]["revision"])
        self.assertEqual(state["config"]["message_blacklist_qq"], ["123", "456"])
        self.assertIn("today", state)
        self.assertIn("decisions", state)


class ProviderCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.slow = False
        async def models():
            self.calls += 1
            self.started.set()
            if self.slow:
                await self.release.wait()
            return ["text-small"]
        self.provider = SimpleNamespace(provider_config={"id": "text", "api_base": "https://fixture.invalid"},
                                        get_model=lambda: "text-small", get_models=models)
        self.registered = [self.provider]
        self.context = SimpleNamespace(get_all_providers=lambda: self.registered,
                                       get_provider_by_id=lambda _: self.provider)
        self.guard = SimpleNamespace(context=self.context, config={})

    async def test_concurrent_cold_reads_share_one_catalog_query(self):
        self.slow = True
        tasks = [asyncio.create_task(guard_webui._providers(self.guard)) for _ in range(10)]
        await self.started.wait()
        self.assertEqual(self.calls, 1)
        self.release.set()
        rows = await asyncio.gather(*tasks)
        self.assertEqual(self.calls, 1)
        self.assertTrue(all(row[0]["id"] == "text" for row in rows))
        rows[0][0]["id"] = "mutated"
        self.assertEqual((await guard_webui._providers(self.guard))[0]["id"], "text")

    async def test_plain_parameter_save_and_existing_model_selection_reuse_cache(self):
        await guard_webui._providers(self.guard)
        guard = plugin(gate_config())
        class Config(dict):
            def save_config(self):
                pass
        guard.config = Config(guard.config)
        guard.engine.config = guard.config
        guard.context = self.context
        # 同一实际插件实例先预热，然后经真实保存处理器验证缓存不失效。
        await guard_webui._providers(guard)
        for values in ({"fragment_wait_seconds": 4}, {"classifier_provider_id": "text"}):
            async def body(**_):
                return values
            with patch.object(guard_webui.request, "json", body, create=True):
                self.assertEqual((await guard_webui.handle_config(guard))["status"], "ok")
        self.assertEqual(self.calls, 2)

    async def test_connection_edit_registration_and_ttl_invalidate(self):
        await guard_webui._providers(self.guard)
        self.provider.provider_config["api_base"] = "https://changed.invalid"
        await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 2)
        self.provider.provider_config["enable"] = False
        self.assertEqual(await guard_webui._providers(self.guard), [])
        self.provider.provider_config["enable"] = True
        replacement = SimpleNamespace(**vars(self.provider))
        self.registered[:] = [replacement]
        await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 3)
        self.guard._console_providers_cache["at"] -= guard_webui.PROVIDERS_CACHE_TTL_SECONDS + 1
        await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 4)

    async def test_router_connection_change_invalidates_without_provider_replacement(self):
        connection = {"id": "router:upstream_1", "api_type": "chat", "model": "model-a",
                      "display": "A", "api_base": "https://fixture.invalid", "key": ["synthetic"]}
        from integrations import guard_router_connections
        with patch.object(guard_router_connections, "router_connections",
                          side_effect=lambda _: {"router:upstream_1": dict(connection)}):
            await guard_webui._providers(self.guard)
            connection["model"] = "model-b"
            await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 2)

    async def test_plugin_instances_do_not_share_catalog(self):
        second = SimpleNamespace(context=self.context)
        await guard_webui._providers(self.guard)
        await guard_webui._providers(second)
        self.assertEqual(self.calls, 2)

    async def test_empty_startup_catalog_does_not_pin_late_registration(self):
        self.registered.clear()
        self.assertEqual(await guard_webui._providers(self.guard), [])
        self.assertIsNone(getattr(self.guard, "_console_providers_cache", None))
        self.registered.append(self.provider)
        self.assertEqual((await guard_webui._providers(self.guard))[0]["id"], "text")
        self.assertEqual(self.calls, 1)

    async def test_state_invalidation_does_not_invalidate_model_catalog(self):
        await guard_webui._providers(self.guard)
        invalidate = getattr(guard_webui, "_invalidate_state", None)
        if callable(invalidate):
            invalidate()
        await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 1)

    async def test_connection_change_during_load_does_not_cache_old_revision(self):
        self.slow = True
        task = asyncio.create_task(guard_webui._providers(self.guard))
        await self.started.wait()
        self.provider.provider_config["display_name"] = "updated"
        self.release.set()
        rows = await task
        self.assertEqual(rows[0]["display"], "updated")
        await guard_webui._providers(self.guard)
        self.assertEqual(self.calls, 2)
