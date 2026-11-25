import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zipfile

from diagnostic_bundle import DiagnosticRecorder
from observability.guard_diagnostics import prepare


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
                args = prepare(plugin)
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
