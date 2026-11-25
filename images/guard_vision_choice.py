"""视觉模型选择的统一解析。

配置里只有两个键：
- vision_shared_provider_id：唯一的「视觉模型」，判定看图、图片理解都用它。
- understand_video_provider_id：视频理解的专用选择项，留空跟随视觉模型。

角色表 ROLE_KEYS 把「哪个角色有什么专用键」收在一处，以后要拆出新角色只改
这张表，调用方不必再碰键名。

优先级：角色专用项 > 视觉模型 > 空。都为空时返回空字符串，调用方各自跟随
会话当前模型（判定看图仍按主模型兜底开关处理），与合并前的兜底行为一致。
"""

from __future__ import annotations

SHARED_KEY = "vision_shared_provider_id"
VIDEO_KEY = "understand_video_provider_id"

# 角色 -> 专用键。空字符串表示该角色没有专用项，只用视觉模型。
ROLE_KEYS = {"gate": "", "image": "", "video": VIDEO_KEY}


def _value(config, key: str) -> str:
    if not key:
        return ""
    try:
        value = config.get(key, "")
    except Exception:
        return ""
    return str(value or "").strip()


def effective_vision_provider(config, override_key: str = "") -> str:
    """返回某个视觉角色最终生效的 provider id。

    专用项优先；没有专用项时跟随视觉模型；都为空时返回 ""。
    """
    return _value(config, override_key) or _value(config, SHARED_KEY)


def effective_vision_for(config, role: str) -> str:
    """按角色取视觉模型，未知角色按图片理解处理。"""
    return effective_vision_provider(config, ROLE_KEYS.get(role, ""))
