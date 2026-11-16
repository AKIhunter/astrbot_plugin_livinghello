"""LivingMemory 原生命名空间适配的离线回归。

原生 2.12 没有 fork 的 search_memories_for_event，只能先用
resolve_event_memory_access 解析账号作用域，再直连 memory_engine。
这些用例锁定三条路径的优先级与检索后的二次复核。
"""
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_gate import gate_config, plugin
from test_reply_delivery import Event

MEMORY = "astrbot_plugin_livingmemory"
PERSONA = "大小姐"
SCOPE = "livingmemory:user:qq:alice"
ADAPTER = "qq-main"


class Access:
    """原生 resolve_event_memory_access 的返回值替身。"""

    def __init__(self, scope=SCOPE, persona_id=PERSONA, allowed=True):
        self.allowed = allowed
        self.persona_id = persona_id
        self.scope = scope


class NativeScope:
    """按调用次数依次返回授权，用来模拟检索期间作用域变化。"""

    def __init__(self, *accesses, visible_adapter=ADAPTER):
        self.accesses = list(accesses) or [Access()]
        self.calls = 0
        self.adapter_checks = 0
        self.visible_adapter = visible_adapter

    async def resolve_event_memory_access(self, config, context, event):
        self.calls += 1
        return self.accesses[min(self.calls - 1, len(self.accesses) - 1)]

    @contextlib.contextmanager
    def adapter_access(self, config, event, *, write=False):
        yield

    def adapter_visible(self, metadata):
        self.adapter_checks += 1
        return metadata.get("adapter_id") == self.visible_adapter


class Engine:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def search_memories(self, **kwargs):
        self.calls.append(kwargs)
        return self.results


class Utils:
    def __init__(self, persona=PERSONA):
        self.persona = persona

    async def get_persona_id(self, context, event):
        return self.persona


def result(text="昨天讨论的任务是修复日志", **metadata):
    values = {"status": "active", "persona_id": PERSONA,
              "session_id": SCOPE, "adapter_id": ADAPTER}
    values.update(metadata)
    return SimpleNamespace(content=text, metadata=values)


def importer(utils=None, scope=None, asked=None):
    def load(name):
        if asked is not None:
            asked.append(name)
        if utils is not None and name.endswith(".core.utils"):
            return utils
        if scope is not None and name.endswith(".core.memory_scope"):
            return scope
        raise ImportError(name)  # request_timing 等可选模块按缺失处理

    return load


class NativeMemoryScopeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = plugin(gate_config())
        self.guard.config["coupling_livingmemory_enabled"] = True
        self.event = Event()
        self.req = SimpleNamespace(extra_user_content_parts=[])
        self.registry = {}
        self.guard.context.get_registered_star = self.registry.get

    def install(self, living):
        self.registry[MEMORY] = SimpleNamespace(activated=True, star_cls=living)

    def living(self, engine, **attributes):
        return SimpleNamespace(
            initializer=SimpleNamespace(memory_engine=engine),
            config_manager=SimpleNamespace(), context=SimpleNamespace(),
            **attributes)

    async def recall(self, utils=None, scope=None, asked=None):
        with patch("main.importlib.import_module",
                   side_effect=importer(utils or Utils(), scope, asked)):
            return await self.guard._recall_gate_memory(self.event, self.req)

    async def test_native_scope_search_hits_with_account_scope_and_persona(self):
        engine = Engine([result()])
        scope = NativeScope(Access())
        self.install(self.living(engine))
        self.assertEqual(await self.recall(scope=scope), ["昨天讨论的任务是修复日志"])
        self.assertEqual(engine.calls[0]["session_id"], SCOPE)
        self.assertEqual(engine.calls[0]["persona_id"], PERSONA)
        # 检索前后各解析一次：第二次用于确认授权没有被改掉。
        self.assertEqual(scope.calls, 2)

    async def test_native_cross_account_result_is_dropped(self):
        engine = Engine([result(session_id="livingmemory:user:qq:bob")])
        self.install(self.living(engine))
        self.assertEqual(await self.recall(scope=NativeScope(Access())), [])

    async def test_native_persona_mismatch_is_dropped(self):
        engine = Engine([result(persona_id="二小姐")])
        self.install(self.living(engine))
        self.assertEqual(await self.recall(scope=NativeScope(Access())), [])

    async def test_native_adapter_mismatch_is_dropped(self):
        engine = Engine([result(adapter_id="qq-other")])
        scope = NativeScope(Access())
        self.install(self.living(engine))
        self.assertEqual(await self.recall(scope=scope), [])
        self.assertEqual(scope.adapter_checks, 1)

    async def test_native_scope_changed_during_search_discards_results(self):
        engine = Engine([result()])
        scope = NativeScope(Access(), Access(scope="livingmemory:user:qq:mallory"))
        self.install(self.living(engine))
        self.assertEqual(await self.recall(scope=scope), [])
        self.assertEqual(len(engine.calls), 1)

    async def test_native_denied_or_unknown_scope_never_searches(self):
        for access in (Access(allowed=False), Access(scope=None)):
            self.registry.clear()
            engine = Engine([result()])
            scope = NativeScope(access)
            self.install(self.living(engine))
            self.assertEqual(await self.recall(scope=scope), [],
                             msg=f"access={access.__dict__}")
            self.assertEqual(engine.calls, [])

    async def test_native_persona_disagreement_never_searches(self):
        engine = Engine([result()])
        self.install(self.living(engine))
        self.assertEqual(await self.recall(utils=Utils("大小姐"),
                                           scope=NativeScope(Access(persona_id="二小姐"))), [])
        self.assertEqual(engine.calls, [])

    async def test_fork_entry_still_wins_when_present(self):
        engine = Engine([result()])
        unified = AsyncMock(return_value=[result()])
        asked = []
        self.install(self.living(engine, search_memories_for_event=unified))
        self.assertEqual(await self.recall(scope=NativeScope(Access()), asked=asked),
                         ["昨天讨论的任务是修复日志"])
        unified.assert_awaited_once()
        self.assertEqual(engine.calls, [])
        self.assertFalse([name for name in asked if name.endswith(".core.memory_scope")])

    async def test_neither_native_nor_fork_falls_back_to_legacy_session_scope(self):
        engine = Engine([result(session_id="group:1001")])
        # 没有 config_manager 就没有原生作用域契约，只能按消息路由检索。
        self.install(SimpleNamespace(initializer=SimpleNamespace(memory_engine=engine)))
        self.assertEqual(await self.recall(), ["昨天讨论的任务是修复日志"])
        self.assertEqual(engine.calls[0]["session_id"], "group:1001")

    async def test_missing_plugin_or_engine_returns_no_memory(self):
        self.assertEqual(await self.recall(), [])

        self.install(self.living(None))
        self.assertEqual(await self.recall(scope=NativeScope(Access())), [])


if __name__ == "__main__":
    unittest.main()
