"""主回复方式：确定性引用/@与Emoji清理；断句和媒体处理遵循宿主。"""
from __future__ import annotations

import json
import random
import re

from astrbot.api.message_components import At, Plain, Reply

FORBID_MAX_ITEMS = 30
FORBID_MAX_CHARS = 20
MAX_CLEANUP_CHARS = 16384
JSON_PARSE_CHAR_BUDGET = 32768
JSON_PARSE_MAX_ATTEMPTS = 16
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_DUP_PUNCT = re.compile(r"([，,。．.！!？?；;：:])\1+")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
# 口头禅后紧跟的标点一并删除，否则会留下「。那这事」这种悬空标点。
_TAIL_PUNCT = r"[，,、；;：:。．.！!？?~…]*"
# 删除后只清理行首行尾的停顿号，句末的。！？属于正文语气，保留。
_EDGE_PUNCT = re.compile(r"^[，,、；;：:]+|[，,、；;：:]+[ \t]*$", re.MULTILINE)


# ── 标点规范化 ──────────────────────────────────────────────────────────
# 普通聊天标点换间隔；代码、网址、文件路径和数字格式是内容，保持原样。
_PROTECTED = re.compile(
    r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`"
    r"|https?://[^\s\u3000-\u303f\u4e00-\u9fff\uff00-\uffef<>\"`]+"
    r"|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    r"|(?:[A-Za-z]:[\\/]|\.{1,2}/|~/|/)[^\s<>\"'`，。！？；：、（）【】]+"
    r"|\b[A-Za-z0-9_]+(?:[./:@+_-][A-Za-z0-9_]+)+\b"
    r"|\b\d+(?:[.,:/+-]\d+)+(?:%)?)"
)
# 未加代码围栏的命令整行保留，参数不能套用聊天标点规则。
_COMMAND_LINE = re.compile(
    r"(?m)^[ \t]*(?:[$#][ \t]+)?(?:sudo[ \t]+)?"
    r"(?:curl|wget|python(?:\d+(?:\.\d+)?)?|pip3?|ssh|scp|rsync|git|"
    r"npm|npx|node|bash|sh|docker|kubectl|systemctl|journalctl)"
    r"(?=[ \t]|$)[^\r\n]*"
)
_JSON_START = re.compile(r"[\[{]")
_CHAT_PUNCT = re.compile(r"""[，,、；;：:。．.！!？?…~～“”‘’「」『』（）()【】\[\]《》〈〉"'—–]+""")
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


def protected_parts(text):
    """有预算地识别技术片段；过长或试解析过多时保守保留整份原文。"""
    if len(text) > MAX_CLEANUP_CHARS:
        yield True, text
        return
    spans = [(m.start(), m.end()) for pattern in (_PROTECTED, _COMMAND_LINE)
             for m in pattern.finditer(text)]
    existing = sorted(spans)
    protected_index = 0
    decoder = json.JSONDecoder()
    json_end = 0
    parse_chars = attempts = 0
    for match in _JSON_START.finditer(text):
        start = match.start()
        if start < json_end:
            continue
        while protected_index < len(existing) and existing[protected_index][1] <= start:
            protected_index += 1
        if (protected_index < len(existing)
                and existing[protected_index][0] <= start < existing[protected_index][1]):
            continue
        parse_chars += len(text) - start
        attempts += 1
        if parse_chars > JSON_PARSE_CHAR_BUDGET or attempts > JSON_PARSE_MAX_ATTEMPTS:
            yield True, text
            return
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, (dict, list)):
            spans.append((start, end))
            json_end = end
    cursor = 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        if start > cursor:
            yield False, text[cursor:start]
        yield True, text[max(cursor, start):end]
        cursor = end
    if cursor < len(text):
        yield False, text[cursor:]


def normalize_punctuation(text, keep_inner_period=False, space="1"):
    """把标点换成间隔。幂等：重复调用结果不变。

    keep_inner_period 为旧调用兼容参数；当前聊天规则统一清理标点。
    space  '1' 一个空格 / '2' 两个空格 / 'random' 每处随机 1~2
    """
    if not text or len(text) > MAX_CLEANUP_CHARS:
        return text
    # 表格的管道、分隔行属于结构，不能套用聊天清理。
    if len(re.findall(r"(?m)^[ \t]*\|.+\|[ \t]*$", text)) >= 2:
        return text
    parts = []
    for protected, part in protected_parts(text):
        if protected:
            parts.append(part)
            continue
        value = part
        value = _CHAT_PUNCT.sub(_GAP, value)
        value = _GAP_RUN.sub(_GAP, value)
        value = _GAP_PAD.sub(_GAP, value)
        if space == "1":
            value = _MULTI_SPACE.sub(" ", value)
        else:
            value = _MULTI_SPACE_WIDE.sub("  ", value)
        # 只整理聊天片段的行边缘，不能对拼回后的代码去缩进。
        value = re.sub(_GAP, lambda _m: " " * punct_gap_width(space), value)
        value = re.sub(r"(?<=\n)[ \t]+|[ \t]+(?=\n)", "", value)
        value = _MANY_BLANK.sub("\n\n", value)
        parts.append(value)
    return "".join(parts).strip(" \t\r\n")


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
    """只清理聊天片段，所有后处理也不得跨入技术片段。"""
    if not text or not phrases or len(text) > MAX_CLEANUP_CHARS:
        return text
    parts = []
    for protected, part in protected_parts(text):
        if protected:
            parts.append(part)
            continue
        changed = False
        for phrase in phrases:
            if phrase in part:
                part = re.sub(re.escape(phrase) + _TAIL_PUNCT, "", part)
                changed = True
        if changed:
            part = _MULTI_SPACE.sub(" ", part)
            part = _DUP_PUNCT.sub(r"\1", part)
            part = _EDGE_PUNCT.sub("", part)
            part = _TRAILING_SPACE.sub("", part)
        parts.append(part)
    return "".join(parts)


def takeover(config):
    return config.get("expression_takeover_enabled", True) is True


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

    def punctuation_chain(self, chain):
        """发送副本统一清理，独立于私聊自动引用/Emoji复用开关。"""
        if not takeover(self.config) or not punct_cleanup_enabled(self.config):
            return list(chain)
        result = []
        for component in chain:
            if isinstance(component, Plain):
                text = normalize_punctuation(component.text, space=punct_space_mode(self.config))
                # 未改变正文时保留组件身份；发送去重按原组件跟踪。
                if text != component.text:
                    component = Plain(text)
            result.append(component)
        return result

    def tool_arguments(self, tool, arguments):
        """工具直接投递不经过回复装饰，只处理 plain 正文，不碰附件名和地址。"""
        if (getattr(tool, "name", "") != "send_message_to_user"
                or not isinstance(arguments, dict) or not takeover(self.config)
                or not punct_cleanup_enabled(self.config)):
            return
        messages = arguments.get("messages")
        if not isinstance(messages, list):
            return
        arguments["messages"] = [
            {**item, "text": normalize_punctuation(item["text"], space=punct_space_mode(self.config))}
            if isinstance(item, dict) and item.get("type") == "plain"
            and isinstance(item.get("text"), str) else item for item in messages
        ]

    def before_send(self, event, chain, plan):
        """仅调整当前主回复自动头；原有引用及指向其他人的@保留。"""
        chain = self.private_headers(event, self.punctuation_chain(chain))
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
