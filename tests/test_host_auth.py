"""宿主源码认证矩阵：管理JWT、过期/伪造、页面资产token越界。"""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


@unittest.skipUnless(os.environ.get("ASTRBOT_NATIVE_SOURCE"), "需导出的生产宿主源码")
class HostAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_api_auth_matrix_from_real_host(self):
        import jwt
        from host_fixtures import dashboard_auth
        cls = dashboard_auth()
        dashboard = cls.__new__(cls)
        dashboard._jwt_secret = "isolated-test-key-" * 4
        dashboard._apply_auth_rate_limit = AsyncMock(return_value=None)
        api = "/api/plug/astrbot_plugin_LivingHello/console/state"
        base = {"username": "offline-user", "exp": 4102444800}
        valid = jwt.encode(base, dashboard._jwt_secret, algorithm="HS256")
        expired = jwt.encode({**base, "exp": 1}, dashboard._jwt_secret, algorithm="HS256")
        forged = jwt.encode(base, "different-isolated-test-key-" * 4, algorithm="HS256")
        empty_user = jwt.encode({**base, "username": ""}, dashboard._jwt_secret, algorithm="HS256")
        asset = jwt.encode({**base, "token_type": "plugin_page_asset"},
                           dashboard._jwt_secret, algorithm="HS256")
        rows = [
            ("anonymous", api, "", {}, {}, 401),
            ("malformed", api, "bad-token", {}, {}, 401),
            ("forged", api, forged, {}, {}, 401),
            ("expired", api, expired, {}, {}, 401),
            ("empty-user", api, empty_user, {}, {}, 401),
            ("valid-header", api, valid, {}, {}, None),
            ("valid-cookie", api, "", {"dashboard_jwt": valid}, {}, None),
            ("query-cannot-authorize-api", api, "", {}, {"asset_token": valid}, 401),
            ("asset-cannot-authorize-api", api, asset, {}, {}, 401),
            ("asset-bridge-allowed", "/api/plugin/page/bridge-sdk.js", "", {},
             {"asset_token": asset}, None),
        ]
        for name, path, bearer, cookies, query, expected in rows:
            request = SimpleNamespace(url=SimpleNamespace(path=path),
                                      headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
                                      cookies=cookies, query_params=query,
                                      state=SimpleNamespace(dashboard_g=SimpleNamespace()))
            with self.subTest(name=name):
                result = await dashboard.auth_middleware(request)
                self.assertEqual(getattr(result, "status_code", None), expected)
                if expected is None:
                    self.assertEqual(request.state.dashboard_g.username, "offline-user")
