"""Safe event metadata shared by contract, without importing the peer plugin."""
from collections.abc import Mapping
from functools import lru_cache
import logging
from pathlib import Path
import re
import uuid


FLOW_KEY = "_living_diagnostics_flow_v1"
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
FLOW_ID = re.compile(r"flow-[0-9a-f]{32}\Z")
STOP = re.compile(r"(astrbot_plugin_[A-Za-z0-9_]{1,80}) - ([A-Za-z0-9_]{1,80}) stopped event propagation\.\Z")
PEERS = {"astrbot_plugin_livinghello", "astrbot_plugin_livingemotion"}
STOP_SITES = {"context_utils.py", "result_decorate/stage.py"}
RAW_CODES = {
    "post_type": {"message", "message_sent", "notice", "request", "meta_event"},
    "message_type": {"private", "group"},
    "notice_type": {"notify", "group_recall", "friend_recall", "group_increase",
                    "group_decrease", "group_admin", "group_upload", "group_ban",
                    "group_card", "offline_file", "friend_add", "essence", "client_status"},
    "request_type": {"friend", "group"},
    "meta_event_type": {"heartbeat", "lifecycle"},
    "sub_type": {"poke", "honor", "lucky_king", "normal", "anonymous", "notice",
                 "friend", "group", "other", "add", "invite", "approve", "leave",
                 "kick", "kick_me", "set", "unset", "ban", "lift_ban", "connect", "enable", "disable"},
}
COMPONENTS = {"Plain", "At", "AtAll", "Reply", "Image", "Face", "MFace", "Poke",
              "Record", "Video", "File", "Forward", "Node", "Nodes", "Contact",
              "Location", "Share", "Json", "Xml", "Music", "Dice", "Rps"}

@lru_cache(maxsize=512)
def log_path(path):
    return Path(path).resolve()

def event_metadata(event):
    """Keep only classified native metadata; absence never implies a message."""
    result = {}
    try:
        getter, setter = getattr(event, "get_extra", None), getattr(event, "set_extra", None)
        if callable(getter) and callable(setter):
            flow = getter(FLOW_KEY)
            if not isinstance(flow, str) or not FLOW_ID.fullmatch(flow):
                flow = "flow-" + uuid.uuid4().hex
                setter(FLOW_KEY, flow)
            result["flow_id"] = flow
        trace = getattr(getattr(event, "trace", None), "trace_id", None)
        if isinstance(trace, str) and HEX_ID.fullmatch(trace):
            result["host_trace_id"] = trace
        message = getattr(event, "message_obj", None)
        raw = getattr(message, "raw_message", None)
        if isinstance(raw, Mapping):
            for field, allowed in RAW_CODES.items():
                value = raw.get(field)
                if value is not None:
                    result["native_" + field] = value if type(value) is str and value in allowed else "other"
        result.setdefault("native_post_type", "unavailable")
        platform = getattr(getattr(event, "platform_meta", None), "name", None)
        if type(platform) is str:
            result["adapter"] = platform if platform in {
                "aiocqhttp", "qq_official", "qq", "telegram", "discord", "webchat",
                "lark", "wecom", "dingtalk", "slack", "satori", "kook", "matrix",
            } else "other"
        host_type = getattr(getattr(message, "type", None), "name", None)
        if host_type in ("GROUP_MESSAGE", "FRIEND_MESSAGE", "OTHER_MESSAGE"):
            result["host_message_type"] = host_type
        chain = getattr(message, "message", None)
        if isinstance(chain, (list, tuple)):
            result["component_count"] = len(chain)
            result["component_types"] = sorted({
                name if name in COMPONENTS else "other"
                for name in (type(part).__name__ for part in chain[:32])
            })
    except Exception:
        # Some legacy adapters don't provide the full event surface.
        result.setdefault("native_post_type", "unavailable")
    return result


def exception_metadata(exc):
    """Walk frames without asking traceback to read source or format the exception."""
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name,
                       "line": tb.tb_lineno, "function": code.co_name})
        tb = tb.tb_next
    return {"exception_type": type(exc).__name__, "frames": frames[-8:]}


class DiagnosticLogHandler(logging.Handler):
    """Capture only source metadata; the one parsed host message is a fixed stop marker."""

    def __init__(self, recorder, host_pipeline=None):
        super().__init__(logging.INFO)
        self.recorder = recorder
        self.host_pipeline = Path(host_pipeline).resolve() if host_pipeline else None

    def emit(self, record):
        try:
            path = log_path(record.pathname)
            trace = getattr(record, "trace_id", None)
            trace = trace if isinstance(trace, str) and HEX_ID.fullmatch(trace) else None
            flow = self.recorder.flow_for_host_trace(trace) if trace else None
            fields = {"at": record.created, "level": record.levelname, "line": record.lineno}
            if trace:
                fields["host_trace_id"] = trace
            if flow:
                fields["flow_id"] = flow
            if path.is_relative_to(self.recorder.plugin_root):
                operation = "runtime"
                fields["site"] = path.relative_to(self.recorder.plugin_root).as_posix()
            elif self.host_pipeline and path.is_relative_to(self.host_pipeline):
                fields["site"] = path.relative_to(self.host_pipeline).as_posix()
                match = (STOP.fullmatch(record.msg)
                         if fields["site"] in STOP_SITES and not record.args
                         and type(record.msg) is str and len(record.msg) <= 210 else None)
                if match and (flow or match[1].lower() in PEERS):
                    operation = "host_stop"
                    fields.update(stopping_plugin=match[1], handler=match[2])
                elif flow and record.levelno >= logging.WARNING:
                    operation = "host_pipeline_log"
                else:
                    return
            else:
                return
            exc = record.exc_info[1] if record.exc_info else None
            if exc is None and isinstance(record.args, tuple):
                exc = next((arg for arg in record.args[:8] if isinstance(arg, BaseException)), None)
            if isinstance(exc, BaseException):
                fields.update(exception_metadata(exc))
            self.recorder.record(operation, fields)
        except Exception:
            self.recorder.dropped += 1
