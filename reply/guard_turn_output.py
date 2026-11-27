"""Only the main non-streaming QQ runner owns this turn's public text."""
import asyncio
from contextlib import aclosing
from copy import copy
from dataclasses import dataclass, replace
import json
import re

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.provider import LLMResponse
from astrbot.core.platform.message_type import MessageType

try:
    from ..images.guard_image_settings import IMAGE_INTENT, image_unavailable_instruction, valid_image_parameters
    from ..observability.guard_request_trace import emit
    from .guard_native_reply import managed_options
    from .guard_reply_health import KEY as HEALTH_KEY
except ImportError:
    from images.guard_image_settings import IMAGE_INTENT, image_unavailable_instruction, valid_image_parameters
    from observability.guard_request_trace import emit
    from reply.guard_native_reply import managed_options
    from reply.guard_reply_health import KEY as HEALTH_KEY

BOUNDARY = "_iter_llm_responses_with_fallback"
IMAGE_TOOL = "livinghello_generate_image"
IMAGE_JOB = "_livinghello_image_job"
LOCAL_REPLY = "_livinghello_local_tool_reply"
REPAIR_ATTEMPTS = 2  # Shared by the two fields after the initial successful model result.
REPAIR_TIMEOUT = 15
PROMPT_REPAIR = (
    '只重写本轮已经决定执行的单张图片参数。保留当前请求与相关上文里已明确的画面和参考图要求；'
    '不要改变任务，也不要编造缺失的关键信息。只返回 JSON 对象 '
    '{"prompt":"画面描述","use_reference":false}，不要调用任何工具、不要写承接语。'
    '关键信息仍不明确时返回空对象。'
)
REPLY_REPAIR = (
    '本轮是一张即时图片，已通过预检。按给定语境及口吻写一句简短的自然承接语，约 8～20 字。'
    '不要描述画面或进度，不要说已经拍好或发出，不追问、也不重做工具。只返回 JSON 对象 {"reply":"承接语"}。'
)


@dataclass(frozen=True)
class ToolReplyDraft:
    name: str
    call_id: str
    args: dict
    text: str | None
    immediate: bool


def reply_body(chain):
    body = [component for component in chain or () if not isinstance(component, (At, Reply))]
    if len(body) == 1 and isinstance(body[0], Plain):
        return body[0].text.strip() or None
    return None


def reply_text(value, prompt=None, *, question=False):
    if (not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f\u2028\u2029<>`\[\]]", value)
            or (not question and re.search(r"[？?]", value))):
        return None
    text = value.strip()
    if (not 4 <= len(text) <= 30 or not any(c.isalnum() for c in text)
            or re.search(r"[。！？!?；;].", text) or "://" in text):
        return None
    body = text.strip(" 。！!")
    if isinstance(prompt, str) and body == prompt.strip(" 。！!"):
        return None
    if re.search(r"(?:已经|正在|还在).*(?:拍|画|做图|出图|生[成图]|上传|发(?:送|图|出|给你|过去))|"
                 r"已(?:拍好|画好|做好|出图|生成|上传|发出|发送|发图|发给你|发过去|送达)|"
                 r"(?:拍好|画好|做好|出图|生成完|发出|发给你|发过去|送达)了", text):
        return None
    return text


def declined_reply(value, args, reason):
    text = reply_text(value, args.get("prompt"), question=reason in {
        "reference_missing", "prompt_too_long", "prompt_adjustment_required"})
    if text and not re.search(r"(?:这就|马上|立刻|明天|下次|改天|稍后|待会|一会儿)"
                              r"[^。！？!?]*(?:拍|画|给你|发图|做图)|等我一下|"
                              r"我(?:来|就|先|会)?(?:给你|帮你)(?:拍|画|做图|发图)", text):
        return text
    return None


def repair_fields(response):
    if (response is None or response.role != "assistant" or response.is_chunk
            or response.tools_call_name or response.tools_call_ids or response.tools_call_args
            or any(not isinstance(c, Plain)
                   for c in getattr(response.result_chain, "chain", None) or ())):
        return {}
    raw = (response.completion_text or "").strip()
    if len(raw) > 20000:
        return {}
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    try:
        fields = json.loads(fence[1] if fence else raw)
    except (ValueError, RecursionError):
        return {}
    return fields if isinstance(fields, dict) else {}


def reply_context(messages):
    """Keep the latest four chat messages and a short persona excerpt; no media or tool output."""
    from astrbot.core.agent.message import Message, TextPart

    def plain(message):
        parts = message.content
        text = parts if isinstance(parts, str) else "\n".join(
            part.text for part in parts or () if isinstance(part, TextPart))
        return text.strip()

    recent = []
    for message in reversed(messages):
        if message.role not in {"user", "assistant"} or message.tool_calls:
            continue
        if text := plain(message):
            recent.append(Message(role=message.role, content=text[-450:]))
        if len(recent) == 4:
            break
    persona = next((plain(message) for message in messages if message.role == "system"), "")
    context = [Message(role="system", content=persona[:600])] if persona else []
    return context + list(reversed(recent))


def strip_tool_text(response):
    if (response is None or getattr(response, "role", None) != "assistant"
            or response.is_chunk or not response.tools_call_name):
        return response
    source = response.result_chain
    chain = list(getattr(source, "chain", None) or ())
    body = [component for component in chain if not isinstance(component, Plain)]
    if len(body) == len(chain) and not response.completion_text:
        return response
    result = copy(response)
    # AstrBot's setter inserts Plain("") if a chain is attached, so detach first.
    result.result_chain = None
    result.completion_text = ""
    if body:
        result.result_chain = copy(source)
        result.result_chain.chain = body
    return result


def detach_tool_replies(response, offered):
    """Remove presentation args before the host executes, traces or stores the call."""
    if response is None or response.role != "assistant" or response.is_chunk:
        return response, None
    names, ids, calls = response.tools_call_name, response.tools_call_ids, response.tools_call_args
    args = list(calls)
    draft = None
    for index, (name, values) in enumerate(zip(names, calls)):
        if (name not in TOOL_REPLIES or not isinstance(values, dict)
                or not {"reply", "immediate"}.intersection(values)):
            continue
        business = values.copy()
        immediate = business.pop("immediate", None)
        text = reply_text(business.pop("reply", None), business.get("prompt"))
        args[index] = business
        if (name in offered and (immediate is True or ("immediate" not in values and text))
                and not business.keys() - {"prompt", "use_reference"}
                and len(names) == len(ids) == len(calls) == 1
                and isinstance(ids[0], str) and ids[0].strip()
                and all(isinstance(c, Plain) for c in
                                   getattr(response.result_chain, "chain", None) or ())):
            draft = ToolReplyDraft(name, ids[0], business, text, immediate is True)
    if args != calls:
        response = copy(response)
        response.tools_call_args = args
    return response, draft


class ImageTurnReply:
    reply_description = (
        "给对方看的候选承接语。immediate 为 true 时填写。结合本轮和相关上文，按人设和口吻写一句约 8～20 字的话，不照抄 prompt、"
        "不描述画面或进度，不声称已经拍好或发出。它只是候选，工具预检通过后才可能发出。"
        "不要在工具调用旁输出聊天正文；需要答别的问题、查询结果或信息不明确时省略此参数。"
    )
    intent_description = (
        "结合本轮与相关上文，确认对方只想现在收到一张图片，且没有其他问题、工具或待办需要处理时填 true，"
        "同一调用中同时给出 prompt 和 reply。混合请求、待办或意图不明确时填 false 或省略。"
    )

    @staticmethod
    def eligible(plugin, event):
        return (getattr(plugin, "_livinghello_detached_enabled", False)
                and not event.get_extra(IMAGE_JOB)
                and not event.get_extra("livingemotion:promise_id")
                and not event.get_extra("_livinghello_requested_promise")
                and not event.get_extra("_livinghello_pending_image"))

    @staticmethod
    def match_job(plugin, event, draft, tool_result):
        args = draft.args
        if args.keys() - {"prompt", "use_reference"}:
            return None
        prompt, reference = args.get("prompt"), args.get("use_reference", False)
        job = event.get_extra(IMAGE_JOB) or {}
        image = plugin._image_generation()
        if (not valid_image_parameters(prompt, reference)
                or not job or job.get("reply") != tool_result
                or job.get("prompt") != prompt.strip() or job.get("use_reference") is not reference
                or job.get("promise_id") is not None
                or event.get_extra("_livinghello_requested_promise")
                or event.get_extra("_livinghello_pending_image") or image.blocked(event)):
            return None
        return job

    @staticmethod
    def resolve(plugin, event, draft, job):
        status = job["status"]
        if status == "prepared" and draft.text:
            delivery = asyncio.get_running_loop().create_future()
            # Start generation now; only outbound media waits for the text receipt.
            plugin._image_generation().start_image_job(event, text_delivery=delivery)
            if job["status"] != "running":
                return None
            return {"job": job, "status": status, "text": draft.text, "delivery": delivery}
        if status in {"refused", "quota", "failed"} and draft.text:
            return {"job": job, "status": status, "text": draft.text, "delivery": None}
        return None

    @staticmethod
    async def after_sent(plugin, event, local):
        job = event.get_extra(IMAGE_JOB)
        if job is not local["job"] or plugin._image_generation().blocked(event):
            return False
        if local["status"] == job["status"] and job["status"] in ("failed", "quota"):
            failure = event.get_extra("_livinghello_image_failure")
            if failure:
                # Immediate preflight rejection closes here, without opening an LE follow-up.
                failure["notified"] = True
        return True

    @staticmethod
    def cancel(event):
        task = event.get_extra("_livinghello_image_task")
        if task is not None and not task.done():
            task.cancel()


TOOL_REPLIES = {IMAGE_TOOL: ImageTurnReply()}


class TurnOutput:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bindings = {}
        self.closed = False

    def accepts(self, event, runner, context):
        return (not self.closed and runner is not None
                and getattr(runner, "run_context", None) is context
                and getattr(getattr(context, "context", None), "event", None) is event
                and getattr(runner, "req", None) is not None
                and runner.req is event.get_extra("_livinghello_provider_request")
                and getattr(runner, "streaming", None) is False
                and getattr(runner, "tool_schema_mode", None) == "full"
                and event.get_platform_name() == "aiocqhttp"
                and event.get_message_type() == MessageType.FRIEND_MESSAGE
                and callable(getattr(runner, BOUNDARY, None)))

    def watch(self, event, runner, context):
        if not self.accepts(event, runner, context):
            return False
        owner = (event.get_extra(HEALTH_KEY) or {}).get("owner")
        if owner is not None:
            # Register before ReplyFallback's listener: remove the outer layer first.
            owner.add_done_callback(lambda _: self.pipeline_done(event, runner))
        return True

    def bind(self, event, runner):
        if id(runner) in self.bindings:
            return
        original = getattr(runner, BOUNDARY)
        ordinary = (
            not event.get_extra("cron_job")
            and not event.get_extra("_livinghello_any_send_attempted")
            and len(event.get_extra("_at_guard_fragment_sources") or ()) <= 1
            and self.plugin._delivery().has_onebot_receipts(event)
            and self.text_route(event)
            and callable(getattr(runner, "_await_or_stop", None))
            and callable(getattr(runner, "_sanitize_contexts_for_provider", None))
        )
        step, draft = 0, None
        remaining = REPAIR_ATTEMPTS
        provider = model = None
        first_messages = ()

        async def repair(field, args, *, decline=None):
            from astrbot.core.agent.message import Message

            nonlocal remaining
            messages = (reply_context(first_messages) if field == "reply" else
                        runner._sanitize_contexts_for_provider(list(first_messages)))
            instruction = REPLY_REPAIR if field == "reply" else PROMPT_REPAIR
            if decline:
                instruction = (image_unavailable_instruction(decline)
                               + ' 按当前人设只写一句，返回 JSON 对象 {"reply":"给对方的话"}。')
            reference = args.get("use_reference")
            if field == "prompt" and type(reference) is bool:
                instruction += f" 保留已确定的 use_reference={str(reference).lower()}。"
            while remaining and self.active_for_local(event, runner):
                remaining -= 1
                response = None
                reason = f"{field}_invalid"
                try:
                    response = await runner._await_or_stop(asyncio.wait_for(
                        provider.text_chat(
                            contexts=[Message(role="system", content=instruction), *messages],
                            func_tool=None, model=model, request_max_retries=1,
                            abort_signal=runner._abort_signal,
                            extra_user_content_parts=(runner.req.extra_user_content_parts
                                                      if field == "prompt" else None)),
                        timeout=REPAIR_TIMEOUT))
                except Exception as exc:
                    reason = type(exc).__name__
                if usage := getattr(response, "usage", None):
                    runner.stats.token_usage += usage
                if not self.active_for_local(event, runner):
                    emit(event, "turn_output_repair", "cancelled", reason=field, calls=1)
                    return None
                fields = repair_fields(response)
                if field == "reply":
                    value = None
                    if fields.keys() == {"reply"}:
                        value = (declined_reply(fields["reply"], args, decline) if decline else
                                 reply_text(fields["reply"], args.get("prompt")))
                else:
                    value = (fields if fields.keys() == {"prompt", "use_reference"}
                             and valid_image_parameters(fields["prompt"], fields["use_reference"])
                             and (type(reference) is not bool or reference == fields["use_reference"])
                             else None)
                emit(event, "turn_output_repair", "success" if value else "failed",
                     reason=field if value else reason, calls=1)
                if value is not None:
                    return value
            return None

        async def wrapped():
            nonlocal step, draft, provider, model, first_messages
            step += 1
            if step == 2 and draft:
                policy = TOOL_REPLIES[draft.name]
                job = self.job_for_result(event, runner, policy, draft)
                failure = job and job.get("preflight_reason")
                if job is not None and not failure and job["status"] == "prepared":
                    if not draft.text and draft.immediate:
                        draft = replace(draft, text=await repair("reply", draft.args))
                        if self.job_for_result(event, runner, policy, draft) is not job:
                            job = None
                    if job is not None and draft.text:
                        failure = self.plugin._image_generation().check_immediate_job(job)
                if job is not None and failure:
                    text = await repair("reply", draft.args, decline=failure)
                    if self.job_for_result(event, runner, policy, draft) is not job:
                        job = None
                    else:
                        draft = replace(draft, text=text)
                local = self.final_response(event, policy, draft, job) if job is not None else None
                if local is not None:
                    if event.get_extra(LOCAL_REPLY)["delivery"] is not None:
                        # Give the background task a turn without waiting for the image.
                        await asyncio.sleep(0)
                    yield local
                    return
                if draft.immediate or failure:
                    yield self.finish_without_image(event, "image_reply_unavailable")
                    return
            req = runner.req
            primary = runner.provider
            original_tools = req.func_tool
            schema, offered = original_tools, ()
            if step == 1 and ordinary:
                schema, offered = self.offer_tools(event, original_tools)
            if offered:
                req.func_tool = schema
            try:
                async with aclosing(original()) as responses:
                    async for response in responses:
                        if response is not None and response.is_chunk:
                            offered = ()  # Some output has already left the model step.
                            yield response
                            continue
                        response, choice = detach_tool_replies(response, offered)
                        if step == 1:
                            draft = choice
                        output = strip_tool_text(response)
                        if output is not response:
                            emit(event, "turn_output", "success", reason="tool_step_text_omitted")
                        break
                    else:
                        return
            finally:
                if schema is not original_tools and req.func_tool is schema:
                    req.func_tool = original_tools
            if step == 1 and draft:
                provider = runner.provider
                model = req.model if provider is primary else None
                first_messages = tuple(runner.run_context.messages)
            if step == 1 and draft and draft.immediate:
                # Give this one-shot route exclusive ownership before the tool can prepare a job.
                event.set_extra(IMAGE_INTENT, True)
                if not self.active_for_local(event, runner):
                    yield self.finish_without_image(event, "image_turn_cancelled", output.usage)
                    return
                if not valid_image_parameters(draft.args.get("prompt"), draft.args.get("use_reference", False)):
                    args = await repair("prompt", draft.args)
                    if args is None:
                        yield self.finish_without_image(event, "image_prompt_unavailable", output.usage)
                        return
                    draft = replace(draft, args=args, text=reply_text(draft.text, args["prompt"]))
                    output.tools_call_args = [args]
            # The host breaks on its first final response; restore tools before yielding it.
            yield output

        had_local = BOUNDARY in vars(runner)
        try:
            setattr(runner, BOUNDARY, wrapped)
        except (AttributeError, TypeError):
            return
        self.bindings[id(runner)] = (runner, original, wrapped, had_local)
        event.set_extra("_livinghello_turn_output_runner", runner)

    def text_route(self, event):
        if event.get_extra("action_type") == "live":
            return False
        try:
            config = self.plugin.context.get_config(umo=event.unified_msg_origin)
            platform = config.get("platform_settings") or {}
            segments = platform.get("segmented_reply") or {}
            return (config["provider_tts_settings"]["enable"] is False
                    and not (config.get("provider_settings") or {}).get("show_tool_use_status")
                    and not platform.get("reply_prefix")
                    and (segments.get("enable") is not True or managed_options(segments) is not None))
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def offer_tools(self, event, tools):
        replacements, offered = [], set()
        for tool in getattr(tools, "tools", ()):
            policy = TOOL_REPLIES.get(tool.name)
            parameters = getattr(tool, "parameters", None)
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            if (policy and getattr(tool, "active", True) and isinstance(properties, dict)
                    and not {"reply", "immediate"}.intersection(properties)
                    and policy.eligible(self.plugin, event)):
                tool = copy(tool)
                tool.parameters = {**parameters, "properties": {
                    **properties, "reply": {"type": "string", "description": policy.reply_description},
                    "immediate": {"type": "boolean", "description": policy.intent_description},
                }}
                offered.add(tool.name)
            replacements.append(tool)
        if not offered:
            return tools, offered
        schema = copy(tools)
        schema.tools = replacements
        return schema, offered

    def active_for_local(self, event, runner):
        return (not self.closed and not event.get_extra("_livinghello_any_send_attempted")
                and not event.get_extra("agent_stop_requested")
                and not runner._is_stop_requested() and not event.is_stopped()
                and self.plugin._delivery().has_onebot_receipts(event)
                and self.text_route(event) and not self.plugin._image_generation().blocked(event))

    def job_for_result(self, event, runner, policy, draft):
        messages = runner.run_context.messages
        tail = messages[-1] if messages else None
        health = event.get_extra(HEALTH_KEY) or {}
        if (not health or getattr(tail, "role", None) != "tool"
                or getattr(tail, "tool_call_id", None) != draft.call_id
                or health.get("tool_error") or not self.active_for_local(event, runner)):
            return None
        return policy.match_job(self.plugin, event, draft, tail.content)

    def final_response(self, event, policy, draft, job):
        local = policy.resolve(self.plugin, event, draft, job)
        if local is None:
            return None
        local.update(policy=policy, rendered=None, receipt=None)
        event.set_extra(LOCAL_REPLY, local)
        emit(event, "turn_output", "success", reason=f"local_tool_{local['status']}",
             tool=draft.name, calls=0)
        return LLMResponse(role="assistant", completion_text=local["text"])

    def cancel_prepared(self, event):
        job = event.get_extra(IMAGE_JOB)
        if event.get_extra(IMAGE_INTENT) and job and job["status"] in {"deciding", "prepared"}:
            job["status"] = "cancelled"
            self.plugin._image_generation().release_reservation(job)
            self.plugin._clear_reply_turn(event)

    def finish_without_image(self, event, reason, usage=None):
        self.cancel_prepared(event)
        event.set_extra("_livinghello_intentional_silence", reason)
        emit(event, "turn_output", "cancelled", reason=reason)
        # An empty assistant result finalizes the host without faking a user interruption.
        return LLMResponse(role="assistant", completion_text="", usage=usage)

    @staticmethod
    def decoration_owner(event, result):
        local = event.get_extra(LOCAL_REPLY)
        if local and result.is_llm_result() and reply_body(result.chain) == local["text"]:
            return local

    async def after_sent(self, event, local):
        if not local.get("handled"):
            local["handled"] = True
            plan = event.get_extra("_at_guard_delivery_plan") or {}
            health = event.get_extra(HEALTH_KEY) or {}
            accepted = (bool(local["receipt"]) and not plan.get("failed") and not health.get("failed")
                        and await local["policy"].after_sent(self.plugin, event, local))
            self.settle_text(event, accepted)

    @staticmethod
    def settle_text(event, accepted, reason="local_tool_not_deliverable"):
        local = event.get_extra(LOCAL_REPLY)
        delivery = local["delivery"] if local else None
        if delivery is None or delivery.done():
            return
        delivery.set_result(accepted)
        if not accepted:
            emit(event, "turn_output", "cancelled", reason=reason)
            local["policy"].cancel(event)

    def pipeline_done(self, event, runner):
        self.release(runner)
        if event.get_extra("_livinghello_turn_output_runner") is runner:
            event.set_extra("_livinghello_turn_output_runner", None)
        self.settle_text(event, False, "local_tool_pipeline_ended_without_confirmation")
        self.cancel_prepared(event)

    def release(self, runner):
        binding = self.bindings.pop(id(runner), None)
        if not binding:
            return
        _, original, wrapped, had_local = binding
        if getattr(runner, BOUNDARY, None) is wrapped:
            if had_local:
                setattr(runner, BOUNDARY, original)
            else:
                delattr(runner, BOUNDARY)

    def close(self):
        self.closed = True
        for runner, *_ in list(self.bindings.values()):
            self.release(runner)
