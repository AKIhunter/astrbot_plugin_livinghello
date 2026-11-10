"""路由声明的图能力（api_type=image_to_text/multimodal）应被收录为可看图。"""
import asyncio
import unittest
from types import SimpleNamespace

import guard_webui


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


if __name__ == "__main__":
    unittest.main()
