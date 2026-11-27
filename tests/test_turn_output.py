"""Replay the real AstrBot runner, buffer, and QQ adapter with fake API responses."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_gate import gate_config, plugin

try:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.astr_agent_run_util import run_agent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    from mcp.types import CallToolResult, TextContent
    from images.guard_image_tool import GenerateImageTool
except ImportError:
    AiocqhttpMessageEvent = None

from images.guard_image_failure import http_failure
from images.guard_image_settings import ImageSettings
from main import Main
from reply.guard_reply_delivery import OWNER_KEY
from reply.guard_reply_turns import ReplyTurns
from reply.guard_turn_output import BOUNDARY, IMAGE_INTENT, IMAGE_JOB, IMAGE_TOOL, LOCAL_REPLY, strip_tool_text
from reply.guard_native_reply import NativeReplyError, managed_regex

DRAFT = "这是工具步骤草稿，不应进气泡或历史"
FINAL = "这是模型对本轮的完整答复"
INTENT = "按这张蹲着的姿势拍一张"
ACK = "好呀，等我一下。"
ARGS = {"prompt": "离线测试画面", "use_reference": False, "reply": ACK}
IMMEDIATE = {"prompt": ARGS["prompt"], "use_reference": False, "immediate": True}


@unittest.skipUnless(AiocqhttpMessageEvent, "需要完整 AstrBot 宿主与 QQ 适配器")
class TurnOutputReplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = plugin(gate_config(reply_quote_enabled=False, reply_fallback_enabled=True,
                                       expression_punct_cleanup=False))
        self.guard.context.get_config = lambda *_a, **_k: {"provider_tts_settings": {"enable": False}}
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.guard.data_dir = Path(folder.name)
        self.guard._livinghello_detached_enabled = True
        self.image = ImageSettings(self.guard)
        self.guard.image_generation = self.image
        self.image.values.update(enabled=True, daily_limit=8)
        self.image.refresh = lambda: None
        self.image.connection = lambda _: ("https://offline.invalid/v1", "fixture", "offline-image")
        self.image.references = lambda _: []
        self.output = Path(folder.name) / "generated.png"
        self.output.write_bytes(b"\x89PNG\r\n\x1a\n" + b"offline-image")
        self.addAsyncCleanup(self.image.close)
        self.addAsyncCleanup(self.guard._reply_health().close)
        self.addAsyncCleanup(self.guard._trace().close)
        # Keep native QQ serialization and dispatch, stub only its base telemetry.
        stub = patch.object(AstrMessageEvent, "send", new=AsyncMock())
        stub.start()
        self.addCleanup(stub.stop)

    def draft(self, name=IMAGE_TOOL, args=None, *, extra=False):
        return LLMResponse(
            role="assistant", result_chain=MessageChain().message(DRAFT),
            tools_call_name=[name] + (["lookup"] if extra else []),
            tools_call_args=[ARGS.copy() if args is None else args] + ([{}] if extra else []),
            tools_call_ids=["call-image"] + (["call-lookup"] if extra else []),
            reasoning_content="private reasoning", reasoning_signature="opaque-signature",
        )

    async def replay(self, *, text=INTENT, mid="1", draft=None, result=123, owns=False,
                     enabled=True, permission=None, before_send=None, during_tool=None,
                     platform="aiocqhttp", speed="fast", skip_hook=False, on_text=None,
                     history=None, model=None, primary_error=False, requested_model=None,
                     before_tool=None, attach_image=False, image_result=456):
        first = draft or self.draft()
        sent, contexts, steps, schemas, executed, upstream_prompts = [], [], [], [], [], []
        requests = []
        generating, generated, finish_generation = (asyncio.Event() for _ in range(3))
        generate = self.image.generate

        async def upstream(_values, _connection, prompt, _reference):
            upstream_prompts.append(prompt)
            steps.append("image_request")
            generating.set()
            if speed == "slow":
                await finish_generation.wait()
            if speed == "failure":
                raise http_failure(400, "{}")
            return self.output, "离线测试画面"

        async def generate_and_observe(*args, **kwargs):
            value = await generate(*args, **kwargs)
            if kwargs.get("_execute"):
                steps.append("image_result")
                generated.set()
            return value

        async def send_private_msg(**payload):
            sent.append(payload)
            if any(segment["type"] == "image" for segment in payload["message"]):
                steps.append("onebot_image")
                return {} if image_result is None else {"message_id": image_result}
            steps.append("onebot_text")
            local = event.get_extra(LOCAL_REPLY)
            is_ack = local and local["delivery"] is not None and "send_message" in vars(event)
            if is_ack:
                await asyncio.wait_for(generating.wait(), 2)
                if speed != "slow":
                    await asyncio.wait_for(generated.wait(), 2)
                    self.assertNotIn("onebot_image", steps)
                    task = event.get_extra("_livinghello_image_task")
                    self.assertIn(task, self.image._tasks)
                    self.assertFalse(task.done())
                if on_text is not None:
                    await on_text(event)
            if isinstance(result, Exception):
                raise result
            steps.append("onebot_text_return")
            return {} if result is None else {"message_id": result}

        raw = AstrBotMessage()
        raw.type, raw.sender = MessageType.FRIEND_MESSAGE, MessageMember("12345")
        raw.self_id, raw.session_id, raw.message_id = "123", "12345", mid
        raw.message_str, raw.raw_message, raw.message = text, {}, [Plain(text)]
        event = AiocqhttpMessageEvent(text, raw, PlatformMetadata(platform, "", platform),
                                    "12345", SimpleNamespace(send_private_msg=send_private_msg))
        # These exercise both normal paths in ReplyDelivery: native and expression-owned.
        if owns:
            event.set_extra(OWNER_KEY, "at_guard")
        self.guard._delivery().patch(event)
        event.set_extra("_livinghello_reply_approved", True)
        event.set_extra("_livinghello_image_persona", "offline-persona")

        class Executor:
            async def execute(_, *, tool, run_context, **args):
                executed.append((tool, args))
                if tool.name == IMAGE_TOOL:
                    if before_tool is not None:
                        before_tool(event)
                    tool_result = await tool.call(run_context, **args)
                    if during_tool is not None:
                        during_tool(event)
                else:
                    tool_result = "lookup result"
                yield CallToolResult(content=[TextContent(type="text", text=tool_result)])

        def provider(name, fail=False):
            async def chat(**kwargs):
                contexts.append(kwargs["contexts"])
                schemas.append(kwargs["func_tool"])
                requests.append((name, kwargs))
                if fail:
                    return LLMResponse(role="err", completion_text="401 Unauthorized")
                if model:
                    response = model(**kwargs)
                    return await response if asyncio.iscoroutine(response) else response
                if len(contexts) == 1 + int(primary_error):
                    return first
                return LLMResponse(role="assistant", completion_text=FINAL)
            return SimpleNamespace(provider_config={"id": name}, text_chat=chat,
                                   meta=lambda: SimpleNamespace(id=name),
                                   get_model=lambda: "offline-model")

        image_tool = GenerateImageTool(service=self.image)
        tools = ToolSet([image_tool, FunctionTool(name="lookup", description="offline fixture",
                                                  parameters={"type": "object", "properties": {}})])
        req = ProviderRequest(
            prompt=text, session_id=event.unified_msg_origin, contexts=history,
            func_tool=tools, model=requested_model)
        if attach_image:
            self.image.attach(event, req)
            tools = req.func_tool
        event.set_extra("_livinghello_provider_request", req if enabled else object())
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=provider("offline-primary", primary_error), request=req,
            fallback_providers=[provider("offline-fallback")] if primary_error else [],
            run_context=ContextWrapper(context=SimpleNamespace(event=event)),
            tool_executor=Executor(), streaming=False,
            agent_hooks=SimpleNamespace(
                on_agent_begin=lambda scope: self.guard.trace_agent_begin(event, scope),
                on_agent_done=lambda scope, reply: self.guard.trace_agent_done(event, scope, reply),
                on_tool_start=AsyncMock(), on_tool_end=AsyncMock(),
            ),
        )
        class PipelineScheduler:
            # Keep the real pipeline owner check, including the missing-hook fallback.
            async def execute(_):
                async for chain in run_agent(runner, buffer_intermediate_messages=True, show_tool_use=False):
                    result_chain = event.get_result()
                    self.assertEqual(chain.get_plain_text(), result_chain.get_plain_text())
                    await self.guard.recheck_before_send(event)
                    if before_send is not None:
                        await before_send(event, result_chain)
                    try:
                        await event.send(result_chain.derive(list(result_chain.chain)))
                    except RuntimeError:
                        # Native RespondStage still dispatches after_message_sent on a Send error.
                        steps.append("onebot_error")
                    if not skip_hook:
                        await self.guard.deliver_pending_image(event)
                        await self.guard.deliver_pending_image(event)
                        finish_generation.set()

        with patch("images.guard_image_settings.image_permission", new=AsyncMock(return_value=permission)), \
                patch.object(self.image, "prepare_image_job", wraps=self.image.prepare_image_job) as preflight, \
                patch("integrations.guard_livingemotion.current_emotion", new=AsyncMock(return_value=None)), \
                patch.object(self.image, "_request", side_effect=upstream) as request, \
                patch.object(self.image, "generate", side_effect=generate_and_observe):
            await asyncio.create_task(PipelineScheduler().execute())
            task = event.get_extra("_livinghello_image_task")
            if task is not None:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
                self.assertNotIn(task, self.image._tasks)
        self.assertNotIn(id(runner), self.guard._turn_output().bindings)
        self.guard._reply_fallback().release(runner)
        self.assertNotIn(BOUNDARY, vars(runner))
        self.assertNotIn("send_message", vars(event))
        self.assertIsNone(event.get_extra("_livinghello_turn_output_runner"))
        self.assertIs(req.func_tool, tools)
        self.assertNotIn("reply", image_tool.parameters["properties"])
        self.assertNotIn("immediate", image_tool.parameters["properties"])
        job = event.get_extra(IMAGE_JOB) or {}
        with self.image._connect() as db:
            request_id = self.image.request_id(event, job.get("prompt", ARGS["prompt"]),
                                               job.get("use_reference", False))
            receipt = db.execute("SELECT status FROM image_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
        return SimpleNamespace(
            event=event, sent=["".join(segment["data"]["text"] for segment in packet["message"]
                                     if segment["type"] == "text") for packet in sent
                               if any(segment["type"] == "text" for segment in packet["message"])],
            steps=steps, contexts=contexts, job=job, req=req, messages=runner.run_context.messages,
            local=event.get_extra(LOCAL_REPLY), started=request.await_count,
            tools=tools, schemas=schemas, executed=executed, upstream_prompts=upstream_prompts,
            requests=requests, preflights=preflight.await_count,
            image_receipt=receipt[0] if receipt else None)

    @staticmethod
    def fields(**values):
        return LLMResponse(role="assistant", completion_text=json.dumps(values, ensure_ascii=False))

    def script(self, *responses):
        remaining = iter(responses)

        def model(**_):
            return next(remaining)
        return model

    async def test_remove_draft_keep_tool_history_and_use_one_model_call(self):
        for mid, text, result, ack, receipt in (
                ("1", "按这张的姿势拍一张", -901, ACK, "sent"),
                ("2", INTENT, None, ACK, "cancelled"),
                ("3", INTENT, "903", ACK, "sent")):
            with self.subTest(mid=mid):
                trace = await self.replay(text=text, mid=mid, result=result, owns=mid != "1")
                self.assertEqual(trace.sent, [ack])
                self.assertEqual(len(trace.contexts), 1)
                self.assertEqual(trace.started, 1)
                self.assertEqual(trace.job["status"], receipt)
                self.assertEqual(trace.image_receipt, receipt)
                self.assertEqual(trace.local["receipt"], str(result) if result else None)
                saved = trace.req.tool_calls_result[0].to_openai_messages()
                self.assertEqual(saved[0]["tool_calls"][0]["id"], "call-image")
                self.assertEqual(saved[1]["tool_call_id"], "call-image")
                self.assertNotIn(DRAFT, str(saved))
                self.assertNotIn(DRAFT, str(trace.messages))
                self.assertEqual(trace.messages[-1].content[0].text, ack)
                self.assertEqual(trace.executed, [(trace.tools.tools[0], {
                    "prompt": ARGS["prompt"], "use_reference": False})])
                self.assertNotIn(ack, str(saved))
                spec = trace.schemas[0].get_tool(IMAGE_TOOL).parameters
                self.assertIn("reply", spec["properties"])
                self.assertNotIn("reply", spec["required"])

    async def test_contextual_photo_is_carried_into_prompt_and_reply_without_second_call(self):
        for index, (scene, acknowledgement) in enumerate((
                ("窗边穿蓝色毛衣，侧身坐，镜头从左边平拍", "好呀宝贝，等我一下。"),
                ("厨房里穿黄色围裙，靠着桌沿，镜头从正面拍", "好嘛，这就给你看。"))):
            with self.subTest(scene=scene):
                previous = f"刚说好这张是{scene}，不要换。"
                history = [{"role": "user", "content": previous},
                           {"role": "assistant", "content": "记住啦，要看就跟我说"}]

                def model(**payload):
                    self.assertIn(previous, str(payload["contexts"]))
                    self.assertIn("拍给我看看宝贝", str(payload["contexts"]))
                    description = payload["func_tool"].get_tool(IMAGE_TOOL).parameters["properties"]
                    self.assertIn("上文", description["prompt"]["description"])
                    self.assertIn("上文", description["reply"]["description"])
                    return self.draft(args={"prompt": f"自然生活照，{scene}。", "reply": acknowledgement})

                result = await self.replay(mid=f"context-{index}", text="拍给我看看宝贝",
                                           history=history, model=model)
                self.assertEqual(len(result.contexts), 1)
                self.assertEqual(result.sent, [acknowledgement])
                self.assertEqual(result.local["receipt"], "123")
                self.assertIn(scene, result.upstream_prompts[0])
                self.assertNotIn(acknowledgement, result.upstream_prompts[0])

    async def test_first_successful_fallback_model_can_supply_a_draft(self):
        trace = await self.replay(primary_error=True)
        self.assertEqual(len(trace.contexts), 2)
        self.assertEqual(trace.sent, [ACK])
        self.assertEqual(trace.image_receipt, "sent")

    async def test_missing_reply_is_repaired_without_resending_the_tool_or_waiting_for_image(self):
        history = [
            {"role": "user", "content": "不要带上的老话题"},
            {"role": "assistant", "content": "早先的回答"},
            {"role": "user", "content": "那就按刚才说的坐姿"},
            {"role": "assistant", "content": "可以呀，宝贝"},
        ]
        trace = await self.replay(
            speed="slow", history=history, requested_model="explicit-model",
            model=self.script(self.draft(args=IMMEDIATE), self.fields(reply=ACK)),
        )
        self.assertEqual([name for name, _ in trace.requests], ["offline-primary"] * 2)
        first, repair = (payload for _, payload in trace.requests)
        self.assertIn("immediate", first["func_tool"].get_tool(IMAGE_TOOL).parameters["properties"])
        self.assertIsNone(repair["func_tool"])
        self.assertIsNone(repair["extra_user_content_parts"])
        self.assertEqual(repair["model"], "explicit-model")
        self.assertEqual(repair["request_max_retries"], 1)
        repair_history = str(repair["contexts"])
        self.assertIn(INTENT, repair_history)
        self.assertIn("那就按刚才说的坐姿", repair_history)
        self.assertNotIn("不要带上的老话题", repair_history)
        for private in (ARGS["prompt"], DRAFT, trace.job["reply"], "call-image"):
            self.assertNotIn(private, repair_history)
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(len(trace.executed), 1)
        self.assertEqual(trace.executed[0][1], {"prompt": ARGS["prompt"], "use_reference": False})
        self.assertEqual(trace.sent, [ACK])
        self.assertEqual(trace.started, 1)
        self.assertLess(trace.steps.index("onebot_text_return"), trace.steps.index("image_result"))
        self.assertLess(trace.steps.index("image_result"), trace.steps.index("onebot_image"))

    async def test_invalid_reply_gets_a_third_try_in_the_same_scope_and_with_the_same_provider(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                trace = await self.replay(
                    mid=f"third-{fallback}", primary_error=fallback, requested_model="primary-only",
                    model=self.script(self.draft(args={**IMMEDIATE, "reply": "已经拍好啦"}),
                                      self.fields(reply="已发给你了"), self.fields(reply=ACK)),
                )
                turns = trace.requests[int(fallback):]
                self.assertEqual(len(turns), 3)
                self.assertEqual({name for name, _ in turns},
                                 {"offline-fallback" if fallback else "offline-primary"})
                _, second, third = (payload for _, payload in turns)
                self.assertEqual(second["contexts"], third["contexts"])
                self.assertIsNone(third["func_tool"])
                self.assertEqual(third["model"], None if fallback else "primary-only")
                self.assertEqual(trace.preflights, 1)
                self.assertEqual(trace.sent, [ACK])
                self.assertEqual(trace.started, 1)

    async def test_prompt_only_repair_keeps_reply_and_original_tool_call_id(self):
        scene = "雨天的公交站，侧面视角，灰色外套"
        trace = await self.replay(
            history=[{"role": "user", "content": scene}],
            model=self.script(self.draft(args={**IMMEDIATE, "prompt": "", "reply": ACK}),
                              self.fields(prompt=scene, use_reference=False)),
        )
        self.assertEqual(len(trace.requests), 2)
        self.assertIn(scene, str(trace.contexts[1]))
        self.assertIn("只重写", str(trace.contexts[1]))
        self.assertIsNone(trace.schemas[1])
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(trace.executed[0][1], {"prompt": scene, "use_reference": False})
        saved = trace.req.tool_calls_result[0].to_openai_messages()
        self.assertEqual(saved[0]["tool_calls"][0]["id"], "call-image")
        self.assertNotIn("immediate", str(saved))
        self.assertNotIn(ACK, str(saved))
        self.assertEqual(trace.sent, [ACK])
        self.assertEqual(trace.upstream_prompts, [scene])

    async def test_both_bad_share_two_repair_calls_and_do_not_repeat_preflight(self):
        trace = await self.replay(
            model=self.script(self.draft(args={"immediate": True, "use_reference": "false"}),
                              self.fields(prompt=ARGS["prompt"], use_reference=False),
                              self.fields(reply=ACK)),
        )
        self.assertEqual(len(trace.requests), 3)
        self.assertIn("只重写", str(trace.contexts[1]))
        self.assertIn("已通过预检", str(trace.contexts[2]))
        self.assertEqual(trace.schemas[1:], [None, None])
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(trace.sent, [ACK])
        self.assertEqual(trace.started, 1)

    async def test_repair_exhaustion_is_silent_and_never_requests_image(self):
        for mode in ("reply", "prompt", "shared"):
            with self.subTest(mode=mode):
                initial = (IMMEDIATE if mode == "reply" else
                           {**IMMEDIATE, "prompt": " ", "reply": ACK if mode == "prompt" else None})
                second = self.fields(prompt=ARGS["prompt"], use_reference=False) if mode == "shared" else self.fields()
                third = self.fields(reply="图片已经发出") if mode == "reply" else self.fields()
                trace = await self.replay(mid=f"exhaust-{mode}", model=self.script(
                    self.draft(args=initial), second, third))
                self.assertEqual(len(trace.requests), 3)
                self.assertEqual(trace.sent, [])
                self.assertEqual(trace.started, 0)
                self.assertIsNone(trace.local)
                self.assertFalse(trace.event.get_extra("agent_user_aborted"))
                self.assertEqual(trace.preflights, int(mode != "prompt"))
                self.assertEqual(trace.job.get("status"), None if mode == "prompt" else "cancelled")

    async def test_preflight_failure_only_repairs_reply_with_actual_reason(self):
        def use_last_slot(event):
            self.image.values["daily_limit"] = 1
            with self.image._connect() as db:
                db.execute("INSERT INTO image_quota_slots VALUES (?, NULL, ?)",
                           ("confirmed-by-another-event", self.image.today()))

        cases = (
            ("reference", {"use_reference": True}, None, "把参考图发我一下吧？", "缺本轮必要的参考图", "failed"),
            ("permission", {}, {"allowed": False}, "这次先不拍啦", "自然婉拒", "refused"),
            ("quota", {}, None, "今天累啦，不拍了。", "已达上限", "quota"),
            ("connection", {}, None, "这会儿暂时出不了图", "暂时无法开始", "failed"),
            ("disabled", {}, None, "我这会儿拍不了呀。", "暂时无法开始", "failed"),
        )
        for name, overrides, permission, message, fact, status in cases:
            with self.subTest(name=name):
                if name == "connection":
                    self.image.connection = lambda _: (_ for _ in ()).throw(NativeReplyError("offline", 503))
                elif name == "disabled":
                    self.image.values["enabled"] = False
                trace = await self.replay(mid=name, permission=permission,
                    before_tool=use_last_slot if name == "quota" else None, model=self.script(
                    self.draft(args={**IMMEDIATE, "reply": ACK, **overrides}),
                    self.fields(reply=message)))
                if name == "quota":
                    self.image.values["daily_limit"] = 8
                self.assertEqual([n for n, _ in trace.requests], ["offline-primary"] * 2)
                self.assertIsNone(trace.schemas[1])
                self.assertIn(fact, str(trace.contexts[1]))
                self.assertNotIn("call-image", str(trace.contexts[1]))
                self.assertNotIn(ARGS["prompt"], str(trace.contexts[1]))
                self.assertEqual(trace.sent, [message])
                self.assertEqual(trace.job["status"], status)
                self.assertEqual(len(trace.executed), 1)
                self.assertNotIn("onebot_image", trace.steps)
                self.assertEqual(trace.started, 0)

    async def test_full_or_held_quota_is_known_before_first_model_call(self):
        self.image.values["daily_limit"] = 1
        for mode, phrase, response in (
            ("full", "已达上限", "今天没精神拍啦。"),
            ("held", "暂占", "现在腾不开手拍呀。"),
        ):
            with self.subTest(mode=mode):
                with self.image._connect() as db:
                    db.execute("DELETE FROM image_quota_slots")
                    lease = self.image._generation_lease() + time.time() if mode == "held" else None
                    db.execute("INSERT INTO image_quota_slots VALUES (?, ?, ?)",
                               (mode, lease, self.image.today() if mode == "full" else None))
                trace = await self.replay(mid=mode, attach_image=True, model=self.script(
                    LLMResponse(role="assistant", completion_text=response)))
                self.assertEqual(len(trace.requests), 1)
                self.assertIsNone(trace.schemas[0].get_tool(IMAGE_TOOL))
                self.assertIn(phrase, str(trace.contexts[0]))
                self.assertIn("有其他问题仍正常回答", str(trace.contexts[0]))
                self.assertEqual(trace.sent, [response])
                self.assertEqual(trace.preflights, 0)
                self.assertEqual(trace.started, 0)

    async def test_quota_change_during_reply_repair_uses_remaining_call_for_refusal(self):
        async def model(**payload):
            if payload["func_tool"]:
                return self.draft(args=IMMEDIATE)
            if "已通过预检" in str(payload["contexts"][0]):
                self.image.values["daily_limit"] = 0
                return self.fields(reply=ACK)
            self.assertIn("已达上限", str(payload["contexts"][0]))
            return self.fields(reply="今天先不拍啦。")

        trace = await self.replay(model=model)
        self.assertEqual(len(trace.requests), 3)
        self.assertEqual(trace.sent, ["今天先不拍啦。"])
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(trace.started, 0)
        self.image.values["daily_limit"] = 1
        self.assertIsNone(self.image.quota_reason())

    async def test_reference_is_read_once_before_positive_reply_and_reused(self):
        self.image.references = lambda _: [object()]
        draft = self.draft(args={**IMMEDIATE, "reply": ACK, "use_reference": True})
        with patch.object(self.image, "_reference_data", new=AsyncMock(
                side_effect=[OSError("unreadable"), (b"offline-image", "image/png")])) as load:
            rejected = await self.replay(mid="reference-failed", model=self.script(
                draft, self.fields(reply="能再发一下参考图吗？")))
            self.assertEqual(rejected.sent, ["能再发一下参考图吗？"])
            self.assertEqual(rejected.started, 0)
            accepted = await self.replay(mid="reference-ok", draft=draft)
        self.assertEqual(load.await_count, 2)
        self.assertEqual(accepted.sent, [ACK])
        self.assertEqual(accepted.preflights, 1)
        self.assertEqual(accepted.started, 1)
        self.assertEqual(self.image.used(), 1)

    async def test_prompt_mapping_failure_is_known_before_ack(self):
        trace = await self.replay(model=self.script(
            self.draft(args={**IMMEDIATE, "prompt": "猫" * 501, "reply": ACK}),
            self.fields(reply="能说得简短一点吗？")))
        self.assertEqual(trace.sent, ["能说得简短一点吗？"])
        self.assertIn("描述过长", str(trace.contexts[1]))
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(trace.started, 0)
        self.assertEqual(self.image.used(), 0)

    async def test_turn_swap_during_reply_repair_does_not_repair_again_or_start_image(self):
        owner = {}

        async def model(**payload):
            if payload["func_tool"]:
                return self.draft(args=IMMEDIATE)
            owner["event"].set_extra("_at_guard_superseded", True)
            return self.fields(reply="不合格吗？")

        trace = await self.replay(during_tool=lambda event: owner.update(event=event), model=model)
        self.assertEqual(len(trace.requests), 2)
        self.assertEqual(trace.job["status"], "cancelled")
        self.assertEqual(trace.sent, [])
        self.assertEqual(trace.started, 0)

    async def test_single_bubble_survives_managed_segmentation_and_punctuation(self):
        self.guard.config.update(expression_takeover_enabled=True, expression_punct_cleanup=True)
        for segments in ({}, {"enable": True, "split_mode": "regex", "only_llm_result": True,
                              "regex": managed_regex(words_enabled=True), "split_words": ["，"]}):
            self.guard.context.get_config = lambda *_a, **_k: {
                "provider_tts_settings": {"enable": False},
                "platform_settings": {"segmented_reply": segments},
            }
            trace = await self.replay(owns=True, mid="managed" if segments else "send_cleanup")
            self.assertEqual(len(trace.contexts), 1)
            self.assertEqual(trace.sent, ["好呀 等我一下"])
            self.assertEqual(trace.local["receipt"], "123")
            self.assertEqual(trace.image_receipt, "sent")

    async def test_mixed_multitool_and_nonmatching_runner_keep_final_model(self):
        baseline = await self.replay(text=INTENT + "，再告诉我为什么", enabled=False)
        self.assertEqual(baseline.sent, [DRAFT + FINAL])
        self.assertIn(DRAFT, str(baseline.contexts[1]))
        for number, (text, draft) in enumerate((
                (INTENT + "，再告诉我为什么", self.draft(args={k: v for k, v in ARGS.items() if k != "reply"})),
                (INTENT + "，再告诉我为什么", self.draft(args={**ARGS, "immediate": False})),
                (INTENT, self.draft(extra=True)),
                (INTENT, self.draft(args=IMMEDIATE, extra=True)),
                (INTENT, self.draft(args={**ARGS, "reply": {"text": ACK}})),
                (INTENT, self.draft(args={**ARGS, "reply": "好啦\n（画面草稿）"})),
                (INTENT, self.draft(args={**ARGS, "promise_id": "old"})),
        ), start=2):
            with self.subTest(text=text, args=draft.tools_call_args):
                trace = await self.replay(mid=str(number), text=text, draft=draft)
                self.assertEqual(trace.sent[0], FINAL)
                self.assertEqual(len(trace.contexts), 2)
                self.assertIsNone(trace.local)
                if "promise_id" in draft.tools_call_args[0]:
                    self.assertEqual(trace.executed[0][1]["promise_id"], "old")
                    self.assertEqual(trace.started, 0)  # No real promise is claimed in this replay.
                else:
                    self.assertEqual(trace.sent, [FINAL])
                    self.assertLess(trace.steps.index("onebot_text_return"),
                                    trace.steps.index("image_request"))
                self.assertNotIn(DRAFT, str(trace.contexts[1]))
                self.assertNotIn(DRAFT, str(trace.req.tool_calls_result))
                self.assertNotIn('"reply":', str(trace.req.tool_calls_result))
                self.assertNotIn("reply", trace.executed[0][1])
                self.assertNotIn("immediate", trace.executed[0][1])
                self.assertFalse(trace.event.get_extra(IMAGE_INTENT))
                self.assertIs(trace.schemas[1], trace.tools)
        self.guard.context.get_config = lambda *_a, **_k: {"provider_tts_settings": {"enable": True}}
        voice = await self.replay(mid="voice")
        self.assertIsNone(voice.local)
        self.assertEqual(len(voice.contexts), 2)
        self.assertIs(voice.schemas[0], voice.tools)

    async def test_changed_tool_result_keeps_normal_model_and_image_path(self):
        def replace_job_receipt(event):
            event.get_extra(IMAGE_JOB)["reply"] = "和工具实际回执不同"

        trace = await self.replay(mid="wrong-receipt", during_tool=replace_job_receipt)
        self.assertIsNone(trace.local)
        self.assertEqual(len(trace.contexts), 2)
        self.assertEqual(trace.sent, [FINAL])
        self.assertLess(trace.steps.index("onebot_text_return"), trace.steps.index("image_request"))

        marked = await self.replay(mid="wrong-receipt-explicit", during_tool=replace_job_receipt,
                                   model=self.script(self.draft(args=IMMEDIATE)))
        self.assertEqual(len(marked.requests), 1)
        self.assertEqual(marked.preflights, 1)
        self.assertEqual(marked.sent, [])
        self.assertEqual(marked.job["status"], "cancelled")
        self.assertEqual(marked.started, 0)

    async def test_unsafe_failure_reply_does_not_send_or_request_an_image(self):
        trace = await self.replay(model=self.script(
            self.draft(args={**IMMEDIATE, "reply": ACK, "use_reference": True}),
            self.fields(reply="我马上画给你。"), self.fields(reply="等我一下哦。")))
        self.assertEqual(len(trace.requests), 3)
        self.assertEqual(trace.sent, [])
        self.assertEqual(trace.preflights, 1)
        self.assertEqual(trace.started, 0)

    async def test_private_only_charges_a_confirmed_image_not_its_text(self):
        for receipt in (None, 0, "invalid", 456):
            with self.subTest(receipt=receipt):
                trace = await self.replay(mid=f"receipt-{receipt}", image_result=receipt)
                self.assertEqual(trace.steps.count("onebot_image"), 1)
                self.assertIn("onebot_text_return", trace.steps)
                self.assertEqual(trace.image_receipt, "sent" if receipt == 456 else "unknown")
                self.assertEqual(self.image.used(), int(receipt == 456))

    async def test_qq_group_and_scheduled_delivery_share_image_receipt_quota(self):
        self.image.values["daily_limit"] = 2
        group_send = AsyncMock(side_effect=[{"message_id": 555}, {}, {"message_id": 666}])
        private_send = AsyncMock()
        bot = SimpleNamespace(send_private_msg=private_send, send_group_msg=group_send)
        raw = AstrBotMessage()
        raw.type, raw.sender = MessageType.GROUP_MESSAGE, MessageMember("12345")
        raw.self_id, raw.session_id, raw.message_id = "123", "12345_789", "group-test"
        raw.group_id, raw.message_str, raw.raw_message, raw.message = "789", INTENT, {}, [Plain(INTENT)]
        metadata = PlatformMetadata("aiocqhttp", "", "qq-test")
        group = AiocqhttpMessageEvent(INTENT, raw, metadata, raw.session_id, bot)
        group.set_extra("_livinghello_image_persona", "offline-persona")
        rid = self.image.reserve(group, "群图", False)
        self.assertTrue(await self.image._deliver_image(group, {"path": str(self.output), "request_id": rid}))
        self.assertEqual(self.image.used(), 1)
        self.guard.context.platform_manager = SimpleNamespace(platform_insts=[
            SimpleNamespace(meta=lambda: metadata, bot=bot)])
        self.guard.context.send_message = AsyncMock(return_value=True)
        for mode, expected in (("unknown", 1), ("sent", 2)):
            rid = self.image.reserve(group, mode, False)
            artifact = {"request_id": rid, "session": group.unified_msg_origin,
                        "path": str(self.output), "prompt": mode}
            self.image.register_deferred_image(artifact)
            result = await Main.deliver_deferred_image(self.guard, group, artifact)
            self.assertEqual(result["status"], mode)
            await Main.deliver_deferred_image(self.guard, group, artifact)
            self.assertEqual(self.image.used(), expected)
        self.assertEqual(group_send.await_count, 3)
        for invocation in group_send.await_args_list:
            self.assertEqual(invocation.kwargs["group_id"], 789)
            self.assertEqual([p["type"] for p in invocation.kwargs["message"]], ["image"])
        self.guard.context.send_message.assert_not_awaited()
        private_send.assert_not_awaited()

    async def test_fast_and_slow_generation_both_send_text_then_image(self):
        for speed in ("fast", "slow"):
            with self.subTest(speed=speed):
                trace = await self.replay(mid=speed, speed=speed)
                positions = [trace.steps.index(s) for s in
                             ("image_request", "image_result", "onebot_text_return", "onebot_image")]
                request, generated, text, image = positions
                self.assertLess(request, text)
                self.assertLess(text, image)
                self.assertLess(generated, image)
                self.assertEqual(generated < text, speed == "fast")
                self.assertEqual(trace.steps.count("onebot_image"), 1)
                self.assertEqual(trace.image_receipt, "sent")

    async def test_unknown_or_unrelated_send_cannot_release_image(self):
        async def replace_text(event, result):
            await event.send(MessageChain().message(ACK))
            result.chain = [Plain("other final result")]

        for i, result in enumerate((0, False, RuntimeError("local rejection"))):
            trace = await self.replay(mid=str(i), result=result)
            self.assertEqual(trace.sent, [ACK])
            self.assertEqual(trace.job["status"], "cancelled")
            self.assertEqual(trace.started, 1)
            self.assertEqual(trace.image_receipt, "cancelled")
            self.assertNotIn("onebot_image", trace.steps)
        unrelated = await self.replay(mid="unrelated", before_send=replace_text)
        self.assertEqual(unrelated.sent, [ACK, "other final result"])
        self.assertEqual(unrelated.started, 1)
        self.assertIsNone(unrelated.local["receipt"])
        self.assertEqual(unrelated.image_receipt, "cancelled")
        slow = await self.replay(mid="slow-rejected", speed="slow", result=None)
        self.assertEqual(slow.started, 1)
        self.assertEqual(slow.image_receipt, "unknown")
        self.assertNotIn("onebot_image", slow.steps)

    async def test_missing_hook_and_hung_text_release_pending_resources(self):
        missing = await self.replay(mid="no-hook", skip_hook=True)
        self.assertEqual(missing.local["receipt"], "123")
        self.assertEqual(missing.image_receipt, "cancelled")
        self.assertNotIn("onebot_image", missing.steps)

        async def wait_while_text_hangs(event):
            await event.get_extra("_livinghello_image_task")

        with patch("images.guard_image_settings.TEXT_DELIVERY_TIMEOUT", 0.05):
            hung = await self.replay(mid="timeout", on_text=wait_while_text_hangs)
        self.assertEqual(hung.image_receipt, "cancelled")
        self.assertNotIn("onebot_image", hung.steps)

    async def test_fast_image_failure_waits_for_text_receipt(self):
        result = await self.replay(mid="failure", speed="failure")
        self.assertEqual(len(result.sent), 2)
        self.assertEqual(result.sent[0], ACK)
        self.assertLess(result.steps.index("image_result"), result.steps.index("onebot_text_return"))
        self.assertEqual(result.steps.count("onebot_text"), 2)
        self.assertLess(result.steps.index("onebot_text_return"),
                        len(result.steps) - 1 - result.steps[::-1].index("onebot_text"))
        self.assertEqual(result.image_receipt, "failed")
        unconfirmed = await self.replay(mid="unconfirmed-failure", speed="failure", result=None)
        self.assertEqual(unconfirmed.sent, [ACK])
        self.assertEqual(unconfirmed.image_receipt, "failed")
        self.assertFalse(unconfirmed.event.get_extra("_livinghello_image_failure")["notified"])

    async def test_non_tool_error_reasoning_usage_and_media_contract(self):
        response = self.draft()
        media = Reply(id="77")
        response.result_chain.chain.append(media)
        clean = strip_tool_text(response)
        self.assertEqual(clean.result_chain.chain, [media])
        self.assertEqual(clean.completion_text, "")
        self.assertEqual(response.completion_text, DRAFT)
        for field in ("tools_call_args", "tools_call_name", "tools_call_ids", "reasoning_content",
                      "reasoning_signature", "usage"):
            self.assertIs(getattr(clean, field), getattr(response, field))
        error = LLMResponse(role="err", completion_text="offline error")
        self.assertIs(strip_tool_text(error), error)

    async def test_superseded_before_preflight_result_never_starts_image(self):
        def supersede(event):
            event.set_extra("_at_guard_superseded", True)
            event.set_extra("_at_guard_send_check", lambda: "superseded")

        trace = await self.replay(owns=True, during_tool=supersede)
        self.assertEqual(trace.sent, [])
        self.assertEqual(trace.started, 0)
        self.assertIsNone(trace.local)

    async def test_superseded_during_generation_keeps_unknown_and_sends_nothing(self):
        async def supersede(event, _result):
            # Wait for the stub request to enter its blocking upstream call.
            async with asyncio.timeout(2):
                while not self.image._request.await_count:
                    await asyncio.sleep(0)
            event.set_extra("_at_guard_superseded", True)
            event.set_extra("_at_guard_send_check", lambda: "superseded")
            ReplyTurns().cancel(event)

        trace = await self.replay(owns=True, before_send=supersede, speed="slow")
        self.assertEqual(trace.sent, [])
        self.assertEqual(trace.job["status"], "cancelled")
        self.assertEqual(trace.started, 1)
        self.assertEqual(trace.image_receipt, "unknown")
        self.assertNotIn("onebot_image", trace.steps)

    async def test_unload_drains_generated_image_while_text_is_pending(self):
        async def unload(_):
            await self.image.close()

        trace = await self.replay(mid="unload", on_text=unload)
        self.assertEqual(trace.image_receipt, "cancelled")
        self.assertEqual(trace.job["status"], "cancelled")
        self.assertNotIn("onebot_image", trace.steps)
