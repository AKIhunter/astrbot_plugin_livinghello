"""MCP 1.x/2.x 工具失败标记兼容，不依据正文猜测送达。"""


def failed(result):
    # MCP 2.x Python属性改为is_error，JSON别名仍为isError；两版都保留。
    return bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
