"""同一轮相邻模型正文去重；比较聊天标点与空白，保留首份原文。"""
from __future__ import annotations

import re

from astrbot.api.message_components import Plain

from .guard_expression import MAX_CLEANUP_CHARS, protected_parts

_ENDS = "。．.！？!?…~～"
_SENTENCES = re.compile(r"[^。．.！？!?…~～]*[。．.！？!?…~～]+|[^。．.！？!?…~～]+$")
_COMPARE_GAPS = re.compile(r"[\s，,、；;：:。．.！？!?…~～—–]+")
# 列表、引用、代码、表格、括号和引号内的重复可能有实际含义，保守保留。
_STRUCTURE = re.compile(
    r"```|~~~|(?m:^[ \t]*(?:[|>#]|[-+*][ \t]|\d+[.)、][ \t]))"
    r"|[“”‘’「」『』（）()\[\]《》〈〉\"]|</?[A-Za-z]"
)


def _chat(text):
    return (len(text) <= MAX_CLEANUP_CHARS and not _STRUCTURE.search(text)
            and not any(protected for protected, _ in protected_parts(text)))


def _key(text):
    # 仅用于比较；输出始终取原文，不套用随机标点间隔或相似度判断。
    return _COMPARE_GAPS.sub("", text)


def _fresh_sentences(paragraph, seen_units):
    sentences = _SENTENCES.findall(paragraph)
    keys = [_key(sentence) for sentence in sentences]
    flat, offsets = "".join(keys), [0]
    complete_ends = {}
    for index, (sentence, key) in enumerate(zip(sentences, keys)):
        offsets.append(offsets[-1] + len(key))
        if key and sentence.strip().endswith(tuple(_ENDS)):
            complete_ends[offsets[-1]] = index + 1
    fresh, index = [], 0
    while index < len(sentences):
        start = offsets[index]
        candidates = seen_units.get(keys[index][:1], ())
        ends = [complete_ends[start + len(key)] for key in candidates
                if start + len(key) in complete_ends and flat.startswith(key, start)]
        if ends:
            # 允许同一正文的句号位置变化，但必须在完整句子边界收口，
            # 不能把「原句还有补充」当成「原句」的重复而裁掉前缀。
            index = max(ends)
        else:
            fresh.append(sentences[index])
            index += 1
    return fresh, len(fresh) != len(sentences)


def merge_text_parts(parts):
    """宿主每次非流式输出是一个 Plain；只与更早的正文比较。

    单份正文内部的有意重复保留。结构化内容原样保留，新增句段按原序留下。
    发生去重时补空行，避免工具前的末段与工具后的首段粘成同一个气泡。
    没有重复时沿用原来的相邻 Plain 拼接方式。
    """
    seen_paragraphs, seen_units = set(), {}
    merged, deduplicated = [], False
    for text in parts:
        if not text:
            continue
        if not _chat(text):
            merged.append(text)
            continue
        paragraphs = [p for p in re.split(r"\r?\n+", text) if p.strip()]
        kept, changed = [], False
        for paragraph in paragraphs:
            key = _key(paragraph)
            if key and key in seen_paragraphs:
                changed = True
                continue
            fresh, hit = _fresh_sentences(paragraph, seen_units)
            changed |= hit
            value = "".join(fresh)
            if value.strip():
                kept.append(value.strip())
        value = "\n\n".join(kept) if changed else text
        deduplicated |= changed
        if value.strip():
            merged.append(value)
        # 整份正文比较完再入集合，不能误删这份正文内部的有意重复。
        seen_paragraphs.update(_key(p) for p in paragraphs if _key(p))
        for paragraph in paragraphs:
            for unit in [paragraph, *_SENTENCES.findall(paragraph)]:
                key = _key(unit)
                if key and unit.strip().endswith(tuple(_ENDS)):
                    seen_units.setdefault(key[0], set()).add(key)
    return ("\n\n" if deduplicated else "").join(merged)


def deduplicate_text_chain(chain):
    """在其他装饰插件折叠 Plain 之前去重；未去重的组件和媒体保留身份。"""
    output, pending, changed = [], [], False

    def flush():
        nonlocal changed
        if not pending:
            return
        parts = [component.text for component in pending]
        text = merge_text_parts(parts) if len(parts) > 1 else parts[0]
        if text != "".join(parts):
            pending[0].text = text
            output.append(pending[0])
            changed = True
        else:
            output.extend(pending)
        pending.clear()

    for component in chain:
        if isinstance(component, Plain):
            pending.append(component)
        else:
            flush()
            output.append(component)
    flush()
    if changed:
        chain[:] = output
    return changed
