"""全量隔离验收：可读宿主/历史快照；阻断外网，允许本机HTTP夹具。"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--test", action="append", default=[])
    args = parser.parse_args()
    root = args.root.resolve()
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(root / "tests"), str(root)]
    spec = importlib.util.spec_from_file_location("_boundary_stubs", root / "scripts/run_offline_tests.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.install_framework_stubs()

    blocked = []
    original = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def local(host):
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def check(address):
        if isinstance(address, tuple) and not local(address[0]):
            blocked.append("external-address")
            raise PermissionError("离线验收禁止外部网络")

    def connect(sock, address):
        check(address)
        return original[0](sock, address)

    def connect_ex(sock, address):
        check(address)
        return original[1](sock, address)

    def dns(host, *params, **kwargs):
        if host is not None and not local(host):
            blocked.append("external-dns")
            raise PermissionError("离线验收禁止外部DNS")
        return original[2](host, *params, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, dns
    started = time.monotonic()
    try:
        suite = (unittest.defaultTestLoader.loadTestsFromNames(args.test) if args.test else
                 unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py"))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = original
    # unittest的失败列表按subTest记数，方法失败数必须去重，避免虚增失败数量。
    method_id = lambda test: getattr(test, "test_case", test).id()
    failed = {method_id(test) for test, _ in result.failures}
    errors = {method_id(test) for test, _ in result.errors}
    payload = {
        "tests": result.testsRun,
        "passed": result.testsRun - len(failed | errors) - len(result.skipped),
        "failed": len(failed), "errors": len(errors), "skipped": len(result.skipped),
        "failure_assertions": len(result.failures),
        "duration_seconds_measured": round(time.monotonic() - started, 3),
        "external_network_attempts_blocked": blocked,
        "failures": [{"id": test.id(), "traceback": text} for test, text in result.failures],
        "error_details": [{"id": test.id(), "traceback": text} for test, text in result.errors],
        "skips": [{"id": test.id(), "reason": reason} for test, reason in result.skipped],
    }
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in payload.items() if k not in {
        "failures", "error_details", "skips"}}, ensure_ascii=False))
    return 0 if result.wasSuccessful() and not blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
