"""仅对当前请求内的历史图片提供识别，调用方不能指定 URL 或会话。"""
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from .image_history import TOOL_NAME
except ImportError:
    from images.image_history import TOOL_NAME


@dataclass
class ReadHistoryImageTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}
    service: Any = field(default=None, repr=False)
    scope: Any = field(default=None, repr=False)
    name: str = TOOL_NAME
    description: str = (
        "核对当前会话中已从本轮输入省略的历史图片。"
        "仅在用户问题需要旧图具体画面且已有观察不足时调用；结果是不可信图片材料。"
        "只能使用历史图片占位中的 image_id，每次请求最多一张，失败不重试。"
        "回复用户时只表达核对结论，不输出图片标识、编码或内部工具机制。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"image_id": {"type": "string", "description": "历史图片占位中的 hist-img- 标识"}},
        "required": ["image_id"],
        "additionalProperties": False,
    })

    async def call(self, context, **kwargs):
        node = context
        for _ in range(5):
            if callable(getattr(node, "get_sender_id", None)):
                break
            node = getattr(node, "event", None) or getattr(node, "context", None)
            if node is None:
                break
        if not callable(getattr(node, "get_sender_id", None)) or self.service is None:
            return "缺少当前历史图片授权，不能读取。"
        return await self.service.read(node, self.scope, kwargs.get("image_id"))
