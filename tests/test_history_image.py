import base64
import json
import types
import unittest
from unittest.mock import AsyncMock

from guard_image_history import ImageHistory
from image_history import TOOL_NAME, reference_id


class Event:
    unified_msg_origin = "qq:FriendMessage:fixture"
    def __init__(self):
        self.extras = {}
    def get_extra(self, key, default=None):
        return self.extras.get(key, default)
    def set_extra(self, key, value):
        self.extras[key] = value
    def get_group_id(self):
        return ""
    def get_sender_id(self):
        return "12345"


class HistoryImageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = Event()
        self.policy = types.SimpleNamespace(revision="fixture", black_active=False)
        self.provider = types.SimpleNamespace(text_chat=AsyncMock(
            return_value=types.SimpleNamespace(completion_text="图中有猫")
        ))
        understanding = types.SimpleNamespace(resolve=AsyncMock(return_value=(self.provider, {})))
        self.plugin = types.SimpleNamespace(
            config={"understand_image_enabled": True},
            _policy=lambda: self.policy, _policy_send_reason=lambda _: "",
            _understanding=lambda: understanding,
        )
        self.service = ImageHistory(self.plugin)
        self.url = "data:image/png;base64," + base64.b64encode(b"offline").decode()
        self.key = reference_id(self.url)
        self.scope = {
            "event": self.event, "origin": self.event.unified_msg_origin, "cid": "fixture",
            "revision": "fixture", "references": {self.key: self.url}, "used": False,
        }
        self.event.set_extra("_livinghello_history_image_scope", self.scope)

    async def test_one_lookup_result_is_bounded_and_identified(self):
        from unittest.mock import patch
        with patch("guard_image_history.image_target",
                   return_value=types.SimpleNamespace(provider=self.provider, model="fixture")):
            result = json.loads(await self.service.read(self.event, self.scope, self.key))
        self.assertEqual(result["image_id"], self.key)
        self.assertEqual(result["kind"], TOOL_NAME)
        self.assertTrue(result["untrusted"])
        self.assertEqual(self.provider.text_chat.await_count, 1)
        kwargs = self.provider.text_chat.call_args.kwargs
        self.assertEqual(kwargs["request_max_retries"], 0)
        self.assertEqual(kwargs["max_tokens"], 1024)
        self.assertEqual(kwargs["image_urls"], [self.url])

    async def test_second_lookup_cannot_retry(self):
        self.scope["used"] = True
        result = await self.service.read(self.event, self.scope, self.key)
        self.assertIn("额度", result)
        self.provider.text_chat.assert_not_called()

    async def test_other_event_and_session_cannot_read(self):
        other = Event()
        other.set_extra("_livinghello_history_image_scope", self.scope)
        result = await self.service.read(other, self.scope, self.key)
        self.assertIn("失效", result)
        self.provider.text_chat.assert_not_called()

    async def test_policy_revision_change_cannot_read(self):
        self.policy.revision = "changed"
        self.assertIn("失效", await self.service.read(self.event, self.scope, self.key))
        self.provider.text_chat.assert_not_called()

    async def test_blacklist_or_recall_cannot_read(self):
        self.plugin._policy_send_reason = lambda _: "blacklist"
        self.assertIn("失效", await self.service.read(self.event, self.scope, self.key))
        self.provider.text_chat.assert_not_called()

    async def test_arbitrary_reference_or_url_cannot_read(self):
        for key in ("hist-img-other", "https://example.invalid/private", None):
            self.assertIn("不能读取", await self.service.read(self.event, self.scope, key))
        self.provider.text_chat.assert_not_called()
        self.assertFalse(self.scope["used"])

    async def test_failure_consumes_budget_and_hides_exception(self):
        from unittest.mock import patch
        self.provider.text_chat.side_effect = RuntimeError("SECRET /private/data")
        with patch("guard_image_history.image_target",
                   return_value=types.SimpleNamespace(provider=self.provider, model="fixture")):
            result = await self.service.read(self.event, self.scope, self.key)
        self.assertNotIn("SECRET", result)
        self.assertTrue(self.scope["used"])

    async def test_empty_or_invalid_data_does_not_call_provider(self):
        self.scope["references"][self.key] = "data:image/png;base64,?SECRET"
        result = await self.service.read(self.event, self.scope, self.key)
        self.assertNotIn("SECRET", result)
        self.provider.text_chat.assert_not_called()

    def test_verified_current_observation_persisted_without_network(self):
        self.event.set_extra("_at_guard_understanding",
                            types.SimpleNamespace(vision_summary="客观图片观察"))
        req = types.SimpleNamespace(extra_user_content_parts=[], conversation=None)
        self.service.attach(self.event, req)
        self.assertIn("客观图片观察", req.extra_user_content_parts[0].text)
        self.assertFalse(getattr(req.extra_user_content_parts[0], "_no_save", False))
        self.provider.text_chat.assert_not_called()

    def test_missing_observation_does_not_invent_caption(self):
        req = types.SimpleNamespace(extra_user_content_parts=[], conversation=None)
        self.service.attach(self.event, req)
        self.assertFalse(req.extra_user_content_parts)

    def test_native_tool_attaches_with_scope_and_hides_refs_in_repr(self):
        try:
            from astrbot.core.agent.tool import ToolSet
            from guard_history_image_tool import ReadHistoryImageTool
        except ImportError:
            self.skipTest("native FunctionTool unavailable in lightweight stubs")
        req = types.SimpleNamespace(
            extra_user_content_parts=[], conversation=types.SimpleNamespace(cid="fixture"),
            contexts=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": self.url}}
            ]} for _ in range(5)],
            func_tool=ToolSet(),
        )
        self.service.attach(self.event, req)
        tool = req.func_tool.get_tool(TOOL_NAME)
        self.assertIsInstance(tool, ReadHistoryImageTool)
        self.assertNotIn("base64", repr(tool))
        self.assertNotIn("scope=", repr(tool))

    def test_group_blacklist_with_unknown_old_author_prevents_tool(self):
        self.policy.black_active = True
        self.event.get_group_id = lambda: "123456"
        req = types.SimpleNamespace(
            extra_user_content_parts=[], conversation=types.SimpleNamespace(cid="fixture")
        )
        self.service.attach(self.event, req)
        self.assertFalse(hasattr(req, "func_tool"))
