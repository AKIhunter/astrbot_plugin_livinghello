"""事件内刷新图片、取平台OCR和有界下载；不使用额外模型，不缓存用户正文。"""
from __future__ import annotations

import asyncio
import base64
import math

MAX_BYTES = 8 * 1024 * 1024
DOWNLOAD_SECONDS = 8
OCR_SECONDS = 5
OCR_MAX_CHARS = 6000


def ocr_text(data):
    """平台置信度兼容0–1和0–100；OCR只是文字证据，不能代表完整画面。"""
    rows = data.get("texts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ""
    texts = []
    # 多留一个字符判断是否需要截断说明；不先拼接全部平台返回文字。
    remaining = OCR_MAX_CHARS + 1
    for row in rows[:300]:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            continue
        try:
            confidence = float(row.get("confidence", 0))
            confidence = confidence / 100 if confidence > 1 else confidence
        except (ValueError, TypeError):
            continue
        if not math.isfinite(confidence) or confidence < .75:
            continue
        text = row["text"]
        start, end = 0, len(text)
        # 用索引去除边缘空白，避免strip为超长带空白行复制整份正文。
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        if texts:
            remaining -= 1  # 换行也占正文字符预算。
        texts.append(text[start:min(end, start + remaining)])
        remaining -= min(end - start, remaining)
        if remaining == 0:
            break
    value = "\n".join(texts)
    return (value if len(value) <= OCR_MAX_CHARS else
            value[:OCR_MAX_CHARS] + "\n[OCR文字超过本轮上限，后续未收录]")


async def download_image(url):
    # aiohttp为宿主依赖。整次读取有界，坏链接不会再交给Provider重复等1800秒。
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_SECONDS)) as client:
        async with client.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > MAX_BYTES:
                raise ValueError("image_too_large")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("image_too_large")
    if not data:
        raise ValueError("image_empty_download")
    # 只接受实际图像，避免把错误页当图片交给模型。
    import io
    from PIL import Image
    from .guard_image_budget import check_decode_budget
    with Image.open(io.BytesIO(data)) as image:
        mime = Image.MIME.get(image.format, "image/png")
        check_decode_budget(image)  # verify不会解压像素，必须另查解码资源预算。
        image.verify()
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()


async def prepare_image(reader, event, url, source, *, on_ocr=None):
    """刷新一次；OCR与下载并行；取消时收束子任务，不输出URL或临时凭据。"""
    file_id = str(source.get("file_id") or "")
    can_ocr = callable(getattr(getattr(event, "bot", None), "call_action", None))
    if not url.startswith(("https://", "http://")):
        return url, "", ""
    fresh = url
    if file_id and can_ocr:
        try:
            info = await reader.api(event, "get_image", file=file_id)
            refreshed = info.get("url") if isinstance(info, dict) else None
            if isinstance(refreshed, str) and refreshed.startswith(("https://", "http://")):
                fresh = refreshed
        except Exception:
            pass  # 刷新不支持时尝试原地址一次。

    async def recognize():
        if not can_ocr:
            return ""
        try:
            result = await asyncio.wait_for(reader.api(event, "ocr_image", image=fresh), OCR_SECONDS)
            text = ocr_text(result)
        except Exception:
            return ""
        # 先发布已经取得的文字，再等待下载；外层取消不会抹掉这份事件内证据。
        if text and on_ocr is not None:
            on_ocr(text)
        return text

    async def fetch():
        try:
            return await download_image(fresh), ""
        except Exception as exc:
            return "", type(exc).__name__

    tasks = [asyncio.create_task(fetch()), asyncio.create_task(recognize())]
    try:
        (image, error), text = await asyncio.gather(*tasks)
        return image, text, error
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
