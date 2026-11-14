"""短视频抽帧：下载 → ffprobe 限时长 → ffmpeg 抽帧 → data URI 列表。

只做「拿到几帧图」这件事，模型调用由调用方负责。
设计上尽量宽容：任何一步失败都返回空列表，绝不把异常抛给聊天主流程。
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import urllib.request
from pathlib import Path

# 短视频传输上限。超过直接放弃，避免把带宽和磁盘拖垮。
MAX_VIDEO_BYTES = 30 * 1024 * 1024
# 抽出来的单帧再压一道，长边 1024 足够看内容。
FRAME_MAX_SIDE = 1024
FRAME_QUALITY = 82


def _is_local(url: str) -> bool:
    return url.startswith("file://") or url.startswith("/")


def _local_path(url: str) -> Path:
    return Path(url[7:] if url.startswith("file://") else url)


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_VIDEO_BYTES + 1)


def _probe_seconds(path: Path) -> float:
    """用 ffprobe 取时长；失败返回 0。"""
    try:
        out = subprocess_run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ])
        data = json.loads(out or "{}")
        return float(data.get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def subprocess_run(args, timeout: float = 30.0) -> str:
    import subprocess
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _shrink_frame(raw: bytes) -> tuple[bytes, str]:
    try:
        import io

        from PIL import Image
    except ImportError:
        return raw, "image/jpeg"
    try:
        image = Image.open(io.BytesIO(raw))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((FRAME_MAX_SIDE, FRAME_MAX_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=FRAME_QUALITY, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return raw, "image/jpeg"


def _extract(path: Path, workdir: Path, seconds: float, count: int) -> list[str]:
    """抽帧并转成 data URI。"""
    fps = max(count / max(seconds, 1.0), 0.05)
    pattern = workdir / "frame_%02d.jpg"
    subprocess_run([
        "ffmpeg", "-nostdin", "-y",
        "-i", str(path),
        "-t", "%.2f" % seconds,
        "-vf", "fps=%.4f" % fps,
        "-frames:v", str(count),
        "-q:v", "4",
        str(pattern),
    ], timeout=max(20.0, seconds * 2))
    frames = []
    for frame in sorted(workdir.glob("frame_*.jpg")):
        try:
            raw = frame.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        data, mime = _shrink_frame(raw)
        frames.append("data:%s;base64,%s" % (mime, base64.b64encode(data).decode()))
        if len(frames) >= count:
            break
    return frames


def _sync_frames(url: str, max_seconds: int, count: int) -> list[str]:
    if _is_local(url):
        path = _local_path(url)
        if not path.is_file():
            return []
        with tempfile.TemporaryDirectory(prefix="lh-video-") as tmp:
            workdir = Path(tmp)
            seconds = min(float(max_seconds), _probe_seconds(path) or float(max_seconds))
            return _extract(path, workdir, seconds, count)
    try:
        raw = _fetch(url)
    except Exception:
        return []
    if not raw or len(raw) > MAX_VIDEO_BYTES:
        return []
    with tempfile.TemporaryDirectory(prefix="lh-video-") as tmp:
        workdir = Path(tmp)
        path = workdir / "input.mp4"
        try:
            path.write_bytes(raw)
        except OSError:
            return []
        seconds = min(float(max_seconds), _probe_seconds(path) or float(max_seconds))
        return _extract(path, workdir, seconds, count)


async def frames_from_video(url: str, *, max_seconds: int = 15, count: int = 5) -> list[str]:
    """返回若干帧的 data URI；拿不到就返回空列表。"""
    if not url:
        return []
    count = max(1, min(int(count), 12))
    max_seconds = max(1, min(int(max_seconds), 120))
    try:
        return await asyncio.to_thread(_sync_frames, url, max_seconds, count)
    except Exception:
        return []
