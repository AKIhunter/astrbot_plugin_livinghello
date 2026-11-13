"""参与判定的原子解析：只修复表示形式，不补造理解或参与结论。"""
from __future__ import annotations

import ast
import io
import json
import math
import re
import tokenize
from dataclasses import dataclass
from typing import Any

BLOCK_REASONS = {
    "others_talking", "conversation_ended", "already_answered",
    "context_missing", "media_unresolved", "no_value",
}
MODEL_REASONS = BLOCK_REASONS | {"directed", "continuation", "useful"}
_PUNCTUATION = str.maketrans("｛｝［］：，", "{}[]:,")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


@dataclass(frozen=True)
class GateParse:
    details: tuple[str, int, str, bool, str] | None = None
    payload: dict[str, Any] | None = None
    reason: str = "no_object"
    format: str = "none"


def _structural_text(text: str) -> str:
    """全角标点仅在结构位置归一化，字符串内容（包括图片摘要）保持原样。"""
    result = []
    quote = ""
    closing = ""
    escaped = False
    for char in text:
        if quote:
            if escaped:
                result.append(char)
                escaped = False
            elif char == "\\":
                result.append(char)
                escaped = True
            elif char == closing:
                result.append(quote)
                quote = closing = ""
            else:
                result.append(char)
        elif char in "\"'“＂‘":
            quote = "'" if char in "'‘" else '"'
            closing = {"“": "”", "＂": "＂", "‘": "’"}.get(char, char)
            result.append(quote)
        else:
            result.append(char.translate(_PUNCTUATION))
    return "".join(result)


def _objects(text: str) -> tuple[list[str], str]:
    """只扫描完整外层对象，不把坏外层/数组中的嵌套判定提拔成结果。"""
    objects = []
    position = 0
    while position < len(text):
        starts = [i for i in (text.find("{", position), text.find("[", position)) if i >= 0]
        if not starts:
            break
        start = min(starts)
        stack = []
        quote = ""
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in "\"'":
                quote = char
            elif char in "{[":
                stack.append(char)
                if len(stack) > 32:
                    return [], "too_deep"
            elif char in "}]":
                if not stack or stack[-1] != ("{" if char == "}" else "["):
                    return [], "malformed_object"
                stack.pop()
                if not stack:
                    if text[start] == "{":
                        objects.append(text[start:index + 1])
                    position = index + 1
                    break
        else:
            return [], "truncated_object"
        if len(objects) > 16:
            return [], "too_many_objects"
    return objects, ""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _decode(text: str) -> tuple[dict | None, str, str]:
    try:
        return json.loads(text, object_pairs_hook=_pairs), "json", ""
    except ValueError as exc:
        if str(exc) == "duplicate_key":
            return None, "none", "duplicate_key"
    try:
        # literal_eval 不执行表达式；token 映射只作用于字符串外的 JSON 常量。
        tokens = []
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and token.string in {"true", "false", "null"}:
                token = token._replace(string={"true": "True", "false": "False", "null": "None"}[token.string])
            tokens.append(token)
        node = ast.parse(tokenize.untokenize(tokens), mode="eval")
        for item in ast.walk(node):
            if isinstance(item, ast.Dict):
                keys = [ast.literal_eval(key) for key in item.keys]
                if len(keys) != len(set(keys)):
                    return None, "none", "duplicate_key"
        value = ast.literal_eval(node)
        if isinstance(value, dict):
            return value, "literal", ""
    except (ValueError, TypeError, SyntaxError, tokenize.TokenError, RecursionError):
        pass
    return None, "none", "malformed_object"


def _text(value):
    return value.strip().lower() if isinstance(value, str) else None


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not _NUMBER.fullmatch(value):
            return None
    try:
        number = float(value)
        # 先验证真实范围再沿用旧版取整，不能将越界值截成可放行高分。
        return number if math.isfinite(number) and 0 <= number <= 100 else None
    except (ValueError, OverflowError):
        return None


def _boolean(value):
    if isinstance(value, bool):
        return value
    text = _text(value)
    return text == "true" if text in {"true", "false"} else None


def _validate(data: dict) -> tuple[tuple | None, str]:
    normalized = {}
    for key, value in data.items():
        if not isinstance(key, str):
            return None, "invalid_key"
        key = key.strip().lower()
        if key in normalized:
            return None, "duplicate_key"
        normalized[key] = value
    fields = (
        ("act", ("act", "action"), _text, {"reply", "silent"}),
        ("conf", ("conf", "confidence", "score"), _score, None),
        ("understood", ("understood",), _boolean, None),
        ("reason", ("reason",), _text, MODEL_REASONS),
        ("ground", ("ground",), _text, {"none", "memory", "web"}),
    )
    values = {}
    for name, aliases, convert, choices in fields:
        present = [normalized[key] for key in aliases if key in normalized]
        if not present:
            return None, "missing_" + name
        converted = [convert(value) for value in present]
        if any(value is None or choices is not None and value not in choices for value in converted):
            return None, "invalid_" + name
        if any(value != converted[0] for value in converted[1:]):
            return None, "conflicting_" + name
        values[name] = converted[0]
    if "need_vision" in normalized and _boolean(normalized["need_vision"]) is None:
        return None, "invalid_need_vision"
    return (values["act"], int(values["conf"]), values["ground"],
            values["understood"], values["reason"]), "ok"


def parse_gate(raw: str) -> GateParse:
    """诊断仅固定原因码；不包含坏原文、异常正文或推理文本。"""
    if not isinstance(raw, str) or not raw.strip():
        return GateParse(reason="empty_response")
    if len(raw) > 12000:
        return GateParse(reason="too_long")
    text = raw.lstrip()
    # 仅移除输出前缀的推理块，不能改写 JSON 字符串内部的标签或枚举。
    while re.match(r"<think\b[^>]*>", text, re.I):
        closing = re.search(r"</think\s*>", text, re.I)
        if closing is None:
            return GateParse(reason="unclosed_think")
        text = text[closing.end():].lstrip()
    normalized = _structural_text(text)
    objects, error = _objects(normalized)
    if error:
        return GateParse(reason=error)
    valid = []
    failure = "no_object"
    for source in objects:
        data, format_name, error = _decode(source)
        if data is None:
            failure = error
            continue
        details, error = _validate(data)
        if details is None:
            failure = error
            continue
        if format_name == "json" and normalized != text:
            format_name = "json_normalized"
        payload = {key.strip().lower(): value for key, value in data.items()}
        if "need_vision" in payload:
            payload["need_vision"] = _boolean(payload["need_vision"])
        valid.append(GateParse(details, payload, "ok", format_name))
    if len(valid) > 1:
        reason = "conflicting_objects" if any(row.details != valid[0].details for row in valid[1:]) else "duplicate_objects"
        return GateParse(reason=reason)
    return valid[0] if valid else GateParse(reason=failure)
