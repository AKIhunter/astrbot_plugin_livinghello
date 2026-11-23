"""同一轮相邻模型正文的精确去重；不做相似度裁剪，不跨媒体或会话记忆。"""
from __future__ import annotations

import re

from .guard_expression import MAX_CLEANUP_CHARS, protected_parts

_ENDS = "。！？!?"
_SENTENCES = re.compile(r"[^。！？!?]*[。！？!?]+|[^。！？!?]+$")
# 列表、引用、代码、表格、括号和引号内的重复可能有实际含义，保守保留。
_STRUCTURE = re.compile(
    r"```|~~~|(?m:^[ \t]*(?:[|>#]|[-+*][ \t]|\d+[.)、][ \t]))"
    r"|[“”‘’「」『』（）()\[\]《》〈〉\"]|</?[A-Za-z]"
)


def _chat(text):
    return (len(text) <= MAX_CLEANUP_CHARS and not _STRUCTURE.search(text)
            and not any(protected for protected, _ in protected_parts(text)))


def merge_text_parts(parts):
    """宿主每次非流式输出是一个 Plain；只与更早的正文比较。

    单份正文内部的有意重复保留。结构化内容原样保留，新增句段按原序留下。
    发生去重时补空行，避免工具前的末段与工具后的首段粘成同一个气泡。
    没有重复时沿用原来的相邻 Plain 拼接方式。
    """
    seen_paragraphs, seen_sentences = set(), set()
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
            if paragraph.strip() in seen_paragraphs:
                changed = True
                continue
            sentences = _SENTENCES.findall(paragraph)
            fresh = [s for s in sentences
                     if not (s.strip().endswith(tuple(_ENDS))
                             and s.strip() in seen_sentences)]
            changed |= len(fresh) != len(sentences)
            value = "".join(fresh)
            if value.strip():
                kept.append(value.strip())
        value = "\n\n".join(kept) if changed else text
        deduplicated |= changed
        if value.strip():
            merged.append(value)
        # 整份正文比较完再入集合，不能误删这份正文内部的有意重复。
        seen_paragraphs.update(p.strip() for p in paragraphs)
        seen_sentences.update(s.strip() for p in paragraphs for s in _SENTENCES.findall(p)
                              if s.strip().endswith(tuple(_ENDS)))
    return ("\n\n" if deduplicated else "").join(merged)
