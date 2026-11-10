"""把交给主模型的图片压进上下文预算，避免 base64 原图撑爆 token。

背景：QQ/OneBot 常把图片给成 base64，原样进 req.image_urls 时，
上下文会被几十万 token 的 base64 文本占满（实测 4000x3000 原图约 28 万 token/张）。

策略（三道闸，任何一道单独生效都能兜住）：
- 张数闸：最多 MAX_IMAGES 张。超了先砍中间，保留「当前消息图」（头部）
  和「连续分片图」（尾部）——这两类才是本轮真正要看的。
- 单张闸：单张 base64 超过 MAX_SINGLE_B64 直接丢，异常大图不冒险。
- 总量闸：压缩后仍超 MAX_TOTAL_B64 就继续丢，保证再多的图也撑不爆请求。
- http/file/本地路径原样保留：宿主自己取，不占 prompt 字节。
- base64 / data URI 用 Pillow 的 draft() 在解码阶段降采样，再压成 JPEG
  （draft 不生成全尺寸位图，实测内存零增长；不加 draft 单张要 +46MB）
"""
from __future__ import annotations

import asyncio
import base64
import io
import re

MAX_SIDE = 1024                     # 长边像素；1024 对参与判断和日常看图足够
QUALITY = 82                        # JPEG 质量
MAX_IMAGES = 12                     # 与分批图片理解上限一致；超限须明确告知模型
MAX_TOTAL_B64 = 1024 * 1024         # 当前内联图片总字符预算，与文字计量分开
MAX_SINGLE_B64 = 12 * 1024 * 1024   # 单张超过这个直接丢
DIRECT_PREFIX = ("http://", "https://", "file://")

_DATA_URI = re.compile(r"^data:image/(\w+);base64,", re.I)


def _decode(url: str) -> bytes | None:
    if url.startswith("base64://"):
        return base64.b64decode(url[9:])
    match = _DATA_URI.match(url)
    if match:
        return base64.b64decode(url[match.end():])
    return None


def _shrink(data: bytes) -> tuple[bytes, str]:
    """降采样并转 JPEG。失败时返回 (原数据, "")，由调用方按原字节计预算。"""
    try:
        from PIL import Image
    except ImportError:
        return data, ""
    try:
        image = Image.open(io.BytesIO(data))
        # 关键：draft 让 JPEG 在解码阶段就按 1/2、1/4、1/8 降采样，
        # 不生成全尺寸位图，内存只按目标尺寸走。
        image.draft("RGB", (MAX_SIDE * 2, MAX_SIDE * 2))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=QUALITY, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, ""


def _priority_order(urls: list[str]) -> list[str]:
    """超量时决定留谁：保头保尾，先砍中间。

    追加顺序是「本轮请求图 → 生图 → 分片 → 其他插件」，头尾都可能是
    当前输入，中间最容易是别处的历史图，所以从中间开始丢。
    """
    if len(urls) <= MAX_IMAGES:
        return list(urls)
    head = MAX_IMAGES // 2
    tail = MAX_IMAGES - head
    return list(urls[:head]) + list(urls[-tail:])


def fit_sync(urls: list[str]) -> tuple[list[str], int]:
    """同步裁剪，重活放线程池调用。返回 (保留列表, 丢弃数)。

    丢弃数含两部分：张数闸裁掉的、以及单张/总量闸丢掉的。
    """
    kept: list[str] = []
    total = 0
    seen: set[str] = set()
    ordered = _priority_order(urls)
    trimmed = max(0, len(urls) - len(ordered))
    dropped = trimmed
    for url in ordered:
        if not url or url in seen:
            dropped += 1
            continue
        seen.add(url)
        if url.startswith(DIRECT_PREFIX) or url.startswith("/"):
            kept.append(url)
            continue
        if len(url) > MAX_SINGLE_B64:
            dropped += 1
            continue
        try:
            data = _decode(url)
        except Exception:
            data = None
        if data is None:
            # 不认识的形式不擅自改动，原样保留
            kept.append(url)
            continue
        data, mime = _shrink(data)
        if not data or not mime:
            if total + len(url) > MAX_TOTAL_B64:
                dropped += 1
                continue
            total += len(url)
            kept.append(url)
            continue
        encoded = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
        if total + len(encoded) > MAX_TOTAL_B64:
            dropped += 1
            continue
        total += len(encoded)
        kept.append(encoded)
    return kept, dropped


async def fit_images(req) -> tuple[int, int, int]:
    """裁剪 req.image_urls。返回 (丢弃数, 处理前字节数, 处理后字节数)。"""
    # 主模型可能直连提供商，不经过路由；同样精简发送副本中的旧图。
    # 原历史对象和数据库保持完整，已有旧图补读工具仍使用原始引用表。
    try:
        from .image_history import compact_contexts
    except ImportError:
        from image_history import compact_contexts
    contexts = getattr(req, "contexts", None)
    if contexts:
        current_in_contexts = (
            getattr(req, "prompt", None) is None and not getattr(req, "image_urls", None)
            and not getattr(req, "audio_urls", None))
        req.contexts, _ = compact_contexts(contexts, current_in_contexts=current_in_contexts)
    urls = list(getattr(req, "image_urls", None) or [])
    if not urls:
        return 0, 0, 0
    before = sum(len(url) for url in urls)
    kept, dropped = await asyncio.to_thread(fit_sync, urls)
    req.image_urls[:] = kept
    if dropped:
        from astrbot.core.agent.message import TextPart
        req.extra_user_content_parts.append(TextPart(
            text=f"[本轮原图预算：输入{len(urls)}张，保留{len(kept)}张，省略{dropped}张。"
                 "省略的图片仅可使用已提供的可靠观察，未被观察覆盖的画面不得猜测；需要时请用户分批重新引用。]"
        ).mark_as_temp())
    after = sum(len(url) for url in kept)
    return dropped, before, after
