"""无模型费用、无平台发送；覆盖多图、部分超时、发送副本历史精简。"""
import asyncio
import base64
import copy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from input.guard_understanding import Observation, Understanding
from images.guard_image_budget import fit_images


class Event:
    def __init__(self):
        self.extra = {}
    def get_extra(self, key, default=None):
        return self.extra.get(key, default)
    def set_extra(self, key, value):
        self.extra[key] = value


class MultiImageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(config={}, _policy_send_reason=lambda _: "")
        self.reader = Understanding(self.plugin)
        self.event = Event()

    async def test_twelve_images_each_seen_once_with_two_inflight(self):
        current = peak = 0
        seen = []
        async def call(provider, **kwargs):
            nonlocal current, peak
            current += 1
            peak = max(current, peak)
            seen.extend(kwargs["image_urls"])
            self.assertEqual(kwargs["max_tokens"], 512)
            self.assertEqual(kwargs["request_max_retries"], 1)
            await asyncio.sleep(.001)
            current -= 1
            return SimpleNamespace(completion_text="合成观察")
        self.reader._call_locked_upstream = AsyncMock(side_effect=call)
        urls = [f"https://example.invalid/{i}.png" for i in range(12)]
        observation = Observation(images=urls)
        state, calls = await self.reader._enrich_images(self.event, observation, object(), {})
        self.assertEqual((state, calls, peak), ("success", 6, 2))
        self.assertCountEqual(seen, urls)
        self.assertIn("图片11—12", observation.vision_summary)
        self.assertFalse(observation.vision_unresolved)

    async def test_limit_and_provider_failure_are_explicit_without_retry(self):
        async def call(provider, **kwargs):
            if kwargs["image_urls"][0].endswith("/2.png"):
                raise asyncio.TimeoutError()
            return SimpleNamespace(completion_text="成功画面")
        self.reader._call_locked_upstream = AsyncMock(side_effect=call)
        observation = Observation(images=[f"https://example.invalid/{i}.png" for i in range(15)])
        state, calls = await self.reader._enrich_images(self.event, observation, object(), {})
        self.assertEqual((state, calls), ("partial", 6))
        self.assertIn("图片3—4未能读取", observation.vision_summary)
        self.assertIn("图片13—15超出", observation.prompt)
        self.assertIn("成功画面", observation.prompt)
        self.assertTrue(observation.vision_unresolved)

    async def test_outer_timeout_preserves_completed_batches_and_cancels_remaining(self):
        active = 0
        async def call(provider, **kwargs):
            nonlocal active
            active += 1
            try:
                if kwargs["image_urls"][0].endswith("/0.png"):
                    return SimpleNamespace(completion_text="第一批已识别")
                await asyncio.sleep(5)
            finally:
                active -= 1
        self.reader._call_locked_upstream = AsyncMock(side_effect=call)
        observation = Observation(images=[f"https://example.invalid/{i}.png" for i in range(8)])
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.reader._enrich_images(
                self.event, observation, object(), {}), .05)
        self.assertIn("第一批已识别", observation.prompt)
        self.assertIn("未完成读取", observation.prompt)
        self.assertEqual(active, 0)
        self.assertTrue(observation.vision_unresolved)

    async def test_direct_primary_compacts_copy_and_keeps_current_images(self):
        contexts = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"https://example.invalid/old{i}.png"}}
        ]} for i in range(6)]
        original = copy.deepcopy(contexts)
        req = SimpleNamespace(contexts=contexts, prompt="当前", image_urls=[
            f"https://example.invalid/current{i}.png" for i in range(8)],
            extra_user_content_parts=[])
        dropped, _, _ = await fit_images(req)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(req.image_urls), 8)
        self.assertEqual(contexts, original)
        self.assertEqual(req.contexts[0]["content"][0]["type"], "text")
        self.assertEqual(req.contexts[-1]["content"][0]["type"], "image_url")

    async def test_main_image_limit_adds_omission_note(self):
        req = SimpleNamespace(contexts=[], image_urls=[
            f"https://example.invalid/{i}.png" for i in range(15)], extra_user_content_parts=[])
        dropped, _, _ = await fit_images(req)
        self.assertEqual((dropped, len(req.image_urls)), (3, 12))
        self.assertIn("省略3张", req.extra_user_content_parts[0].text)

    async def test_inline_jpeg_is_resized_and_remains_decodable(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow 未安装，运行时沿用原大小预算")
        stream = io.BytesIO()
        Image.new("RGB", (4096, 3072), "blue").save(stream, format="JPEG")
        original = "base64://" + base64.b64encode(stream.getvalue()).decode()
        req = SimpleNamespace(contexts=[], image_urls=[original], extra_user_content_parts=[])
        dropped, before, after = await fit_images(req)
        self.assertEqual(dropped, 0)
        self.assertLess(after, before)
        self.assertTrue(req.image_urls[0].startswith("data:image/jpeg;base64,"))
        with Image.open(io.BytesIO(base64.b64decode(req.image_urls[0].split(",", 1)[1]))) as image:
            self.assertLessEqual(max(image.size), 1024)
            self.assertEqual(image.format, "JPEG")

    async def test_unreadable_inline_images_still_obey_total_budget(self):
        # 可解码但非图像的编码不能绕过总量闸，须告知模型有图被省略。
        urls = ["base64://" + base64.b64encode(char * 400000).decode()
                for char in (b"x", b"y")]
        req = SimpleNamespace(contexts=[], image_urls=urls, extra_user_content_parts=[])
        dropped, _, after = await fit_images(req)
        self.assertEqual((dropped, len(req.image_urls)), (1, 1))
        self.assertLessEqual(after, 1024 * 1024)
        self.assertIn("省略1张", req.extra_user_content_parts[0].text)

    async def test_locked_native_upstream_never_removes_images_or_falls_back(self):
        client = SimpleNamespace(
            get_model=lambda: "qwen3-vl-plus",
            provider_config={"type": "openai_chat_completion", "modalities": ["image"]},
            _prepare_chat_payload=AsyncMock(return_value=({"messages": ["synthetic"]}, [])),
            _query=AsyncMock(side_effect=RuntimeError("synthetic provider rejection")),
            text_chat=AsyncMock(side_effect=AssertionError("native删图降级不得调用")),
        )
        router = SimpleNamespace(upstreams=[SimpleNamespace(client=client)],
                                 text_chat=AsyncMock())
        with self.assertRaises(RuntimeError):
            await Understanding._call_locked_upstream(router, model="qwen3-vl-plus",
                prompt="合成图片", system_prompt="规则", image_urls=["https://example.invalid/one"],
                max_tokens=512, request_max_retries=0)
        client._query.assert_awaited_once()
        self.assertEqual(client._query.await_args.kwargs["request_max_retries"], 0)
        self.assertEqual(client._query.await_args.args[0]["max_tokens"], 512)
        client.text_chat.assert_not_awaited()
        router.text_chat.assert_not_awaited()
        router.upstreams = []
        with self.assertRaises(ValueError):
            await Understanding._call_locked_upstream(router, model="qwen3-vl-plus")
        router.text_chat.assert_not_awaited()

    async def test_fragment_sources_share_six_calls(self):
        self.reader._call_locked_upstream = AsyncMock(
            return_value=SimpleNamespace(completion_text="合成观察"))
        shared = {"remaining": 6}
        events = [Event() for _ in range(4)]
        observations = [Observation(images=[
            f"https://example.invalid/{i}/{j}" for j in range(4)]) for i in range(4)]
        for event in events:
            event.set_extra("_livinghello_vision_call_budget", shared)
        await asyncio.gather(*(self.reader._enrich_images(event, observation, object(), {})
                               for event, observation in zip(events, observations)))
        self.assertEqual(self.reader._call_locked_upstream.await_count, 6)
        self.assertEqual(shared["remaining"], 0)
        self.assertTrue(any(o.vision_unresolved for o in observations))
