"""Proof of image dispatch, kept separate from the acknowledgement's text receipt."""
import asyncio
from collections.abc import Mapping
import re


class ImageReceiptBot:
    def __init__(self, bot, target, group, on_confirmed=None):
        self.bot, self.target, self.group = bot, target, group
        self.message_id = None
        self.on_confirmed = on_confirmed

    def __getattr__(self, name):
        return getattr(self.bot, name)

    def observe(self, result, target, group, message):
        if (self.message_id is not None or target != self.target or group != self.group
                or not isinstance(result, Mapping)
                or [segment.get("type") for segment in message] != ["image"]):
            return
        mid = result.get("message_id")
        if ((type(mid) is int or (type(mid) is str and re.fullmatch(r"-?[0-9]{1,20}", mid)))
                and int(mid) != 0):
            self.message_id = str(mid)
            if self.on_confirmed is not None:
                self.on_confirmed()

    async def send_private_msg(self, *, user_id, message, **kwargs):
        result = await self.bot.send_private_msg(user_id=user_id, message=message, **kwargs)
        self.observe(result, user_id, False, message)
        return result

    async def send_group_msg(self, *, group_id, message, **kwargs):
        result = await self.bot.send_group_msg(group_id=group_id, message=message, **kwargs)
        self.observe(result, group_id, True, message)
        return result


async def send_event_image(event, packet, *, on_confirmed):
    """True=confirmed, False=explicit rejection, None=unknown; no retry on unknown."""
    if getattr(event, "get_platform_name", lambda: "")() != "aiocqhttp":
        result = await event.send(packet)
        if result is True:
            on_confirmed()
        return result if type(result) is bool else None
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

    if not isinstance(event, AiocqhttpMessageEvent) or "send_message" in vars(event):
        return None
    group = event.get_group_id()
    target = str(group or event.get_sender_id() or "")
    if not (target.isascii() and target.isdecimal()):
        return None
    native = event.send_message
    if native != AiocqhttpMessageEvent.send_message:
        return None
    bot = ImageReceiptBot(event.bot, int(target), bool(group), on_confirmed)
    owner = asyncio.current_task()

    async def observe(*args, **kwargs):
        if asyncio.current_task() is owner and kwargs.get("message_chain") is packet:
            kwargs["bot"] = bot
        return await native(*args, **kwargs)

    event.send_message = observe
    try:
        result = await event.send(packet)
    except Exception:
        if bot.message_id is not None:
            return True
        raise
    finally:
        if getattr(event, "send_message", None) is observe:
            del event.send_message
    return True if bot.message_id is not None else (False if result is False else None)


async def send_session_image(context, session, packet):
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        # Alternate hosts may implement an explicit confirmation contract here.
        result = await context.send_message(session, packet)
        return result if type(result) is bool else None
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType

    route = MessageSession.from_str(session)
    for platform in manager.platform_insts:
        meta = platform.meta()
        if meta.id != route.platform_name:
            continue
        if meta.name != "aiocqhttp":
            # Context.send_message returns True on routing alone; ask the adapter.
            result = await platform.send_by_session(route, packet)
            return result if type(result) is bool else None
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

        group = route.message_type == MessageType.GROUP_MESSAGE
        target = route.session_id.split("_")[-1] if group else route.session_id
        if not (target.isascii() and target.isdecimal()):
            return False
        bot = ImageReceiptBot(platform.bot, int(target), group)
        await AiocqhttpMessageEvent.send_message(
            bot=bot, message_chain=packet, is_group=group, session_id=target)
        return True if bot.message_id is not None else None
    return False
