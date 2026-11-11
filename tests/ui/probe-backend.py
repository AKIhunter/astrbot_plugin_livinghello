"""UI隔离桥：运行真实服务端处理器，配置与模型均使用内存替身。"""
import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / "tests"), str(root)]
from run_offline_tests import install_framework_stubs
install_framework_stubs()
import guard_webui
from test_gate import plugin


async def main():
    payload = json.load(sys.stdin)
    inputs = []
    async def text_chat(**kwargs):
        prompt = kwargs["prompt"]
        data = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
        inputs.append(data)
        return SimpleNamespace(completion_text='{"act":"reply","conf":88,"understood":true,"reason":"useful","ground":"none"}')
    model = SimpleNamespace(text_chat=text_chat, provider_config={"id": "fixture-provider", "model": "fixture-text-small"})
    class Config(dict):
        def save_config(self):
            pass
    guard = plugin(Config(payload.get("config", {})))
    registry = {}
    for kind, connected in payload.get("connected", {}).items():
        if not connected:
            continue
        current = (SimpleNamespace(api=SimpleNamespace(api_version=1)) if kind == "livingemotion"
                   else SimpleNamespace(initializer=SimpleNamespace(memory_engine=object()),
                                        search_memories_for_event=lambda: None))
        registry["astrbot_plugin_" + kind] = SimpleNamespace(activated=True, star_cls=current)
    guard.context.get_registered_star = lambda name: registry.get(name)
    guard.context.get_provider_by_id = lambda _: model
    async def get_using(_):
        return model
    guard.context.get_using_provider_async = get_using
    guard._policy()
    before = copy.deepcopy(guard.engine.stats)
    async def body(**_):
        return payload.get("body", {})
    async def state(_):
        return {"config": dict(guard.config), "function_coupling": guard.get_function_coupling(), "message_policy": {
            **guard.get_message_policy_snapshot(), "companion": guard.message_policy_companion()}}
    with patch.object(guard_webui.request, "json", body, create=True), \
         patch.object(guard_webui, "error_response", side_effect=lambda message, status_code=400, data=None:
                      {"status": "error", "message": message, "code": status_code, "data": data}), \
         patch.object(guard_webui, "_providers", return_value=[{"id": "fixture-provider"}]), \
         patch.object(guard_webui, "_state", state), \
         patch.object(guard_webui, "_native_settings", return_value=SimpleNamespace(
             read=lambda _: {"config_id": "default", "values": {}})):
        route = payload.get("route")
        if route == "console/probe":
            response = await guard_webui.handle_probe(guard)
        elif route == "console/config":
            response = await guard_webui.handle_config(guard)
        elif route == "console/message-policy/validate":
            response = await guard_webui.handle_message_policy_validate(guard)
        elif route == "snapshot":
            response = {"status": "ok", "data": await state(guard)}
        else:
            raise ValueError("隔离桥路由不支持")
    if route == "console/probe":
        assert before == guard.engine.stats and not guard.engine._budget and not guard.engine.log.ring
    print(json.dumps({"response": response, "config": dict(guard.config),
                      "function_coupling": guard.get_function_coupling(),
                      "snapshot": (await state(guard))["message_policy"], "model_inputs": inputs},
                     ensure_ascii=False))


asyncio.run(main())
