"""LivingHello evidence sources, without models or cross-plugin file reads."""
import json
from pathlib import Path

try:
    from ..diagnostic_bundle import DiagnosticRecorder, credentials_from, json_lines, sqlite_rows, runtime_environment, local_plugin_version, regular
    from ..diagnostic_event import event_metadata
except ImportError:
    from diagnostic_bundle import DiagnosticRecorder, credentials_from, json_lines, sqlite_rows, runtime_environment, local_plugin_version, regular
    from diagnostic_event import event_metadata

DECISION_FIELDS = frozenset((
    "ts", "request_id", "message_id", "session_id", "conversation_id", "action",
    "decision_action", "reason", "decision_reason", "src", "score", "conf",
    "threshold", "th", "mdl", "model_calls", "total_ms", "timings", "reply_error",
    "stage", "parse_reason", "failure_category", "flow_id", "host_trace_id",
    "min_length", "normalized_length", "input_length", "has_images", "ms", "mode", "gen_skipped",
))
SETTING_FIELDS = (
    "gate_mode", "classifier_provider_id", "vision_shared_provider_id",
    "classifier_timeout_seconds", "vision_timeout_seconds", "gate_total_timeout_seconds",
    "reply_primary_timeout_seconds", "reply_fallback_enabled", "watermark_scope",
    "private_watermark_window_seconds", "gate_reply_max_age_seconds",
    "expression_takeover_enabled", "coupling_livingemotion_enabled", "coupling_livingmemory_enabled",
    "gate_min_length", "participation_confidence_threshold", "apply_to_private", "gate_upstream_fanout_enabled",
)
REQUEST_FIELDS = frozenset((
    "schema_version", "request_id", "session_id", "message_id", "started_at",
    "updated_at", "instance_id", "state", "coverage", "events", "features",
    "source_ids", "summary", "config_snapshot", "truncated", "completion_note",
    "flow_id", "host_trace_id", "envelope", "merged_into",
))


def request_record(row):
    projected = {key: value for key, value in row.items() if key in REQUEST_FIELDS}
    # The console trace can keep an excerpt from a tool error. The shareable ZIP
    # keeps its type/stage, never the free-form excerpt.
    if isinstance(projected.get("features"), dict):
        projected["features"] = {key: {k: v for k, v in value.items() if k != "detail"}
                                 for key, value in projected["features"].items()
                                 if isinstance(value, dict)}
    if isinstance(projected.get("events"), list):
        projected["events"] = [{k: v for k, v in value.items() if k != "detail"}
                               for value in projected["events"] if isinstance(value, dict)]
    return projected


def decision_record(row):
    return {key: value for key, value in row.items() if key in DECISION_FIELDS}


def start(plugin):
    def credentials():
        return credentials_from(plugin.context.get_config()) + credentials_from(plugin.config)
    recorder = DiagnosticRecorder(plugin.data_dir, "astrbot_plugin_LivingHello",
                                  Path(__file__).resolve().parents[1], credentials=credentials)
    try:
        import astrbot

        host_file = getattr(astrbot, "__file__", None)
        logger = getattr(astrbot, "logger", None)
        if logger is not None:
            recorder.install(logger, host_pipeline=Path(host_file).parent / "core/pipeline" if host_file else None)
        # Recent hosts use a separate non-propagating logger for each plugin.
        sink = getattr(plugin, "logger", None)
        if sink is not None and not getattr(sink, "propagate", True):
            recorder.install(sink)
    except (ImportError, OSError):
        pass
    recorder.record("lifecycle", {"status": "loaded"})
    plugin.engine.log.diagnostics = lambda row: recorder.record("decision", decision_record(row))
    return recorder


def record_boundary(plugin, event, stage, status, reason):
    """Early branches don't always have a request trace; retain their safe metadata."""
    recorder = getattr(plugin, "diagnostics", None)
    if recorder is None:
        return
    try:
        row = event_metadata(event)
        recorder.remember_host_event(row)
        row.update(stage=stage, status=status, reason_code=reason,
                   session_id=str(getattr(event, "unified_msg_origin", "") or ""),
                   message_id=str(getattr(getattr(event, "message_obj", None), "message_id", "") or ""))
        recorder.record("native_step", row)
    except Exception:
        recorder.dropped += 1


def prepare(plugin):
    recorder = plugin.diagnostics
    root = Path(plugin.data_dir) if plugin.data_dir else None
    # Copy live state on the event loop before starting compression.
    trace = getattr(plugin, "request_trace", None)
    live = {key: request_record(trace._snapshot(row)) for key, row in list(trace.rows.items())} if trace else {}

    def decisions(_start, _end):
        if root is None:
            raise FileNotFoundError("decisions unavailable")
        paths = ([root / "decisions.jsonl"]
                 + sorted(root.glob("decisions.jsonl.archive-*"), reverse=True)
                 + [root / "decisions.jsonl.1"])
        if not any(regular(path) for path in paths):
            raise FileNotFoundError("decisions unavailable")
        for row in json_lines(paths, line_limit=1024 * 1024):
            yield decision_record(row) if isinstance(row, dict) else None

    def requests(start, end):
        if root is None:
            raise FileNotFoundError("requests unavailable")
        # Persisted rows may lag; take each live request snapshot without touching
        # Provider APIs, then prefer it over the same persisted request.
        yield from live.values()
        for row in sqlite_rows(root / "request_traces.sqlite3",
                               "SELECT request_id,payload_json FROM requests "
                               "WHERE started_at<? AND updated_at>=? ORDER BY updated_at DESC", (end, start)):
            if row["request_id"] not in live:
                try:
                    payload = json.loads(row["payload_json"])
                    yield request_record(payload) if isinstance(payload, dict) else None
                except ValueError:
                    yield None

    trace = getattr(plugin, "request_trace", None)
    settings = {key: plugin.config.get(key) for key in SETTING_FIELDS}
    engine = getattr(plugin, "engine", None)
    if engine:
        settings.update(effective_gate_min_length=engine.min_length(),
                        effective_participation_threshold=engine.threshold())
    try:
        from ..integrations.guard_coupling import snapshot as coupling_snapshot
    except ImportError:
        from integrations.guard_coupling import snapshot as coupling_snapshot
    snapshot = {"environment": {
        **runtime_environment(plugin.context, local_plugin_version(recorder.plugin_root), settings),
        "trace_health": dict(trace.health) if trace else {"storage": "unavailable"},
        "trace_pending": len(trace.pending) if trace else None,
        "coupling": coupling_snapshot(plugin.context, plugin.config),
        "snapshot_kind": "current runtime, not historical state",
    }, "settings": settings}
    return recorder, snapshot, {"decisions.jsonl": decisions, "requests.jsonl": requests}
