"""同一API模型的思考模式只影响可读名称，不影响调用身份。"""
import copy
from types import SimpleNamespace
import unittest

from guard_webui import _native_model_display, _providers


class OfficialModelLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_labels_preserve_id_model_and_configuration(self):
        common = {"api_base": "https://api.deepseek.com/v1", "type": "openai_chat_completion"}
        configs = [
            common | {"id": "deepseek/deepseek-flash", "model": "deepseek-flash", "custom_extra_body": {}},
            common | {"id": "deepseek/deepseek-flash-quiet", "model": "deepseek-flash",
                      "custom_extra_body": {"thinking": {"type": "disabled"}}},
            common | {"id": "deepseek/deepseek-v4-pro", "model": "deepseek-v4-pro", "custom_extra_body": {}},
        ]
        original = copy.deepcopy(configs)
        providers = [SimpleNamespace(provider_config=c, get_model=lambda c=c: c["model"])
                     for c in configs]
        context = SimpleNamespace(
            get_all_providers=lambda: providers,
            get_provider_by_id=lambda pid: next(p for p in providers if p.provider_config["id"] == pid))
        result = await _providers(SimpleNamespace(context=context))
        self.assertEqual(len(result), 3)
        labels = {p["id"]: p for p in result}
        self.assertEqual(labels[configs[0]["id"]]["display"], "deepseek-flash（默认思考）")
        self.assertEqual(labels[configs[1]["id"]]["display"], "deepseek-flash（quiet 不思考）")
        self.assertEqual(labels[configs[2]["id"]]["display"], "deepseek-v4-pro（默认思考）")
        for config in configs:
            self.assertEqual(labels[config["id"]]["model"], config["model"])
        self.assertEqual(configs, original)

    def test_configuration_wins_over_quiet_name_and_custom_name_survives(self):
        config = {"api_base": "https://api.deepseek.com", "id": "flash-quiet",
                  "custom_extra_body": {"thinking": {"type": "enabled"}}}
        self.assertEqual(_native_model_display(config, "deepseek-flash", ""), "deepseek-flash（思考）")
        config["custom_extra_body"] = {"thinking": {"type": "disabled"}}
        self.assertEqual(_native_model_display(config, "deepseek-flash", "主模型"), "主模型（quiet 不思考）")
        config["custom_extra_body"] = {}
        self.assertEqual(_native_model_display(config, "deepseek-flash", ""), "deepseek-flash（默认思考）")

    def test_third_party_unknown_models_and_malformed_extra_unchanged(self):
        for endpoint in ("https://thirdparty.example/v1", "https://api.deepseek.com.example",
                         "https://api.deepseek.com@thirdparty.example", "http://[invalid"):
            self.assertEqual(_native_model_display({"api_base": endpoint}, "deepseek-flash", "原名"), "原名")
        for extra in (None, [], "{}", {"thinking": None}, {"thinking": "disabled"},
                      {"thinking": {"type": "unknown"}}):
            config = {"api_base": "https://api.deepseek.com", "custom_extra_body": extra}
            self.assertEqual(_native_model_display(config, "deepseek-flash", "原名"), "原名")
        self.assertEqual(_native_model_display({"api_base": "https://api.deepseek.com"},
                                             "deepseek-chat", "旧模型"), "旧模型")
