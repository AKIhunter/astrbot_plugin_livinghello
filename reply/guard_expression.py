"""主回复方式：确定性引用/@与Emoji清理；断句和媒体处理遵循宿主。"""
from __future__ import annotations

import random
import re

from astrbot.api.message_components import At, Plain, Reply

FORBID_MAX_ITEMS = 30
FORBID_MAX_CHARS = 20
_FENCE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_DUP_PUNCT = re.compile(r"([，,。．.！!？?；;：:])\1+")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
# 口头禅后紧跟的标点一并删除，否则会留下「。那这事」这种悬空标点。
_TAIL_PUNCT = r"[，,、；;：:。．.！!？?~…]*"
# 删除后只清理行首行尾的停顿号，句末的。！？属于正文语气，保留。
_EDGE_PUNCT = re.compile(r"^[，,、；;：:]+|[，,、；;：:]+[ \t]*$", re.MULTILINE)


# ── 标点规范化 ──────────────────────────────────────────────────────────
# 行末句号换成间隔；句中句号保留；逗号、顿号、分号换成间隔。
# 感叹号、问号、省略号承载语气，一律不动。代码块与 ASCII 小数点不碰。
_CN_TO_SPACE = re.compile(r"[，、；;]")
_CN_PERIOD = re.compile(r"[。．]")
# 行末（含行尾空白）的连续句号整段换成间隔；句中的句号不受影响。
_TRAILING_PERIOD = re.compile(r"[。．]+[ \t]*$", re.MULTILINE)
_ASCII_COMMA_CJK = re.compile(r"(?<=[\u4e00-\u9fff\u3000-\u303f]),(?=[\u4e00-\u9fff\u3000-\u303f0-9A-Za-z])")
_LINE_TRAIL = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANK = re.compile(r"\n{3,}")
_INLINE_EDGE = re.compile(r"^[ \t]+|[ \t]+$", re.MULTILINE)
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
# 目标宽度为 2 时只能压 3 个以上，否则二次调用会把两个空格压回一个。
_MULTI_SPACE_WIDE = re.compile(r"[ \t]{3,}")
# 占位符：先记「这里要断开」，等空白全部收拢完再统一换成 N 个空格。
_GAP = "\x00"
_GAP_RUN = re.compile(r"\x00(?:[ \t]*\x00)*")
_GAP_PAD = re.compile(r"[ \t]*\x00[ \t]*")
_GAP_EDGE = re.compile(r"(?m)^[ \t]*\x00|\x00[ \t]*$")


def punct_gap_width(mode):
    """把配置值翻译成实际空格数。未知值退回 1，不抛异常。"""
    if mode == "2":
        return 2
    if mode == "random":
        return random.choice((1, 2))
    return 1


def normalize_punctuation(text, keep_inner_period=True, space="1"):
    """把标点换成间隔。幂等：重复调用结果不变。

    keep_inner_period=True  句中句号保留（2026-11-14 用户指定）
    space  '1' 一个空格 / '2' 两个空格 / 'random' 每处随机 1~2
    """
    if not text:
        return text
    parts = _FENCE.split(text)
    for index, part in enumerate(parts):
        if not part or part.startswith(("```", "~~~")):
            continue
        value = part
        # 先处理行末句号，再处理句中；顺序反了会误伤刚换出来的占位。
        value = _TRAILING_PERIOD.sub(_GAP, value)
        if not keep_inner_period:
            value = _CN_PERIOD.sub(_GAP, value)
        value = _ASCII_COMMA_CJK.sub(_GAP, value)
        value = _CN_TO_SPACE.sub(_GAP, value)
        value = _GAP_RUN.sub(_GAP, value)
        value = _GAP_PAD.sub(_GAP, value)
        if space == "1":
            value = _MULTI_SPACE.sub(" ", value)
        else:
            value = _MULTI_SPACE_WIDE.sub("  ", value)
        value = _GAP_EDGE.sub("", value)
        value = _LINE_TRAIL.sub("", value)
        value = _MANY_BLANK.sub("\n\n", value)
        value = _INLINE_EDGE.sub("", value)
        value = re.sub(_GAP, lambda _m: " " * punct_gap_width(space), value)
        parts[index] = value
    return "".join(parts)


def punct_cleanup_enabled(config):
    """开关：默认开。读不到配置时保持开启，避免静默失效。"""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return True
    value = getter("expression_punct_cleanup")
    return True if value is None else bool(value)


def punct_space_mode(config):
    """空格宽度：'1' / '2' / 'random'。未知值按 1 处理。"""
    getter = getattr(config, "get", None)
    value = getter("expression_punct_space") if callable(getter) else None
    return value if value in ("1", "2", "random") else "1"


def forbid_phrases(config):
    """读取口头禅禁忌；非法项静默丢弃，不阻断主回复。"""
    raw = config.get("expression_forbid_phrases") if hasattr(config, "get") else None
    if isinstance(raw, str):
        raw = re.split(r"[,，、\n]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    phrases = []
    for item in raw:
        value = str(item or "").strip()
        if not value or len(value) > FORBID_MAX_CHARS or value in phrases:
            continue
        phrases.append(value)
    # 长词优先，避免短词先把长词的一部分删掉。
    phrases.sort(key=len, reverse=True)
    return phrases[:FORBID_MAX_ITEMS]


def strip_forbidden(text, phrases):
    """删除命中的口头禅；代码块保持原样，删除后收拢悬空标点与多余空白。"""
    if not text or not phrases:
        return text
    parts = _FENCE.split(text)
    changed = False
    for index, part in enumerate(parts):
        if not part or part.startswith(("```", "~~~")):
            continue
        for phrase in phrases:
            if phrase in part:
                part = re.sub(re.escape(phrase) + _TAIL_PUNCT, "", part)
                changed = True
        parts[index] = part
    if not changed:
        return text
    cleaned = "".join(parts)
    cleaned = _MULTI_SPACE.sub(" ", cleaned)
    cleaned = _DUP_PUNCT.sub(r"\1", cleaned)
    cleaned = _EDGE_PUNCT.sub("", cleaned)
    cleaned = _TRAILING_SPACE.sub("", cleaned)
    return cleaned


def takeover(config):
    return config.get("expression_takeover_enabled", True) is True


def congling_for(context, event):
    """每次解析已激活实例及会话开关，避免重载后缓存旧实例。"""
    for name in ("astrbot_plugin_congling", "congling"):
        try:
            metadata = context.get_registered_star(name)
            plugin = getattr(metadata, "star_cls", None)
            if (getattr(metadata, "activated", False) and plugin is not None
                    and callable(getattr(plugin, "enabled_for", None))
                    and plugin.enabled_for(event)):
                return plugin
        except Exception:
            continue
    return None


class Expression:
    def __init__(self, plugin):
        self.plugin = plugin
        self.config = plugin.config

    def reuse(self, event):
        return bool(event.get_group_id()) or self.config.get("expression_private_reuse", False) is True

    def private_headers(self, event, chain):
        """私聊不复用时去掉当前消息的引用/@，也覆盖宿主新建的自动头。"""
        if self.reuse(event):
            return list(chain)
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        sender = str(event.get_sender_id() or "")
        return [c for c in chain if not (
            isinstance(c, Reply) and str(c.id) == mid
            or isinstance(c, At) and str(c.qq) == sender)]

    def clean(self, text):
        if self.config.get("expression_strip_emoji", True):
            text = re.sub("[\U0001f000-\U0001faff\u2600-\u27bf\ufe00-\ufe0f\u200d]+", "", text)
            text = re.sub(r"[（(][^（）()]{0,12}[ω・∀▽｀´°￣][^（）()]{0,12}[)）]", "", text)
        # 换行、标点、代码与模型原字均保留，不暗藏其他表达规则。
        try:
            from .guard_thinking_filter import strip_thinking
        except ImportError:
            from reply.guard_thinking_filter import strip_thinking
        text = strip_thinking(text)[0]
        return strip_forbidden(text, forbid_phrases(self.config))

    async def decorate(self, event, result):
        if not takeover(self.config) or not getattr(result, "is_llm_result", lambda: False)():
            return
        self.clean_chain(result, event)

    def clean_chain(self, result, event=None):
        if event is not None and not self.reuse(event):
            return
        result.chain = [
            Plain(self.clean(c.text)) if isinstance(c, Plain) else c for c in result.chain or []
        ]
        result.chain = [c for c in result.chain if not isinstance(c, Plain) or c.text.strip()]

    def before_send(self, event, chain, plan):
        """仅调整当前主回复自动头；原有引用及指向其他人的@保留。"""
        chain = self.private_headers(event, chain)
        if not self.reuse(event):
            return chain
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        sender = str(event.get_sender_id() or "")
        semantic = plan["semantic_headers"]
        chain = [c for c in chain if id(c) in semantic or not (
            isinstance(c, Reply) and str(c.id) == mid
            or isinstance(c, At) and str(c.qq) == sender)]
        body = [c for c in chain if not isinstance(c, (Reply, At))]
        # 语音由原生单独发送，不消费首条可承载回复头的机会。
        if not body or all(type(c).__name__ == "Record" for c in body):
            return chain
        if plan["header_sent"]:
            return chain
        heads = []
        if (self.config.get("reply_quote_enabled", True) is True
                and mid and mid not in {"0", "None", "null"}
                and not any(isinstance(c, Reply) for c in chain)):
            heads.append(Reply(id=mid))
        platform = getattr(event, "get_platform_name", lambda: "")()
        if (self.config.get("reply_mention_enabled", False) is True and event.get_group_id()
                and platform not in {"qq_official_webhook", "weixin_official_account"}
                and sender and sender not in {"0", "None", "null"}
                and not any(isinstance(c, At) and str(c.qq) == sender for c in chain)):
            heads.append(At(qq=sender, name=event.get_sender_name()))
        # 引用优先于@；保留原组件相对顺序和正文中其他人的@。
        if heads and isinstance(heads[0], Reply):
            return heads + chain
        if heads and chain and isinstance(chain[0], Reply):
            return [chain[0], *heads, *chain[1:]]
        return heads + chain
