"""主回复模型的短流水视图；原始缓存、权限元数据和判定输入保持独立。"""
from __future__ import annotations

import html
import json
import math


def _field(value, limit=50):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())[:limit]


def _quoted(value, limit=None):
    text = value if isinstance(value, str) else ""
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    # 保留正文的换行/空格；JSON字符串避免正文伪造下一条流水。
    return json.dumps(text, ensure_ascii=False)


def age_label(age):
    if isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age):
        return f"[{max(0, int(age))}秒前]"
    return "[时间未知]"


def scene_line(meta):
    qq = meta.get("platform") in {"qq", "aiocqhttp"}
    prefix = "QQ" if qq else ""
    if meta.get("session_type") == "group":
        scene = f"{prefix}群 {_field(meta.get('group_id')) or '未知'}"
    else:
        scene = f"{prefix}私聊 {_field(meta.get('private_qq') or meta.get('sender_id')) or '未知'}"
    return f"会话：{scene}｜机器人 {prefix}{_field(meta.get('bot_id')) or '未知'}"


def message_row(meta, text, *, body_source="", text_limit=None, quote_limit=120):
    """返回未转义的一行；只有已有完整副本时才以来源说明替代正文。"""
    id_label = "QQ" if meta.get("platform") in {"qq", "aiocqhttp"} else "ID"
    name = _field(meta.get("sender_name")) or "未知"
    sender = _field(meta.get("sender_id"))
    actor = f"{name}({id_label}{sender})" if sender else f"{name}(身份未知)"
    mid = _field(meta.get("message_id"), 80)
    pieces = [actor + (f"#{mid}" if mid else "")]
    mentions = meta.get("mention_targets")
    if isinstance(mentions, (list, tuple)):
        ids = [_field(value) for value in mentions[:20]]
        if any(ids):
            pieces.append("@" + ",".join(value for value in ids if value))
    quotes = meta.get("reply_to")
    for quote in quotes[:2] if isinstance(quotes, list) else ():
        if not isinstance(quote, dict):
            continue
        target = _field(quote.get("sender_id")) or "未知"
        quote_id = _field(quote.get("message_id"), 80)
        reference = f"引用{id_label}{target}" + (f"#{quote_id}" if quote_id else "")
        if quote.get("text"):
            reference += ":" + _quoted(quote["text"], quote_limit)
        pieces.append(reference)
    media = meta.get("unread_media")
    kinds = [_field(value, 30) for value in media[:5]] if isinstance(media, list) else []
    if meta.get("has_images") and "Image" not in kinds:
        kinds.insert(0, "Image")
    if any(kinds):
        pieces.append("未解析媒体=" + ",".join(value for value in kinds if value))
    body = f"正文见{body_source}" if body_source else _quoted(text, text_limit)
    return " ".join(pieces) + ": " + body


def history_row(raw):
    """兼容当前JSON缓存和旧文本行，不为旧记录猜测作者ID。"""
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        meta = None
    if isinstance(meta, dict):
        return message_row(meta, meta.get("text", ""), text_limit=200)
    if isinstance(raw, str) and raw.startswith("[我]: "):
        return "机器人: " + _quoted(raw[5:], 300)
    return "来源未知: " + _quoted(raw if isinstance(raw, str) else "", 600)


def topic_text(meta, current_text, history_lines, *, body_source="", scene_source=""):
    """当前消息只保留必要来源；历史行已在选取时转义与限长。"""
    scene = f"会话见{scene_source}" if scene_source else scene_line(meta)
    lines = [html.escape(scene, quote=False), "历史（旧→新）:"]
    lines.extend(history_lines or ["（无）"])
    if body_source == "at_guard_fragments连续消息":
        current = "正文见at_guard_fragments连续消息（作者、时间和引用同块）"
    else:
        current = age_label(meta.get("age_seconds")) + " " + message_row(
            meta, current_text, body_source=body_source, quote_limit=None)
    lines.append("当前消息: " + html.escape(current, quote=False))
    return "\n".join(lines)
