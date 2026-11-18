"""原生 FunctionTool 包装，供当前主模型按需选择文生图或图生图。"""
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
try:
    from .guard_image_contract import IMAGE_DESCRIPTION
except ImportError:  # 兼容插件现有单文件/顶层包测试入口。
    from images.guard_image_contract import IMAGE_DESCRIPTION


@dataclass
class GenerateImageTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}
    service: Any = None
    name: str = "livinghello_generate_image"
    description: str = IMAGE_DESCRIPTION
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "所需画面或编辑要求，1~4000字"},
            "use_reference": {"type": "boolean", "description": "true使用本次输入图片做图生图；false文生图"},
            "followup_id": {"type": "string", "description": (
                "用户已明确调整此前被内容规则拒绝的画面时，填当前会话待办ID，恢复原待办。"
                "仅限文生图；结果未知、发送未知和接口设置问题不能用它强行重试。")},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    })

    async def call(self, context, **kwargs):
        node = context
        for _ in range(5):
            if callable(getattr(node, "get_sender_id", None)):
                break
            node = getattr(node, "event", None) or getattr(node, "context", None)
            if node is None:
                return "缺少当前会话，不能生图或发送图片。"
        if not callable(getattr(node, "get_sender_id", None)) or self.service is None:
            return "缺少当前会话或生图能力。"
        if kwargs.get("followup_id") is not None:
            if kwargs.get("use_reference", False) or node.get_extra("cron_job"):
                return "该待办需要用户新一轮调整文生图需求，不能用参考图或定时任务强行恢复。"
            try:
                from ..integrations.guard_livingemotion import image_followup
            except ImportError:
                from integrations.guard_livingemotion import image_followup
            receipt = await image_followup(
                self.service.context, node, getattr(self.service.plugin, "config", None),
                promise_id=kwargs["followup_id"], prompt=kwargs.get("prompt"))
            if receipt and receipt.get("status") == "resumed":
                return "已用用户调整后的画面恢复原待办，稍后由原任务交付，不要重复生图或新建承诺。"
            return "原待办没有恢复，需要明确调整画面，或先核对未知结果；不要再调用生图绕过。"
        if node.get_extra("cron_job"):
            # 定时主代理没有普通回复发送阶段，不能把图片挂着等 after-sent。
            if kwargs.get("use_reference", False):
                return "定时生图没有本次用户参考图片，请使用文生图。"
            result = await self.service.plugin.execute_deferred_image(node, kwargs.get("prompt"))
            if result.get("status") == "sent":
                detail = str(result.get("revised") or "")
                return "图片已发送到当前会话。" + (f"实际画面：{detail}。" if detail else "")
            return "定时图片未确认发送，请勿再次调用生图工具。"
        return await self.service.generate(node, kwargs.get("prompt"), kwargs.get("use_reference", False))
