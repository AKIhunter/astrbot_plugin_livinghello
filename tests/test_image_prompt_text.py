"""Offline model routing, migration, and request accounting."""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from images.guard_image_settings import DEFAULTS, ImageSettings
from images.guard_image_text import LEGACY, PromptTextUnavailable
from reply.guard_native_reply import NativeReplyError


class ProviderOpenAIOfficial:
    def __init__(self, provider_id):
        self.provider_config = {"id": provider_id, "custom_extra_body": {"n": 5, "temperature": 0.5}}
        self.api_keys = ["test-fake", "test-fake-2"]
        self.client = SimpleNamespace(with_options=Mock(return_value=object()))
        self.reply = AsyncMock(return_value=SimpleNamespace(role="assistant", completion_text=provider_id))

    def get_model(self):
        return "model"

    async def text_chat(self, **kwargs):
        return await self.reply(self, **kwargs)


class MultiApiRouterProvider:
    def __init__(self, client, api_type="chat", slot=3):
        self.provider_config = {"id": "router"}
        self.pinned_slot = slot
        self.upstreams = [SimpleNamespace(slot=3, api_type=api_type, client=client)]
        self.max_output_tokens = 120


class ImagePromptTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        owner = "astrbot.core.provider.sources.openai_source"
        native = ModuleType(owner)
        native.ProviderOpenAIOfficial = ProviderOpenAIOfficial
        self.enterContext(patch.dict(sys.modules, {owner: native}))
        self.providers = {key: ProviderOpenAIOfficial(key)
                          for key in ("closing", "general", "fallback", "session")}
        self.event = SimpleNamespace(unified_msg_origin="test:group:room")
        self.profiles = {self.event.unified_msg_origin: self.profile("")}
        self.context = SimpleNamespace(
            get_config=Mock(side_effect=self.profiles.__getitem__),
            get_provider_by_id=Mock(side_effect=self.providers.get),
            get_current_chat_provider_id=AsyncMock(return_value="session"),
            provider_manager=SimpleNamespace(inst_map=self.providers))
        self.plugin = SimpleNamespace(context=self.context, data_dir=Path(directory.name))
        self.settings = ImageSettings(self.plugin)
        self.old = Mock(return_value={"closing": "closing", "general": "general", "fallback": "fallback",
                                      "daily_limit": 4})
        self.allow = AsyncMock(return_value=True)

    @staticmethod
    def profile(provider_id):
        return {"agent_runner": {"runner_type": "local", "config": {"model": {"provider_id": provider_id}}}}

    def old_config(self):
        self.settings.path.write_text(json.dumps({k: v for k, v in DEFAULTS.items()
                                                   if not k.startswith("prompt_text_")}))
        self.settings = ImageSettings(self.plugin)

    async def complete(self, key):
        return await self.settings.prompt_text.complete(
            self.event, key, "output prompt", {"description": "窗边的猫"},
            authorize=self.allow, read_legacy=self.old)

    async def test_legacy_order_and_cap_are_migrated_once(self):
        self.old_config()
        self.assertEqual(self.settings.values["prompt_text_provider_id"], LEGACY)
        self.assertTrue(self.settings.read()["prompt_text_usage"]["migration_pending"])
        self.old.assert_not_called()
        self.assertEqual(await self.complete("first"), "closing")
        self.old.return_value = {"closing": "session", "daily_limit": 1}
        self.settings = ImageSettings(self.plugin)
        self.assertEqual(await self.complete("first"), "closing")  # persistent replay
        self.assertEqual(await self.complete("second"), "closing")
        self.providers["closing"].provider_config["enable"] = False
        self.assertEqual(await self.complete("third"), "general")
        self.providers["general"].provider_config["enable"] = False
        self.assertEqual(await self.complete("fourth"), "fallback")
        self.old.assert_called_once()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(self.settings.read()["prompt_text_usage"]["limit"], 4)
        self.assertEqual(self.settings.used(), 0)
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_quota_exhausted"):
            await self.complete("fifth")

    async def test_legacy_empty_models_still_use_session_then_migrated_fallback(self):
        self.old_config()
        self.profiles[self.event.unified_msg_origin] = self.profile("general")
        self.old.return_value = {"closing": "", "general": "", "fallback": "fallback", "daily_limit": 6}
        self.assertEqual(await self.complete("first"), "session")
        self.context.get_current_chat_provider_id.return_value = "missing"
        self.assertEqual(await self.complete("second"), "fallback")
        self.assertEqual(self.context.get_current_chat_provider_id.await_count, 2)
        self.context.get_config.assert_not_called()

    async def test_blank_selector_uses_each_target_profile_before_session_override(self):
        first = self.event.unified_msg_origin
        second = "test:friend:user"
        self.profiles.update({first: self.profile("general"), second: self.profile("closing")})
        self.assertEqual(await self.complete("first"), "general")
        self.event.unified_msg_origin = second
        self.assertEqual(await self.complete("second"), "closing")
        self.context.get_current_chat_provider_id.assert_not_awaited()

        self.profiles[second] = self.profile("")
        self.assertEqual(await self.complete("third"), "session")
        self.context.get_current_chat_provider_id.assert_awaited_once_with(second)
        self.profiles[second] = {"agent_runner": {"runner_type": "dify", "config": {}}}
        self.assertEqual(await self.complete("fourth"), "session")

    async def test_explicit_model_ignores_native_profile_and_session(self):
        self.profiles[self.event.unified_msg_origin] = self.profile("general")
        self.settings.values["prompt_text_provider_id"] = "closing"
        self.assertEqual(await self.complete("first"), "closing")
        self.settings.values["prompt_text_provider_id"] = "missing"
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_model_unavailable"):
            await self.complete("second")
        self.context.get_config.assert_not_called()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(self.settings.prompt_text.usage()["used"], 1)

    async def test_unavailable_main_model_does_not_use_session_or_claim_quota(self):
        route = self.event.unified_msg_origin
        self.providers["general"].provider_config["enable"] = False
        self.providers["closing"].provider_config["api_type"] = "image_generation"
        for provider_id in ("missing", "general", "closing"):
            with self.subTest(provider=provider_id):
                self.profiles[route] = self.profile(provider_id)
                with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_model_unavailable"):
                    await self.complete("same-slot")
        with patch.object(self.context, "get_config", side_effect=RuntimeError("profile read failed")):
            with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_model_unavailable"):
                await self.complete("same-slot")
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.providers["session"].reply.assert_not_awaited()
        self.assertEqual(self.settings.prompt_text.usage()["used"], 0)

        self.profiles[route] = self.profile("")
        self.assertEqual(await self.complete("same-slot"), "session")

    async def test_choosing_pinned_chat_limits_wire_options_without_touching_live_provider(self):
        pinned = MultiApiRouterProvider(self.providers["session"])
        self.providers["router:upstream_3"] = pinned
        self.providers["router"] = MultiApiRouterProvider(self.providers["session"], slot=0)
        self.providers["image-only"] = MultiApiRouterProvider(self.providers["fallback"], api_type="image_generation")
        available = {target["id"] for target in self.settings.read()["prompt_text_providers"]}
        self.assertIn("router:upstream_3", available)
        self.assertFalse({"router", "image-only"} & available)
        await self.settings.save({"expected_revision": self.settings.read()["revision"],
                                  "values": {"prompt_text_provider_id": "router:upstream_3",
                                             "prompt_text_daily_limit": 2}})
        self.assertEqual(await self.complete("first"), "session")
        self.old.assert_not_called()
        live = self.providers["session"]
        args, kwargs = live.reply.await_args
        self.assertEqual(kwargs["request_max_retries"], 1)
        self.assertEqual(kwargs["contexts"], [])
        self.assertIsNone(kwargs["func_tool"])
        self.assertEqual(live.provider_config["custom_extra_body"], {"n": 5, "temperature": 0.5})
        self.assertEqual(args[0].provider_config["custom_extra_body"], {"n": 1, "temperature": 0.5, "max_tokens": 120})
        self.assertEqual(len(args[0].api_keys), 1)
        self.assertEqual(len(live.api_keys), 2)
        live.client.with_options.assert_called_once_with(max_retries=0, timeout=30)
        pinned.max_input_chars = 10
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_input_limit"):
            await self.complete("too-long")
        pinned.max_input_chars = 1000
        self.assertEqual(self.settings.prompt_text.usage()["used"], 1)
        for value in (True, -1, 1001, 1.5):
            with self.assertRaises(NativeReplyError):
                await self.settings.save({"expected_revision": self.settings.read()["revision"],
                                          "values": {"prompt_text_daily_limit": value}})
        await self.settings.save({"expected_revision": self.settings.read()["revision"],
                                  "values": {"prompt_text_daily_limit": 0}})
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_quota_exhausted"):
            await self.complete("second")
        self.assertEqual(await self.complete("first"), "session")  # lowering the cap keeps completed work

    async def test_timeout_and_simultaneous_call_do_not_duplicate_or_reset_quota(self):
        self.settings.values["prompt_text_daily_limit"] = 1
        started = asyncio.Event()
        release = asyncio.Event()

        async def timeout(*args, **kwargs):
            started.set()
            await release.wait()
            raise TimeoutError()

        self.providers["session"].reply.side_effect = timeout
        work = asyncio.create_task(self.complete("first"))
        await started.wait()
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_result_unknown"):
            await self.complete("first")
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_quota_exhausted"):
            await self.complete("second")
        release.set()
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_result_unknown"):
            await work
        self.settings = ImageSettings(self.plugin)
        self.settings.today = lambda: "2099-01-01"
        self.assertEqual(self.settings.prompt_text.usage()["used"], 0)
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_result_unknown"):
            await self.complete("first")  # The next day still cannot dispatch this slot again.
        self.providers["session"].reply.assert_awaited_once()

    async def test_authorization_lost_before_reservation_does_not_charge(self):
        self.allow.return_value = False
        with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_authorization_changed"):
            await self.complete("first")
        self.assertEqual(self.settings.prompt_text.usage()["used"], 0)
        self.providers["session"].reply.assert_not_awaited()


class NativeImagePromptTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_wire_sends_one_request_even_on_upstream_500(self):
        try:
            from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Native
            import httpx
        except ImportError:
            self.skipTest("需要 AstrBot OpenAI Chat Completions 适配器")

        requests = []

        def intercept(req):
            requests.append(json.loads(req.content))
            if len(requests) == 1:
                return httpx.Response(500, json={"error": {"message": "offline failure"}})
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 1, "model": "model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "窗边的猫"}}],
            })

        http = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(intercept)))
        with patch.object(Native, "_create_http_client", return_value=http):
            live = Native({"id": "native", "key": ["fixture-only"], "model": "model",
                           "api_base": "https://offline.invalid/v1",
                           "custom_extra_body": {"n": 5, "max_tokens": 5000, "messages": []}}, {})
        context = SimpleNamespace(get_provider_by_id=lambda _: live)
        with TemporaryDirectory() as directory:
            settings = ImageSettings(SimpleNamespace(context=context, data_dir=Path(directory)))
            settings.values["prompt_text_provider_id"] = "native"
            event = SimpleNamespace(unified_msg_origin="test:group:room")
            allow = AsyncMock(return_value=True)

            async def complete(key):
                return await settings.prompt_text.complete(
                    event, key, "output prompt", {"description": "窗边的猫"},
                    authorize=allow, read_legacy=None)

            with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_result_unknown"):
                await complete("failed")
            with self.assertRaisesRegex(PromptTextUnavailable, "prompt_text_result_unknown"):
                await complete("failed")
            self.assertEqual(len(requests), 1)
            self.assertEqual(await complete("success"), "窗边的猫")
            self.assertEqual(len(requests), 2)
            for body in requests:
                self.assertEqual((body["max_tokens"], body["n"], body["stream"]), (500, 1, False))
                self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
                self.assertNotIn("tools", body)
