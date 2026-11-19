"""生图描述映射与成功用词记录；不接触聊天配置、密钥或上游错误正文。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

try:
    from ..reply.guard_native_reply import NativeReplyError
except ImportError:
    from reply.guard_native_reply import NativeReplyError

CONTRACT = "livinghello.image-prompt-mapping.v1"
LIMITS = {"desc_soft": 400, "desc_hard": 500, "total_hard": 900}
BUILTIN = {"version": 1, "limits": LIMITS, "blocklist": [], "rules": [
    {"id": "collar", "type": "token", "from": "neckline", "to": "collar"},
]}
# 自动学习只接受局部等义用词。姿势、表情、年龄、衣着覆盖度等不能借“等义”改掉。
SCENE_WORDS = re.compile(
    r"symmetr|asymmetr|pose|standing|sitting|lying|nude|naked|undress|child|minor|"
    r"cleavage|midriff|navel|waist|smil|frown|sad|happy|teen|adult|boy|girl|\d|"
    r"\bnot\b|\bno\b|without|不|没有|无|"
    r"对称|不对称|站|坐|躺|跪|姿势|表情|微笑|大笑|皱眉|裸|脱衣|"
    r"儿童|未成年|岁|露胸|露腰|肚脐|腰部", re.I)


class PromptReviewError(NativeReplyError):
    def __init__(self, kind, notice=""):
        super().__init__("生图描述需要调整", 422)
        self.kind, self.notice = kind, notice


def token_pattern(text):
    left = r"(?<!\w)" if text and text[0].isascii() and text[0].isalnum() else ""
    right = r"(?!\w)" if text and text[-1].isascii() and text[-1].isalnum() else ""
    return re.compile(left + re.escape(text) + right, re.I)


def simple_pattern(text):
    # 运维表只支持无分组/反向引用的简单正则，避免热更新引入灾难性回溯。
    if (not isinstance(text, str) or not 1 <= len(text) <= 200
            or "(" in text or ")" in text or re.search(r"\\[1-9]|[+*?}][+*?{]", text)
            or len(re.findall(r"(?<!\\)[*+]", text)) > 1):
        raise ValueError("unsafe regex")
    return re.compile(text, re.I)


def substitutions(prompt, pairs):
    """同时替换，避免 A→B、B→C 在同一请求中连锁；英文按词边界匹配。"""
    patterns = [(token_pattern(p["from"]), p["to"]) for p in pairs]
    hits = []
    for pattern, target in patterns:
        for match in pattern.finditer(prompt):
            hits.append((match.start(), match.end(), target))
    result, cursor = [], 0
    for start, end, target in sorted(hits, key=lambda hit: (hit[0], -hit[1])):
        if start < cursor:
            continue
        result.extend((prompt[cursor:start], target))
        cursor = end
    result.append(prompt[cursor:])
    return "".join(result)


class ImagePromptMapping:
    def __init__(self, directory, prompt_overhead=None):
        self.directory = directory
        self.prompt_overhead = prompt_overhead
        self.ledger = directory / "image_prompt_mappings.sqlite3" if directory else None
        self._stamp = None
        self._rules = copy.deepcopy(BUILTIN)
        self.warning = ""

    def rules(self, values):
        relative = values.get("prompt_guard_rules_path") or "image_prompt_rules.json"
        path = Path(relative)
        if not path.is_absolute():
            path = self.directory / path if self.directory else path
        try:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp = (str(path), None, None)
            self._rules = copy.deepcopy(BUILTIN)
            self.warning = "映射表不存在，使用内置规则。"
            return self._rules
        if stamp == self._stamp:
            return self._rules
        previous_path = self._stamp[0] if self._stamp else None
        self._stamp = stamp
        try:
            if stat.st_size > 256 * 1024:
                raise ValueError("rules too large")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            self.validate_table(data)
            self._rules = data
            self.warning = ""
        except (ValueError, OSError, TypeError, re.error):
            if previous_path != str(path):
                self._rules = copy.deepcopy(BUILTIN)
            self.warning = "映射表无效，继续使用上一份有效规则或内置规则。"
        return self._rules

    @staticmethod
    def validate_table(data):
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                or not isinstance(data.get("rules"), list) or len(data["rules"]) > 200):
            raise ValueError("invalid table")
        limits = data.setdefault("limits", copy.deepcopy(LIMITS))
        if (set(limits) != set(LIMITS)
                or any(type(v) is not int or not 1 <= v <= 4000 for v in limits.values())
                or not limits["desc_soft"] <= limits["desc_hard"] <= limits["total_hard"]):
            raise ValueError("invalid limits")
        blocks = data.setdefault("blocklist", [])
        if (not isinstance(blocks, list) or len(blocks) > 100
                or any(not isinstance(s, str) or not 1 <= len(s) <= 100 for s in blocks)):
            raise ValueError("invalid blocklist")
        ids = set()
        for rule in data["rules"]:
            if (not isinstance(rule, dict) or not isinstance(rule.get("id"), str)
                    or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,60}", rule["id"])
                    or rule["id"] in ids
                    or rule.get("type") not in {"token", "regex", "inject_if_missing", "advisory"}):
                raise ValueError("invalid rule")
            ids.add(rule["id"])
            kind = rule["type"]
            source = rule.get("from") if kind == "token" else rule.get("pattern")
            if kind == "token":
                if not isinstance(source, str) or not 1 <= len(source) <= 100:
                    raise ValueError("invalid token")
            else:
                simple_pattern(source)
            if kind != "advisory" and (
                    not isinstance(rule.get("to"), str) or not 1 <= len(rule["to"]) <= 200):
                raise ValueError("invalid replacement")
            if kind == "advisory" or rule.get("requires_confirmation"):
                if not isinstance(rule.get("notice"), str) or not 1 <= len(rule["notice"]) <= 200:
                    raise ValueError("missing notice")

    @staticmethod
    def scope(values, connection):
        base, _, model = connection
        identity = [values["provider_id"], base.rstrip("/"), model, values["protocol"]]
        return {"key": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                "provider_id": values["provider_id"], "model": model, "protocol": values["protocol"]}

    def connect(self):
        db = sqlite3.connect(self.ledger, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS mappings (scope TEXT, source TEXT, target TEXT, "
                   "successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
                   "updated REAL NOT NULL, PRIMARY KEY(scope,source,target))")
        db.execute("CREATE TABLE IF NOT EXISTS observations (request_id TEXT PRIMARY KEY, "
                   "scope TEXT NOT NULL, original_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL, "
                   "status TEXT NOT NULL, created REAL NOT NULL)")
        return db

    def learned(self, scope):
        if not self.ledger or not self.ledger.exists():
            return []
        db = self.connect()
        try:
            return [{"from": row[0], "to": row[1], "successes": row[2]} for row in db.execute(
                "SELECT source,target,successes FROM mappings WHERE scope=? AND successes>failures "
                "ORDER BY updated DESC LIMIT 200", (scope["key"],))]
        finally:
            db.close()

    def limits(self, values, rules):
        limits = copy.deepcopy(rules["limits"])
        if self.prompt_overhead:
            # 时间注入也占总长，给 LE 的压缩预算必须与实际请求一致。
            room = max(0, limits["total_hard"] - self.prompt_overhead(values))
            limits["desc_hard"] = min(limits["desc_hard"], room)
            limits["desc_soft"] = min(limits["desc_soft"], limits["desc_hard"])
        return limits

    def policy(self, values, connection):
        rules = self.rules(values)
        scope = self.scope(values, connection)
        try:
            count = len(self.learned(scope))
        except sqlite3.Error:
            count = 0
        return {"contract": CONTRACT, "enabled": values.get("prompt_guard_enabled", True),
                "retry_limit": max(0, min(10, int(values.get("prompt_guard_retry_limit", 2)))),
                "learn_enabled": values.get("prompt_guard_learn_enabled", True),
                "limits": self.limits(values, rules), "scope": scope,
                "learned_count": count, "warning": self.warning}

    def apply(self, values, connection, original, repair=None):
        scope = self.scope(values, connection)
        result = {"original": original, "prompt": original, "scope": scope, "hits": [],
                  "candidates": [], "learned": []}
        if not values.get("prompt_guard_enabled", True):
            return result
        rules = self.rules(values)
        prompt = original
        if repair:
            # 重启恢复的候选只在同一实际连接/模型下有效，且再做一次结构校验。
            if repair.get("scope") != scope or repair.get("contract") != CONTRACT:
                raise PromptReviewError("prompt_adjustment_required", "生图模型已变化，需要重新确认画面。")
            approved = self.prepare_repair(values, connection, original, repair.get("proposal"))
            if approved.get("status") != "ready":
                raise PromptReviewError("prompt_adjustment_required", approved.get("notice", "请调整画面。"))
            prompt = approved["prompt"]
            result["candidates"] = approved["candidates"]
        for blocked in rules["blocklist"]:
            if token_pattern(blocked).search(prompt):
                raise PromptReviewError("prompt_adjustment_required", "这段描述需要你确认调整，我先保留原画面。")
        for rule in rules["rules"]:
            kind = rule["type"]
            pattern = token_pattern(rule["from"]) if kind == "token" else simple_pattern(rule["pattern"])
            found = pattern.search(prompt)
            if kind in {"token", "regex"} and found:
                before = sorted(word.casefold() for word in SCENE_WORDS.findall(found.group()))
                after = sorted(word.casefold() for word in SCENE_WORDS.findall(rule["to"]))
                if before != after:
                    raise PromptReviewError("prompt_adjustment_required",
                        rule.get("notice") or "这条用词替换会改变画面，需要你确认后再试。")
            if (kind == "advisory" or rule.get("requires_confirmation")) and found:
                raise PromptReviewError("prompt_adjustment_required", rule["notice"])
            if kind == "inject_if_missing":
                if not found and rule["to"].casefold() not in prompt.casefold():
                    prompt += "\n" + rule["to"]
                    result["hits"].append(rule["id"])
            elif kind in {"token", "regex"} and found:
                prompt = pattern.sub(lambda _: rule["to"], prompt)
                result["hits"].append(rule["id"])
        if values.get("prompt_guard_learn_enabled", True):
            try:
                learned = self.learned(scope)
            except sqlite3.Error:
                learned = []
            used = [p for p in learned if token_pattern(p["from"]).search(prompt)]
            prompt = substitutions(prompt, used)
            result["learned"] = used
            result["hits"].extend("learned" for _ in used)
        limits = self.limits(values, rules)
        if len(prompt) > limits["desc_hard"]:
            raise PromptReviewError("prompt_too_long", "描述太长，需要保留画面要点后缩短。")
        result["prompt"] = prompt
        result["limits"] = limits
        return result

    def prepare_repair(self, values, connection, prompt, proposal):
        policy = self.policy(values, connection)
        base = {"contract": CONTRACT, "scope": policy["scope"]}
        if not policy["enabled"] or policy["retry_limit"] == 0:
            return {**base, "status": "disabled"}
        if not isinstance(proposal, dict) or proposal.get("changes_scene") is not False:
            notice = proposal.get("notice") if isinstance(proposal, dict) else None
            return {**base, "status": "review", "notice":
                    notice[:200] if isinstance(notice, str) and notice.strip()
                    else "新的画面需要你确认后再试。"}
        pairs = proposal.get("substitutions", [])
        if not isinstance(pairs, list) or len(pairs) > 8:
            return {**base, "status": "invalid"}
        clean, seen = [], set()
        for pair in pairs:
            if (not isinstance(pair, dict) or set(pair) != {"from", "to"}
                    or any(not isinstance(pair[k], str) or not 1 <= len(pair[k].strip()) <= 60
                           or re.search(r"[\n\r,.;，。；<>]", pair[k]) for k in ("from", "to"))
                    or pair["from"] == pair["to"] or SCENE_WORDS.search(pair["from"])
                    or SCENE_WORDS.search(pair["to"]) or pair["from"].casefold() in seen
                    or not token_pattern(pair["from"]).search(prompt)):
                return {**base, "status": "review", "notice": "用词调整可能改变画面，需要你确认。"}
            seen.add(pair["from"].casefold())
            clean.append({"from": pair["from"], "to": pair["to"]})
        candidate = substitutions(prompt, clean)
        shortened = proposal.get("shortened_prompt")
        if shortened is not None:
            # 全文压缩仅针对超长描述，不作为可复用替换规则学习。
            if (clean or len(prompt) <= policy["limits"]["desc_hard"]
                    or not isinstance(shortened, str) or not shortened.strip()
                    or len(shortened.strip()) > policy["limits"]["desc_hard"]):
                return {**base, "status": "invalid"}
            candidate = shortened.strip()
        if candidate == prompt:
            return {**base, "status": "noop"}
        return {**base, "status": "ready", "prompt": candidate, "candidates": clean,
                "proposal": copy.deepcopy(proposal)}

    def observe(self, request_id, result, status):
        """成功图片已解析才记词；失败降权。重复回执不累加，整段原文只存哈希。"""
        if not self.ledger or not result or status not in {"success", "content_rejected"}:
            return
        pairs = result["candidates"] + result["learned"]
        if not pairs:
            return
        db = self.connect()
        try:
            with db:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                    (request_id, result["scope"]["key"],
                     hashlib.sha256(result["original"].encode()).hexdigest(),
                     hashlib.sha256(result["prompt"].encode()).hexdigest(), status, time.time()))
                if not inserted.rowcount:
                    return
                for pair in { (p["from"], p["to"]) for p in pairs }:
                    if status == "success":
                        db.execute("UPDATE mappings SET failures=MAX(failures,successes) "
                                   "WHERE scope=? AND source=? AND target<>?",
                                   (result["scope"]["key"], *pair))
                    db.execute(
                        "INSERT INTO mappings VALUES (?,?,?,?,?,?) ON CONFLICT(scope,source,target) "
                        "DO UPDATE SET successes=successes+excluded.successes, "
                        "failures=failures+excluded.failures,updated=excluded.updated",
                        (result["scope"]["key"], *pair, int(status == "success"),
                         int(status == "content_rejected"), time.time()))
                db.execute("DELETE FROM mappings WHERE rowid IN "
                           "(SELECT rowid FROM mappings ORDER BY updated DESC LIMIT -1 OFFSET 1000)")
                db.execute("DELETE FROM observations WHERE created<?", (time.time() - 30 * 86400,))
        finally:
            db.close()
