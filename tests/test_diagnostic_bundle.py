"""Offline retention, redaction, corruption, concurrency and cleanup contract."""
import importlib.util
import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / "diagnostic_bundle.py"
spec = importlib.util.spec_from_file_location("isolated_diagnostic_bundle", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.now = 1800000000.0
        self.recorder = module.DiagnosticRecorder(
            Path(self.folder.name), "test_plugin", SOURCE.parent,
            credentials=["synthetic-nonstandard-credential"], clock=lambda: self.now)

    def tearDown(self):
        self.recorder.close()
        self.assertFalse(self.recorder._worker.is_alive())
        self.folder.cleanup()

    def bundle(self, sources=None, snapshot=None):
        return self.recorder.export(snapshot or {}, sources or {})

    def read(self, bundle):
        with zipfile.ZipFile(bundle.path) as archive:
            return {name: archive.read(name).decode() for name in archive.namelist()}

    def test_nested_credentials_private_bodies_aliases_and_hashes(self):
        self.recorder.record("event", {
            "at": self.now - 1, "request_id": "a" * 32,
            "account_id": "123456789", "body": "private-body-sentinel",
            "nested": {"api_key": "synthetic-nonstandard-credential", "prompt": "private-prompt"},
            "detail": "failed synthetic-nonstandard-credential Bearer sk-offline0123456789 https://host/path?weird=credential QQ 123456789",
        })
        bundle = self.bundle()
        content = self.read(bundle)
        all_text = "\n".join(content.values())
        for forbidden in ("private-body-sentinel", "private-prompt", "synthetic-nonstandard-credential",
                          "sk-offline0123456789", "123456789", "https://host"):
            self.assertNotIn(forbidden, all_text)
        row = json.loads(content["runtime.jsonl"])
        self.assertEqual(row["request_id"], "a" * 32)
        self.assertIn(row["account_id"], row["detail"])
        manifest = json.loads(content["manifest.json"])
        import hashlib
        self.assertEqual(manifest["files"]["runtime.jsonl"]["sha256"],
                         hashlib.sha256(content["runtime.jsonl"].encode()).hexdigest())
        bundle.close()

    def test_time_edges_and_cross_boundary_causal_chain(self):
        start = self.now - module.WINDOW
        rows = [{"at": at, "marker": marker} for at, marker in (
            (start - .1, "old"), (start, "start"), (self.now - .1, "last"), (self.now, "future"))]
        rows.append({"started_at": start - 10, "updated_at": start + 10,
                     "marker": "crossing", "events": [{"at": start - 10, "status": "running"}]})
        bundle = self.bundle({"events.jsonl": lambda *_: iter(rows)})
        content = self.read(bundle)
        self.assertEqual([json.loads(line)["marker"] for line in content["events.jsonl"].splitlines()],
                         ["start", "last", "crossing"])
        bundle.close()

    def test_corruption_oversized_line_and_partial_line_remain_visible(self):
        path = Path(self.folder.name) / "legacy.jsonl"
        path.write_bytes(b'broken\n' + b"x" * (module.LINE_LIMIT + 5) + b"\n" +
                         json.dumps({"at": self.now - 1, "status": "failed"}).encode() +
                         b"\n{\"unfinished\":")
        bundle = self.bundle({"events.jsonl": lambda *_: module.json_lines([path])})
        manifest = json.loads(self.read(bundle)["manifest.json"])
        self.assertEqual(manifest["files"]["events.jsonl"]["invalid"], 3)
        self.assertEqual(manifest["files"]["events.jsonl"]["records"], 1)
        bundle.close()

    def test_missing_and_failed_sources_allow_remaining_evidence(self):
        def broken(*_):
            raise FileNotFoundError("synthetic-nonstandard-credential")
        bundle = self.bundle({"events.jsonl": broken,
                              "behavior.jsonl": lambda *_: iter([{"at": self.now - 1, "status": "failed"}])})
        content = self.read(bundle)
        manifest = json.loads(content["manifest.json"])
        self.assertEqual(manifest["skipped"][0]["reason"], "FileNotFoundError")
        self.assertEqual(manifest["files"]["behavior.jsonl"]["records"], 1)
        bundle.close()

    def test_export_limit_truncates_on_record_boundary(self):
        self.recorder.export_limit = 16 * 1024 + 2048
        rows = [{"at": self.now - 1, "detail": "x" * 1000} for _ in range(20)]
        bundle = self.bundle({"events.jsonl": lambda *_: iter(rows)})
        content = self.read(bundle)
        manifest = json.loads(content["manifest.json"])
        self.assertTrue(manifest["files"]["events.jsonl"]["truncated"])
        for line in content["events.jsonl"].splitlines():
            self.assertIsInstance(json.loads(line), dict)
        bundle.close()

    def test_busy_lease_cleanup_and_retry(self):
        first = self.bundle()
        with self.assertRaises(module.DiagnosticBusy):
            self.bundle()
        first.close()
        self.assertFalse(first.path.exists())
        first.close()
        second = self.bundle()
        iterator = second.chunks()
        next(iterator)
        iterator.close()
        self.assertFalse(second.path.exists())
        third = self.bundle()
        self.recorder.close()
        self.assertFalse(third.path.exists())

    def test_sqlite_snapshot_never_creates_missing_database(self):
        path = Path(self.folder.name) / "business.sqlite3"
        with self.assertRaises(FileNotFoundError):
            list(module.sqlite_rows(path, "SELECT * FROM events", ()))
        self.assertFalse(path.exists())
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE events(at REAL,status TEXT)")
            db.execute("INSERT INTO events VALUES(?,?)", (self.now - 1, "failed"))
        bundle = self.bundle({"events.jsonl": lambda *_: module.sqlite_rows(path, "SELECT * FROM events", ())})
        self.assertEqual(json.loads(self.read(bundle)["events.jsonl"])["status"], "failed")
        bundle.close()

    def test_retention_size_cap_and_drop_state_survive_restart(self):
        self.recorder.disk_limit = 500
        for _ in range(8):
            self.recorder.record("event", {"at": self.now - 1, "detail": "x" * 100})
        self.assertTrue(self.recorder.flush())
        paths = list(self.recorder.directory.glob("records-*.jsonl"))
        self.assertLessEqual(sum(p.stat().st_size for p in paths), 500)
        self.assertGreater(self.recorder.pruned_files, 0)
        self.now += module.WINDOW + 1
        self.recorder.record("new", {"at": self.now - 1})
        self.assertTrue(self.recorder.flush())
        bundle = self.bundle()
        records = [json.loads(line) for line in self.read(bundle)["runtime.jsonl"].splitlines()]
        self.assertEqual([row["operation"] for row in records], ["new"])
        bundle.close()
        self.recorder.close()
        reopened = module.DiagnosticRecorder(Path(self.folder.name), "test_plugin", SOURCE.parent,
                                             clock=lambda: self.now)
        try:
            self.assertGreater(reopened.pruned_files, 0)
        finally:
            reopened.close()

    def test_size_cap_is_enforced_on_each_append_before_next_rotation(self):
        self.recorder.disk_limit = 500
        old = self.recorder.directory / "records-00000000000000000000-old.jsonl"
        old.write_text(json.dumps({"at": self.now - 10, "detail": "x" * 250}) + "\n")
        os.utime(old, (self.now - 10, self.now - 10))
        for index in range(6):
            self.recorder.record("event", {"at": self.now - 1, "index": index})
            self.assertTrue(self.recorder.flush())
            paths = list(self.recorder.directory.glob("records-*.jsonl"))
            self.assertLessEqual(sum(path.stat().st_size for path in paths), 500)
        self.assertFalse(old.exists())
        self.assertGreater(self.recorder.pruned_files, 0)

    def test_startup_removes_recent_orphan_exports_and_status_temps(self):
        self.recorder.close()
        directory = self.recorder.directory
        exports = directory / "exports"
        exports.mkdir(exist_ok=True)
        orphan = exports / "test_plugin-orphan.zip"
        with zipfile.ZipFile(orphan, "w") as archive:
            archive.writestr("example.json", "{}")
        temporary = directory / "status-abandoned.tmp"
        temporary.write_text('{"unfinished":')
        unrelated = directory / "unrelated.txt"
        unrelated.write_text("keep")
        aliases = (directory / "aliases").read_bytes()
        reopened = module.DiagnosticRecorder(Path(self.folder.name), "test_plugin", SOURCE.parent,
                                             clock=lambda: self.now)
        try:
            self.assertFalse(orphan.exists())
            self.assertFalse(temporary.exists())
            self.assertEqual(unrelated.read_text(), "keep")
            self.assertEqual((directory / "aliases").read_bytes(), aliases)
            self.assertEqual(reopened.storage, "ready")
        finally:
            reopened.close()

    def test_idle_worker_expires_logs_without_new_records(self):
        self.recorder.record("event", {"at": self.now - 1})
        self.assertTrue(self.recorder.flush())
        old = list(self.recorder.directory.glob("records-*.jsonl"))
        self.assertTrue(old)
        pruned = threading.Event()
        original = self.recorder._prune

        def observe(*args, **kwargs):
            original(*args, **kwargs)
            if not any(path.exists() for path in old):
                pruned.set()

        later = time.monotonic() + 61
        self.now += module.WINDOW + 1
        with mock.patch.object(self.recorder, "_prune", side_effect=observe), \
                mock.patch.object(module.time, "monotonic", return_value=later):
            self.assertTrue(pruned.wait(3), "idle cleanup did not remove expired files")
        self.assertTrue(self.recorder._queue.empty())

    def test_cleanup_keeps_zip_while_compression_is_in_progress(self):
        started, resume = threading.Event(), threading.Event()
        bundles, failures = [], []

        def paused(*_):
            started.set()
            resume.wait(3)
            return iter([{"at": self.now - 1, "status": "failed"}])

        def export():
            try:
                bundles.append(self.bundle({"events.jsonl": paused}))
            except Exception as exc:
                failures.append(type(exc).__name__)

        worker = threading.Thread(target=export)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            paths = list((self.recorder.directory / "exports").glob("*.zip"))
            self.assertEqual(len(paths), 1)
            aged = time.time() - 7200
            os.utime(paths[0], (aged, aged))
            with self.recorder._io_lock:
                self.recorder._prune()
            self.assertTrue(paths[0].exists(), "cleanup removed an active compression")
        finally:
            resume.set()
            worker.join(3)
            for bundle in bundles:
                bundle.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(list((self.recorder.directory / "exports").glob("*.zip")))

    def test_log_handler_filters_other_plugins_and_detaches(self):
        logger = logging.getLogger("isolated-diagnostics")
        self.recorder.install(logger)
        self.recorder.install(logger)
        self.assertEqual(len(logger.handlers), 1)
        for file in (SOURCE, Path(self.folder.name) / "other_plugin.py"):
            record = logging.LogRecord(logger.name, logging.ERROR, str(file), 42, "upstream failed", (), None)
            record.created = self.now - 1
            logger.handle(record)
        self.assertTrue(self.recorder.flush())
        bundle = self.bundle()
        content = self.read(bundle)
        self.assertEqual(json.loads(content["manifest.json"])["files"]["runtime.jsonl"]["records"], 1)
        bundle.close()
        self.recorder.close()
        self.assertEqual(logger.handlers, [])

    def test_symlink_log_is_not_exported(self):
        path = Path(self.folder.name) / "external"
        path.write_text(json.dumps({"at": self.now - 1, "detail": "not-our-source"}))
        link = self.recorder.directory / "records-99999999999999999999.jsonl"
        link.symlink_to(path)
        bundle = self.bundle()
        self.assertNotIn("not-our-source", self.read(bundle)["runtime.jsonl"])
        bundle.close()

    def test_dynamic_credentials_and_unstructured_log_body_are_omitted(self):
        credentials = ["first-synthetic-credential"]
        self.recorder.redactor = module.Redactor("salt", lambda: credentials)
        credentials[:] = ["second-synthetic-credential"]
        logger = logging.getLogger("private-runtime-diagnostics")
        self.recorder.install(logger)
        for message, args in (("message sent: %s", ("private-body-sentinel",)),
                              ("generated private-body-sentinel", ())):
            record = logging.LogRecord(logger.name, logging.ERROR, str(SOURCE), 10, message, args, None)
            record.created = self.now - 1
            logger.handle(record)
        self.recorder.record("failure", {"at": self.now - 1, "detail": credentials[0]})
        bundle = self.bundle()
        content = "\n".join(self.read(bundle).values())
        self.assertNotIn("private-body-sentinel", content)
        self.assertNotIn(credentials[0], content)
        self.assertIn("message sent: %s", content)
        bundle.close()

    def test_sqlite_wal_snapshot_consistent_during_concurrent_commit(self):
        path = Path(self.folder.name) / "wal.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE events(at REAL, status TEXT)")
            db.executemany("INSERT INTO events VALUES(?,?)", [(1, "before"), (2, "before")])
            db.commit()
            iterator = module.sqlite_rows(path, "SELECT * FROM events ORDER BY at", ())
            self.assertEqual(next(iterator)["status"], "before")
            db.execute("UPDATE events SET status='after'")
            db.commit()
            self.assertEqual(next(iterator)["status"], "before")
            iterator.close()
        finally:
            db.close()

    def test_cancelled_compression_and_unload_leave_no_zip_or_lease(self):
        started, resume = threading.Event(), threading.Event()

        def slow(*_):
            started.set()
            resume.wait(2)
            return iter([{"at": self.now - 1, "status": "failed"}])

        async def exercise():
            task = asyncio.create_task(module.export_async(self.recorder, {}, {"events.jsonl": slow}))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(exercise())
        self.assertFalse(list((self.recorder.directory / "exports").glob("*.zip")))
        self.assertFalse(self.recorder._export_lock.locked())
        started.clear()
        resume.clear()
        failures = []
        def export():
            try:
                self.bundle({"events.jsonl": slow})
            except RuntimeError as exc:
                failures.append(type(exc).__name__)
        worker = threading.Thread(target=export)
        worker.start()
        self.assertTrue(started.wait(2))
        self.recorder.close()
        resume.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ["RuntimeError"])
        self.assertFalse(list((self.recorder.directory / "exports").glob("*.zip")))

    def test_admin_token_rejects_resource_expired_duplicate_and_other_user(self):
        import jwt
        from types import SimpleNamespace
        from starlette.datastructures import Headers
        host = SimpleNamespace(get_config=lambda: {
            "dashboard": {"username": "admin", "jwt_secret": "synthetic-jwt-secret-for-tests-only"}})
        def request(claims, *, username="admin", duplicate=False):
            token = jwt.encode(claims, host.get_config()["dashboard"]["jwt_secret"], algorithm="HS256")
            header = (b"authorization", ("Bearer " + token).encode())
            return SimpleNamespace(headers=Headers(raw=[header] * (2 if duplicate else 1)), username=username)
        claims = {"username": "admin", "exp": time.time() + 60}
        self.assertEqual(module.require_admin(host, request(claims)), "admin")
        for bad in ({**claims, "exp": time.time() - 10},
                    {**claims, "scope": "plugin_asset"}, {**claims, "auth_source": "asset"},
                    {**claims, "username": "other"}):
            with self.subTest(claims=bad), self.assertRaises(PermissionError):
                module.require_admin(host, request(bad))
        for invalid in (request(claims, duplicate=True), request(claims, username="other"),
                        SimpleNamespace(headers=Headers(), username=None)):
            with self.assertRaises(PermissionError):
                module.require_admin(host, invalid)

    def test_corrupt_status_or_alias_file_does_not_break_plugin_initialization(self):
        self.recorder.close()
        for filename, value in (("status.json", "[]"), ("aliases", "")):
            path = self.recorder.directory / filename
            original = path.read_bytes()
            path.write_text(value)
            reopened = module.DiagnosticRecorder(Path(self.folder.name), "test_plugin", SOURCE.parent)
            try:
                self.assertEqual(reopened.storage, "unavailable")
                self.assertGreater(reopened.write_errors, 0)
            finally:
                reopened.close()
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
