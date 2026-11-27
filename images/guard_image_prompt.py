"""生图描述映射与成功用词记录；不接触聊天配置、密钥或上游错误正文。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from astrbot.api import logger

try:
    from ..reply.guard_native_reply import NativeReplyError
except ImportError:
    from reply.guard_native_reply import NativeReplyError

CONTRACT = "livinghello.image-prompt-mapping.v1"
MAX_PROMPT_CHARS = 4000
LIMITS = {"desc_soft": 400, "desc_hard": 500, "total_hard": 900}
BUILTIN = {"version": 1, "limits": LIMITS, "blocklist": [], "rules": [
    {"id": "collar", "type": "token", "from": "neckline", "to": "collar"},
]}


def prompt_fingerprint(prompt):
    return hashlib.sha256(prompt.strip().encode()).hexdigest()


def valid_image_parameters(prompt, use_reference):
    return (isinstance(prompt, str) and 1 <= len(prompt.strip()) <= MAX_PROMPT_CHARS
            and type(use_reference) is bool)


# 三种来源使用同一校验；词表只能识别明显变化，不能证明任意文本语义等价。
SCENE_WORDS = re.compile(
    r"\b(?:symmetr\w*|asymmetr\w*|poses?|standing|sitting|lying|kneel(?:ing)?|reclin(?:e|ing)|"
    r"nude|naked|undress\w*|topless|child\w*|minor\w*|"
    r"cleavage|midriff|navel|waist|smil\w*|frown\w*|sad|unhappy|happy|teen\w*|adult\w*|boys?|girls?)\b|\d|"
    r"\bnot\b|\bno\b|without|不|没有|无|"
    r"对称|不对称|站|坐|躺|跪|姿势|表情|微笑|大笑|皱眉|裸|脱衣|"
    r"儿童|未成年|岁|露胸|腰腹|肚脐|腰部|露脐|露腰|腰", re.I)
# 颜色只在明确语境内豁免；单独的 nude / 裸仍保留检查。
COLOR_CONTEXT = re.compile(
    r"\bnude\s+(?:color|colour|beige|shade|pink|brown|lipstick|makeup|nail)\b|裸色|裸粉|裸妆", re.I)
LYING_DIRECTION = re.compile(
    r"\blying\s+(?:on\s+(?:(?:her|his|their|the)\s+)?(?P<body>back|stomach)"
    r"|face\s+(?P<face>up|down))\b", re.I)
REVIEW_NOTICE = "用词调整可能改变姿势、表情、年龄或衣着覆盖度，需要你确认。"


def scene_signature(text):
    """在完整上下文里比较受保护要点，允许已知的同义朝向与颜色措辞。"""
    text = COLOR_CONTEXT.sub("", text)
    directions = []

    def direction(match):
        upward = (match.group("body") or "").lower() == "back" or (
            match.group("face") or "").lower() == "up"
        directions.append("lying:up" if upward else "lying:down")
        return ""

    text = LYING_DIRECTION.sub(direction, text)
    def canonical(word):
        word = word.casefold()
        if word == "unhappy":
            return "sad"
        if word == "poses":
            return "pose"
        for stem in ("asymmetr", "symmetr", "kneel", "reclin", "undress", "child",
                     "minor", "smil", "frown", "teen", "adult", "boy", "girl"):
            if word.startswith(stem):
                return stem
        # 中文同义归一（2026-11-22）：同一身体部位/覆盖度概念的不同措辞
        # 指向同一签名项——「腰」与「腰部」是同一个部位，「露脐」即露出「肚脐」。
        # 改词只在「覆盖度是否改变」上判断，不再因用字差异误拒。
        zh_norm = {
            "腰部": "腰",
            "腰腹": "腰",
            "露腰": "腰",
            "露脐": "肚脐",
            "肚脐": "肚脐",
        }
        return zh_norm.get(word, word)

    return sorted(directions + [
        canonical(word)
        for word in SCENE_WORDS.findall(text)])


def equivalent_scene(before, after):
    return scene_signature(before) == scene_signature(after)


class TableValidationError(ValueError):
    """只保留固定原因和规则序号，不将文件内容带入日志或页面。"""
    def __init__(self, reason, rule_index=None):
        super().__init__(reason)
        self.reason, self.rule_index = reason, rule_index


TABLE_ERRORS = {
    "table": "版本或规则列表格式不正确",
    "limits": "长度限制格式或范围不正确",
    "blocklist": "阻断词列表格式不正确",
    "rule": "规则ID、类型或唯一性不正确",
    "token": "来源词须为1～100字的字符串，或1～20个不重复的来源词",
    "regex_unsafe": "正则不支持分组、反向引用或复杂重复",
    "regex_invalid": "正则语法不正确",
    "replacement": "替换内容必须是1～200字的字符串",
    "notice": "确认规则缺少1～200字的说明",
    "too_large": "文件超过256 KiB",
    "encoding": "文件不是有效的UTF-8文本",
    "read": "文件无法读取",
}


class PromptReviewError(NativeReplyError):
    def __init__(self, kind, notice=""):
        super().__init__("生图描述需要调整", 422)
        self.kind, self.notice = kind, notice


def token_pattern(text):
    left = r"(?<!\w)" if text and text[0].isascii() and text[0].isalnum() else ""
    right = r"(?!\w)" if text and text[-1].isascii() and text[-1].isalnum() else ""
    return re.compile(left + re.escape(text) + right, re.I)


def rule_token_pattern(source):
    """数组中的每个来源都按字面词边界匹配，长词优先，不开放复杂正则。"""
    sources = source if isinstance(source, list) else [source]
    return re.compile("|".join(token_pattern(s).pattern
                               for s in sorted(sources, key=len, reverse=True)), re.I)


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
    def __init__(self, directory, prompt_overhead=None, logger=logger):
        self.directory = directory
        self.prompt_overhead = prompt_overhead
        self.ledger = directory / "image_prompt_mappings.sqlite3" if directory else None
        self._stamp = None
        self._rules = copy.deepcopy(BUILTIN)
        self.warning = ""
        self.logger = logger
        self.rules_source = "builtin"
        self.load_status = "missing"
        self.rules_file = "image_prompt_rules.json"

    def _load_result(self, path, status, source, warning=""):
        self.load_status, self.rules_source, self.warning = status, source, warning
        # 只展示文件名；异常正文、完整路径与规则内容都不进入日志。
        name = path.name
        self.rules_file = (name if re.fullmatch(r"[\w.-]{1,100}", name)
                           and not name.lower().startswith(("sk-", "sk_")) else "自定义映射表")
        log = self.logger.info if status == "loaded" else self.logger.warning
        log("[LH-img] 映射表 file=%s mtime=%s state=%s source=%s rules=%d%s",
            self.rules_file, self._stamp[1], status, source, len(self._rules["rules"]),
            " reason=" + warning if warning else "")

    def rules(self, values):
        relative = values.get("prompt_guard_rules_path") or "image_prompt_rules.json"
        path = Path(relative)
        if not path.is_absolute():
            path = self.directory / path if self.directory else path
        try:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (str(path), None, None)
            if stamp == self._stamp:
                return self._rules
            self._stamp = stamp
            self._rules = copy.deepcopy(BUILTIN)
            self._load_result(path, "missing", "builtin", "映射表不存在或无法访问，使用内置规则。")
            return self._rules
        if stamp == self._stamp:
            return self._rules
        previous_path = self._stamp[0] if self._stamp else None
        keep_previous = previous_path == str(path) and self.rules_source in {"file", "previous"}
        self._stamp = stamp
        try:
            if stat.st_size > 256 * 1024:
                raise TableValidationError("too_large")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            self.validate_table(data)
            self._rules = data
            self._load_result(path, "loaded", "file")
        except (ValueError, OSError, UnicodeError) as exc:
            if not keep_previous:
                self._rules = copy.deepcopy(BUILTIN)
            if isinstance(exc, json.JSONDecodeError):
                reason = f"JSON语法错误（第{exc.lineno}行，第{exc.colno}列）"
            elif isinstance(exc, TableValidationError):
                prefix = f"第{exc.rule_index}条规则：" if exc.rule_index is not None else ""
                reason = prefix + TABLE_ERRORS[exc.reason]
            else:
                reason = TABLE_ERRORS["encoding" if isinstance(exc, UnicodeError) else "read"]
            fallback = "上一份有效规则" if keep_previous else "内置规则"
            self._load_result(path, "invalid", "previous" if keep_previous else "builtin",
                              f"映射表无效：{reason}；使用{fallback}。")
        return self._rules

    @staticmethod
    def validate_table(data):
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                or not isinstance(data.get("rules"), list) or len(data["rules"]) > 200):
            raise TableValidationError("table")
        limits = data.setdefault("limits", copy.deepcopy(LIMITS))
        if (not isinstance(limits, dict) or set(limits) != set(LIMITS)
                or any(type(v) is not int or not 1 <= v <= 4000 for v in limits.values())
                or not limits["desc_soft"] <= limits["desc_hard"] <= limits["total_hard"]):
            raise TableValidationError("limits")
        blocks = data.setdefault("blocklist", [])
        if (not isinstance(blocks, list) or len(blocks) > 100
                or any(not isinstance(s, str) or not 1 <= len(s) <= 100 for s in blocks)):
            raise TableValidationError("blocklist")
        ids = set()
        for index, rule in enumerate(data["rules"], 1):
            if (not isinstance(rule, dict) or not isinstance(rule.get("id"), str)
                    or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,60}", rule["id"])
                    or rule["id"] in ids
                    or not isinstance(rule.get("type"), str)
                    or rule.get("type") not in {"token", "regex", "inject_if_missing", "advisory"}):
                raise TableValidationError("rule", index)
            ids.add(rule["id"])
            kind = rule["type"]
            source = rule.get("from") if kind == "token" else rule.get("pattern")
            if kind == "token":
                sources = source if isinstance(source, list) else [source]
                if (not 1 <= len(sources) <= 20
                        or any(not isinstance(s, str) or not 1 <= len(s) <= 100
                               or not s.strip() for s in sources)
                        or len({s.casefold() for s in sources}) != len(sources)):
                    raise TableValidationError("token", index)
            else:
                try:
                    simple_pattern(source)
                except re.error:
                    raise TableValidationError("regex_invalid", index) from None
                except ValueError:
                    raise TableValidationError("regex_unsafe", index) from None
            if rule.get("when") is not None:
                # 条件正则与 pattern 走同一套安全检查，避免热更新引入灾难性回溯。
                try:
                    simple_pattern(rule["when"])
                except re.error:
                    raise TableValidationError("regex_invalid", index) from None
                except ValueError:
                    raise TableValidationError("regex_unsafe", index) from None
            if kind != "advisory" and (
                    not isinstance(rule.get("to"), str) or not 1 <= len(rule["to"]) <= 200):
                raise TableValidationError("replacement", index)
            if kind == "advisory" or rule.get("requires_confirmation"):
                if not isinstance(rule.get("notice"), str) or not 1 <= len(rule["notice"]) <= 200:
                    raise TableValidationError("notice", index)

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
                "learned_count": count, "warning": self.warning,
                "rule_count": len(rules["rules"]) if values.get("prompt_guard_enabled", True) else 0,
                "rules_source": self.rules_source, "load_status": self.load_status,
                "rules_file": self.rules_file, "rules_mtime_ns": self._stamp[1]}

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
        pending = []
        for rule in rules["rules"]:
            kind = rule["type"]
            # when：条件规则。只有 prompt 命中该正则时才生效。
            # 用于表达「款式 × 姿势」这类组合约束——单项都安全，叠加才拒
            # （实测：雪纺吊带睡裙靠沙发 200，同款侧躺 451）。
            condition = rule.get("when")
            if condition and not re.search(condition, prompt, re.I):
                continue
            pattern = rule_token_pattern(rule["from"]) if kind == "token" else simple_pattern(rule["pattern"])
            found = pattern.search(prompt)
            if (kind == "advisory" or rule.get("requires_confirmation")) and found:
                raise PromptReviewError("prompt_adjustment_required", rule["notice"])
            if kind == "token":
                # 只登记候选，统一在循环后同时替换：避免 A→B 的产物被 B→C 再吃一次
                # （历史故障：半透明薄纱 → 细网纱细网纱）。
                if found:
                    pending.append((rule, pattern))
                continue
            candidate = prompt
            if kind == "inject_if_missing":
                if not found and rule["to"].casefold() not in prompt.casefold():
                    candidate += "\n" + rule["to"]
            elif kind == "regex" and found:
                candidate = pattern.sub(lambda _: rule["to"], prompt)
            if candidate != prompt:
                if rule.get("requires_confirmation") or not equivalent_scene(prompt, candidate):
                    raise PromptReviewError("prompt_adjustment_required", rule.get("notice") or REVIEW_NOTICE)
                prompt = candidate
                result["hits"].append(rule["id"])
        if pending:
            spans = []
            for rule, pattern in pending:
                for match in pattern.finditer(prompt):
                    spans.append((match.start(), match.end(), rule))
            # 同一位置取最长匹配，重叠区间直接跳过，保证每条原文只被改写一次。
            spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
            pieces, cursor, applied = [], 0, []
            for start, end, rule in spans:
                if start < cursor:
                    continue
                pieces.extend((prompt[cursor:start], rule["to"]))
                cursor = end
                applied.append(rule)
            pieces.append(prompt[cursor:])
            candidate = "".join(pieces)
            if candidate != prompt:
                if not equivalent_scene(prompt, candidate):
                    raise PromptReviewError("prompt_adjustment_required", REVIEW_NOTICE)
                prompt = candidate
                result["hits"].extend(rule["id"] for rule in applied)
        if values.get("prompt_guard_learn_enabled", True):
            try:
                learned = self.learned(scope)
            except sqlite3.Error:
                learned = []
            # 升级前的成功记录也重新校验，不让旧漏判绕过当前规则；不删除旧数据。
            used = [p for p in learned if token_pattern(p["from"]).search(prompt)
                    and equivalent_scene(prompt, substitutions(prompt, [p]))]
            if not equivalent_scene(prompt, substitutions(prompt, used)):
                used = []
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
                    or pair["from"] == pair["to"] or pair["from"].casefold() in seen
                    or not token_pattern(pair["from"]).search(prompt)):
                return {**base, "status": "review", "notice": REVIEW_NOTICE}
            seen.add(pair["from"].casefold())
            clean.append({"from": pair["from"], "to": pair["to"]})
        candidate = substitutions(prompt, clean)
        shortened = proposal.get("shortened_prompt")
        if shortened is not None:
            # 全文压缩：只要确实更短、且在总长限制内就接受。
            # 旧逻辑要求「原 prompt 超过 desc_hard」才允许压缩，导致
            # 一百多字（本地能过、上游必拒）的描述没有任何自救通道。
            if (clean or not isinstance(shortened, str) or not shortened.strip()
                    or len(shortened.strip()) >= len(prompt)
                    or len(shortened.strip()) > policy["limits"]["total_hard"]):
                return {**base, "status": "invalid"}
            candidate = shortened.strip()
        if candidate == prompt:
            return {**base, "status": "noop"}
        if not equivalent_scene(prompt, candidate):
            return {**base, "status": "review", "notice": REVIEW_NOTICE}
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
