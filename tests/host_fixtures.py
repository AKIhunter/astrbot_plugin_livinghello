"""执行宿主源码，替换初始化与外部资源；不连接运行中的服务。"""
import ast
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import nullcontext
import copy
import logging
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Generic, TypeVar


def source():
    root = os.environ.get("ASTRBOT_NATIVE_SOURCE")
    if not root:
        raise ImportError("需要 ASTRBOT_NATIVE_SOURCE 宿主源码快照")
    return Path(root)


def compile_classes(relative, names, namespace):
    path = source() / relative
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, ast.ClassDef) and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise AssertionError(f"宿主接口变化：{relative}")
    nodes.insert(0, ast.ImportFrom(module="__future__",
                                 names=[ast.alias(name="annotations")], level=0))
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(path), "exec"), namespace)


def scheduler():
    native = ModuleType("_livinghello_native_scheduler_fixture")
    native.__dict__.update(
        AsyncGenerator=AsyncGenerator, cast=lambda _, value: value,
        log_context=lambda *_: nullcontext(),
        logger=logging.getLogger("livinghello-host-test"),
        active_event_registry=SimpleNamespace(register=lambda _: None, unregister=lambda _: None),
        WebChatMessageEvent=type("WebChatMessageEvent", (), {}),
        WecomAIBotMessageEvent=type("WecomAIBotMessageEvent", (), {}),
    )
    compile_classes("core/pipeline/scheduler.py", {"PipelineScheduler"}, native.__dict__)
    return native


def install_tools():
    """使用真实宿主工具类与真实参数校验，只隔离宿主启动依赖。"""
    if "astrbot.core.agent.tool" in sys.modules:
        return
    from pydantic import Field, model_validator
    from pydantic.dataclasses import dataclass
    import jsonschema
    native = ModuleType("astrbot.core.agent.tool")
    native.__dict__.update(
        copy=copy, AsyncGenerator=AsyncGenerator, Awaitable=Awaitable, Callable=Callable,
        Any=Any, Generic=Generic, TContext=TypeVar("TContext"), Field=Field,
        model_validator=model_validator, dataclass=dataclass, jsonschema=jsonschema,
        ParametersType=dict[str, Any], ToolExecResult=str,
        MessageEventResult=object,
        deprecated=lambda **_: lambda fn: fn,
    )
    sys.modules[native.__name__] = native
    try:
        compile_classes("core/agent/tool.py", {"ToolSchema", "FunctionTool", "ToolSet"}, native.__dict__)
    except Exception:
        sys.modules.pop(native.__name__, None)
        raise
    context = ModuleType("astrbot.core.astr_agent_context")
    context.AstrAgentContext = object
    sys.modules[context.__name__] = context


def dashboard_auth():
    """执行生产宿主的认证方法；不启动Web服务或读取真实密钥。"""
    import jwt
    from starlette.responses import JSONResponse
    namespace = {"jwt": jwt, "JSONResponse": JSONResponse,
                 "error": lambda message: {"status": "error", "message": message},
                 "cast": lambda _, value: value, "DASHBOARD_JWT_COOKIE_NAME": "dashboard_jwt"}
    path = source() / "dashboard/plugin_page_auth.py"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    path = source() / "dashboard/server.py"
    owner = next(n for n in ast.parse(path.read_text()).body
                 if isinstance(n, ast.ClassDef) and n.name == "AstrBotDashboard")
    names = {"auth_middleware", "_validate_dashboard_token", "_extract_dashboard_jwt"}
    methods = [n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in names]
    if {n.name for n in methods} != names:
        raise AssertionError("宿主认证方法变化")
    owner.body = methods
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        owner], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["AstrBotDashboard"]
