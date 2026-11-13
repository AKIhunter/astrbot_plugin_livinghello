"""外部接口边界：非法输入不写配置，异常不泄露内部数据。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from console import guard_webui as api
from test_gate import gate_config, plugin


class ConsoleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_error_does_not_return_exception_secrets(self):
        with patch.object(api, "_state", AsyncMock(
                side_effect=RuntimeError("api_key=sk-private-test /var/lib/private"))), \
                patch.object(api, "error_response",
                             lambda message, **kw: {"message": message, **kw}), \
                patch.object(api.logger, "exception"):
            result = await api.handle_state(object())
        self.assertEqual(result["status_code"], 500)
        self.assertNotIn("sk-private-test", str(result))
        self.assertNotIn("/var/lib/private", str(result))

    async def test_trace_id_validation_happens_before_storage_access(self):
        guard = SimpleNamespace(_trace=Mock(side_effect=AssertionError("storage touched")))
        for rid in ("", "../requests", "A" * 32, "' OR 1=1--", "a" * 33):
            with self.subTest(rid=rid), \
                    patch.object(api.request, "query", {"request_id": rid}, create=True), \
                    patch.object(api, "error_response", lambda message, **kw: kw):
                self.assertEqual((await api.handle_request_trace(guard))["status_code"], 400)
        guard._trace.assert_not_called()

    async def test_trace_missing_unavailable_and_exception_are_distinct(self):
        for outcome, health, expected in (
            (None, "ready", 404), (None, "unavailable", 503),
            (RuntimeError("private-path"), "ready", 500),
        ):
            service = SimpleNamespace(health={"storage": health}, get=AsyncMock(
                side_effect=outcome if isinstance(outcome, Exception) else None,
                return_value=outcome))
            guard = SimpleNamespace(_trace=lambda: service)
            with self.subTest(expected=expected), \
                    patch.object(api.request, "query", {"request_id": "a" * 32}, create=True), \
                    patch.object(api, "error_response", lambda message, **kw: {"message": message, **kw}), \
                    patch.object(api.logger, "exception"):
                result = await api.handle_request_trace(guard)
            self.assertEqual(result["status_code"], expected)
            self.assertNotIn("private-path", str(result))

    async def test_malformed_or_mixed_config_never_partially_saves(self):
        guard = plugin(gate_config())
        before = dict(guard.config)
        for body in ([], None, "", {}, {"gate_min_length": 3, "api_key": "private"},
                     {"classifier_timeout_seconds": float("nan")},
                     {"classifier_timeout_seconds": float("inf")}):
            with self.subTest(body=body), \
                    patch.object(api.request, "json", AsyncMock(return_value=body), create=True):
                result = await api.handle_config(guard)
            self.assertIsInstance(result, str)
            self.assertEqual(guard.config, before)

    async def test_search_rejects_unknown_category_and_bounds_query(self):
        guard = plugin(gate_config())
        with patch.object(api.request, "query", {"category": "admin"}, create=True), \
                patch.object(api, "error_response", lambda message, **kw: kw):
            self.assertEqual((await api.handle_decisions(guard))["status_code"], 400)
        with patch.object(api.request, "query", {"query": "x" * 10000}, create=True):
            self.assertEqual(len((await api.handle_decisions(guard))["data"]["query"]), 120)

    async def test_route_methods_prefix_uniqueness_and_bound_owner(self):
        registered = []
        owner = object()
        context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
        api.register_webui(owner, context)
        self.assertEqual(len(registered), 17)
        pairs = {(route, tuple(methods)) for route, _, methods, _ in registered}
        self.assertEqual(len(pairs), 17)
        self.assertTrue(all(route.startswith("/astrbot_plugin_LivingHello/console/")
                            for route, *_ in registered))
        state = next(fn for route, fn, _, _ in registered if route.endswith("/state"))
        with patch.object(api, "_state", AsyncMock(return_value={"owner": True})) as read:
            self.assertEqual((await state())["data"], {"owner": True})
        read.assert_awaited_once_with(owner)
