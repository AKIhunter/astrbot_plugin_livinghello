"""Editable prompts: real save handler, real injection paths, isolated providers."""
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.message import TextPart
from console import guard_webui as api
from images.guard_image_settings import ImageSettings, TOOL_NAME
from reply import guard_prompt_injection as prompt
from reply.guard_native_reply import managed_regex
from reply.guard_segments import ReplySegments
import prompt_bundle as lh_bundle
from test_gate import plugin, gate_config

ROOT = Path(__file__).resolve().parents[1]
LE = ROOT.parent / "astrbot_plugin_livingemotion"
OLD_GENERAL = (
    "称呼沿用当前身份。按当前内容完整回应，不限制整次回复的总字数；该展开时说清楚，短答不硬凑内容。\n"
    "接眼前这句话，别加总结、评价、建议或新的话题。被叫到就应一声，不为续聊补一句「怎么了」。必要的澄清可以问，已经能回答时直接说。\n"
    "日常直接聊天，用说的话体现情绪，不写身体动作、旁白或自演场景；对方明确要角色扮演时再随场景表达。\n"
    "不把接话写成点评话语重量、宣告立场或承诺陪伴，不套用「我认」「我不认」「很重」「更重」「有压」「不接」「我守着」「这话有分量」「我兜着」。这些词确有实际含义时可以正常使用。开头和语气词自然变化。\n"
    "普通聊天不用书面标点，以空格表达停顿；代码、网址、数字格式保留。聊天不用列表、加粗、标题，代码和命令按实际需要排版，段落和气泡遵循当前回复设置。排版要求不约束记忆写入。"
)
OLD_SEGMENT = (
    "先完整回应当前内容，不限制整次回复的总字数。按意思自行分段，"
    "普通聊天每段尽量8–20字，每段之间用空行分隔；需要时可以多段，"
    "短答不硬凑字数或段数，代码、网址、表格及必要的完整解释不强拆。"
    "同一段内不要仅因出现句号就换行；不要输出分段编号或分隔标签。"
)
# Load the real LE modules without running the framework entry point.
package = ModuleType("_lhle_candidate")
package.__path__ = [str(LE)]
sys.modules[package.__name__] = package
class LazyPeer:
    """Load the real companion only in the explicitly selected peer suite."""
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return getattr(importlib.import_module("_lhle_candidate." + self.name), name)


le_bundle = LazyPeer("prompt_bundle")
le_coupling = LazyPeer("livinghello_coupling")


class Event:
    def __init__(self):
        self.extra = {}
        self.message_obj = NS(message_id="isolated-message")
        self.unified_msg_origin = "test:private:isolated"
    def get_extra(self, key, default=None):
        return self.extra.get(key, default)
    def set_extra(self, key, value):
        self.extra[key] = value


def request(*, memory=True):
    return NS(extra_user_content_parts=[], system_prompt="personality",
              func_tool=NS(tools=[NS(name="store_memory")] if memory else []))


def host(guard=None):
    return NS(get_registered_star=lambda name: (
        NS(activated=True, star_cls=guard) if guard is not None
        and name == "astrbot_plugin_LivingHello" else None))


class DiskConfig(dict):
    def __init__(self, path, **values):
        super().__init__(values)
        self.path = path
        self.fail = False
    def save_config(self):
        if self.fail:
            raise OSError("synthetic_disk_failure")
        self.path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8-sig")


class PromptTests(unittest.IsolatedAsyncioTestCase):
    def guard(self, config):
        guard = plugin(config)
        guard.context = host()
        return guard

    async def save(self, guard, body):
        with patch.object(api.request, "json", AsyncMock(return_value=body), create=True), \
                patch.object(api, "_outage_recovery", return_value=NS(
                    state=api.OutageState.HEALTHY, current_event=None)), \
                patch.object(api, "error_response", lambda message, **kw: {"error": message, **kw}), \
                patch.object(api, "_response", lambda data, *a: {"data": data}):
            return await api.handle_config(guard)

    def test_all_schema_backend_catalog_defaults_match(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text())
        catalog = prompt.catalog()
        items = catalog["shared"] + catalog["core"] + catalog["ordinary"]
        self.assertEqual(len(items), 9)
        self.assertEqual(len({item["stem"] for item in items}), 9)
        for item in items:
            stem = item["stem"]
            self.assertIs(schema[stem + "_enabled"]["default"], True)
            self.assertEqual(schema[stem + "_text"]["default"], item["default"])
            self.assertEqual(schema[stem + "_text"]["max_chars"], catalog["max_chars"])
            self.assertIn(stem + "_text", api.EDITABLE)
            self.assertIn(stem + "_enabled", api.EDITABLE)
        for stem, _, _ in prompt.LEGACY_PRESETS:
            self.assertFalse(schema[stem + "_enabled"]["default"])
        prompt.validate({})

    def test_legacy_four_remain_opt_in(self):
        text = prompt.expression_text({}, group=True)
        for _, title, _ in prompt.LEGACY_PRESETS:
            self.assertNotIn(title + "：", text)
        for stem, title, default in prompt.LEGACY_PRESETS:
            self.assertIn(title + "：", prompt.expression_text({stem + "_enabled": True}, group=True))

    def test_group_prompt_scope(self):
        config = {"general_prompt_enabled": False, "group_prompt_text": "群聊独有"}
        self.assertEqual(prompt.expression_text(config), "")
        self.assertIn("群聊独有", prompt.expression_text(config, group=True))

    def test_personality_defaults_and_custom_text(self):
        for variant in ("v3", "v4"):
            for stem in ("prompt_core", "prompt_memory", "general_prompt", "group_prompt"):
                stored = {stem + "_text": prompt.preset_default(stem, "v4")}
                self.assertEqual(prompt.preset_text(stored, stem, variant=variant),
                                 stored[stem + "_text"] if stem == "general_prompt"
                                 else prompt.preset_default(stem, variant))
                stored[stem + "_text"] = "手写  文本\n<标签> 😀"
                self.assertEqual(prompt.preset_text(stored, stem, variant=variant), stored[stem + "_text"])

    def test_shipped_prompt_migration_preserves_edits_and_disabled_text(self):
        for stem, old in (("general_prompt", OLD_GENERAL), ("prompt_segment", OLD_SEGMENT)):
            for variant in ("v3", "v4"):
                with self.subTest(stem=stem, variant=variant):
                    config = {stem + "_text": old}
                    self.assertEqual(prompt.preset_text(config, stem, variant=variant),
                                     prompt.preset_default(stem, variant))
                    self.assertEqual(config[stem + "_text"], old)
                    # Even a one-character user edit must not migrate.
                    config[stem + "_text"] = old + " "
                    self.assertEqual(prompt.preset_text(config, stem, variant=variant), old + " ")
                    config.update({stem + "_text": old, stem + "_enabled": False})
                    self.assertEqual(prompt.preset_text(config, stem, variant=variant), "")
                    self.assertEqual(config[stem + "_text"], old)
        # A historical hash for one field cannot migrate another field.
        self.assertEqual(prompt.preset_value({"prompt_segment_text": OLD_GENERAL}, "prompt_segment"),
                         OLD_GENERAL)

    async def test_invalid_mixed_save_has_no_partial_write(self):
        with tempfile.TemporaryDirectory() as folder:
            config = DiskConfig(Path(folder) / "config.json", **gate_config())
            config.save_config()
            guard = self.guard(config)
            before = dict(config), config.path.read_bytes()
            for body in (
                {"prompt_core_text": "改动", "prompt_image_order_enabled": "true"},
                {"prompt_memory_text": " ", "prompt_memory_enabled": True},
                {"prompt_segment_text": "x" * 10001},
                {"prompt_image_description_text": "\ud800"},
                {"prompt_paragraph_text": 3},
                {"prompt_image_prepared_text": "改动", "unknown": True},
            ):
                result = await self.save(guard, body)
                self.assertIn("error", result)
                self.assertEqual((dict(config), config.path.read_bytes()), before)

    async def test_save_failure_rolls_back_then_unrelated_save_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            config = DiskConfig(Path(folder) / "config.json", **gate_config())
            config.save_config()
            guard = self.guard(config)
            before = dict(config), config.path.read_bytes()
            config.fail = True
            result = await self.save(guard, {"prompt_core_text": "未落盘", "prompt_core_enabled": False})
            self.assertIn("error", result)
            self.assertEqual((dict(config), config.path.read_bytes()), before)
            config.fail = False
            result = await self.save(guard, {"general_prompt_text": "新保存"})
            self.assertTrue(result["data"]["saved"])
            self.assertNotIn("prompt_core_text", config)

    def test_shared_bundle_implementations_identical(self):
        self.assertEqual((ROOT / "prompt_bundle.py").read_bytes(), (LE / "prompt_bundle.py").read_bytes())

    def test_shared_hook_orders_custom_off_memory_gating_and_retry(self):
        for order in ("LH-first", "LE-first"):
            for memory in (False, True):
                for disabled in ((), ("prompt_core",), ("prompt_memory",), ("prompt_core", "prompt_memory")):
                    config = {stem + "_text": "共享自定义-" + stem for stem in ("prompt_core", "prompt_memory")}
                    config.update({stem + "_enabled": False for stem in disabled})
                    guard = self.guard(config)
                    context = host(guard)
                    req = request(memory=memory)
                    def lh():
                        prompt.attach_shared(guard, Event(), req, TextPart)
                    def le():
                        settings = le_coupling.shared_prompts(context, req, "v4")
                        modules = le_bundle.shared_modules("v4", core=True, memory=memory, settings=settings)
                        modules["le.state_explanation"] = "LE自身状态说明"
                        le_bundle.offer(req, "LE", modules, TextPart)
                    (lh if order == "LH-first" else le)()
                    (le if order == "LH-first" else lh)()
                    before = [p.text for p in req.extra_user_content_parts]
                    config.update(prompt_core_text="下一轮才生效", prompt_core_enabled=True,
                                  prompt_memory_text="下一轮记忆", prompt_memory_enabled=True)
                    lh(); le()
                    self.assertEqual([p.text for p in req.extra_user_content_parts], before)
                    self.assertEqual(len(before), 1)
                    body = before[0]
                    self.assertEqual(body.count("[shared.context]"), int("prompt_core" not in disabled))
                    self.assertEqual(body.count("[shared.memory]"),
                                     int(memory and "prompt_memory" not in disabled))
                    self.assertIn("LE自身状态说明", body)
                    self.assertTrue(req.extra_user_content_parts[0].no_save)
                    self.assertNotIn("下一轮", body)

    def test_standalone_old_disabled_peer_and_aliases(self):
        for context in (host(), host(NS()), NS()):
            req = request()
            self.assertIsNone(le_coupling.shared_prompts(context, req, "v4"))
            modules = le_bundle.shared_modules("v4", core=False, memory=True, settings=None)
            self.assertNotIn("shared.context", modules)
            self.assertIn("shared.memory", modules)
        guard = self.guard({"prompt_core_enabled": False, "prompt_memory_enabled": False})
        for alias in le_coupling.NAMES:
            context = NS(get_registered_star=lambda name: NS(activated=True, star_cls=guard)
                         if name == alias else None)
            self.assertEqual(le_coupling.shared_prompts(context, request(), "v4"),
                             {"shared.context": "", "shared.memory": ""})
        disabled = NS(get_registered_star=lambda name: NS(activated=False, star_cls=guard))
        self.assertIsNone(le_coupling.shared_prompts(disabled, request(), "v4"))

    def test_shared_contract_rejects_bad_producers(self):
        for value in ("bad", {}, {"contract": "other", "modules": {}},
                      {"contract": "livinghello.shared-prompts.v1", "modules": {
                          "shared.context": "\ud800", "shared.memory": ""}}):
            with self.assertRaises(ValueError):
                lh_bundle.request_shared_settings(request(), value)

    async def test_actual_le_application_retains_dynamic_state_and_relationship(self):
        application = importlib.import_module("_lhle_candidate.runtime.application")
        adapter = importlib.import_module("_lhle_candidate.adapters.astrbot")
        class Host:
            def __init__(self, guard):
                self.guard = guard
                self.persona_manager = self.conversation_manager = self
            def get_registered_star(self, name):
                return host(self.guard).get_registered_star(name)
            async def get_curr_conversation_id(self, route):
                return None
            async def resolve_selected_persona(self, **kwargs):
                return "native", {"prompt": "离线人格"}, None, False
            async def get_current_chat_provider_id(self, route):
                raise ValueError("isolated_no_provider")
            async def send_message(self, route, chain):
                raise AssertionError("isolated_send_forbidden")
        class Bridge(adapter.AstrBotBridge):
            text_part = staticmethod(TextPart)
            chain = staticmethod(lambda text: text)
        class Message(Event):
            def __init__(self):
                super().__init__()
                self.platform_meta = NS(id="adapter", name="aiocqhttp")
                self.message_obj.timestamp = 0
                self.message_str = "离线消息"
                self.unified_msg_origin = "adapter:FriendMessage:isolated"
            def get_self_id(self):
                return "bot"
            def get_sender_id(self):
                return "isolated"
            def get_group_id(self):
                return ""
            def stop_event(self):
                self.stopped = True
        for disabled in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                guard = self.guard({
                    "prompt_core_enabled": not disabled, "prompt_core_text": "LH控制的共享说明",
                    "prompt_memory_enabled": not disabled, "prompt_memory_text": "LH控制的记忆写入"})
                app = application.Application(Host(guard), {}, Path(folder), bridge_factory=Bridge)
                try:
                    await app.initialize()
                    event, req = Message(), request()
                    await app.on_message(event)
                    await app.on_llm_request(event, req)
                    body = "\n".join(p.text for p in req.extra_user_content_parts)
                    self.assertIn(application.SUFFIX, body)
                    self.assertIn('"relationship"', body)
                    self.assertIn("[le.state_explanation]", body)
                    self.assertIn("[le.relationship_alignment]", body)
                    self.assertEqual(body.count("[shared.context]"), int(not disabled))
                    self.assertEqual(body.count("[shared.memory]"), int(not disabled))
                    if not disabled:
                        self.assertIn("LH控制的共享说明", body)
                        self.assertIn("LH控制的记忆写入", body)
                    prompt.attach_shared(guard, event, req, TextPart)
                    body = "\n".join(p.text for p in req.extra_user_content_parts)
                    self.assertEqual(body.count("[shared.context]"), int(not disabled))
                    self.assertEqual(body.count("[shared.memory]"), int(not disabled))
                finally:
                    await app.terminate()

    def test_request_config_is_frozen_and_new_request_sees_save(self):
        config = {"prompt_segment_text": "本轮", "unrelated": "不传递"}
        req = request()
        self.assertEqual(prompt.request_config(req, config), {"prompt_segment_text": "本轮"})
        config["prompt_segment_text"] = "新一轮"
        self.assertEqual(prompt.request_config(req, config)["prompt_segment_text"], "本轮")
        self.assertEqual(prompt.request_config(request(), config)["prompt_segment_text"], "新一轮")

    def test_segment_and_paragraph_actual_attachment(self):
        for only_llm, stem in ((True, "prompt_segment"), (False, "prompt_paragraph")):
            for enabled in (True, False):
                raw = dict(split_mode="regex", regex=managed_regex(), only_llm_result=only_llm)
                guard = NS(config={stem + "_enabled": enabled, stem + "_text": "自定义分段"},
                           context=NS(get_config=lambda _: {"platform_settings": {"segmented_reply": raw}}))
                segments = ReplySegments(guard)
                event, req = Event(), request()
                segments.attach(event, req); segments.attach(event, req)
                self.assertEqual([p.text for p in req.extra_user_content_parts],
                                 ["自定义分段"] if enabled else [])
                if enabled:
                    self.assertTrue(req.extra_user_content_parts[0].no_save)
                other = request()
                segments.attach(event, other)
                self.assertEqual(len(other.extra_user_content_parts), int(enabled))

    def image_service(self, folder, config):
        guard = NS(config=config, context=host(), data_dir=Path(folder),
                   _livinghello_detached_enabled=True, logger=logging.getLogger("isolated-image"))
        service = ImageSettings(guard)
        service.values.update(enabled=True, daily_limit=20)
        service.refresh = lambda: None
        return service

    def test_tool_descriptor_and_order_isolation_schema_and_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"prompt_image_description_text": "第一份说明", "prompt_image_order_text": "先检查"}
            service = self.image_service(folder, config)
            cached = service.ensure_tool()
            event, req = Event(), request(memory=False)
            service.attach(event, req)
            first_tool = req.func_tool.tools[0]
            self.assertEqual(first_tool.description, "第一份说明")
            self.assertEqual(first_tool.parameters, cached.parameters)
            self.assertIs(first_tool.service, service)
            self.assertEqual(req.system_prompt, "personality\n先检查")
            config.update(prompt_image_description_text="新说明", prompt_image_order_text="新顺序")
            service.attach(event, req)
            self.assertEqual(req.func_tool.tools[0].description, "第一份说明")
            self.assertEqual(req.system_prompt.count("先检查"), 1)
            req2 = request(memory=False)
            service.attach(Event(), req2)
            self.assertEqual(req2.func_tool.tools[0].description, "新说明")
            self.assertEqual(first_tool.description, "第一份说明")
            self.assertEqual(cached.description, prompt.IMAGE_DESCRIPTION)
            config.update(prompt_image_description_enabled=False, prompt_image_order_enabled=False)
            req3 = request(memory=False)
            service.attach(Event(), req3)
            self.assertEqual(req3.func_tool.tools[0].name, TOOL_NAME)
            self.assertEqual(req3.func_tool.tools[0].description, "")
            self.assertEqual(req3.system_prompt, "personality")
            self.assertEqual(req3.func_tool.tools[0].parameters, cached.parameters)

    async def test_prepared_receipt_switch_custom_and_frozen_config(self):
        for enabled in (True, False):
            with tempfile.TemporaryDirectory() as folder:
                config = {"prompt_image_prepared_enabled": enabled, "prompt_image_prepared_text": "预检后的自定义"}
                service = self.image_service(folder, config)
                service.blocked = lambda _: False
                service.connection = lambda _: None
                service.used = lambda: 0
                event = Event()
                service.attach(event, request(memory=False))
                config["prompt_image_prepared_text"] = "下一轮"
                with patch("integrations.guard_livingemotion.current_emotion", AsyncMock(return_value=None)), \
                        patch("images.guard_image_settings.image_permission", AsyncMock(return_value=None)):
                    text = await service.generate(event, "离线猫", False)
                self.assertEqual(text, "预检后的自定义" if enabled else "")
                self.assertEqual(event.get_extra("_livinghello_image_job")["status"], "prepared")


def add_stem_tests(stem):
    def default_on(self):
        self.assertEqual(prompt.preset_text({}, stem), prompt.preset_default(stem))
    def off_retains(self):
        config = {stem + "_enabled": False, stem + "_text": "保留  编辑\n😀"}
        before = dict(config)
        self.assertEqual(prompt.preset_text(config, stem), "")
        self.assertEqual(config, before)
        prompt.validate(config)
    def custom_and_restore(self):
        config = {stem + "_enabled": True, stem + "_text": "手写  二次\n<tag> 😀"}
        self.assertEqual(prompt.preset_text(config, stem), config[stem + "_text"])
        config[stem + "_text"] = prompt.preset_default(stem)
        self.assertEqual(prompt.preset_text(config, stem), prompt.preset_default(stem))
    def invalid_runtime_and_validation(self):
        for key, value in ((stem + "_enabled", 1), (stem + "_enabled", "false"),
                           (stem + "_text", " "), (stem + "_text", "\udfff"),
                           (stem + "_text", "😀" * 10001), (stem + "_text", None)):
            config = {key: value}
            with self.assertRaises(ValueError):
                prompt.validate(config)
            self.assertEqual(prompt.preset_text(config, stem), "")
        self.assertEqual(len(prompt.preset_text({stem + "_text": "😀" * 10000}, stem)), 10000)
    async def save_edit_again_off_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            config = DiskConfig(Path(folder) / "config.json", **gate_config())
            guard = self.guard(config)
            for enabled, text in ((True, "第一版  原样\n😀"), (True, "第二版"),
                                  (False, "第二版"), (False, ""),
                                  (True, prompt.preset_default(stem))):
                result = await self.save(guard, {stem + "_enabled": enabled, stem + "_text": text})
                self.assertTrue(result["data"]["saved"], result)
                persisted = json.loads(config.path.read_text("utf-8-sig"))
                self.assertEqual(persisted[stem + "_text"], text)
                self.assertIs(persisted[stem + "_enabled"], enabled)
                self.assertEqual(prompt.preset_text(persisted, stem), text if enabled else "")
    for name, method in locals().copy().items():
        if callable(method):
            setattr(PromptTests, "test_" + stem + "_" + name, method)


for stem, _, _ in prompt.VISIBLE_PRESETS:
    add_stem_tests(stem)


class PeerPromptTests(PromptTests):
    """Explicit: scripts/run_boundary_tests.py --test peer_prompt_presets.PeerPromptTests."""


for _peer_name in (
    "test_shared_bundle_implementations_identical",
    "test_shared_hook_orders_custom_off_memory_gating_and_retry",
    "test_standalone_old_disabled_peer_and_aliases",
    "test_actual_le_application_retains_dynamic_state_and_relationship",
):
    setattr(PeerPromptTests, _peer_name, getattr(PromptTests, _peer_name))
    delattr(PromptTests, _peer_name)
