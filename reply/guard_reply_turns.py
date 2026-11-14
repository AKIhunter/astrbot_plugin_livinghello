"""短锁准备、持久用户输入、独立生成；顶替只发停止信号，不等待收尾。"""
from __future__ import annotations

import asyncio
import copy

KEY = "_livinghello_reply_turns"


class ReplyTurns:
    def __init__(self):
        self.tasks = {}

    def cancel(self, event):
        event.set_extra("agent_stop_requested", True)
        runner = event.get_extra("_livinghello_reply_runner")
        if runner and hasattr(runner, "request_stop"):
            runner.request_stop()
        task = self.tasks.get(id(event), (None, None))[1]
        # 准备阶段先完成输入保存；模型阶段立即取消，不 await 旧任务。
        if task and not task.done() and event.get_extra("_livinghello_input_saved"):
            task.cancel()
        image = event.get_extra("_livinghello_image_task")
        if image and not image.done():
            image.cancel()

    @staticmethod
    def valid(event):
        return not (event.get_extra("_at_guard_superseded")
                    or event.get_extra("_at_guard_delivery_blocked"))

    def lease(self, event, manager):
        return PreparationLease(self, event, manager)

    def before_reset(self, event, req):
        # 历史已由宿主在短锁内读取；每轮持有独立快照。
        req.contexts = copy.deepcopy(req.contexts)

    async def persist_input(self, event, req, runner, conversation_manager, dump_messages):
        if event.get_extra("_livinghello_input_saved") or not req.conversation:
            return
        rows = []
        skipped_system = False
        for message in runner.run_context.messages:
            if message.role == "system" and not skipped_system:
                skipped_system = True
                continue
            if getattr(message, "_no_save", False):
                continue
            rows.append(message)
        await conversation_manager.update_conversation(
            event.unified_msg_origin, req.conversation.cid,
            history=dump_messages(rows), token_usage=None)
        event.set_extra("_livinghello_input_saved", True)

    def busy(self, event):
        # 无视停止信号的异常依赖不能让连续打断无限堆积。
        rows = [(e, t) for e, t in self.tasks.values() if not t.done()]
        return len(rows) > 256 or sum(
            e.unified_msg_origin == event.unified_msg_origin for e, _ in rows) > 32

    async def save(self, event, manager, operation):
        if event.get_extra("_livinghello_preparation_held"):
            if self.valid(event):
                await operation()
            return
        async with manager.acquire_lock(event.unified_msg_origin):
            if self.valid(event):
                await operation()

    def close(self):
        for event, _ in list(self.tasks.values()):
            event.set_extra("_at_guard_superseded", True)
            self.cancel(event)


class PreparationLease:
    def __init__(self, turns, event, manager):
        self.turns, self.event = turns, event
        self.lock = manager.acquire_lock(event.unified_msg_origin)
        self.held = False

    async def __aenter__(self):
        predecessor = self.event.get_extra("_livinghello_reply_predecessor")
        if predecessor and not predecessor.done():
            # 水位窗口外仍保留旧轮完成后再开始的行为。
            try:
                await asyncio.shield(predecessor)
            except asyncio.CancelledError:
                if not predecessor.cancelled():
                    raise
            except Exception:
                pass
        await self.lock.__aenter__()
        self.held = True
        self.event.set_extra("_livinghello_preparation_held", True)
        self.turns.tasks[id(self.event)] = (self.event, asyncio.current_task())
        if not self.event.get_extra("_livinghello_send_guarded"):
            original_send = self.event.send
            async def guarded_send(*args, **kwargs):
                # 工具与宿主直接发送同样受归属约束，包括无视取消的旧任务。
                if self.turns.valid(self.event):
                    return await original_send(*args, **kwargs)
            self.event.send = guarded_send
            self.event.set_extra("_livinghello_send_guarded", True)
        return self

    async def release(self):
        if self.held:
            self.held = False
            self.event.set_extra("_livinghello_preparation_held", False)
            await self.lock.__aexit__(None, None, None)

    async def __aexit__(self, *args):
        await self.release()
        self.turns.tasks.pop(id(self.event), None)
