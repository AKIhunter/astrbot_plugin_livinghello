"""路由声明的图能力（api_type=image_to_text/multimodal）应被收录为可看图。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from console import guard_webui
from gate.guard_engine import GateFailure
from images.guard_media import image_target
from test_context_policy import output
from test_gate import GateEvent, gate_config, plugin
from test_multimodal import IMAGE, NEED_IMAGE, Provider


class FakeProvider:
    def __init__(self, provider_id, model, pinned=0):
        self.provider_config = {
            "id": provider_id,
            "type": "multi_api_router" if pinned else "openai_chat_completion",
            "model": model,
            "display_name": model,
        }
        if pinned:
            self.provider_config["upstream_slot"] = pinned
            self.provider_config["parent_provider_id"] = "multi_api_router"
            # 指定上游 Provider 在路由器里持有 upstreams 列表；这里模拟空列表，
            # 与线上 route_enabled=False 的上游一致。原生 provider 没有该属性。
            self.upstreams = []
        self._model = model

    def get_model(self):
        return self._model


ROUTER_CONFIG = {
    "enabled": True,
    "provider_id": "multi_api_router",
    "upstream_4": {"name": "Qwen3.8", "model": "qwen3.8-max",
                   "api_base": "https://example.invalid", "api_key": "k",
                   "api_type": "image_to_text"},
    "upstream_6": {"name": "DeepSeek", "model": "deepseek-v4.1",
                   "api_base": "https://example.invalid", "api_key": "k",
                   "api_type": "chat"},
}


def catalog(providers):
    context = SimpleNamespace(
        get_all_providers=lambda: providers,
        get_registered_star=lambda name: SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config=ROUTER_CONFIG)),
    )
    plugin = SimpleNamespace(context=context, config={})
    return {item["id"]: item for item in asyncio.run(guard_webui._providers(plugin))}


class RouterVisionDeclarationTest(unittest.TestCase):
    @staticmethod
    def route(model, api_type):
        client = FakeProvider("client", model)
        provider = FakeProvider("multi_api_router:upstream_15", model, 15)
        provider.upstreams = [SimpleNamespace(client=client, api_type=api_type)]
        return provider

    def test_hy4_multimodal_is_selected_at_runtime_and_in_catalog(self):
        for model in ("hy4flash-free", "hy4-preview-f"):
            with self.subTest(model=model):
                provider = self.route(model, "multimodal")
                target = image_target(provider, "multi_api_router:upstream_15")
                self.assertEqual(target.model, model)
                self.assertIs(target.provider, provider)
                self.assertTrue(catalog([provider])[provider.provider_config["id"]]["supports_images"])

    def test_image_to_text_overrides_text_name_heuristic_only_for_router(self):
        provider = self.route("qwen3.8-max", "image_to_text")
        self.assertEqual(image_target(provider).model, "qwen3.8-max")
        with self.assertRaises(Exception):
            image_target(FakeProvider("native", "qwen3.8-max"))

    def test_declared_chat_is_not_selected_even_with_visual_model_name(self):
        provider = self.route("qwen3-vl-plus", "chat")
        with self.assertRaises(Exception):
            image_target(provider, "multi_api_router:upstream_15")
        self.assertFalse(catalog([provider])[provider.provider_config["id"]]["supports_images"])

    def test_mixed_router_uses_declared_visual_slot_and_keeps_pin(self):
        provider = self.route("hy4-preview-f", "multimodal")
        provider.upstreams.insert(0, SimpleNamespace(
            client=FakeProvider("text", "qwen3-vl-plus"), api_type="chat"))
        self.assertEqual(image_target(provider).model, "hy4-preview-f")
        with self.assertRaises(Exception):
            image_target(provider, "multi_api_router::qwen3-vl-plus")

    def test_router_image_to_text_is_vision_even_if_name_not_matched(self):
        # qwen3.8-max 不命中任何视觉正则，靠路由声明才应被收录。
        items = catalog([FakeProvider("multi_api_router:upstream_4", "qwen3.8-max", 4)])
        self.assertTrue(items["multi_api_router:upstream_4"]["supports_images"])

    def test_router_chat_stays_text(self):
        items = catalog([FakeProvider("multi_api_router:upstream_6", "deepseek-v4.1", 6)])
        self.assertFalse(items["multi_api_router:upstream_6"]["supports_images"])

    def test_native_provider_keeps_name_heuristic(self):
        # 宿主原生 provider 没有路由连接，仍按原逻辑：不认识的 gpt-6 不算视觉。
        items = catalog([FakeProvider("native-gpt6", "gpt-6-astra"),
                         FakeProvider("native-qwen-vl", "qwen3-vl-plus")])
        self.assertFalse(items["native-gpt6"]["supports_images"])
        self.assertTrue(items["native-qwen-vl"]["supports_images"])


class ManualVisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_save_and_gate_send_image_to_selected_slot_despite_same_model(self):
        model = "deepseek-v4.1-flash"
        chat, visual = [
            RouterVisionDeclarationTest.route(model, kind) for kind in ("chat", "multimodal")]
        chat_id = chat.provider_config["id"]
        visual.provider_config.update(id="multi_api_router:upstream_16", upstream_slot=16)
        chat.upstreams[0].client.text_chat = AsyncMock(
            return_value=SimpleNamespace(completion_text=output()))
        visual.upstreams[0].client.text_chat = AsyncMock()
        chat.text_chat = visual.text_chat = AsyncMock()
        aggregate = FakeProvider("auto-text", "automatic")
        aggregate.upstreams = chat.upstreams
        disabled = FakeProvider("disabled", model)
        disabled.provider_config["enable"] = False
        speech = FakeProvider("stt", "whisper")
        providers = {p.provider_config["id"]: p for p in (chat, visual, aggregate, disabled)}
        guard = plugin(gate_config())

        class Config(dict):
            pass

        guard.config = guard.engine.config = Config(guard.config)
        writes = []
        guard.config.save_config = lambda: writes.append(dict(guard.config))
        guard.context = SimpleNamespace(
            get_all_providers=lambda: list(providers.values()),
            get_all_stt_providers=lambda: [speech],
            get_provider_by_id=providers.get,
            get_using_provider_async=AsyncMock(return_value=chat),
        )

        async def save(**values):
            with patch.object(guard_webui.request, "json", AsyncMock(return_value=values), create=True):
                return await guard_webui.handle_config(guard)

        field = "vision_shared_provider_id"
        for key, bad in (
            (field, "missing"), (field, "disabled"), (field, "stt"), (field, "auto-text"),
            ("understand_video_provider_id", chat_id),
        ):
            with self.subTest(key=key, bad=bad):
                self.assertEqual((await save(**{key: bad}))["status"], "error")
        self.assertFalse(writes)
        result = await save(**{field: chat_id})
        self.assertEqual(result["data"]["config"], {field: chat_id})
        self.assertEqual(writes[0][field], chat_id)

        decision = await guard.engine.decide(
            group_key="test", current_text="看看这张图", context_lines=[], bot_line="",
            is_private=False,
            resolve_provider=AsyncMock(return_value=Provider(NEED_IMAGE)),
            resolve_vision_provider=guard._resolve_vision_provider,
            resolve_main_provider=AsyncMock(),
            resolve_images=AsyncMock(return_value=[IMAGE]),
            message_meta={"has_images": True},
        )
        self.assertEqual((decision.stage, decision.action), ("vision", "reply"))
        call = chat.upstreams[0].client.text_chat
        call.assert_awaited_once()
        self.assertEqual(call.await_args.kwargs["image_urls"], [IMAGE["url"]])
        visual.upstreams[0].client.text_chat.assert_not_awaited()
        chat.text_chat.assert_not_awaited()

        with self.assertRaisesRegex(GateFailure, "main_no_vision"):
            await guard._resolve_main_vision_provider(GateEvent())
        self.assertEqual((await save(**{field: ""}))["data"]["config"], {field: ""})
        self.assertIsNone(await guard._resolve_vision_provider())

    async def test_manual_native_responses_sends_picture_and_does_not_retry_without_it(self):
        native = FakeProvider("native-responses", "deepseek-v4.1-flash")
        native.provider_config["type"] = "openai_responses"
        native._prepare_chat_payload = AsyncMock(return_value=({"input": []}, []))
        native._query = AsyncMock(side_effect=RuntimeError("fixture rejects pictures"))
        native.text_chat = AsyncMock()
        target = image_target(native, "native-responses", allow_unconfirmed=True)
        with self.assertRaisesRegex(RuntimeError, "rejects pictures"):
            await target.provider.text_chat(
                prompt="描述图片", system_prompt="规则", image_urls=[IMAGE["url"]], max_tokens=512)
        self.assertEqual(native._prepare_chat_payload.await_args.args[1], [IMAGE["url"]])
        native._query.assert_awaited_once()
        self.assertEqual(native._query.await_args.args[0]["max_output_tokens"], 512)
        native.text_chat.assert_not_awaited()

    async def test_manual_router_keeps_declared_capability_for_same_name_in_auto_route(self):
        text = SimpleNamespace(client=FakeProvider("text", "deepseek-v4.1-flash"), api_type="chat")
        vision = SimpleNamespace(client=FakeProvider("vision", "deepseek-v4.1-flash"), api_type="multimodal")
        router = FakeProvider("router", "automatic")
        router.text_chat = AsyncMock()
        router.upstreams = [text, vision]
        self.assertIs(image_target(router, "router", allow_unconfirmed=True).provider, vision.client)
        with self.assertRaises(GateFailure):
            image_target(router, "router::removed-model", allow_unconfirmed=True)
        router.upstreams.pop()
        with self.assertRaises(GateFailure):
            image_target(router, "router", allow_unconfirmed=True)
        self.assertIs(image_target(router, "router::deepseek-v4.1-flash",
                                   allow_unconfirmed=True).provider, text.client)


if __name__ == "__main__":
    unittest.main()
