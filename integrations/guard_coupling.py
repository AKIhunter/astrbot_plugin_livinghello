"""功能耦合的实时注册表契约；只读元数据，不缓存实例或访问模型目录。"""
from __future__ import annotations

import copy

CONTRACT = "livinghello.function-coupling.v1"
SPECS = {
    "livingemotion": ("LivingEmotion", "coupling_livingemotion_enabled",
                      "https://github.com/AKIhunter/astrbot_plugin_livingemotion"),
    "livingmemory": ("LivingMemory", "coupling_livingmemory_enabled",
                     "https://github.com/AKIhunter/astrbot_plugin_livingmemory"),
}
MEMORY_TOOLS = {"recall_long_term_memory", "read_memory_evidence"}


def instance(context, kind):
    try:
        meta = context.get_registered_star("astrbot_plugin_" + kind)
        return meta.star_cls if meta and meta.activated else None
    except Exception:
        return None


def snapshot(context, config):
    rows = {}
    for kind, (name, key, url) in SPECS.items():
        current = instance(context, kind)
        ready = current is not None
        reason = "插件未加载或已停用"
        if ready and kind == "livingemotion":
            api = getattr(current, "api", None)
            app = vars(current).get("application")
            ready = (getattr(api, "api_version", None) == 1
                     and not getattr(app, "closed", False)
                     and getattr(getattr(app, "config", None), "enabled", True) is not False)
            reason = "插件未就绪或已关闭"
        elif ready:
            engine = getattr(getattr(current, "initializer", None), "memory_engine", None)
            ready = (engine is not None and (
                callable(getattr(current, "search_memories_for_event", None))
                or callable(getattr(engine, "search_memories", None))))
            reason = "记忆引擎尚未就绪"
        rows[kind] = dict(name=name, key=key, url=url, connected=ready,
                          enabled=config.get(key, False) is True,
                          effective=ready and config.get(key, False) is True,
                          reason="" if ready else reason)
    return dict(contract=CONTRACT, managed=True, integrations=rows)


def enabled(context, config, kind):
    return snapshot(context, config)["integrations"][kind]["effective"]


def clean_request(req, *, emotion, memory):
    """末端清理只改当前请求；ToolSet 可来自宿主共享注册表，必须先复制。"""
    prefixes = []
    if not emotion:
        prefixes += ["<!--livingemotion:state:v1-->", "<!--livingemotion:crawled_history:v2-->"]
    if not memory:
        prefixes += ["<RAG-Faiss-Memory>", "<Agent-Recall-Policy>",
                     "<at_guard_memory", "<!--livingemotion:memory:v2-->"]
    # 从绫动态层由协作入口让位；清理旧请求残留，固定人设和感知仍保留。
    prefixes += ["<!--congling:state-->", "<!--congling:memory-->",
                 "<!--congling:crawled_history-->", "<!--congling:wake_reply-->",
                 "<!--congling:night-->"]
    parts = getattr(req, "extra_user_content_parts", None)
    if isinstance(parts, list):
        parts[:] = [p for p in parts if not getattr(p, "text", "").startswith(tuple(prefixes))]
    tools = getattr(req, "func_tool", None)
    blocked_tools = {"congling_self_memory", "congling_dream_recall"}
    if not memory:
        blocked_tools |= MEMORY_TOOLS
    if isinstance(getattr(tools, "tools", None), list):
        req.func_tool = copy.copy(tools)
        req.func_tool.tools = [tool for tool in tools.tools
                               if getattr(tool, "name", "") not in blocked_tools]
