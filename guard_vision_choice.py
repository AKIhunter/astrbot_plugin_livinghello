"""视觉模型选择的统一解析。

配置里三个键的关系：
- vision_shared_provider_id：主选择项「视觉模型（共用）」，判定看图和图片理解默认都用它。
- vision_provider_id：判定看图的显式覆盖，留空跟随共用项。
- understand_image_provider_id：图片理解的显式覆盖，留空跟随共用项。

优先级：显式覆盖 > 共用项 > 空。
三者都为空时返回空字符串，调用方保持原有逻辑（各自跟随会话当前模型 /
判定看图按主模型兜底开关处理），因此升级前后行为完全一致。
"""

from __future__ import annotations

SHARED_KEY = "vision_shared_provider_id"
GATE_OVERRIDE_KEY = "vision_provider_id"
UNDERSTAND_OVERRIDE_KEY = "understand_image_provider_id"


def _value(config, key: str) -> str:
    try:
        value = config.get(key, "")
    except Exception:
        return ""
    return str(value or "").strip()


def effective_vision_provider(config, override_key: str) -> str:
    """返回某个视觉角色最终生效的 provider id。

    显式覆盖优先；没有覆盖时跟随共用视觉模型；都为空时返回 ""。
    """
    return _value(config, override_key) or _value(config, SHARED_KEY)
