"""Bounded, plugin-local diagnostic evidence. Standard library only.

This file is also shipped by LivingEmotion: neither plugin depends on the other.
Business stores remain authoritative; this recorder only retains safe evidence.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import math
import os
import platform
import queue
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

WINDOW = 72 * 3600
DISK_LIMIT = 32 * 1024 * 1024
EXPORT_LIMIT = 64 * 1024 * 1024
LINE_LIMIT = 32 * 1024
PRIVATE_FIELDS = frozenset((
    "body", "text", "content", "ctx", "context", "prompt", "raw", "description",
    "note", "summary_text", "context_summary", "delivered_text", "reply_text",
    "summary", "materials", "materials_json", "judged_input", "chat_data",
    "recent_messages_old_to_new", "image_sources", "image_summaries",
))
SECRET_KEY = re.compile(r"api.?key|authorization|cookie|password|secret|jwt|access.?token|refresh.?token", re.I)
ACCOUNT_KEY = re.compile(r"^(?:session_id|unified_msg_origin|account_id|actor_id|qq|group_id|target_id|context_id|persona_id|owner_key|conversation_id|scope_key|source_scope_key)$", re.I)
TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{6,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b")
AUTH = re.compile(r"(?i)\b(?:authorization|cookie|api[_-]?key|password|jwt[_-]?secret|access[_-]?token)\b[\"']?\s*[:=]\s*(?:[\"'][^\"'\n]*[\"']|[^\n,;}]+)")
URL = re.compile(r"https?://[^\s<>\"']+")
NUMBER = re.compile(r"(?<![\w])\d{5,12}(?![\w])")


class DiagnosticBusy(RuntimeError):
    pass


def regular(path: Path) -> bool:
    """Reject symbolic source files; host data roots may use OS path aliases."""
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def json_lines(paths, line_limit=LINE_LIMIT):
    """Read bounded lines; expose corrupt/oversized records to the manifest."""
    for path in paths:
        if not regular(Path(path)):
            continue
        with Path(path).open("rb") as source:
            while True:
                line = source.readline(line_limit + 1)
                if not line:
                    break
                if len(line) > line_limit:
                    while line and not line.endswith(b"\n"):
                        line = source.readline(line_limit + 1)
                    yield None
                    continue
                try:
                    row = json.loads(line)
                    yield row if isinstance(row, dict) else None
                except (ValueError, UnicodeError):
                    yield None


def sqlite_rows(path, query, parameters):
    """A consistent, read-only, time-bounded snapshot; never create a database."""
    path = Path(path)
    if not regular(path):
        raise FileNotFoundError("diagnostic source unavailable")
    deadline = time.monotonic() + 10
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("BEGIN")
        try:
            for row in db.execute(query, parameters):
                yield dict(row)
        finally:
            db.rollback()
    finally:
        db.close()


class Redactor:
    def __init__(self, salt, credentials=()):
        self.salt = salt
        self._credentials = credentials
        self.credentials = sorted(
            {s for s in (() if callable(credentials) else credentials) if isinstance(s, str) and s},
            key=len, reverse=True,
        )

    def alias(self, value):
        if isinstance(value, str) and re.fullmatch(r"account-[0-9a-f]{12}", value):
            return value
        return "account-" + hashlib.sha256((self.salt + str(value)).encode()).hexdigest()[:12]

    def known_credentials(self):
        if callable(self._credentials):
            return sorted({s for s in self._credentials() if isinstance(s, str) and s},
                          key=len, reverse=True)
        return self.credentials

    def text(self, value, known=None):
        value = str(value)
        # Mask whole authenticated URLs, even when the key name is unfamiliar.
        value = URL.sub("[url]", value)
        if known is None:
            known = self.known_credentials()
        for credential in known:
            value = value.replace(credential, "[secret]")
        value = TOKEN.sub("[secret]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "[credential]", value)
        value = AUTH.sub("[credential]", value)
        value = NUMBER.sub(lambda match: self.alias(match.group()), value)
        return value[:8192]

    def clean(self, value, depth=0, known=None):
        if known is None:
            known = self.known_credentials()
        if depth > 8:
            return "[depth limit]"
        if isinstance(value, dict):
            output = {}
            for key, item in list(value.items())[:256]:
                key = str(key)
                if key.lower() in PRIVATE_FIELDS and not (
                        key.lower() == "summary" and isinstance(item, dict)):
                    continue
                safe_key = self.text(key, known)
                if SECRET_KEY.search(key):
                    output[safe_key] = "set" if item else "unset"
                elif ACCOUNT_KEY.fullmatch(key) and item:
                    output[safe_key] = self.alias(item)
                else:
                    output[safe_key] = self.clean(item, depth + 1, known)
            return output
        if isinstance(value, (list, tuple)):
            return [self.clean(item, depth + 1, known) for item in value[:256]]
        if isinstance(value, str):
            return self.text(value, known)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value if math.isfinite(value) and abs(value) <= 1e15 else None
        return "[unsupported]"


def credentials_from(config):
    """Gather only credentials for masking, never return a configuration dump."""
    result = []

    def walk(value, sensitive=False, depth=0):
        if depth > 10:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, sensitive or bool(SECRET_KEY.search(str(key))), depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, sensitive, depth + 1)
        elif sensitive and isinstance(value, str):
            result.append(value)
    walk(config)
    return result


def require_admin(context, request):
    """The same strict Dashboard user shape used by LE's existing private API."""
    import jwt
    try:
        dashboard = context.get_config().get("dashboard", {})
        headers = request.headers.getlist("Authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            raise PermissionError("unauthorized")
        token = headers[0][7:].strip()
        if not token or len(token) > 8192 or not isinstance(dashboard.get("jwt_secret"), str) or not dashboard["jwt_secret"]:
            raise PermissionError("unauthorized")
        claims = jwt.decode(token, dashboard["jwt_secret"],
                            algorithms=["HS256"], options={"require": ["exp", "username"]})
        if (type(claims["exp"]) not in (int, float) or not math.isfinite(claims["exp"])
                or set(claims) - {"username", "exp", "auth_source"}
                or claims.get("auth_source", "password") not in ("password", "desktop")
                or not isinstance(claims["username"], str) or not claims["username"]
                or claims["username"] != request.username
                or claims["username"] != dashboard["username"]):
            raise PermissionError("forbidden")
        return claims["username"]
    except PermissionError:
        raise
    except Exception:
        raise PermissionError("unauthorized") from None


class _LogHandler(logging.Handler):
    def __init__(self, recorder):
        super().__init__(logging.INFO)
        self.recorder = recorder

    def emit(self, record):
        try:
            # A fallback global logger is acceptable only with this source filter.
            path = Path(record.pathname).resolve()
            if not path.is_relative_to(self.recorder.plugin_root):
                return
            # Arbitrary log arguments and f-strings can contain chat/prompt bodies.
            # Retain a format template, source location and exception type instead.
            message = str(record.msg)[:1024] if record.args else "runtime log"
            if record.exc_info:
                # Exception frame locations help debugging; omit source-code/body lines.
                message += "\n" + "".join(
                    f"{Path(frame.filename).name}:{frame.lineno} {frame.name}\n"
                    for frame in traceback.extract_tb(record.exc_info[2])
                ) + type(record.exc_info[1]).__name__
            self.recorder.record("runtime", {
                "at": record.created, "level": record.levelname,
                "module": record.module, "line": record.lineno,
                "detail": message,
            })
        except Exception:
            self.recorder.dropped += 1


def runtime_environment(host, plugin_version, settings):
    """Read local metadata only: never enumerate models or contact providers."""
    try:
        from astrbot import __version__ as host_version
    except ImportError:
        host_version = None
    providers = []
    try:
        config = host.get_config()
        for provider in config.get("provider", []):
            if isinstance(provider, dict):
                providers.append({key: provider.get(key) for key in ("id", "type", "api_type", "enable")})
    except Exception:
        pass
    settings["providers"] = providers
    return {"host_version": host_version, "plugin_version": plugin_version,
            "snapshot_kind": "current runtime, not historical state"}


def local_plugin_version(plugin_root):
    try:
        metadata = (Path(plugin_root) / "metadata.yaml").read_text(encoding="utf-8-sig")
        match = re.search(r"(?m)^version:\s*[\"']?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?)", metadata)
        return match.group(1) if match else None
    except OSError:
        return None


async def export_async(recorder, snapshot, sources):
    """Drain a cancelled compression worker; it must not leave a ZIP/lease."""
    task = asyncio.create_task(asyncio.to_thread(recorder.export, snapshot, sources))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if task.exception() is None:
            task.result().close()
        raise


class ExportedBundle:
    def __init__(self, owner, path):
        self.owner, self.path = owner, path
        self._closed = False
        self._lock = threading.Lock()

    def chunks(self):
        try:
            with self.path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    yield chunk
        finally:
            self.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.path.unlink(missing_ok=True)
            finally:
                with self.owner._bundle_lock:
                    self.owner._bundles.discard(self)
                self.owner._export_lock.release()


def download_response(bundle):
    """Cleanup even if ASGI sending fails before iteration or is disconnected."""
    from astrbot.api.web import stream_response
    headers = {
        "Cache-Control": "no-store", "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{bundle.path.name}"',
    }
    base = stream_response(iter(()), content_type="application/zip")

    class CleanupResponse(type(base)):
        async def __call__(self, scope, receive, send):
            try:
                return await super().__call__(scope, receive, send)
            finally:
                bundle.close()

    return CleanupResponse(bundle.chunks(), media_type="application/zip", headers=headers)


class DiagnosticRecorder:
    def __init__(self, data_dir, plugin_name, plugin_root, *, credentials=(),
                 clock=time.time, disk_limit=DISK_LIMIT, export_limit=EXPORT_LIMIT):
        self.plugin_name = plugin_name
        self.plugin_root = Path(plugin_root).resolve()
        self.clock, self.disk_limit, self.export_limit = clock, disk_limit, export_limit
        self.directory = Path(data_dir).resolve() / "diagnostics" if data_dir else None
        self.dropped = self.write_errors = self.pruned_files = 0
        self.started_at = self.clock()
        self._queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._export_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._bundles = set()
        self._bundle_lock = threading.Lock()
        self._handlers = []
        self._worker = None
        self._active = None
        self._active_hour = None
        self._record_bytes = None
        salt = secrets.token_hex(16)
        self.storage = "unavailable"
        try:
            if self.directory is not None:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.directory.resolve() != self.directory or self.directory.is_symlink():
                    raise OSError("unsafe diagnostic directory")
                self.directory.chmod(0o700)
                key = self.directory / "aliases"
                if regular(key):
                    with key.open(encoding="utf-8") as source:
                        previous_salt = source.read(64)
                    if not re.fullmatch(r"[0-9a-f]{32}", previous_salt):
                        raise ValueError("invalid alias salt")
                    salt = previous_salt
                else:
                    with key.open("x", encoding="utf-8") as target:
                        os.chmod(key, 0o600)
                        target.write(salt)
                status = self.directory / "status.json"
                if regular(status):
                    with status.open("rb") as source:
                        state = json.loads(source.read(LINE_LIMIT))
                    if not isinstance(state, dict):
                        raise ValueError("invalid diagnostic status")
                    for name in ("dropped", "write_errors", "pruned_files"):
                        value = state.get(name, 0)
                        if type(value) is int and value >= 0:
                            setattr(self, name, value)
                    previous = state.get("started_at")
                    if isinstance(previous, (int, float)) and math.isfinite(previous):
                        self.started_at = previous
                self.storage = "ready"
        except (OSError, ValueError, TypeError):
            self.write_errors += 1
        self.redactor = Redactor(salt, credentials)
        if self.storage == "ready":
            try:
                # A restart has no active export: clear crash leftovers immediately.
                self._prune(startup=True)
            except OSError:
                self.write_errors += 1
                self.storage = "unavailable"
            self._worker = threading.Thread(
                target=self._run, name=f"{plugin_name}-diagnostics", daemon=True)
            self._worker.start()

    def install(self, sink):
        if self._stop.is_set() or self._handlers:
            return
        handler = _LogHandler(self)
        try:
            sink.addHandler(handler)
            self._handlers.append((sink, handler))
        except (AttributeError, TypeError):
            handler.close()
            self.dropped += 1

    def record(self, operation, value):
        if self._stop.is_set():
            return
        try:
            row = self.redactor.clean(value)
            row["operation"] = operation
            row.setdefault("at", self.clock())
            row["plugin"] = self.plugin_name
            encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
            if len(encoded) > LINE_LIMIT or not self._worker:
                self.dropped += 1
            else:
                self._queue.put_nowait(encoded)
        except Exception:
            self.dropped += 1

    def health(self):
        return {"storage": self.storage, "started_at": self.started_at,
                "pending": self._queue.unfinished_tasks, "dropped": self.dropped,
                "write_errors": self.write_errors, "pruned_files": self.pruned_files,
                "window_seconds": WINDOW, "disk_limit_bytes": self.disk_limit,
                "closed": self._stop.is_set()}

    def _prune(self, *, reserve=0, startup=False):
        now = self.clock()
        paths = sorted(self.directory.glob("records-*.jsonl"))
        total = sum(path.stat().st_size for path in paths if regular(path))
        limit = max(0, self.disk_limit - reserve)
        for path in paths:
            if not regular(path):
                continue
            info = path.stat()
            if info.st_mtime <= now - WINDOW or total > limit:
                path.unlink()
                total -= info.st_size
                self.pruned_files += 1
                if path == self._active:
                    self._active = None
        self._record_bytes = total
        exports = self.directory / "exports"
        if exports.is_dir() and not exports.is_symlink():
            with self._bundle_lock:
                active = {bundle.path for bundle in self._bundles}
            for path in exports.glob("*.zip"):
                # Compression holds the lease before it registers the completed bundle.
                if (regular(path) and path not in active and not self._export_lock.locked()
                        and (startup or path.stat().st_mtime < time.time() - 3600)):
                    path.unlink()
        for path in self.directory.glob("status-*.tmp"):
            if regular(path):
                path.unlink()
        status = self.directory / "status.json"
        temporary = self.directory / ("status-" + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as target:
                os.chmod(temporary, 0o600)
                json.dump(self.health(), target)
            temporary.replace(status)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self):
        last_prune = 0
        while not self._stop.is_set() or not self._queue.empty():
            item = None
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                pass
            try:
                with self._io_lock:
                    rotated = False
                    if self._record_bytes is None:
                        self._prune()
                    if item is not None:
                        if len(item) > self.disk_limit:
                            self.dropped += 1
                        else:
                            # Enforce the total budget before every append, not only rotation.
                            if self._record_bytes + len(item) > self.disk_limit:
                                self._prune(reserve=len(item))
                            hour = int(self.clock() // 3600)
                            if (self._active is None or hour != self._active_hour
                                    or self._active.stat().st_size + len(item) > min(1024 * 1024, self.disk_limit)):
                                self._active = self.directory / (
                                    f"records-{int(self.clock() * 1000000):020d}-{secrets.token_hex(4)}.jsonl")
                                with self._active.open("xb"):
                                    os.chmod(self._active, 0o600)
                                self._active_hour = hour
                                rotated = True
                            with self._active.open("ab") as target:
                                target.write(item)
                            self._record_bytes += len(item)
                            os.utime(self._active, (self.clock(), self.clock()))
                    if (time.monotonic() - last_prune >= 60 or self._stop.is_set()
                            or rotated):
                        self._prune()
                        last_prune = time.monotonic()
                self.storage = "ready"
            except Exception:
                self.write_errors += 1
                self.storage = "unavailable"
                self._active = None
                self._record_bytes = None
            finally:
                if item is not None:
                    self._queue.task_done()

    def flush(self, timeout=2):
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._queue.all_tasks_done.wait(left)
        return True

    def export(self, snapshot, sources):
        if self._stop.is_set():
            raise RuntimeError("diagnostic recorder closed")
        if not self._export_lock.acquire(blocking=False):
            raise DiagnosticBusy("diagnostic export busy")
        path = None
        try:
            if (self.directory is None or self.directory.is_symlink()
                    or self.directory.resolve() != self.directory
                    or self.storage == "unavailable" and not self.directory.exists()):
                raise RuntimeError("diagnostic storage unavailable")
            self.flush()
            now = self.clock()
            start = now - WINDOW
            folder = self.directory / "exports"
            folder.mkdir(mode=0o700, exist_ok=True)
            if folder.resolve() != folder or folder.is_symlink():
                raise OSError("unsafe export directory")
            folder.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix=self.plugin_name + "-", suffix=".zip", dir=folder)
            os.close(fd)
            path = Path(name)
            manifest = {
                "schema_version": 1, "plugin": self.plugin_name, "exported_at": now,
                "range": {"start": start, "end": now, "timezone": "UTC",
                          "selection": "intersecting requests retain their causal chain"},
                "collection": self.health(), "files": {}, "skipped": [],
                "coverage": "available evidence only; missing history cannot be recovered",
                "export_limit_bytes": self.export_limit,
            }
            budget = max(0, self.export_limit - 16 * 1024)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                def write(name, records, *, history=True):
                    nonlocal budget
                    count = invalid = size = 0
                    earliest = latest = None
                    digest = hashlib.sha256()
                    truncated = False
                    with archive.open(name, "w") as output:
                        try:
                            for row in records:
                                if not isinstance(row, dict):
                                    invalid += 1
                                    continue
                                stamp = row.get("at", row.get("ts", row.get("started_at", row.get("occurred_at", row.get("created_at")))))
                                end = row.get("updated_at", stamp)
                                if history:
                                    if not all(type(value) in (int, float) and math.isfinite(value) for value in (stamp, end)):
                                        invalid += 1
                                        continue
                                    if stamp >= now or end < start:
                                        continue
                                clean = self.redactor.clean(row)
                                data = (json.dumps(clean, ensure_ascii=False, allow_nan=False) + "\n").encode()
                                if len(data) > LINE_LIMIT:
                                    invalid += 1
                                    continue
                                if len(data) > budget:
                                    truncated = True
                                    break
                                output.write(data)
                                digest.update(data)
                                size += len(data)
                                budget -= len(data)
                                count += 1
                                if history:
                                    earliest = stamp if earliest is None else min(earliest, stamp)
                                    latest = end if latest is None else max(latest, end)
                        except Exception as exc:
                            manifest["skipped"].append({"source": name, "reason": type(exc).__name__})
                        finally:
                            close = getattr(records, "close", None)
                            if callable(close):
                                close()
                    manifest["files"][name] = {
                        "records": count, "invalid": invalid, "bytes": size,
                        "sha256": digest.hexdigest(), "first_at": earliest,
                        "last_at": latest, "truncated": truncated,
                    }

                environment = {"python": platform.python_version(), "system": platform.system(),
                               "snapshot_at": now, "collection": self.health(),
                               **snapshot.get("environment", {})}
                write("environment.json", [environment], history=False)
                write("settings.json", [snapshot.get("settings", {})], history=False)
                paths = sorted(self.directory.glob("records-*.jsonl"), reverse=True)
                write("runtime.jsonl", json_lines(paths))
                for filename, source in sources.items():
                    if re.fullmatch(r"[a-z_]+\.jsonl", filename) is None:
                        raise ValueError("invalid diagnostic member name")
                    try:
                        records = source(start, now)
                    except Exception as exc:
                        manifest["skipped"].append({"source": filename, "reason": type(exc).__name__})
                        records = iter(())
                    write(filename, records)
                archive.writestr("说明.txt",
                    "近3天诊断包：服务端导出时刻向前72小时。\n"
                    "仅包含仍可用的证据；升级前清理、留存不足、损坏或超量记录不能补回。\n"
                    "请查看manifest.json中的采集状态、实际范围、跳过和截断。\n"
                    "已脱敏凭据和账号，剔除聊天全文、日记梦境和提示词；分享前检查错误片段。\n"
                    "提交问题时请补充发生时间与异常现象。\n")
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            with self._bundle_lock:
                if self._stop.is_set():
                    raise RuntimeError("diagnostic recorder closed")
                bundle = ExportedBundle(self, path)
                self._bundles.add(bundle)
            return bundle
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            self._export_lock.release()
            raise

    def close(self):
        if self._stop.is_set():
            return
        for sink, handler in self._handlers:
            sink.removeHandler(handler)
            handler.close()
        self._handlers.clear()
        with self._bundle_lock:
            self._stop.set()
            bundles = list(self._bundles)
        if self._worker:
            self._worker.join(timeout=3)
        for bundle in bundles:
            bundle.close()
