"""QQ 消息名单：纯校验和不可变策略，正文不参与身份判断。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType

CONTRACT = "livinghello.message-policy.v1"
HISTORY_CONTRACT = "congling.message-policy.v1"
COLORS = ("black", "gray", "white")
FIELDS = tuple(f"message_{color}list_{kind}" for kind in ("qq", "groups") for color in COLORS)
LABELS = {"black": "黑名单", "gray": "灰名单", "white": "白名单"}
QQ_PLATFORMS = frozenset({"aiocqhttp", "onebot", "onebot_v11", "qq"})
SEPARATORS = re.compile(r"[\s,，、;；]+")


def normalize_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text):
        return None
    text = text.lstrip("0")
    return text or None


def normalize_policy_values(values):
    """保留全部字段错误；错误候选只能展示，不能发布到运行策略。"""
    normalized, errors = {}, []
    total_bytes = 0
    for key in FIELDS:
        raw = values.get(key, [])
        try:
            total_bytes += len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            errors.append({"field": key, "code": "type", "message": "名单必须是文字或平坦号码数组"})
            normalized[key] = []
            continue
        parts = SEPARATORS.split(unicodedata.normalize("NFKC", raw)) if isinstance(raw, str) else raw
        if not isinstance(parts, list):
            errors.append({"field": key, "code": "type", "message": "名单必须是文字或平坦号码数组"})
            normalized[key] = []
            continue
        result, seen = [], set()
        for part in parts:
            if isinstance(part, str) and not part.strip():
                continue
            identifier = normalize_id(part)
            if identifier is None:
                errors.append({"field": key, "code": "format",
                               "message": "号码须为1–20位正整数，不能使用昵称、小数或科学计数法"})
            elif identifier not in seen:
                seen.add(identifier)
                result.append(identifier)
        normalized[key] = result
        if len(result) > 500:
            errors.append({"field": key, "code": "limit", "message": "每个名单字段最多500个号码"})
    if total_bytes > 128 * 1024:
        errors.append({"field": FIELDS[0], "code": "size", "message": "六个名单输入合计最多128 KiB"})
    for kind in ("qq", "groups"):
        for index, left in enumerate(COLORS):
            for right in COLORS[index + 1:]:
                a, b = f"message_{left}list_{kind}", f"message_{right}list_{kind}"
                conflicts = sorted(set(normalized[a]) & set(normalized[b]))
                if conflicts:
                    message = f"{'QQ' if kind == 'qq' else 'QQ群'} {', '.join(conflicts[:10])} 同时存在于{LABELS[left]}和{LABELS[right]}，请先从一处移除"
                    for field in (a, b):
                        errors.append({"field": field, "code": "conflict", "message": message,
                                       "conflicting_fields": [a, b], "ids": conflicts})
    canonical = json.dumps({key: normalized[key] for key in sorted(FIELDS)},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"valid": not errors, "normalized_values": normalized, "errors": errors,
            "revision": hashlib.sha256(canonical.encode()).hexdigest()[:16],
            "counts": {key: len(items) for key, items in normalized.items()},
            "whitelist_active": bool(normalized["message_whitelist_qq"] or normalized["message_whitelist_groups"])}


def validate_policy_config(config):
    return normalize_policy_values({key: config.get(key, []) for key in FIELDS})


def event_source(event):
    """只用平台真实元信息，UMO 第一段是实例ID，不能冒充平台类型。"""
    def get(name):
        method = getattr(event, name, None)
        return str(method() or "") if callable(method) else ""
    meta = getattr(event, "platform_meta", None)
    platform = get("get_platform_name") or str(getattr(meta, "name", "") or "")
    group = get("get_group_id")
    return {"platform": platform.lower(), "session_kind": "group" if group else "private",
            "sender_id": get("get_sender_id"), "group_id": group,
            "session_id": str(getattr(event, "unified_msg_origin", "") or "")}


class MessagePolicy:
    def __init__(self, config):
        self.values = MappingProxyType({key: () for key in FIELDS})
        self.revision = ""
        self.errors = []
        self.healthy = False
        self.has_snapshot = False
        self.counters = Counter()
        self.refresh(config)

    def publish(self, validation):
        if not validation["valid"]:
            raise ValueError("不能发布非法名单")
        self.values = MappingProxyType({key: tuple(validation["normalized_values"][key]) for key in FIELDS})
        self.revision = validation["revision"]
        self.healthy = self.has_snapshot = True
        self.errors = []

    def refresh(self, config):
        validation = validate_policy_config(config)
        if validation["valid"]:
            if validation["revision"] != self.revision or not self.healthy:
                self.publish(validation)
        else:
            # 手动非法配置不能变成空名单；旧快照继续提供原有保护。
            self.healthy = False
            self.errors = validation["errors"]
        return validation

    @property
    def active(self):
        return any(self.values.values()) or not self.has_snapshot

    @property
    def black_active(self):
        return bool(self.values["message_blacklist_qq"] or self.values["message_blacklist_groups"]) or not self.has_snapshot

    def snapshot(self):
        return {"contract": CONTRACT, "revision": self.revision, "healthy": self.healthy,
                "active": self.active,
                "whitelist_active": bool(self.values["message_whitelist_qq"] or self.values["message_whitelist_groups"]),
                "normalized_values": {key: list(value) for key, value in self.values.items()},
                "counts": {key: len(value) for key, value in self.values.items()},
                "errors": self.errors, "blocked_counts": dict(self.counters)}

    def check(self, *, platform="", session_kind="", sender_id="", group_id="",
              explicit_call=False, purpose="read", **_):
        base = {"contract": CONTRACT, "revision": self.revision, "read_allowed": True,
                "reply_allowed": True, "requires_explicit": False, "reason": "allowed"}
        if not self.active:
            return base
        if not platform:
            return {**base, "read_allowed": False, "reply_allowed": False, "reason": "source_unknown"}
        if str(platform).lower() not in QQ_PLATFORMS:
            return base
        if not self.has_snapshot:
            return {**base, "read_allowed": False, "reply_allowed": False, "reason": "policy_invalid"}
        sender, group = normalize_id(sender_id), normalize_id(group_id)
        if session_kind not in {"group", "private"} or (session_kind == "group" and not group):
            return {**base, "read_allowed": False, "reply_allowed": False, "reason": "source_unknown"}
        if (sender in self.values["message_blacklist_qq"] or
                session_kind == "group" and group in self.values["message_blacklist_groups"]):
            return {**base, "read_allowed": False, "reply_allowed": False, "reason": "blacklist"}
        if not sender and self.values["message_blacklist_qq"]:
            return {**base, "read_allowed": False, "reply_allowed": False, "reason": "source_unknown"}
        white = self.values["message_whitelist_qq"] or self.values["message_whitelist_groups"]
        if white and not (sender in self.values["message_whitelist_qq"] or
                          session_kind == "group" and group in self.values["message_whitelist_groups"]):
            return {**base, "reply_allowed": False, "reason": "outside_whitelist"}
        gray = sender in self.values["message_graylist_qq"] or session_kind == "group" and group in self.values["message_graylist_groups"]
        if gray:
            return {**base, "requires_explicit": True, "reply_allowed": explicit_call is True,
                    "reason": "gray_explicit" if explicit_call is True else "gray_silent"}
        return base
