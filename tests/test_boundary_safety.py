"""边界回归：真实插件逻辑；平台、模型及下载替身，无网络或付费调用。"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import struct
import sys
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

from astrbot.api.message_components import Plain
from images import guard_image_budget as budget, guard_image_evidence as evidence
from input.guard_understanding import Observation
from reply import guard_expression as expression
from reply.guard_native_reply import managed_regex
from test_gate import gate_config, plugin
from test_reply_delivery import Event
import test_native_pipeline as native

try:
    from PIL import Image
except ImportError:
    Image = None

needs_pillow = unittest.skipUnless(Image is not None, "图片解码验收需要 Pillow")
needs_host = unittest.skipUnless(
    (native.SOURCE / "core/pipeline/respond/stage.py").is_file(),
    "需提供 ASTRBOT_NATIVE_SOURCE 宿主源码")


def png(width, height):
    """按行压缩，构造合法PNG时不先分配全尺寸位图。"""
    compressor = zlib.compressobj(9)
    row = b"\0" * (1 + width * 3)
    payload = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()

    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data
                + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", payload) + chunk(b"IEND", b""))


def uri(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def session_for(raw):
    class Response:
        content_length = len(raw)

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def iter_chunked(self, size):
            yield raw

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return Response()
    return SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kw: kw)


def old_ocr(data):
    # 只为常规长度边界的输出对照，不用于内存测量。
    texts = [r["text"].strip() for r in data["texts"][:300]
             if isinstance(r, dict) and isinstance(r.get("text"), str)
             and r.get("confidence") == 99 and r["text"].strip()]
    result = "\n".join(texts)
    return result if len(result) <= 6000 else result[:6000] + "\n[OCR文字超过本轮上限，后续未收录]"


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, **options):
        config = gate_config(expression_takeover_enabled=True, expression_punct_cleanup=True)
        config.update(options)
        guard = plugin(config)
        event = Event(group="")
        event.message_str = ""
        event.get_platform_name = lambda: "aiocqhttp"
        event.bot = SimpleNamespace(call_action=AsyncMock())
        return guard, guard._understanding(), event

    @needs_pillow
    def test_large_png_is_rejected_before_pixel_load(self):
        raw = png(5000, 5000)
        with patch.object(Image.Image, "load", side_effect=AssertionError("pixel_load")) as load:
            self.assertEqual(budget._shrink(raw), (b"", ""))
            load.assert_not_called()

    @needs_pillow
    def test_rejected_inline_png_never_falls_back_to_original(self):
        kept, dropped = budget.fit_sync([uri(png(5000, 5000))])
        self.assertEqual((kept, dropped), ([], 1))

    @needs_pillow
    async def test_download_rejects_large_png_by_header(self):
        with patch.dict(sys.modules, {"aiohttp": session_for(png(5000, 5000))}):
            with self.assertRaisesRegex(ValueError, "pixels_exceeded"):
                await evidence.download_image("https://example.invalid/big.png")

    @needs_pillow
    async def test_pixel_rejection_keeps_ocr_and_does_not_call_provider(self):
        _, reader, event = self.fixture()
        reader.api = AsyncMock(return_value={"texts": [{"text": "已读文字证据", "confidence": 99}]})
        obs = Observation(images=["https://example.invalid/big.png"], image_sources=[{}])
        provider = SimpleNamespace(text_chat=AsyncMock())
        with patch.dict(sys.modules, {"aiohttp": session_for(png(5000, 5000))}):
            state, calls = await reader._enrich_images(event, obs, provider, {})
        self.assertEqual((state, calls), ("partial", 0))
        provider.text_chat.assert_not_awaited()
        self.assertIn("已读文字证据", obs.vision_summary)
        self.assertIn("画面未读取", obs.vision_summary)
        self.assertTrue(obs.vision_unresolved)

    @needs_pillow
    def test_pixel_limit_inclusive_and_next_pixel_rejected(self):
        with patch.object(budget, "MAX_DECODE_PIXELS", 16):
            self.assertEqual(budget._shrink(png(4, 4))[1], "image/jpeg")
            self.assertEqual(budget._shrink(png(17, 1)), (b"", ""))

    def test_source_dimension_limit_runs_before_draft(self):
        image = SimpleNamespace(size=(32769, 1), width=32769, height=1, draft=Mock())
        with self.assertRaises(ValueError):
            budget.check_decode_budget(image)
        image.draft.assert_not_called()

    @needs_pillow
    def test_large_jpeg_still_uses_safe_draft(self):
        stream = io.BytesIO()
        with Image.new("RGB", (5000, 5000), "blue") as image:
            image.save(stream, format="JPEG")
        data, mime = budget._shrink(stream.getvalue())
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), 1024)
            image.load()

    @needs_pillow
    def test_gif_pixel_rejection_does_not_fall_back_to_url(self):
        stream = io.BytesIO()
        with Image.new("P", (5, 4)) as image:
            image.save(stream, format="GIF")
        with patch.object(budget, "MAX_DECODE_PIXELS", 16), \
                patch.object(budget, "_fetch_gif", return_value=stream.getvalue()):
            self.assertEqual(budget.fit_sync(["https://example.invalid/big.gif"]), ([], 1))

    @needs_pillow
    def test_identified_decode_error_does_not_fall_back_to_original(self):
        with patch.object(Image.Image, "thumbnail", side_effect=OSError("bad_decode")):
            self.assertEqual(budget._shrink(png(8, 8)), (b"", ""))

    @needs_pillow
    def test_actual_threads_admit_two_decodes_and_drop_third(self):
        release = threading.Event()
        ready = threading.Condition()
        active = 0
        original = Image.Image.thumbnail

        def blocked(image, *args, **kwargs):
            nonlocal active
            with ready:
                active += 1
                ready.notify_all()
            if not release.wait(2):
                raise AssertionError("decode_test_timeout")
            return original(image, *args, **kwargs)
        with patch.object(Image.Image, "thumbnail", blocked), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(budget._shrink, png(8, 8)) for _ in range(2)]
            try:
                with ready:
                    self.assertTrue(ready.wait_for(lambda: active == 2, timeout=1))
                self.assertEqual(budget._shrink(png(8, 8)), (b"", ""))
            finally:
                release.set()
            self.assertTrue(all(f.result(timeout=2)[1] == "image/jpeg" for f in futures))
        self.assertEqual(budget._shrink(png(8, 8))[1], "image/jpeg")

    @needs_pillow
    async def test_cancelled_request_keeps_slot_until_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = Image.Image.thumbnail

        def blocked(image, *args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("decode_test_timeout")
            return original(image, *args, **kwargs)
        slots = threading.BoundedSemaphore(1)
        request = SimpleNamespace(contexts=[], image_urls=[uri(png(8, 8))], extra_user_content_parts=[])
        with patch.object(budget, "_DECODE_SLOTS", slots), patch.object(Image.Image, "thumbnail", blocked):
            task = asyncio.create_task(budget.fit_images(request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(budget._shrink(png(8, 8)), (b"", ""))
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(slots.acquire, True, 1))
                slots.release()

    def test_ocr_large_rows_allocate_less_than_128kib(self):
        row = {"text": "x" * 50000, "confidence": 99}
        data = {"texts": [row] * 300}
        tracemalloc.start()
        try:
            result = evidence.ocr_text(data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result, "x" * 6000 + "\n[OCR文字超过本轮上限，后续未收录]")
        self.assertLess(peak, 128 * 1024)

    def test_ocr_padded_large_line_does_not_copy_full_stripped_body(self):
        data = {"texts": [{"text": " " + "x" * 5000000 + " ", "confidence": 99}]}
        tracemalloc.start()
        try:
            result = evidence.ocr_text(data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result[:6000], "x" * 6000)
        self.assertLess(peak, 128 * 1024)

    def test_ocr_character_and_newline_boundaries_match_previous_output(self):
        for lines in ([], [" "], ["x" * 6000], ["x" * 6001],
                      ["x" * 5999, "y"], ["x" * 6000, "y"],
                      [" 甲 ", "\t乙\n"], ["", "甲", " ", "乙"]):
            with self.subTest(lengths=list(map(len, lines))):
                data = {"texts": [{"text": text, "confidence": 99} for text in lines]}
                self.assertEqual(evidence.ocr_text(data), old_ocr(data))

    def test_ocr_300_row_limit_and_invalid_rows_preserved(self):
        data = {"texts": [None, {"text": 123}, {"text": "低", "confidence": 0},
                          *[{"text": "甲", "confidence": 99} for _ in range(300)]]}
        self.assertEqual(evidence.ocr_text(data), "\n".join(["甲"] * 297))

    def test_malformed_json_has_bounded_decoder_attempts_and_preserves_original(self):
        text = '["a",' * 1000 + "?"
        decoder = expression.json.JSONDecoder()
        attempts = Mock(wraps=decoder.raw_decode)
        with patch.object(expression.json, "JSONDecoder", return_value=SimpleNamespace(raw_decode=attempts)):
            self.assertEqual(expression.normalize_punctuation(text), text)
        self.assertLessEqual(attempts.call_count, 16)

    def test_overlong_text_is_preserved_by_normalizer_forbidden_and_tool(self):
        text = "口头禅，" + "a" * 16384 + '：{"enabled":true}。'
        self.assertEqual(expression.normalize_punctuation(text), text)
        self.assertEqual(expression.strip_forbidden(text, ["口头禅"]), text)
        guard, _, _ = self.fixture()
        args = {"messages": [{"type": "plain", "text": text}]}
        expression.Expression(guard).tool_arguments(SimpleNamespace(name="send_message_to_user"), args)
        self.assertEqual(args["messages"][0]["text"], text)

    def test_ascii_word_and_email_boundary_preserved(self):
        for text in ("a" * 16384, "dev@example.org", "甲 dev+ops@example.org 乙"):
            self.assertEqual(expression.normalize_punctuation(text), text)
        self.assertEqual(expression.normalize_punctuation("说明，dev@example.org。完成！"),
                         "说明 dev@example.org 完成")

    async def test_tool_long_word_does_not_delay_3ms_timer_past_20ms(self):
        loop = asyncio.get_running_loop()
        fired = loop.create_future()
        started = time.perf_counter()
        loop.call_later(.003, lambda: fired.set_result(time.perf_counter() - started))
        guard, _, _ = self.fixture()
        expression.Expression(guard).tool_arguments(SimpleNamespace(name="send_message_to_user"),
            {"messages": [{"type": "plain", "text": "a" * 12000}]})
        self.assertLess(await fired, .020)

    @needs_host
    async def test_actual_host_forbidden_outside_fence_keeps_runnable_code(self):
        original = native.plugin
        text = '口头禅。\n```python\nif True:\n    if True:\n        print("ok")\n```'

        def factory(config):
            config["expression_forbid_phrases"] = ["口头禅"]
            return original(config)
        for enabled in (False, True):
            for only in (False, True):
                with self.subTest(enabled=enabled, only=only), patch.object(native, "plugin", factory):
                    event, _, _ = await native.NativePipelineTests().run_pipeline(
                        text=text, quote=False, mention=False, native_quote=False, native_mention=False,
                        punctuation=True,
                        segments={"enable": enabled, "only_llm_result": only, "regex": managed_regex()})
                body = "".join(c.text for result in event.sent for c in result.chain if isinstance(c, Plain))
                self.assertNotIn("口头禅", body)
                self.assertIn('    if True:\n        print("ok")', body)
                compile(body.split("```python\n", 1)[1].split("\n```", 1)[0], "<reply>", "exec")

    def test_forbidden_in_technical_spans_is_kept(self):
        for technical in (
            '```python\nprint("口头禅")\n```', '~~~python\nprint("口头禅")\n~~~',
            '`口头禅:a.b(c)`', '{"值":"口头禅", "a": 1}',
            'python -c "print(\'口头禅\')"', '/tmp/口头禅.json',
        ):
            self.assertEqual(expression.strip_forbidden("口头禅。\n" + technical, ["口头禅"]).strip(),
                             technical)

    def test_plain_forbidden_cleanup_still_removes_suspended_punctuation(self):
        self.assertEqual(expression.strip_forbidden("口头禅。  你好，，\n", ["口头禅"]), " 你好\n")

    def test_untouched_code_has_identical_bytes(self):
        text = '```python\nif True:\n\tprint("ok")\n\n\n```'
        self.assertEqual(expression.strip_forbidden(text, ["口头禅"]), text)

    async def test_actual_tool_hook_only_cleans_plain_body_and_honors_switch(self):
        guard, _, event = self.fixture(expression_private_reuse=False)
        attachment = {"type": "file", "text": "报告-v1.2.pdf", "path": "/tmp/报告-v1.2.pdf"}
        args = {"session": "test:FriendMessage:123",
                "messages": [{"type": "plain", "text": "九点整，该起来了！"}, attachment]}
        await guard.trace_tool_start(event, SimpleNamespace(name="send_message_to_user"), args)
        self.assertEqual(args["messages"][0]["text"], "九点整 该起来了")
        self.assertIs(args["messages"][1], attachment)
        self.assertEqual(args["session"], "test:FriendMessage:123")
        guard.config["expression_punct_cleanup"] = False
        args["messages"][0]["text"] = "关闭后，保留！"
        await guard.trace_tool_start(event, SimpleNamespace(name="send_message_to_user"), args)
        self.assertEqual(args["messages"][0]["text"], "关闭后，保留！")

    async def test_sparse_ocr_uses_visual_and_preserves_original_numbers(self):
        _, reader, event = self.fixture(understand_hard_timeout_seconds=60)
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="文字梗图")))
        obs = Observation(images=["bad", "good"], image_sources=[{}, {}])
        with patch.object(evidence, "prepare_image", AsyncMock(side_effect=[
            ("", "10:12", "HTTPError"), ("local-image.png", "按钮", "")])):
            state, calls = await reader._enrich_images(event, obs, provider, {})
        self.assertEqual((state, calls), ("partial", 1))
        self.assertEqual(provider.text_chat.await_args.kwargs["image_urls"], ["local-image.png"])
        self.assertIn("图片2", provider.text_chat.await_args.kwargs["prompt"])
        self.assertIn("图片1下载失败", obs.vision_summary)

    async def test_refresh_and_ocr_survive_bad_download_without_visual_call(self):
        _, reader, event = self.fixture()
        reader.api = AsyncMock(side_effect=lambda e, action, **kw:
            {"url": "https://example.invalid/fresh"} if action == "get_image"
            else {"texts": [{"text": "可靠文字证据", "confidence": 99}]})
        obs = Observation(images=["https://example.invalid/expired"],
                          image_sources=[{"file_id": "cached-file"}])
        provider = SimpleNamespace(text_chat=AsyncMock())
        with patch.object(evidence, "download_image", AsyncMock(side_effect=TimeoutError)):
            state, calls = await reader._enrich_images(event, obs, provider, {})
        self.assertEqual((state, calls), ("partial", 0))
        provider.text_chat.assert_not_awaited()
        self.assertIn("可靠文字证据", obs.vision_summary)
        self.assertIn("https://example.invalid/fresh",
                      [call.kwargs.get("image") for call in reader.api.await_args_list])

    async def test_cached_parse_time_is_not_granted_again(self):
        _, reader, event = self.fixture(understand_image_enabled=True, understand_hard_timeout_seconds=3)
        obs = Observation(images=["image.png"], timings={"input_parse": 3500})
        reader.parse = AsyncMock(return_value=obs)
        reader.enrich = AsyncMock()
        self.assertIs(await reader._observe_source(event), obs)
        reader.enrich.assert_not_awaited()
        self.assertTrue(obs.vision_unresolved)

    @needs_pillow
    async def test_download_verifies_image_and_enforces_byte_limit(self):
        raw = png(4, 4)
        with patch.dict(sys.modules, {"aiohttp": session_for(raw)}):
            data_uri = await evidence.download_image("https://example.invalid/image")
            self.assertEqual(base64.b64decode(data_uri.split(",", 1)[1]), raw)
            with patch.object(evidence, "MAX_BYTES", 8):
                with self.assertRaisesRegex(ValueError, "too_large"):
                    await evidence.download_image("https://example.invalid/image")

    @needs_host
    async def test_actual_host_four_segment_combinations_and_prompt(self):
        text = "先把需要说明的内容说完整。\n\n再按意思分成自然的小段。"
        for only in (False, True):
            for enabled in (False, True):
                with self.subTest(only=only, enabled=enabled):
                    event, guard, _ = await native.NativePipelineTests().run_pipeline(
                        text=text, quote=False, mention=False, native_quote=False, native_mention=False,
                        punctuation=True,
                        segments={"enable": enabled, "only_llm_result": only,
                                  "regex": managed_regex(), "split_mode": "regex"})
                    bodies = ["".join(c.text for c in m.chain if isinstance(c, Plain)) for m in event.sent]
                    self.assertEqual(len(bodies), 2 if enabled else 1)
                    self.assertNotIn("。", "".join(bodies))
                    self.assertEqual("".join("".join(bodies).split()),
                                     "先把需要说明的内容说完整再按意思分成自然的小段")
                    req = SimpleNamespace(extra_user_content_parts=[])
                    guard.segments.attach(event, req)
                    self.assertEqual("8–100" in req.extra_user_content_parts[0].text, only)

    def test_missing_pillow_drops_inline_image_and_keeps_direct_path(self):
        with patch.dict(sys.modules, {"PIL": None}):
            urls = [uri(png(4, 4)), "/local/image.png"]
            self.assertEqual(budget.fit_sync(urls), (["/local/image.png"], 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
