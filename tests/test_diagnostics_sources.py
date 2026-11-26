import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import diagnostic_bundle
from console import guard_webui
from diagnostic_bundle import DiagnosticRecorder
from observability import guard_diagnostics


class SourceTests(unittest.TestCase):
    def test_archives_large_body_and_live_snapshot_without_provider_calls(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            now = 1800000000
            recorder = DiagnosticRecorder(root, "LH", root, clock=lambda: now)
            try:
                for name, request in (("decisions.jsonl", "latest"),
                                      ("decisions.jsonl.archive-1", "archived"),
                                      ("decisions.jsonl.1", "legacy")):
                    (root / name).write_text(json.dumps({
                        "ts": now - 1, "request_id": request, "text": "private-body" * 8000,
                        "action": "silence", "unknown": "private-unknown"}) + "\n")
                live = {"started_at": now - 2, "updated_at": now - 1, "request_id": "live",
                        "session_id": "123456789", "state": "active", "events": [],
                        "judged_input": {"text": "private-judged"}, "unknown": "private-unknown"}
                trace = SimpleNamespace(rows={"live": live}, _snapshot=lambda row: dict(row),
                                        health={}, pending=[])
                plugin = SimpleNamespace(diagnostics=recorder, data_dir=root, request_trace=trace,
                                         config={}, context=SimpleNamespace(get_config=lambda: {}))
                args = guard_diagnostics.prepare(plugin)
                live["state"] = "changed_after_snapshot"
                bundle = recorder.export(args[1], args[2])
                try:
                    with zipfile.ZipFile(bundle.path) as archive:
                        decisions = archive.read("decisions.jsonl").decode()
                        requests = archive.read("requests.jsonl").decode()
                    self.assertEqual(len(decisions.splitlines()), 3)
                    self.assertNotIn("private-", decisions + requests)
                    self.assertEqual(json.loads(requests)["state"], "active")
                    self.assertNotIn("123456789", requests)
                finally:
                    bundle.close()
            finally:
                recorder.close()


class ConsoleDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_exports_slow_handler_and_failures_without_exception_text(self):
        folder = self.enterContext(TemporaryDirectory())
        root = Path(folder)
        recorder = DiagnosticRecorder(root, "LH", root)
        self.addCleanup(recorder.close)
        plugin = SimpleNamespace(diagnostics=recorder, context=None)

        async def handle_sample(_):
            return "ok"

        async def handle_broken(_):
            raise ValueError("private-handler-sentinel")

        with patch.object(guard_webui, "SLOW_ENDPOINT_MS", 0):
            self.assertEqual(await guard_webui._bind(plugin, handle_sample)(), "ok")
        with patch.object(guard_webui, "SLOW_ENDPOINT_MS", float("inf")):
            self.assertEqual(await guard_webui._bind(plugin, handle_sample)(), "ok")
            with self.assertRaisesRegex(ValueError, "private-handler-sentinel"):
                await guard_webui._bind(plugin, handle_broken)()
        with patch.object(diagnostic_bundle, "require_admin", return_value="admin"), \
                patch.object(guard_webui.request, "query", {}, create=True), \
                patch.object(guard_diagnostics, "prepare", side_effect=OSError("private-export-sentinel")):
            result = await guard_webui.handle_diagnostics_export(plugin)
        self.assertEqual(result["status_code"], 503)

        bundle = recorder.export({}, {})
        try:
            with zipfile.ZipFile(bundle.path) as archive:
                text = archive.read("runtime.jsonl").decode()
            self.assertNotIn("private-", text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(
                [(row["event"], row["endpoint"], row.get("exception_type")) for row in rows],
                [("console_slow", "sample", None), ("console_handler_failed", "broken", "ValueError"),
                 ("diagnostics_export_failed", "diagnostics_export", "OSError")],
            )
            self.assertGreaterEqual(rows[0]["duration_ms"], 0)
        finally:
            bundle.close()
