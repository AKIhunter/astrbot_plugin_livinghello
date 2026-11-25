"""把宿主 4.29 的接口适配成测试书写时的契约。

测试写的是「组件用 url 构造、路由返回字典、临时部件有 no_save」这套旧契约。
宿主 4.29 改成了 `Image(file=)` / `At(**kw)` / `JSONResponse` /
`model_dump_for_context()`。适配只发生在测试进程里，插件代码不做兼容分支；
单向、集中、可被删除。pytest（conftest）与 unittest（run_offline_tests）共用。
"""
import json
import sys


def _payload(self):
    """把响应体解析成可断言的字典；非 JSON 响应返回空字典。"""
    cached = getattr(self, "_lh_payload", None)
    if cached is not None:
        return cached
    try:
        value = json.loads(bytes(self.body).decode("utf-8"))
    except Exception:
        value = {}
    if not isinstance(value, dict):
        value = {}
    self._lh_payload = value
    return value


def _install_components():
    from astrbot.core.message import components

    for name in ("Image", "Record"):
        owner = getattr(components, name, None)
        if owner is None or getattr(owner.__init__, "_lh_adapted", False):
            continue
        original = owner.__init__

        def __init__(self, file=None, _original=original, **kwargs):
            url = kwargs.pop("url", None)
            if file is None:
                file = url
            elif url and "url" not in kwargs:
                # 宿主同时收 url 字段；两边都写，旧断言读 .url 也能过。
                kwargs["url"] = url
            _original(self, file, **kwargs)

        __init__._lh_adapted = True
        owner.__init__ = __init__

    at = getattr(components, "At", None)
    if at is not None and not getattr(at.__init__, "_lh_adapted", False):
        original = at.__init__

        def __init__(self, qq=None, _original=original, **kwargs):
            if qq is not None:
                kwargs.setdefault("qq", qq)
            _original(self, **kwargs)

        __init__._lh_adapted = True
        at.__init__ = __init__


def _install_response_view():
    try:
        from starlette.responses import JSONResponse
    except Exception:
        return
    if getattr(JSONResponse, "_lh_adapted", False):
        return

    def __getitem__(self, key):
        payload = _payload(self)
        if key in payload:
            return payload[key]
        if key == "status_code":
            return self.status_code
        raise KeyError(key)

    def __contains__(self, key):
        return key in _payload(self) or key == "status_code"

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return _payload(self).keys()

    def items(self):
        return _payload(self).items()

    def __iter__(self):
        return iter(_payload(self))

    def __len__(self):
        return len(_payload(self))

    def __repr__(self):
        return f"JSONResponse(status_code={self.status_code}, payload={_payload(self)})"

    JSONResponse.__getitem__ = __getitem__
    JSONResponse.__contains__ = __contains__
    JSONResponse.get = get
    JSONResponse.keys = keys
    JSONResponse.items = items
    JSONResponse.__iter__ = __iter__
    JSONResponse.__len__ = __len__
    JSONResponse.__repr__ = __repr__
    JSONResponse._lh_adapted = True


def _install_request():
    """路由处理器要读 request；这里给一个可变替身，测试可继续 patch 属性。"""
    from types import SimpleNamespace

    try:
        from astrbot.api import web
    except Exception:
        return

    async def json(default=None, _state=None):
        return {} if default is None else default

    fake = SimpleNamespace(
        method="POST", path="/", headers={}, cookies={}, content_type="application/json",
        client_host="127.0.0.1", path_params={}, plugin_name="test", username=None,
        query={}, json=json,
    )

    async def body():
        return b""

    async def form():
        return {}

    async def files():
        return {}

    fake.body, fake.form, fake.files = body, form, files

    old = getattr(web, "request", None)
    web.request = fake
    var = getattr(web, "_request_var", None)
    if var is not None:
        var.set(fake)
    # 已 import 过 request 的插件模块一并替换，避免测试里两个 request 不同步。
    if old is not None:
        for module in list(sys.modules.values()):
            if getattr(module, "request", None) is old:
                setattr(module, "request", fake)


def _install_part_views():
    from astrbot.core.agent.message import ContentPart

    if getattr(ContentPart, "_lh_adapted", False):
        return

    def no_save(self):
        """宿主把「不进上下文」记在序列化结果的 _no_save 上。"""
        dump = getattr(self, "model_dump_for_context", None)
        if callable(dump):
            try:
                return bool(dump().get("_no_save"))
            except Exception:
                return False
        return bool(getattr(self, "no_save", False))

    ContentPart.no_save = property(no_save)
    ContentPart._lh_adapted = True


def _install_all():
    _install_components()
    _install_response_view()
    _install_request()
    try:
        _install_part_views()
    except ImportError:
        # 旧宿主没有 agent.message 包；其它用例不受影响。
        pass


def install():
    """幂等安装；宿主不可导入时直接跳过（离线替身自带旧契约）。"""
    try:
        import astrbot
    except ImportError:
        return False
    if not getattr(astrbot, "__file__", None):
        # 离线替身是空壳模块，没有 __file__；它已经按旧契约实现。
        return False
    _install_all()
    return True
