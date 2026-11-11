"""真实入口的隔离验证：停用、重载、身份复核和请求局部工具开关。"""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import guard_webui
from guard_coupling import clean_request
from guard_livingemotion import state_lines
from test_gate import gate_config, plugin
from test_livingemotion_handoff import API, PLUGIN, STATE, meta, scope
from test_reply_delivery import Event


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class CouplingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = plugin(Config(gate_config()))
        self.registry = {PLUGIN: meta(API())}
        self.guard.context.get_registered_star = self.registry.get
        self.event = Event()
        self.event.extra["livingemotion:context"] = scope(self.event)
        self.req = SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text=STATE)])

    async def asyncTearDown(self):
        await self.guard.terminate()

    async def test_default_off_does_not_read_state_or_block(self):
        api = self.registry[PLUGIN].star_cls.api
        self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [])
        self.assertEqual(await self.guard._livingemotion_block_reason(self.event), "")
        api.get_capabilities.assert_not_awaited()
        await self.guard.listen_livingemotion(self.event, self.req)
        self.assertEqual(self.req.extra_user_content_parts, [])

    async def test_connected_disabled_and_reload_are_discovered_each_time(self):
        self.guard.config["coupling_livingemotion_enabled"] = True
        row = lambda: self.guard.get_function_coupling()["integrations"]["livingemotion"]
        self.assertTrue(row()["effective"])
        self.registry[PLUGIN].activated = False
        self.assertFalse(row()["connected"])
        self.assertFalse(row()["effective"])
        self.registry[PLUGIN] = meta(API())
        self.assertTrue(row()["effective"])
        self.registry[PLUGIN].star_cls.application = SimpleNamespace(
            closed=False, config=SimpleNamespace(enabled=False))
        self.assertFalse(row()["connected"])

    async def test_switch_off_while_awaiting_capabilities_discards_state(self):
        self.guard.config["coupling_livingemotion_enabled"] = True
        api = self.registry[PLUGIN].star_cls.api
        async def caps():
            self.guard.config["coupling_livingemotion_enabled"] = False
            return api.caps
        api.get_capabilities.side_effect = caps
        self.assertEqual(await state_lines(self.guard.context, self.event, self.req,
                                          config=self.guard.config), [])

    async def test_switch_off_while_awaiting_availability_does_not_block(self):
        self.guard.config["coupling_livingemotion_enabled"] = True
        api = self.registry[PLUGIN].star_cls.api
        async def availability(_):
            self.guard.config["coupling_livingemotion_enabled"] = False
            return dict(api_version=1, status="ok", available=False, block_chat=True)
        api.get_availability.side_effect = availability
        self.assertEqual(await self.guard._livingemotion_block_reason(self.event), "")

    async def test_scoped_v1_capabilities_are_used(self):
        self.guard.config["coupling_livingemotion_enabled"] = True
        api = self.registry[PLUGIN].star_cls.api
        seen = []
        async def caps(context=None):
            seen.append(context)
            return api.caps
        api.get_capabilities = caps
        self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [STATE])
        self.assertEqual(seen, [scope(self.event)])

    async def test_backend_rejects_unconnected_enable_but_allows_disable(self):
        for kind in ("livingemotion", "livingmemory"):
            key = f"coupling_{kind}_enabled"
            self.registry.clear()
            self.guard.config[key] = True
            for value in (True, False):
                with patch.object(guard_webui.request, "json", AsyncMock(return_value={key:value}), create=True), \
                     patch.object(guard_webui, "_providers", AsyncMock(side_effect=AssertionError("不应读模型目录"))), \
                     patch.object(guard_webui, "_failure", side_effect=lambda message, status_code=400:
                                  {"status":"error","code":status_code}):
                    response = await guard_webui.handle_config(self.guard)
                self.assertEqual(response["status"], "error" if value else "ok")
                self.assertEqual(self.guard.config[key], value)

    async def test_backend_saves_both_connected_flags(self):
        self.registry["astrbot_plugin_livingmemory"] = meta(None)
        self.registry["astrbot_plugin_livingmemory"].star_cls = SimpleNamespace(
            initializer=SimpleNamespace(memory_engine=object()),
            search_memories_for_event=AsyncMock(return_value=[]))
        flags = {f"coupling_{kind}_enabled":True for kind in ("livingemotion","livingmemory")}
        with patch.object(guard_webui.request, "json", AsyncMock(return_value=flags), create=True):
            response = await guard_webui.handle_config(self.guard)
        self.assertEqual(response["status"], "ok")
        self.assertTrue(all(self.guard.get_function_coupling()["integrations"][k]["effective"]
                            for k in ("livingemotion","livingmemory")))

    async def test_memory_reload_after_search_discards_results(self):
        self.guard.config["coupling_livingmemory_enabled"] = True
        async def search(*_, **__):
            self.registry.pop("astrbot_plugin_livingmemory")
            return [SimpleNamespace(content="旧实例结果", metadata={"persona_id":"大小姐"})]
        living = SimpleNamespace(initializer=SimpleNamespace(memory_engine=object()),
                                 search_memories_for_event=search)
        self.registry["astrbot_plugin_livingmemory"] = SimpleNamespace(activated=True, star_cls=living)
        utils = SimpleNamespace(get_persona_id=AsyncMock(return_value="大小姐"))
        with patch("main.importlib.import_module", return_value=utils):
            self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [])

    async def test_memory_identity_is_revalidated_before_use(self):
        self.guard.config["coupling_livingmemory_enabled"] = True
        result = SimpleNamespace(content="已撤销授权", metadata={"persona_id":"大小姐"})
        living = SimpleNamespace(initializer=SimpleNamespace(memory_engine=object()),
                                 search_memories_for_event=AsyncMock(return_value=[result]),
                                 validate_identity_results=AsyncMock(return_value=[]))
        self.registry["astrbot_plugin_livingmemory"] = SimpleNamespace(activated=True, star_cls=living)
        with patch("main.importlib.import_module",
                   return_value=SimpleNamespace(get_persona_id=AsyncMock(return_value="大小姐"))):
            self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [])
        living.validate_identity_results.assert_awaited_once_with([result])

    async def test_memory_switch_off_during_persona_resolution_prevents_search(self):
        self.guard.config["coupling_livingmemory_enabled"] = True
        living = SimpleNamespace(initializer=SimpleNamespace(memory_engine=object()),
                                 search_memories_for_event=AsyncMock(return_value=[]))
        self.registry["astrbot_plugin_livingmemory"] = SimpleNamespace(activated=True, star_cls=living)
        async def persona(*_):
            self.guard.config["coupling_livingmemory_enabled"] = False
            return "大小姐"
        with patch("main.importlib.import_module", return_value=SimpleNamespace(get_persona_id=persona)):
            self.assertEqual(await self.guard._recall_gate_memory(self.event, self.req), [])
        living.search_memories_for_event.assert_not_awaited()

    async def test_memory_tools_do_not_mutate_shared_toolset(self):
        tools = [SimpleNamespace(name=name) for name in
                 ("recall_long_term_memory", "read_memory_evidence", "knowledge_search",
                  "memorize_long_term_memory", "congling_self_memory", "congling_dream_recall")]
        shared = SimpleNamespace(tools=tools)
        self.req.func_tool = shared
        self.req.extra_user_content_parts += [SimpleNamespace(text="<RAG-Faiss-Memory>old")]
        await self.guard.listen_livingmemory(self.event, self.req)
        self.assertIsNot(self.req.func_tool, shared)
        self.assertEqual(len(shared.tools), 6)
        self.assertEqual([t.name for t in self.req.func_tool.tools],
                         ["knowledge_search", "memorize_long_term_memory"])
        self.assertEqual([p.text for p in self.req.extra_user_content_parts], [STATE])

    async def test_private_freshness_window_is_published_by_contract(self):
        row = self.guard.get_function_coupling()["reply_freshness"]
        self.assertEqual(row["contract"], "livinghello.reply-freshness.v1")
        self.assertEqual(row["private_watermark_window_seconds"], 15.0)
        self.guard.config["private_watermark_window_seconds"] = 99
        self.assertEqual(self.guard.get_function_coupling()["reply_freshness"]
                         ["private_watermark_window_seconds"], 60.0)
        spec = importlib.util.spec_from_file_location(
            "_freshness_test", Path(__file__).resolve().parents[2]
            / "astrbot_plugin_livingemotion/livinghello_coupling.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.registry.clear()
        self.registry["astrbot_plugin_at_guard"] = SimpleNamespace(
            activated=True, star_cls=self.guard)
        # 只经 couplings 契约取值，不读本插件配置。
        self.assertEqual(module.reply_freshness_window(self.guard.context), 60.0)
        self.registry["astrbot_plugin_at_guard"].activated = False
        self.assertEqual(module.reply_freshness_window(self.guard.context), 0.0)

    async def test_companion_policies_follow_alias_and_deactivation(self):
        for name in ("astrbot_plugin_LivingHello", "astrbot_plugin_livinghello"):
            with self.subTest(name=name):
                await self._check_companion_alias(name)

    async def _check_companion_alias(self, name):
        root = Path(__file__).resolve().parents[2]
        self.registry.clear()
        self.registry[name] = SimpleNamespace(activated=True, star_cls=self.guard)
        for relative in ("astrbot_plugin_livingemotion/livinghello_coupling.py",
                         "LivingMemory/core/livinghello_coupling.py",
                         "astrbot_plugin_congling/core/livinghello_coupling.py"):
            spec = importlib.util.spec_from_file_location("_coupling_test", root / relative)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "policy"):
                self.assertTrue(module.policy(self.guard.context)["managed"])
            else:
                self.assertFalse(module.allowed(self.guard.context))
            original = self.guard.get_function_coupling
            self.guard.get_function_coupling = lambda: (_ for _ in ()).throw(RuntimeError("合成策略故障"))
            if hasattr(module, "policy"):
                self.assertTrue(module.policy(self.guard.context)["managed"])
                self.assertEqual(module.policy(self.guard.context)["integrations"], {})
            else:
                self.assertFalse(module.allowed(self.guard.context))
            self.guard.get_function_coupling = original
        self.registry[name].activated = False
        self.assertTrue(module.policy(self.guard.context) is None)

    async def test_congling_memory_hook_yields_when_managed(self):
        root = Path(__file__).resolve().parents[2] / "astrbot_plugin_congling"
        tree = ast.parse((root / "main.py").read_text(encoding="utf-8-sig"))
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        method = next(node for node in owner.body if getattr(node, "name", "") == "memory_hook")
        method.decorator_list = []
        namespace = {"AstrMessageEvent": object, "Any": object}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "memory_hook", "exec"), namespace)
        cg = SimpleNamespace(function_coupling_managed=lambda:True,
                             enabled_for=lambda _:True, memory=SimpleNamespace(inject=AsyncMock()))
        await namespace["memory_hook"](cg, self.event, self.req)
        cg.memory.inject.assert_not_awaited()

    async def test_congling_dynamic_state_yields_but_scene_and_persona_remain(self):
        from test_congling_expression_handoff import methods
        actual = methods("humanize/persona.py", ("inject",))
        cg = SimpleNamespace(
            function_coupling_managed=lambda:True,
            probe=SimpleNamespace(should_yield=lambda _:False),
            emotion=SimpleNamespace(prompt_line=AsyncMock()))
        persona = SimpleNamespace(
            plugin=cg, config=SimpleNamespace(bool=lambda *args:True),
            resolve=AsyncMock(return_value={"token":"native"}),
            _replace_framework_persona=AsyncMock(),
            _relation_line=AsyncMock(), _state_line=AsyncMock(),
            _scene_line=lambda _:"场景：群聊")
        req = SimpleNamespace(conversation=None, system_prompt="", parts=[])
        await actual.inject(persona, self.event, req)
        persona._relation_line.assert_not_awaited()
        persona._state_line.assert_not_awaited()
        cg.emotion.prompt_line.assert_not_awaited()
        self.assertIn("congling:persona", req.system_prompt)
        self.assertEqual(req.parts[0][0], "场景：群聊")

    async def test_congling_recall_tool_rejects_before_cache_access(self):
        root = Path(__file__).resolve().parents[2] / "astrbot_plugin_congling"
        tree = ast.parse((root / "capability/registry.py").read_text(encoding="utf-8-sig"))
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "invoke")
        namespace = {"Any":object,"CapabilityResult":SimpleNamespace}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "invoke", "exec"), namespace)
        registry = SimpleNamespace(plugin=SimpleNamespace(function_coupling_managed=lambda:True),
                                   _cache_get=AsyncMock())
        for name in ("self.memory","dream.recall"):
            result = await namespace["invoke"](registry,name,event=self.event)
            self.assertEqual(result.error,"delegated")
        registry._cache_get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
