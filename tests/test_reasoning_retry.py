"""只回思考时补一次正文；不接触真实模型和平台。"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from astrbot.core.agent.message import TextPart
from reply.guard_reasoning_retry import DEFAULT_PROVIDER, KEY, REQUEST_KEY, ReasoningRetry
from test_gate import gate_config, plugin
from test_reply_delivery import Event


class Trace:
    def __init__(self):
        self.rows = []
        self.last_seen = {}

    def record(self, event, feature, status, **values):
        self.rows.append({"feature": feature, "status": status, **values})


class Think:
    type = "think"

    def __init__(self, think="先想一下"):
        self.think = think


def assistant(parts):
    return SimpleNamespace(role="assistant", content=list(parts))


def request(**overrides):
    values = {
        "prompt": "现在回我",
        "contexts": [{"role": "user", "content": "现在回我"}],
        "system_prompt": "人设",
        "func_tool": SimpleNamespace(name="tools"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def response(text="", reasoning="先想一下"):
    return SimpleNamespace(completion_text=text, reasoning_content=reasoning)


class ReasoningRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = SimpleNamespace(text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text="补上的正文", reasoning_content=None)))
        self.guard = plugin(gate_config(reasoning_retry_enabled=True), provider=self.provider)
        self.context_owner = self.guard.context
        self.guard._delivery = lambda: SimpleNamespace(recalled=lambda _event: False)
        self.guard._policy_send_reason = lambda _event: ""
        self.retry = ReasoningRetry(self.guard)
        self.event = Event()
        self.event.set_extra(REQUEST_KEY, request())
        self.trace = Trace()
        self.event.set_extra("_livinghello_trace", self.trace)

    def context(self, *parts):
        return SimpleNamespace(messages=[assistant(parts)])

    def test_only_reasoning_without_text_needs_retry(self):
        self.assertTrue(ReasoningRetry.needs_retry(response()))
        self.assertFalse(ReasoningRetry.needs_retry(response(text="有正文")))
        self.assertFalse(ReasoningRetry.needs_retry(response(reasoning="")))
        self.assertFalse(ReasoningRetry.needs_retry(None))

    async def test_success_replaces_text_and_repairs_history(self):
        run_context = self.context(Think())
        result = await self.retry.recover(self.event, run_context, response())

        self.assertTrue(result)
        self.assertEqual(run_context.messages[-1].content[0].type, "think")
        self.assertIsInstance(run_context.messages[-1].content[1], TextPart)
        self.assertEqual(run_context.messages[-1].content[1].text, "补上的正文")
        self.assertEqual(self.event.get_extra(KEY), "recovered")
        self.assertEqual(self.trace.rows[-1]["feature"], "main_model_retry")
        self.assertEqual(self.trace.rows[-1]["status"], "success")
        self.assertEqual(self.trace.rows[-1]["model"], DEFAULT_PROVIDER)
        kwargs = self.provider.text_chat.await_args.kwargs
        self.assertEqual(kwargs["prompt"], "现在回我")
        self.assertEqual(kwargs["system_prompt"], "人设")
        self.assertEqual(kwargs["request_max_retries"], 1)
        self.assertEqual(kwargs["func_tool"].name, "tools")
        self.assertEqual(kwargs["contexts"], [{"role": "user", "content": "现在回我"}])

    async def test_second_call_does_not_retry_again(self):
        run_context = self.context(Think())
        await self.retry.recover(self.event, run_context, response())
        self.assertFalse(await self.retry.recover(self.event, run_context, response()))
        self.provider.text_chat.assert_awaited_once()

    async def test_existing_text_part_is_not_duplicated(self):
        message = assistant([Think(), TextPart(text="宿主已有正文")])
        await self.retry.recover(self.event, SimpleNamespace(messages=[message]), response())
        self.assertEqual(len(message.content), 2)

    async def test_empty_retry_keeps_original_response(self):
        self.provider.text_chat.return_value = SimpleNamespace(completion_text="   ")
        original = response()
        run_context = self.context(Think())
        self.assertFalse(await self.retry.recover(self.event, run_context, original))
        self.assertEqual(original.completion_text, "")
        self.assertEqual(len(run_context.messages[-1].content), 1)
        self.assertEqual(self.event.get_extra(KEY), "empty")

    async def test_blocked_event_skips_without_calling_model(self):
        cases = (
            ("recalled", lambda: setattr(self.guard, "_delivery",
                                         lambda: SimpleNamespace(recalled=lambda _event: True))),
            ("blocked", lambda: self.event.set_extra("_at_guard_delivery_blocked", "no_reply")),
            ("policy", lambda: setattr(self.guard, "_policy_send_reason",
                                       lambda _event: "policy_changed")),
        )
        for reason, prepare in cases:
            event = Event()
            event.set_extra(REQUEST_KEY, request())
            event.set_extra("_livinghello_trace", self.trace)
            self.event = event
            self.guard._delivery = lambda: SimpleNamespace(recalled=lambda _event: False)
            self.guard._policy_send_reason = lambda _event: ""
            prepare()
            self.assertFalse(await self.retry.recover(event, self.context(Think()), response()))
            self.assertEqual(event.get_extra(KEY), "skipped")
            self.assertEqual(self.trace.rows[-1]["reason"], reason)
        self.provider.text_chat.assert_not_awaited()

    async def test_missing_provider_runtime_errors_and_timeout(self):
        run_context = self.context(Think())
        self.guard.context = SimpleNamespace(get_provider_by_id=lambda _pid: None)
        self.assertFalse(await self.retry.recover(self.event, run_context, response()))
        self.assertEqual(self.event.get_extra(KEY), "provider_missing")
        self.guard.context = self.context_owner

        event = Event()
        event.set_extra(REQUEST_KEY, request())
        event.set_extra("_livinghello_trace", self.trace)
        self.provider.text_chat.side_effect = asyncio.TimeoutError()
        self.assertFalse(await self.retry.recover(event, run_context, response()))
        self.assertEqual(event.get_extra(KEY), "timeout")

        event = Event()
        event.set_extra(REQUEST_KEY, request())
        event.set_extra("_livinghello_trace", self.trace)
        self.provider.text_chat.side_effect = ValueError("bad payload")
        self.assertFalse(await self.retry.recover(event, run_context, response()))
        self.assertEqual(event.get_extra(KEY), "failed")
        self.assertEqual(self.trace.rows[-1]["error_type"], "ValueError")

    async def test_disabled_missing_request_or_response_is_untouched(self):
        run_context = self.context(Think())
        self.guard.config["reasoning_retry_enabled"] = False
        self.assertFalse(await self.retry.recover(self.event, run_context, response()))
        self.assertIsNone(self.event.get_extra(KEY))

        self.guard.config["reasoning_retry_enabled"] = True
        bare = Event()
        bare.set_extra("_livinghello_trace", self.trace)
        self.assertFalse(await self.retry.recover(bare, run_context, response()))
        self.assertIsNone(bare.get_extra(KEY))
        self.assertFalse(await self.retry.recover(self.event, run_context, None))
        self.provider.text_chat.assert_not_awaited()

    async def test_provider_id_and_timeout_config_are_bounded(self):
        self.guard.config["reasoning_retry_provider_id"] = "  "
        self.assertEqual(self.retry.provider_id(), DEFAULT_PROVIDER)
        self.guard.config["reasoning_retry_provider_id"] = "deepseek/deepseek-chat"
        self.assertEqual(self.retry.provider_id(), "deepseek/deepseek-chat")
        for value, expected in (("bad", 120.0), (1, 10.0), (9999, 300.0)):
            self.guard.config["reasoning_retry_timeout_seconds"] = value
            self.assertEqual(self.retry.timeout(), expected)

    async def test_payload_passes_only_present_extras(self):
        payload = ReasoningRetry._payload(request())
        self.assertNotIn("image_urls", payload)
        self.assertNotIn("audio_urls", payload)
        self.assertNotIn("extra_user_content_parts", payload)
        full = ReasoningRetry._payload(request(
            image_urls=["a.png"], audio_urls=["a.wav"], extra_user_content_parts=[TextPart(text="块")]))
        self.assertEqual(full["image_urls"], ["a.png"])
        self.assertEqual(full["audio_urls"], ["a.wav"])
        self.assertEqual(len(full["extra_user_content_parts"]), 1)

    async def test_agent_done_retries_before_health_judgement(self):
        calls = []

        async def recover(_event, _run_context, reply):
            calls.append("retry")
            reply.completion_text = "补上的正文"
            return True

        self.guard._reasoning_retry = lambda: SimpleNamespace(recover=recover)
        self.guard._trace = lambda event=None: SimpleNamespace(
            rows={}, end=lambda *_a, **_k: None,
            identity=lambda _event: {"request_id": "r"})
        self.guard._reply_health = lambda: SimpleNamespace(
            model_done=lambda _event, reply: calls.append(("model_done", reply.completion_text)))
        await self.guard.trace_agent_done(self.event, self.context(Think()), response())
        self.assertEqual(calls, ["retry", ("model_done", "补上的正文")])


if __name__ == "__main__":
    unittest.main()
