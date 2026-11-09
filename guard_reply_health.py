"""回复闭环监测：只认最终投递证据；管理员提醒共用持久化的十分钟额度。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, closing
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain, Reply

KEY = "_livinghello_reply_health"
WINDOW = 600
# 可单独勾选通知的模型；tts 由宿主执行，只能间接感知，默认不提醒。
MODEL_WATCH = {
    "classifier": "判断模型",
    "vision": "判定看图模型",
    "understand_image": "图片理解模型",
    "understand_voice": "语音转写模型",
    "main_model": "主对话模型",
    "tts": "语音回复模型",
    "image_gen": "AI 生图模型",
}
LAYERS = {"classifier", "preparation", "main_model", "delivery", "pipeline"}
INTENTIONAL = {
    "message_recalled", "policy_changed", "blacklist_user", "blacklist_group",
    "whitelist_user", "whitelist_group", "graylist_user", "graylist_group",
    "no_reply", "new_message", "stale_context", "expired", "duplicate",
    "message_expired", "context_changed",
}
CATEGORIES = {"timeout", "connection_error", "http_429", "http_5xx", "empty_response",
              "protocol_error", "authentication_error", "provider_missing",
              "configuration_error", "send_unknown", "final_message_missing",
              "model_reply_missing", "completion_timeout", "preparation_failed",
              "quota_exhausted", "model_execution_error"}
ERROR_LABELS = {
    "quota_exhausted": "上游账户余额或额度不足",
    "timeout": "模型请求超时", "connection_error": "上游连接失败",
    "http_429": "上游限流", "http_5xx": "上游服务异常",
    "authentication_error": "上游认证或权限异常",
    "empty_response": "模型返回为空", "model_execution_error": "模型执行异常",
    "model_reply_missing": "未取得最终模型回复",
    "final_message_missing": "最终回复未确认发送",
    "send_unknown": "回复发送失败或结果未确认",
    "completion_timeout": "回复流程超时", "preparation_failed": "消息准备失败",
}


def safe_excerpt(value, limit=160):
    """提醒只带当前消息摘录，凭据、地址、文件路径和大编码先脱敏。"""
    text = str(value or "")
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*", "[私钥已隐藏]", text)
    text = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:sk-|sessdata=)[^\s,，;；]+)", "[凭据已隐藏]", text)
    text = re.sub(r"(?i)(?:api[_-]?key|token|password|密码|secret|authorization|bili_jct)"
                  r"\s*[=:：]\s*[^\s,，;；]+", "[凭据已隐藏]", text)
    text = re.sub(r"(?:https?://|data:)[^\s]+", "[地址已隐藏]", text)
    text = re.sub(r"(?:/(?:Users|home|root|opt|var|tmp|etc)/|[A-Za-z]:\\)[^\s]+",
                  "[路径已隐藏]", text)
    text = re.sub(r"[A-Za-z0-9+/=_-]{64,}", "[长编码已隐藏]", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def error_category(value):
    text = str(value or "").casefold()
    if any(word in text for word in ("not enough quota", "insufficient_quota",
                                     "insufficient balance", "余额不足", "额度不足")):
        return "quota_exhausted"
    if "429" in text or "rate limit" in text:
        return "http_429"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if re.search(r"\b50[0-9]\b", text):
        return "http_5xx"
    if "connection" in text:
        return "connection_error"
    if re.search(r"\b(?:401|403)\b", text):
        return "authentication_error"
    return "model_execution_error"


def qq_ids(raw):
    if isinstance(raw, str):
        raw = re.split(r"[\s,，;；]+", raw.strip())
    if not isinstance(raw, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(v).strip() for v in raw
                             if not isinstance(v, bool)
                             and re.fullmatch(r"[1-9][0-9]{0,19}", str(v).strip())))[:20]


def send_accepted(value):
    """AstrBot 的 None 是标准成功返回；显式负回执不算成功。"""
    if value is False:
        return False
    if isinstance(value, dict):
        if value.get("status") in {"failed", "error"} or value.get("success") is False:
            return False
        if value.get("retcode") not in (None, 0, "0"):
            return False
    return True


def substantive(chain):
    return any(not isinstance(c, (At, Reply))
               and (not isinstance(c, Plain) or c.text.strip()) for c in chain or [])


class ReminderStore:
    """先原子认领再发送。崩溃/超时不释放额度，不重试未知写入。"""
    def __init__(self, directory):
        self.path = Path(directory) / "reply_health.sqlite3" if directory else None
        self.health = "ready" if self.path else "unavailable"

    @contextmanager
    def _db(self):
        if self.path is None:
            raise OSError("storage unavailable")
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            self.path.chmod(0o600)
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS health (id INTEGER PRIMARY KEY, payload TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS reminder (id INTEGER PRIMARY KEY, claimed REAL)")
                yield db

    def read(self):
        try:
            with self._db() as db:
                row = db.execute("SELECT payload FROM health WHERE id=1").fetchone()
                claim = db.execute("SELECT claimed FROM reminder WHERE id=1").fetchone()
            self.health = "ready"
            return (json.loads(row[0]) if row else {}), (claim[0] if claim else None)
        except (OSError, sqlite3.Error, ValueError):
            self.health = "unavailable"
            return {}, None

    def save(self, summary):
        try:
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO health VALUES (1,?)",
                           (json.dumps(summary, ensure_ascii=False),))
            self.health = "ready"
        except (OSError, sqlite3.Error):
            self.health = "unavailable"

    def claim(self, now, window=WINDOW):
        if not math.isfinite(now):
            return False
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT claimed FROM reminder WHERE id=1").fetchone()
                # 时间倒退继续抑制，不能因重载或时钟倒退重新获得额度。
                if row and now - row[0] < window:
                    return False
                db.execute("INSERT OR REPLACE INTO reminder VALUES (1,?)", (now,))
            self.health = "ready"
            return True
        except (OSError, sqlite3.Error):
            self.health = "unavailable"
            return False


def report_model_fault(plugin, key, category, detail="", event=None):
    """后台触发一次模型级提醒；未启用或未配置监测时静默跳过。"""
    factory = getattr(plugin, "_reply_health", None)
    if not callable(factory):
        return
    try:
        task = asyncio.ensure_future(factory().model_fault(
            key, category, detail=detail, event=event))
    except Exception:
        return
    bucket = getattr(plugin, "_model_fault_tasks", None)
    if bucket is None:
        bucket = set()
        try:
            plugin._model_fault_tasks = bucket
        except Exception:
            return
    bucket.add(task)
    task.add_done_callback(bucket.discard)


class ReplyHealth:
    MAX_ACTIVE = 256

    def __init__(self, plugin, clock=time.time):
        self.plugin, self.clock = plugin, clock
        self.store = ReminderStore(getattr(plugin, "data_dir", None))
        self.summary, self.claimed_at = self.store.read()
        self.active, self.tasks = {}, set()
        self.closed = False
        self.notification = "not_attempted"
        self.state_lock = asyncio.Lock()
        # 首次升级时继承旧通知的最近时间，不能升级后立刻重复 @。
        legacy = getattr(plugin, "classifier_outage", None)
        try:
            latest = legacy.current_event or legacy.store.load_latest()
            stamps = [float(n.sent_at) for n in latest.notifications] if latest else []
            if self.claimed_at is None and stamps and self.store.claim(max(stamps)):
                self.claimed_at = max(stamps)
        except (AttributeError, TypeError, ValueError):
            pass

    @property
    def enabled(self):
        return self.plugin.config.get("classifier_outage_recovery_enabled", False) is True

    def admins(self):
        explicit = self.plugin.config.get("outage_notify_admin_qq", [])
        if explicit:
            return qq_ids(explicit)
        try:
            return qq_ids(self.plugin.context.get_config().get("admins_id", []))
        except Exception:
            return []

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @staticmethod
    async def storage_call(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # SQLite线程不可取消；重载/测试清理前必须等它完成，避免后台迟写。
            await asyncio.shield(task)
            raise

    def watch(self, event, *, expected=False):
        if self.closed:
            return None
        row = event.get_extra(KEY)
        if row is not None:
            row["expected"] |= expected
            return row
        if len(self.active) >= self.MAX_ACTIVE:
            self.plugin.logger.warning("[LivingHello] 回复监测容量已满。")
            return None
        row = {"event": event, "service": self, "expected": expected, "model_done": False,
               "sent": 0, "failed": False, "fault": None, "ended": False,
               "token": object(), "owner": None, "started": self.clock()}
        event.set_extra(KEY, row)
        self.active[id(event)] = row
        owner = asyncio.current_task()
        coro = owner.get_coro() if owner else None
        # 只能绑定真实 pipeline 任务；不能误绑插件回调或整个 EventBus。
        if getattr(coro, "__qualname__", "").endswith("PipelineScheduler.execute"):
            row["owner"] = owner
            owner.add_done_callback(lambda task: self._pipeline_done(row, task))
        row["watchdog"] = self.spawn(self._watchdog(row))
        return row

    def provider(self, event, provider):
        """只记录实际本轮使用的模型名称，禁止读取上游密钥与地址。"""
        row = self.watch(event, expected=True)
        if not row or provider is None:
            return
        try:
            meta = provider.meta()
            row["provider_id"] = safe_excerpt(getattr(meta, "id", ""))
            row["model"] = safe_excerpt(provider.get_model())
            row["provider_evidence"] = "agent_begin"
        except Exception:
            pass

    async def suppress_native_error(self, event, message, *, native_exception=None):
        """同时覆盖宿主直发和结果发送；只认异常类型/宿主异常上下文。"""
        row = event.get_extra(KEY)
        if not row or not event.get_group_id():
            return False
        kind = getattr(getattr(message, "result_content_type", None), "name", "")
        text = "\n".join(str(c.text) for c in getattr(message, "chain", []) or []
                         if isinstance(c, Plain))
        typed = kind == "AGENT_RUNNER_ERROR"
        native_text = (row["expected"] and not row["model_done"] and text.startswith(
            ("Error occurred while processing agent request:",
             "Error occurred during AI execution.\nError Type:")))
        if not (typed or native_text or native_exception is not None):
            return False
        # 类型/宿主except已证明Agent失败；初始化异常可能早于on_agent_begin。
        row["expected"] = True
        # 不把宿主异常原文转发给任何聊天；类别足够定位，原文留在宿主日志。
        row["fault"] = ("main_model", error_category(
            native_exception if native_exception is not None else text))
        row["failed"] = True
        if hasattr(message, "chain"):
            message.chain = []
        if hasattr(message, "async_stream"):
            message.async_stream = None
        await self.finish(event, pipeline_error=True)
        return True

    def record_failure(self, event, layer, category, at):
        if event is None:
            return {}
        trace = event.get_extra("_livinghello_trace")
        identity = trace.identity(event) if trace else {
            "request_id": event.get_extra("_livinghello_request_id") or uuid.uuid4().hex,
            "message_id": str(getattr(getattr(event, "message_obj", None), "message_id", "")),
        }
        event.set_extra("_livinghello_request_id", identity["request_id"])
        row = event.get_extra(KEY) or {}
        fault = {"layer": layer, "category": category, "at": at,
                 **{key: row[key] for key in ("provider_id", "model", "provider_evidence")
                    if row.get(key)}}
        # 只记录当前消息；不复制历史图片、引用正文或其他会话的上下文。
        group = str(event.get_group_id() or "")
        fallback = {"ts": int(at), "gid": group,
                    "session_id": str(getattr(event, "unified_msg_origin", "")),
                    "session_type": "group" if group else "private",
                    "category": "group" if group and getattr(event, "is_at_or_wake_command", False)
                    else "public" if group else "private",
                    "mode": self.plugin.config.get("gate_mode", "enforce"),
                    "chat_data": {"current_message": {
                        "text": safe_excerpt(getattr(event, "message_str", ""), 300),
                        "message_id": identity["message_id"], "group_id": group}},
                    "gen_skipped": False}
        log = getattr(getattr(self.plugin, "engine", None), "log", None)
        if log:
            log.reply_failure(identity, fault, fallback)
        return {**identity, **fault}

    def _pipeline_done(self, row, task):
        if self.closed or row["ended"]:
            return
        error = task.cancelled()
        if not error:
            error = task.exception() is not None
        self.spawn(self.finish(row["event"], pipeline_error=error))

    async def _watchdog(self, row):
        raw = self.plugin.config.get("reply_health_timeout_seconds", 900)
        try:
            seconds = max(60, min(7200, float(raw)))
            if not math.isfinite(seconds):
                seconds = 900
        except (TypeError, ValueError):
            seconds = 900
        await asyncio.sleep(seconds)
        if not row["ended"] and not self.closed:
            await self.finish(row["event"], timed_out=True)

    def model_done(self, event, response):
        row = self.watch(event, expected=True)
        if row:
            row["model_done"] = response is not None

    def classifier_failed(self, event, category):
        row = self.watch(event)
        if row:
            row["fault"] = ("classifier", category)

    def arm(self, event, result, *, streaming=False):
        row = event.get_extra(KEY)
        if not row or not row["expected"] or not row["model_done"]:
            return
        if not streaming and not getattr(result, "is_llm_result", lambda: False)():
            return
        if getattr(result, "_livinghello_health_armed", False):
            return
        derive = getattr(result, "derive", None)
        try:
            result._livinghello_reply_token = row["token"]
            result._livinghello_health_armed = True
            if callable(derive):
                def tagged(chain=None):
                    packet = derive(chain)
                    packet._livinghello_reply_token = row["token"]
                    return packet
                result.derive = tagged
        except (AttributeError, TypeError):
            pass  # 不支持身份标记的宿主只能给出未确认，不能猜测成功。

    def sent(self, event, message, accepted):
        row = event.get_extra(KEY)
        if not row or getattr(message, "_livinghello_reply_token", None) is not row["token"]:
            return
        if accepted:
            row["sent"] += int(substantive(getattr(message, "chain", [])))
        else:
            row["failed"] = True

    async def finish(self, event, *, pipeline_error=False, timed_out=False):
        row = event.get_extra(KEY)
        if not row or row["ended"] or self.closed:
            return
        row["ended"] = True
        self.active.pop(id(event), None)
        watchdog = row.get("watchdog")
        if watchdog and watchdog is not asyncio.current_task():
            watchdog.cancel()
        blocked = str(event.get_extra("_at_guard_delivery_blocked") or "")
        policy = event.get_extra("_livinghello_message_policy") or {}
        intentional = blocked in INTENTIONAL or (policy and not policy.get("reply_allowed", True))
        plan = event.get_extra("_at_guard_delivery_plan") or {}
        success = (row["model_done"] and row["sent"] > 0 and not row["failed"]
                   and not plan.get("failed") and not pipeline_error and not timed_out)
        trace = event.get_extra("_livinghello_trace")
        if intentional or (not row["expected"] and not row["fault"]):
            if trace and intentional:
                trace.finish(event, "silent" if policy and not policy.get("reply_allowed", True)
                             else "cancelled", blocked or policy.get("reason", "intentional_silence"))
            return
        if success:
            if trace:
                trace.finish(event, "completed", "final_delivery_confirmed")
            async with self.state_lock:
                if not self.summary.get("failed_at") or row["started"] >= self.summary["failed_at"]:
                    self.summary["recovered_at"] = self.clock()
                    self.summary["state"] = "healthy"
                    await self.storage_call(self.store.save, dict(self.summary))
            return
        self.plugin.logger.warning(
            "[LivingHello] 回复流程未确认完成: model_done=%r sent=%r failed=%r fault=%r "
            "plan_failed=%r blocked=%r expected=%r",
            row["model_done"], row["sent"], row["failed"], row["fault"],
            bool(plan.get("failed")), blocked, row["expected"],
        )
        layer, category = row["fault"] or (
            ("delivery", "send_unknown" if row["failed"] else "final_message_missing")
            if row["model_done"] else ("main_model", "model_reply_missing"))
        if timed_out:
            layer, category = "pipeline", "completion_timeout"
        if trace:
            trace.end(event, event.get_extra("_livinghello_model_span"),
                      "timeout" if timed_out else "failed", reason=category)
            trace.record(event, "delivery", "unknown" if row["failed"] else "failed", reason=category)
            trace.finish(event, "incomplete" if timed_out else "failed", category)
        await self.failure(layer, category, event=event)

    async def failure(self, layer, category, *, event=None, platform_id="", at=None):
        if self.closed:
            return 0
        now = self.clock() if at is None else float(at)
        layer = layer if layer in LAYERS else "pipeline"
        category = category if category in CATEGORIES else "model_reply_missing"
        # 留痕独立于提醒开关和提醒节流，避免“群里没回复、列表仍显示放行”。
        self.record_failure(event, layer, category, now)
        async with self.state_lock:
            self.summary.update(state="failed", failed_at=now, recovered_at=None,
                                layer=layer if layer in LAYERS else "pipeline",
                                category=category if category in CATEGORIES else "model_reply_missing")
            await self.storage_call(self.store.save, dict(self.summary))
        watched = {"classifier": "classifier", "main_model": "main_model",
                   "preparation": "vision"}.get(layer)
        if watched and not self.watch_model(watched):
            return 0
        return await self.notify(event=event, platform_id=platform_id, at=now)

    def window_seconds(self):
        """两次提醒的最小间隔，按分钟配置，钳在 1 分钟到 24 小时。"""
        raw = self.plugin.config.get("outage_notify_interval_minutes", 10)
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            minutes = 10.0
        if not math.isfinite(minutes):
            minutes = 10.0
        return max(1.0, min(1440.0, minutes)) * 60.0

    def watch_model(self, key):
        if key not in MODEL_WATCH or not self.enabled:
            return False
        default = False if key == "tts" else True
        return self.plugin.config.get("outage_watch_" + key, default) is True

    async def model_fault(self, key, category, *, detail="", event=None, platform_id=""):
        """单个模型调用失败时提醒管理员；与全局提醒共用同一去抖额度。"""
        if not self.watch_model(key):
            return 0
        label = MODEL_WATCH.get(key, key)
        if str(category) == "bug":
            # 代码缺陷：换一套说法，别再让人去翻 provider 配置。
            message = (f"【LivingHello】{label}这轮没走完，原因是插件内部错误。"
                       "模型和网络都没问题，不需要改配置。")
        else:
            clean_category = str(category) if str(category) in CATEGORIES else error_category(category)
            message = (f"【LivingHello】{label}调用失败："
                       f"{ERROR_LABELS.get(clean_category, clean_category)}。"
                       "刚才那一轮没能正常完成，麻烦看一下 provider 配置。")
        # 异常原文可能包含凭据，不向聊天转发。
        return await self.notify(event=event, platform_id=platform_id, message=message)

    async def notify(self, *, event=None, platform_id="", at=None, message=None):
        if self.closed or not self.enabled:
            return 0
        admins = self.admins()
        if not admins:
            self.notification = "missing_admin_qq"
            self.plugin.logger.warning("[LivingHello] 未配置有效管理员 QQ，断线提醒仅保留状态。")
            return 0
        if event is not None:
            platform_id = self.plugin._notification_platform_id(event)
        if not platform_id or not re.fullmatch(r"[\w.-]{1,128}", platform_id):
            self.notification = "missing_platform"
            return 0
        # 只在 QQ/OneBot 平台定位管理员私聊，不把 QQ 号映射为其他平台用户。
        if event is not None:
            try:
                kind = str(event.get_platform_name() or "").lower()
            except Exception:
                kind = platform_id.lower()
            if not any(word in kind for word in ("aiocqhttp", "onebot", "qq", "napcat")):
                self.notification = "unsupported_platform"
                return 0
        elif not any(word in platform_id.lower() for word in ("onebot", "qq", "napcat", "aiocqhttp")):
            self.notification = "unsupported_platform"
            return 0
        now = self.clock() if at is None else float(at)
        if not await self.storage_call(self.store.claim, now, self.window_seconds()):
            self.notification = "throttled" if self.store.health == "ready" else "storage_unavailable"
            return 0
        self.claimed_at = now
        category = str(self.summary.get("category", "model_reply_missing"))
        text = message or ("【LivingHello 回复异常】\n原因：" +
                           ERROR_LABELS.get(category, category))
        if event is not None:
            group = str(event.get_group_id() or "")
            text += "\n来源：" + ("群 " + safe_excerpt(group, 32) if group else "私聊")
            rid = str(event.get_extra("_livinghello_request_id") or "")
            if rid:
                text += "\n请求编号：" + rid
            mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
            if mid:
                text += "\n消息编号：" + safe_excerpt(mid, 64)
            if group:
                text += "\n消息摘录：" + (safe_excerpt(getattr(event, "message_str", "")) or "[媒体消息]")
            row = event.get_extra(KEY) or {}
            if row.get("model"):
                label = "报错时模型" if row.get("provider_evidence") == "native_error_runner" else "本轮起始模型"
                text += "\n" + label + "：" + row["model"]
            text += "\n可在“最近判断”搜索请求编号查看详情。"
        routes = [f"{platform_id}:FriendMessage:{admin}" for admin in admins]
        count = 0
        for route in routes:
            # 私聊管理员，纯文本通知，不带原群引用或群内 @。
            chain = MessageChain()
            chain.chain = [Plain(text)]
            try:
                result = await self.plugin.context.send_message(route, chain)
                count += int(send_accepted(result))
            except Exception:
                pass  # 未知发送保持本轮已认领，禁止自动重发。
        self.notification = "sent" if count == len(routes) else "send_unknown"
        return count

    def snapshot(self):
        return {**self.summary, "monitoring": len(self.active),
                "notification": self.notification, "storage": self.store.health,
                "admins": self.admins(),
                "reminder_minutes": max(1, int(self.window_seconds() // 60)),
                "reminder_remaining_seconds": (
                    max(0, math.ceil(self.window_seconds() - (self.clock() - self.claimed_at)))
                    if self.claimed_at is not None else 0)}

    async def close(self):
        self.closed = True
        # 不取消真实 pipeline；重载自身的 observer 和发送任务即可。
        tasks = [t for t in self.tasks if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
