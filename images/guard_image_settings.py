"""LivingHello 独立生图：复用已注册连接、统一额度和当前会话投递。"""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import random
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit
from types import SimpleNamespace

try:
    from ..reply.guard_native_reply import NativeReplyError, revision
    from ..integrations.guard_router_connections import router_connections, CHAT_TYPES
    from ..observability.guard_request_trace import emit, TRACE_KEY, touch
    from ..reply.guard_reply_health import report_model_fault
except ImportError:
    from reply.guard_native_reply import NativeReplyError, revision
    from integrations.guard_router_connections import router_connections, CHAT_TYPES
    from observability.guard_request_trace import emit, TRACE_KEY, touch
    from reply.guard_reply_health import report_model_fault

TOOL_NAME = "livinghello_generate_image"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
STYLE_MAX_CHARS = 1000
IMAGE_EVENT = ContextVar("livinghello_image_event", default=None)
FIELDS = {
    "enabled": ("启用AI生图", "bool", "主模型可按需要调用文生图或图生图工具，关闭后不提供工具。"),
    "provider_id": ("生图模型 / 路由上游", "provider", "复用AstrBot已注册模型或多API路由的具体上游连接，不读取BiliBot。所选API必须实际支持生图。"),
    "model": ("生图模型名称（可选）", "string", "留空使用所选模型名；同一API的生图模型名可在此指定。不会更改主对话模型。"),
    "protocol": ("生图接口", "protocol", "标准图片接口使用images/generations和images/edits；对话生图接口使用chat/completions的image模态。"),
    "daily_limit": ("每日生图次数上限", "int", "全插件共用，按服务器本地自然日重置。每次上游请求计一次，失败和结果未知也计数；0禁止生图，重载不清零。"),
    "size": ("生成尺寸", "size", "使用服务支持的尺寸，建议分辨率范围512x512~2048x2048；可选自定义宽高。每次只请求1张图片。"),
    "style_prompt": ("画面风格提示词（全局）", "text", "每次生图自动附加在描述之后，用来固定整套出图风格；留空则完全不附加。用户明确要求画风时以用户描述为准，本段作为补充。最多1000字；与描述合计超过4000字时只截断风格部分。"),
    "timeout_seconds": ("生图超时（秒）", "int", "10~500秒，超时不会换上游补发，防止重复收费。原自主回复时效仍生效。"),
}
DEFAULTS = dict(enabled=False, provider_id="", model="", protocol="images",
                daily_limit=20, size="1024x1024",
                style_prompt="",
                timeout_seconds=90)


class ImageSettings:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.lock = asyncio.Lock()
        data_dir = getattr(plugin, "data_dir", None)
        self.directory = Path(data_dir) if data_dir else None
        self.path = self.directory / "image_generation.json" if self.directory else None
        self.ledger = self.directory / "image_generation_quota.sqlite3" if self.directory else None
        self.values = copy.deepcopy(DEFAULTS)
        self.error = ""
        self._stamp = None
        if self.path and self.path.exists():
            try:
                self.values = self._load(self.path)
                self._stamp = self._mtime(self.path)
            except Exception:
                self.error = "生图配置损坏，请恢复备份；当前禁止生图。"
        self._tool = None
        self._closed = False
        self._tasks = set()

    @staticmethod
    def _mtime(path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def refresh(self):
        """磁盘配置被外部改过就重载一次；写坏了保留旧值，不打断在线生图。

        以前只在插件加载时读一次，直接改 image_generation.json 必须重启才生效，
        表现为「改了额度还是画不了」。这里按 mtime 判断，每轮只多一次 stat。
        """
        path = self.path
        if not path:
            return
        stamp = self._mtime(path)
        if stamp is None or stamp == self._stamp:
            return
        try:
            values = self._load(path)
        except Exception:
            # 外部写坏了：保留上一份可用配置，只留警告，不把功能锁死。
            self._stamp = stamp
            self.plugin.logger.warning("[LivingHello] 生图配置热重载失败，继续使用旧值")
            return
        self.values = values
        self._stamp = stamp
        self.error = ""
        self.plugin.logger.warning(
            "[LivingHello] 生图配置已热重载：enabled=%r limit=%r provider=%r",
            values["enabled"], values["daily_limit"], values["provider_id"])

    @classmethod
    def _load(cls, path):
        """读取磁盘配置并按旧版缺省字段迁移，返回与内存同口径的值。

        旧文件没有 style_prompt：补默认值后再校验。加载与保存比较
        共用这一套键，避免把「旧格式」误判成磁盘已被外部改动。
        """
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(loaded, dict) or set(loaded) - set(DEFAULTS)
                or set(DEFAULTS) - {"style_prompt"} - set(loaded)):
            raise ValueError("invalid settings")
        loaded.setdefault("style_prompt", DEFAULTS["style_prompt"])
        cls._validate(loaded)
        return loaded

    def targets(self):
        """连接秘密仅在服务端使用，返回浏览器的列表只保留ID和模型名。"""
        targets = {}
        getter = getattr(self.context, "get_all_providers", None)
        for provider in getter() if callable(getter) else []:
            config = getattr(provider, "provider_config", {}) or {}
            pid = str(config.get("id") or "")
            if not pid or config.get("enable") is False:
                continue
            upstreams = getattr(provider, "all_upstreams", None) or getattr(provider, "upstreams", None)
            if config.get("type") == "multi_api_router" and upstreams:
                parent = str(config.get("parent_provider_id") or pid.split(":upstream_", 1)[0])
                for upstream in upstreams:
                    if getattr(upstream, "api_type", "chat") not in CHAT_TYPES | {"image_generation"}:
                        continue
                    client = getattr(upstream, "client", None)
                    slot = getattr(upstream, "slot", 0)
                    if client is not None and slot:
                        targets[f"{parent}:upstream_{slot}"] = (
                            client, str(getattr(upstream, "name", "") or f"上游 {slot}"), "router_upstream")
            elif config.get("type") != "multi_api_router":
                targets[pid] = (provider, str(config.get("display_name") or pid), "provider")
        for pid, config in router_connections(self.context).items():
            if config["api_type"] not in CHAT_TYPES | {"image_generation"}:
                continue
            # image_generation不在get_all_providers内；这里只复用连接，不创建聊天客户端。
            connection = SimpleNamespace(provider_config=config,
                                         get_model=lambda model=config["model"]: model)
            targets[pid] = (connection, config["display"], "router_upstream")
        return targets

    def _public_targets(self):
        return [dict(id=pid, display=label, model=str(provider.get_model() or ""), kind=kind)
                for pid, (provider, label, kind) in self.targets().items()]

    @staticmethod
    def today():
        return datetime.now().date().isoformat()

    @contextmanager
    def _connect(self):
        if not self.ledger:
            raise NativeReplyError("生图数据目录不可用，不能保存额度", 503)
        db = sqlite3.connect(self.ledger, timeout=5)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS calls (request_id TEXT PRIMARY KEY, "
                       "day TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def used(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (self.today(),)).fetchone()[0]

    def read(self):
        self.refresh()
        providers = self._public_targets()
        try:
            used = self.used()
            quota_error = ""
        except Exception:
            used, quota_error = 0, "无法读取持久额度，暂时禁止生图。"
        return dict(available=self.directory is not None and not self.error and not quota_error,
                    scope="LivingHello独立能力；全插件共用每日额度",
                    reason=self.error or quota_error, values=copy.deepcopy(self.values),
                    fields=[dict(key=k, title=v[0], kind=v[1], help=v[2]) for k, v in FIELDS.items()],
                    providers=providers, revision=revision(self.values),
                    usage=dict(day=self.today(), used=used, limit=self.values["daily_limit"]),
                    tool_name=TOOL_NAME, tool_ready=self._tool is not None)

    @staticmethod
    def _validate(values):
        for key, value in values.items():
            valid = False
            if key == "enabled":
                valid = isinstance(value, bool)
            elif key in {"daily_limit", "timeout_seconds"}:
                low, high = (0, 1000) if key == "daily_limit" else (10, 500)
                valid = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
            elif key == "protocol":
                valid = isinstance(value, str) and value in {"images", "chat_images"}
            elif key == "style_prompt":
                valid = isinstance(value, str) and len(value) <= STYLE_MAX_CHARS
            elif key == "size":
                valid = isinstance(value, str) and value in {"auto", "1024x1024", "1536x1024", "1024x1536"}
                if not valid and isinstance(value, str):
                    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value)
                    valid = bool(match) and 512 <= int(match.group(1)) <= 2048 and 512 <= int(match.group(2)) <= 2048
            else:
                valid = isinstance(value, str) and len(value) <= 200
            if not valid:
                raise NativeReplyError(f"{FIELDS[key][0]}的值无效")

    async def save(self, body):
        if not isinstance(body, dict) or set(body) != {"expected_revision", "values"}:
            raise NativeReplyError("生图配置请求格式无效")
        async with self.lock:
            if not self.path or self.error:
                raise NativeReplyError(self.error or "生图配置目录不可用", 503)
            if body["expected_revision"] != revision(self.values):
                raise NativeReplyError("生图设置已变化，请重新读取；草稿保留", 409)
            try:
                disk = self._load(self.path) if self.path.exists() else None
            except (ValueError, OSError) as exc:
                raise NativeReplyError("生图磁盘配置不可读，请恢复备份", 503) from exc
            # 冲突必须在 try 之外抛出：NativeReplyError 本身就是 ValueError，
            # 放在 try 里会被上面的 except 吞掉并误报成「配置不可读」。
            if disk is not None and disk != self.values:
                raise NativeReplyError("生图磁盘配置已变化，请重新读取", 409)
            patch = body["values"]
            if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
                raise NativeReplyError("只能保存独立生图公开字段")
            values = {**self.values, **patch}
            self._validate(values)
            if values["provider_id"] and values["provider_id"] not in self.targets():
                raise NativeReplyError("生图模型当前不可用，请选择已注册模型或路由上游")
            if values["enabled"]:
                if not values["provider_id"]:
                    raise NativeReplyError("启用前请选择生图模型")
                if not self.ensure_tool():
                    raise NativeReplyError("当前宿主不支持生图工具，未开启生图", 503)
                self.connection(values)  # 保存前验证真实连接，不发API请求。
            try:
                self._write(values)
            except OSError as exc:
                raise NativeReplyError("生图配置保存失败，运行设置未改变", 500) from exc
            self.values = values
            # 记下自己写的版本，避免紧接着的 read() 把刚存的内容当成外部改动重载。
            self._stamp = self._mtime(self.path)
            return self.read()

    def _write(self, values):
        fd, temporary = tempfile.mkstemp(prefix=".image-settings-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def connection(self, values):
        target = self.targets().get(values["provider_id"])
        if not target:
            raise NativeReplyError("生图模型或路由上游不可用", 503)
        provider = target[0]
        config = getattr(provider, "provider_config", {}) or {}
        base = str(config.get("api_base") or config.get("base_url") or "").strip().rstrip("/")
        key = config.get("key") or config.get("api_key")
        key = next((v for v in key if isinstance(v, str) and v), "") if isinstance(key, list) else key
        model = values["model"].strip() or str(provider.get_model() or "")
        if urlsplit(base).scheme not in {"https", "http"} or not urlsplit(base).netloc:
            raise NativeReplyError("所选模型没有可用的OpenAI兼容API地址", 503)
        if urlsplit(base).username or urlsplit(base).password or urlsplit(base).query or urlsplit(base).fragment:
            raise NativeReplyError("生图API地址不能包含凭据、查询参数或片段", 503)
        if not isinstance(key, str) or not key or not model or model == "automatic":
            raise NativeReplyError("生图连接缺少密钥或真实模型名称", 503)
        return base, key, model

    def ensure_tool(self):
        if self._tool is not None:
            return self._tool
        try:
            try:
                from .guard_image_tool import GenerateImageTool
            except ImportError:
                from images.guard_image_tool import GenerateImageTool
            self._tool = GenerateImageTool(service=self)
        except ImportError:
            return None
        return self._tool

    def needed(self, event):
        """生图工具对群聊和私聊一律挂载，是否真的画图交给主模型判断。

        关键词表只能覆盖穷举出来的说法：「拍个自拍」「看看你的睡相」这类
        自然表达必然漏网，等于替模型做了「本轮不用画」的决定，而它手里
        根本没有工具可用。工具描述已经写明调用边界，选择权交回模型。
        """
        return True

    def attach(self, event, req):
        # 面板保存之外的外部改动（直接改 JSON）也要在下一轮生效。
        self.refresh()
        if self._closed or self.error or not self.values["enabled"] or self.values["daily_limit"] == 0:
            emit(event, "image_tool_attachment", "disabled", reason="image_unavailable")
            return
        if not self.needed(event):
            emit(event, "image_tool_attachment", "skipped", reason="private_no_image_intent")
            return
        tool = self.ensure_tool()
        if tool is None:
            emit(event, "image_tool_attachment", "failed", reason="tool_import_failed")
            return
        from astrbot.core.agent.tool import ToolSet
        # 复制工具集，避免在共享人格工具集中污染其他请求。
        tools = ToolSet()
        for existing in getattr(getattr(req, "func_tool", None), "tools", []):
            tools.add_tool(existing)
        if not any(t.name == TOOL_NAME for t in tools.tools):
            tools.add_tool(tool)
        req.func_tool = tools
        if getattr(self.plugin, "_livinghello_detached_enabled", False):
            instruction = ("生图需求必须先调用livinghello_generate_image取得本轮决定，"
                           "然后才给承接或婉拒的文字；工具决定前不要承诺会画。")
            if instruction not in (getattr(req, "system_prompt", "") or ""):
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n" + instruction
        # 标记本次请求挂过生图工具，供最晚钩子复核它是否被后续插件覆盖。
        event.set_extra("_livinghello_image_tool_attached", True)
        emit(event, "image_tool_attachment", "success", tool=TOOL_NAME,
             count=len(tools.tools), types=[t.name for t in tools.tools])

    def blocked(self, event):
        if getattr(self.plugin, "_livinghello_detached_enabled", False):
            if event.get_extra("_at_guard_superseded"):
                return True
            check = getattr(self.plugin, "_stale_reason", None)
            if check and check(event, self.plugin._group_key(event)):
                return True
        if self._closed or event.get_extra("_at_guard_delivery_blocked"):
            return True
        if self.plugin._delivery().recalled(event):
            return True
        if self.plugin._judged_event(event) and self.plugin._gate_mode() == "enforce":
            return bool(self.plugin._stale_reason(event, self.plugin._group_key(event)))
        return False

    def reserve(self, event, prompt, use_reference):
        scope = str(getattr(event, "unified_msg_origin", "") or "")
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        # 缺少平台ID时只在事件生命周期内去重，不混用不同用户请求。
        nonce = event.get_extra("_livinghello_image_nonce")
        if not nonce:
            nonce = os.urandom(16).hex()
            event.set_extra("_livinghello_image_nonce", nonce)
        request_id = hashlib.sha256(json.dumps(
            [scope, mid or nonce, prompt, use_reference], ensure_ascii=False).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                return None
            used = db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (self.today(),)).fetchone()[0]
            if used >= self.values["daily_limit"]:
                raise NativeReplyError("今日生图次数已用完，请勿重复调用", 429)
            db.execute("INSERT INTO calls VALUES (?, ?, 'reserved', ?)", (request_id, self.today(), time.time()))
            cutoff = (datetime.now().date() - timedelta(days=7)).isoformat()
            db.execute("DELETE FROM calls WHERE day<?", (cutoff,))
        return request_id

    def mark(self, request_id, status):
        try:
            with self._connect() as db:
                db.execute("UPDATE calls SET status=? WHERE request_id=?", (status, request_id))
        except (sqlite3.Error, OSError):
            # 已预占的记录仍计数；完成标记故障不能触发重试或退回额度。
            self.plugin.logger.warning("[LivingHello] 生图完成状态未能更新，保留预占额度")

    @staticmethod
    def references(event):
        """只接受本次输入或引用的真实图片，不接受模型提交任意URL/文件路径。"""
        result = []
        sources = event.get_extra("_at_guard_fragment_sources", ()) or (event,)
        for source in sources:
            for component in source.get_messages():
                if type(component).__name__ == "Image":
                    result.append(component)
                elif type(component).__name__ == "Reply":
                    result.extend(c for c in (getattr(component, "chain", None) or [])
                                  if type(c).__name__ == "Image")
        return result[:1]

    async def prepare_image_job(self, event, prompt, use_reference):
        self.refresh()
        cached = event.get_extra("_livinghello_image_job")
        if cached:
            return cached.get("reply") or "这条生图请求正在确认，请勿重复调用；不要提前承诺。"
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000 \
                or not isinstance(use_reference, bool):
            return "生图参数无效。"
        if self.blocked(event) or self._closed or self.error or not self.values["enabled"]:
            self.plugin.logger.warning(
                "[LH-img] job 拒绝：blocked=%r closed=%r error=%r enabled=%r superseded=%r",
                self.blocked(event), self._closed, bool(self.error), self.values["enabled"],
                event.get_extra("_at_guard_superseded"))
            return "当前不能生图，请自然回应，不要承诺。"
        job = dict(status="deciding", prompt=prompt.strip(), use_reference=use_reference,
                   values=copy.deepcopy(self.values), reply="")
        event.set_extra("_livinghello_image_job", job)
        try:
            from ..integrations.guard_livingemotion import current_emotion
        except ImportError:
            from integrations.guard_livingemotion import current_emotion
        mood = await current_emotion(self.context, event, getattr(self.plugin, "config", None))
        job["emotion"] = mood
        negative = bool(mood) and max(mood["emotions"][k] for k in
                                     ("anger", "sadness", "fear")) >= .6
        # 用户确认的负面阈值；每个事件只抽一次，拒绝不占生图额度。
        draw = random.random() if negative else None
        job["draw"] = draw
        if negative and draw < .5:
            job.update(status="refused", reply=(
                "当前情绪明显负面，这轮决定不生图。请结合当前情绪自然地婉拒，"
                "仍正常文字回应；不要积极承诺、不要自动重试此工具。当前情绪："
                + json.dumps(mood, ensure_ascii=False)))
            return job["reply"]
        if self.blocked(event):
            job.update(status="cancelled", reply="本轮已被新消息顶替，不要再回应或生图。")
            return job["reply"]
        if use_reference and not self.references(event):
            job.update(status="failed", reply="需要参考图，请自然地请用户补图，不要承诺已经开画。")
            return job["reply"]
        try:
            self.connection(job["values"])
            if self.used() >= self.values["daily_limit"]:
                raise NativeReplyError("今日生图次数已用完", 429)
        except Exception as exc:
            self.plugin.logger.warning(
                "[LH-img] job 预检失败：provider=%r used=%r limit=%r err=%r",
                job["values"].get("provider_id"), self.used(), self.values["daily_limit"],
                f"{type(exc).__name__}: {exc}"[:120])
            # 额度用尽属于每日上限，不是接口故障；必须让模型说人话，
            # 否则它会把「画不了」解释成「接口没缓过来」，把人带偏去查 provider。
            if isinstance(exc, NativeReplyError) and getattr(exc, "status_code", None) == 429:
                job.update(status="quota", reply=(
                    "今日生图额度已经用完（每日上限，不是接口故障）。"
                    "请自然告诉用户今天画满了、明天再画，不要说接口坏了、正在修复或稍后重试。"))
            else:
                job.update(status="failed", reply="现在暂时画不了，请自然说明；不要重试此工具。")
            return job["reply"]
        job.update(status="prepared", reply=(
            "生图已准备，尚未请求上游。请先用一句自然的话承接需求，不要说图片已完成；"
            "你的文字投递成功后才后台生成并发图，请勿再调用本工具。"))
        if mood:
            job["reply"] += "当前情绪：" + json.dumps(mood, ensure_ascii=False)
        return job["reply"]

    def start_image_job(self, event):
        job = event.get_extra("_livinghello_image_job")
        if not job or job["status"] != "prepared":
            self.plugin.logger.warning(
                "[LH-img] start 跳过：job=%r status=%r",
                bool(job), (job or {}).get("status"))
            return
        if self.blocked(event) or job["values"] != self.values:
            job["status"] = "cancelled"
            self.plugin.logger.warning(
                "[LH-img] start 取消：blocked=%r values_changed=%r superseded=%r",
                self.blocked(event), job["values"] != self.values,
                event.get_extra("_at_guard_superseded"))
            return
        job["status"] = "running"
        task = asyncio.create_task(self._run_image_job(event, job))
        event.set_extra("_livinghello_image_task", task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_image_job(self, event, job):
        try:
            self.plugin.logger.warning("[LH-img] 后台开跑：prompt=%d字", len(job["prompt"]))
            response = await self.generate(
                event, job["prompt"], job["use_reference"], _execute=True)
            if self.blocked(event):
                job["status"] = "cancelled"
                self.plugin.logger.warning(
                    "[LH-img] 后台结果被拦：blocked=True resp=%r", str(response)[:80])
                return
            pending = event.get_extra("_livinghello_pending_image")
            if pending:
                event.set_extra("_livinghello_pending_image", None)
                request_id = pending["request_id"]
                try:
                    from astrbot.api.message_components import Image
                    await event.send(event.chain_result([Image.fromFileSystem(pending["path"])]))
                except Exception:
                    self.mark(request_id, "unknown")
                    job["status"] = "unknown"
                    return  # 平台投递不明确：不追加回复、不自动重发。
                self.mark(request_id, "sent")
                job["status"] = "sent"
            else:
                job["status"] = "unknown" if "结果未知" in response else "failed"
                self.plugin.logger.warning(
                    "[LH-img] 后台无图：status=%r resp=%r", job["status"], str(response)[:120])
                from astrbot.api.message_components import Plain
                mood = job.get("emotion")
                line = "这张图暂时没顺利出来……先放一放吧。"
                if mood and max(mood["emotions"][k] for k in
                                ("anger", "sadness", "fear")) >= .6:
                    dominant = max(("anger", "sadness", "fear"), key=mood["emotions"].get)
                    feeling = {"anger": "有点烦", "sadness": "心情有点低落",
                               "fear": "有点紧张"}[dominant]
                    line = f"这会儿{feeling}，图也没顺利出来……先缓缓吧。"
                if not self.blocked(event):
                    await event.send(event.chain_result([Plain(line)]))
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception:
            job["status"] = "unknown"
        finally:
            clear = getattr(self.plugin, "_clear_reply_turn", None)
            if clear:
                clear(event)

    async def generate(self, event, prompt, use_reference=False, *, _execute=False):
        if getattr(self.plugin, "_livinghello_detached_enabled", False) and not _execute:
            return await self.prepare_image_job(event, prompt, use_reference)
        service = event.get_extra(TRACE_KEY)
        # 同步工具内部步骤关联宿主span，次数仍只在实际调用边界累计。
        parent_span = next(reversed(event.get_extra("_livinghello_tool_spans", {}).values()), None)
        span = service.start(event, "image_tool", tool=TOOL_NAME, parent_span=parent_span) if service else None
        # 生图内部调用与宿主tool-start共用请求，但不重复累计工具次数。
        if service and not event.get_extra("_livinghello_tool_spans"):
            service.increment(event, "tool_calls")
        emit(event, "image_api", "skipped", calls=0, reason="preflight")
        def outcome(status, reason):
            emit(event, "image_preflight", status, reason=reason)
            if service:
                service.end(event, span, status, reason=reason)
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= MAX_PROMPT_CHARS or not isinstance(use_reference, bool):
            outcome("failed", "invalid_parameters")
            return "生图参数无效：需要1~4000字的描述及图生图开关。"
        if self._closed or self.error or not self.values["enabled"]:
            outcome("disabled", "image_disabled")
            return "AI生图未启用。"
        if self.blocked(event):
            reason = event.get_extra("_at_guard_delivery_blocked") or self.plugin._stale_reason(
                event, self.plugin._group_key(event)) or "reply_cancelled"
            outcome("cancelled", reason)
            return "当前回复已取消，未请求生图。"
        values = copy.deepcopy(self.values)
        request_id = None
        task = asyncio.current_task()
        self._tasks.add(task)
        token = IMAGE_EVENT.set(event)
        terminal_status, terminal_reason = "failed", "preflight_failed"
        try:
            connection = self.connection(values)
            refs = self.references(event) if use_reference else []
            if use_reference and not refs:
                terminal_reason = "reference_missing"
                return "图生图需要本次消息或引用中的图片，请用户补充图片。"
            # 上传前读来源；无效图片不计生图额度。
            reference = await self._reference_data(refs[0], values) if refs else None
            async with self.lock:
                if self.blocked(event) or self.values != values or not self.values["enabled"]:
                    terminal_status, terminal_reason = "cancelled", "reply_or_config_changed"
                    return "回复或生图配置已变化，未请求生图。"
                # 参考图下载可能跨过模型重载；请求前重新解析当前注册连接。
                connection = self.connection(values)
                request_id = self.reserve(event, prompt.strip(), use_reference)
            if not request_id:
                terminal_status, terminal_reason = "skipped", "duplicate_image_call"
                return "本条消息已请求过相同生图，结果可能已发送或未知，请勿重复调用。"
            emit(event, "image_quota", "success", reason="reserved")
            emit(event, "image_preflight", "success")
            image, revised = await asyncio.wait_for(self._request(
                values, connection, prompt.strip(), reference), values["timeout_seconds"])
            if self.blocked(event) or not self.values["enabled"]:
                self.mark(request_id, "cancelled")
                terminal_status, terminal_reason = "cancelled", "reply_cancelled_after_generation"
                emit(event, "image_delivery", "cancelled", reason=terminal_reason)
                return "图片已生成但回复已取消，未发送；本次仍计额度。"
            # 不在这里直接发图：先把图挂到本轮事件上，等主回复送出后再单独发一条。
            # 「先承接需求、后给图」的两个气泡，比图先砸过去更像真人说话。
            if event.get_extra("_at_guard_delivery_blocked"):
                self.mark(request_id, "cancelled")
                terminal_status, terminal_reason = "cancelled", "reply_cancelled_before_image_send"
                emit(event, "image_delivery", "cancelled", reason=terminal_reason)
                return "发送前回复已取消，未发送图片；本次仍计额度。"
            event.set_extra("_livinghello_pending_image", {
                "path": str(image),
                "request_id": request_id,
                "created": time.time(),
                "revised": revised,
            })
            terminal_status, terminal_reason = "success", "image_queued"
            # count 记录回传画法的字符数，0 表示上游没给 revised_prompt。
            emit(event, "image_delivery", "queued", reason="after_reply", count=len(revised))
            # 把服务端改写后的画法回传：主模型据此知道自己真正画了什么，
            # 后续追问不至于全靠猜；明确禁止复述，避免把画法念给用户听。
            drawn = f"服务端实际按「{revised}」绘制。" if revised else ""
            return ("图片已生成。" + drawn
                    + "会在本条回复发出之后作为独立消息自动发送。"
                    "请用一句话承接用户的需求，不要复述画法，也不要再调用本工具。")
        except asyncio.CancelledError:
            terminal_status, terminal_reason = "cancelled", "task_cancelled"
            if request_id:
                self.mark(request_id, "unknown")
            raise
        except Exception as exc:
            terminal_status = "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed"
            terminal_reason = "quota_exhausted" if isinstance(exc, NativeReplyError) and getattr(
                exc, "status_code", None) == 429 else "image_exception"
            if isinstance(exc, NativeReplyError) and "今日生图次数" in str(exc):
                terminal_reason = "quota_exhausted"
                emit(event, "image_quota", "skipped", reason=terminal_reason)
            emit(event, "image_error", terminal_status, reason=terminal_reason, error_type=type(exc).__name__)
            if terminal_reason != "quota_exhausted":
                # 额度用尽属于产品限制，不是模型故障，不打扰管理员。
                report_model_fault(self.plugin, "image_gen", terminal_status,
                                   str(exc)[:80], event)
            if service:
                row = service.rows.get(event.get_extra("_livinghello_request_id"), {})
                if row.get("features", {}).get("image_api", {}).get("status") == "running":
                    emit(event, "image_api", terminal_status, reason=terminal_reason)
            if request_id:
                self.mark(request_id, "unknown")
            # 错误只展示受控提示，不将URL、密钥或服务响应正文送给模型。
            message = str(exc) if isinstance(exc, NativeReplyError) else "接口异常或超时"
            return f"生图未完成：{message}。已请求的次数仍计额度；结果未知时不要自动重试。"
        finally:
            if service:
                service.end(event, span, terminal_status, reason=terminal_reason)
            IMAGE_EVENT.reset(token)
            self._tasks.discard(task)

    @staticmethod
    def _image_bytes(value):
        if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 200:
            raise NativeReplyError("图片大小超出限制")
        if value.startswith("data:"):
            if not value.startswith("data:image/") or ";base64," not in value:
                raise NativeReplyError("图片编码格式不支持")
            value = value.split(",", 1)[1]
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise NativeReplyError("图片编码无效") from exc
        return ImageSettings._check_image(data)

    @staticmethod
    def _check_image(data):
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise NativeReplyError("图片为空或超过10MiB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return data, "image/jpeg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return data, "image/webp"
        raise NativeReplyError("只支持PNG、JPEG或WebP图片")

    async def _download(self, url, timeout):
        event = IMAGE_EVENT.get()
        started = time.monotonic()
        if event:
            emit(event, "image_download", "running")
        try:
            data = await self._download_raw(url, timeout)
            if event:
                emit(event, "image_download", "success", ms=round((time.monotonic()-started)*1000, 2))
            return data
        except BaseException as exc:
            if event:
                emit(event, "image_download", "cancelled" if isinstance(exc, asyncio.CancelledError) else
                     "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed",
                     error_type=type(exc).__name__)
            raise

    async def _download_raw(self, url, timeout):
        import aiohttp
        if urlsplit(url).scheme not in {"https", "http"}:
            raise NativeReplyError("图片URL格式不支持")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                        trust_env=True) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise NativeReplyError(f"图片下载HTTP {response.status}")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise NativeReplyError("图片超过10MiB")
                    chunks.append(chunk)
                return self._check_image(b"".join(chunks))

    async def _reference_data(self, image, values):
        value = str(getattr(image, "url", "") or getattr(image, "file", "") or getattr(image, "path", ""))
        if value.startswith(("https://", "http://")):
            return await self._download(value, values["timeout_seconds"])
        if value.startswith("data:"):
            return self._image_bytes(value)
        if value.startswith("base64://"):
            return self._image_bytes(value[9:])
        # 平台提供的组件路径可以使用，工具参数不能指定任意本地文件。
        path = Path(value.removeprefix("file://"))
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise NativeReplyError("当前图片文件不可用或过大")
        return self._check_image(path.read_bytes())

    @staticmethod
    def styled_prompt(prompt, style):
        """把全局风格追加到本次描述末尾，两种生图协议都走这一处。

        风格为空时原样返回；描述本身才是这轮真正要的画面，总量超过
        上限时只截断风格部分，保证用户描述完整。
        """
        style = (style or "").strip()
        if not style:
            return prompt
        room = MAX_PROMPT_CHARS - len(prompt) - 1
        if room <= 0:
            return prompt
        return prompt + "\n" + style[:room]

    async def _request(self, values, connection, prompt, reference):
        import aiohttp
        base, key, model = connection
        headers = {"Authorization": f"Bearer {key}"}
        prompt = self.styled_prompt(prompt, values.get("style_prompt", ""))
        request = dict(model=model, prompt=prompt, n=1, size=values["size"])
        protocol = values["protocol"]
        if protocol == "chat_images":
            endpoint = "/chat/completions"
            content = [dict(type="text", text=prompt)]
            if reference:
                data, mime = reference
                content.append(dict(type="image_url", image_url={
                    "url": f"data:{mime};base64," + base64.b64encode(data).decode()}))
            body = {"json": dict(model=model, messages=[dict(role="user", content=content)],
                                 modalities=["image"], n=1)}
        elif reference:
            endpoint = "/images/edits"
            form = aiohttp.FormData()
            for k, v in request.items():
                form.add_field(k, str(v))
            data, mime = reference
            form.add_field("image", data, filename="reference." + mime.split("/")[1], content_type=mime)
            body = {"data": form}
        else:
            endpoint = "/images/generations"
            body = {"json": request}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=values["timeout_seconds"]),
                                        trust_env=True) as session:
            event = IMAGE_EVENT.get()
            started = time.monotonic()
            if event:
                service = event.get_extra(TRACE_KEY)
                if service:
                    service.increment(event, "image_api_calls")
                emit(event, "image_api", "running", calls=1, model=model)
            async with session.post(base + endpoint, headers=headers, allow_redirects=False, **body) as response:
                if response.status not in {200, 201}:
                    # 上游 400 的真实原因只在响应体里（例如「该模型不支持 Chat Completions」），
                    # 原来直接丢弃，只剩一句「HTTP 400」，等于把排查线索扔了。
                    # 只写日志，不改进用户看到的文案。
                    detail = ""
                    try:
                        detail = (await response.text())[:400]
                    except Exception:
                        detail = "<响应体不可读>"
                    self.plugin.logger.warning(
                        "[LivingHello] 生图接口 HTTP %s endpoint=%s model=%s resp=%s",
                        response.status, endpoint, model, " ".join(detail.split()))
                    raise NativeReplyError(f"生图接口HTTP {response.status}")
                if response.content_length and response.content_length > MAX_IMAGE_BYTES * 2:
                    raise NativeReplyError("生图响应过大")
                chunks, length = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    length += len(chunk)
                    if length > MAX_IMAGE_BYTES * 2:
                        raise NativeReplyError("生图响应过大")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                payload = json.loads(raw)
        if event:
            emit(event, "image_api", "success", calls=1, ms=round((time.monotonic()-started)*1000, 2))
        image_value = self.extract(payload)
        data, mime = (await self._download(image_value, values["timeout_seconds"])
                      if image_value.startswith(("https://", "http://")) else self._image_bytes(image_value))
        folder = self.directory / "generated_images"
        folder.mkdir(exist_ok=True)
        # 有界每日额度配合7天清理，避免临时媒体长期累积。
        for old in folder.glob("image-*"):
            if old.stat().st_mtime < time.time() - 7 * 86400:
                old.unlink()
        fd, filename = tempfile.mkstemp(prefix="image-", suffix="." + mime.split("/")[1], dir=folder)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return Path(filename), self.revised(payload)

    @staticmethod
    def revised(payload):
        """取服务端实际使用的画法描述；拿不到就返回空串。

        OpenAI 系把改写后的提示词放在 data[].revised_prompt，百炼兼容响应
        放在 output.choices[].message。只用来告诉主模型「实际画的是什么」，
        不写日志、不外发，也不改变生图行为。
        """
        if not isinstance(payload, dict):
            return ""
        out = payload.get("output")
        if isinstance(out, dict) and not payload.get("data") and not payload.get("choices"):
            payload = out
        rows = payload.get("data")
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    value = item.get("revised_prompt")
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:600]
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                for key in ("revised_prompt", "prompt"):
                    value = message.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:600]
        return ""

    @staticmethod
    def extract(payload):
        if not isinstance(payload, dict) or payload.get("error"):
            raise NativeReplyError("生图服务返回错误或无效响应")
        # 保留线上阿里百炼兼容响应：output.choices[].message.content[].image。
        out = payload.get("output")
        if isinstance(out, dict) and not payload.get("data") and not payload.get("choices"):
            payload = out
        rows = payload.get("data", [])
        choices = payload.get("choices", [])
        message = (choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {})
        message = message if isinstance(message, dict) else {}
        rows = rows or message.get("images", []) or message.get("content", [])
        if isinstance(rows, str):
            rows = [rows]
        for item in rows if isinstance(rows, list) else []:
            if isinstance(item, dict):
                nested = item.get("image_url", {})
                value = item.get("b64_json") or item.get("url") or item.get("image") or (
                    nested.get("url") if isinstance(nested, dict) else nested)
            else:
                value = item
            if isinstance(value, str) and value:
                inline = re.search(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=\r\n]+",value)
                if inline:
                    return inline[0].replace("\r","").replace("\n","")
                markdown = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)",value)
                if markdown:
                    return markdown[1]
                return value
        raise NativeReplyError("生图响应中没有图片")

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
