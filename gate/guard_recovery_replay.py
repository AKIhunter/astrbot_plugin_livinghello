"""LH 自有断线补答：有界原消息保留、当前规则复判、原生 Agent 与发送回执。"""
from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict

try:
    from .guard_classifier_outage import BackfillStatus, OutageState
except ImportError:
    from gate.guard_classifier_outage import BackfillStatus, OutageState

CONTRACT = "livinghello.classifier-recovery.v1"
KEY = "_livinghello_classifier_recovery"


class RecoveryReplay:
    CAPACITY = 256
    TIMEOUT = 180

    def __init__(self, plugin):
        self.plugin = plugin
        self.messages = OrderedDict()
        self.closed = False

    def remember(self, event):
        outage = self.plugin._outage().current_event
        if self.closed or outage is None or event.get_extra(KEY):
            return
        key = (outage.event_id, str(event.unified_msg_origin))
        # 每个受影响会话只补最新一条；旧消息不在恢复时连发刷屏。
        self.messages[key] = event
        self.messages.move_to_end(key)
        while len(self.messages) > self.CAPACITY:
            self.messages.popitem(last=False)

    def close(self):
        self.closed = True
        self.messages.clear()

    def valid(self, event, work):
        outage = self.plugin._outage()
        return (not self.closed and outage.enabled
                and outage.state is OutageState.RECOVERING
                and outage.current_event.event_id == work.event_id
                and not self.plugin._policy_send_reason(event)
                and not self.plugin._stale_reason(event, self.plugin._group_key(event)))

    @staticmethod
    def attempted(event):
        health = event.get_extra("_livinghello_reply_health") or {}
        plan = event.get_extra("_at_guard_delivery_plan") or {}
        return bool(event.get_extra("_livinghello_any_send_attempted")
                    or event.get_extra("_livinghello_recovery_tool_attempted")
                    or plan.get("attempted") or health.get("failed") or health.get("sent"))

    @staticmethod
    def replay_event(source, work):
        event = copy.copy(source)
        # 清掉原流水线的闭包、停止标志和请求；保留平台连接及原消息对象。
        # send 重新绑定到副本，由 LH 本轮发送装饰重新安装保护。
        event.__dict__.pop("send", None)
        event._extras = {}
        if "extra" in event.__dict__:  # 最小隔离事件替身
            event.extra = {}
        event._result = None
        event._force_stopped = False
        event._has_send_oper = False
        event._execution_active = False
        event._temporary_local_files = []
        event.call_llm = True
        event.is_wake = True
        event.set_extra(KEY, work.event_id)
        event.set_extra("_at_guard_recorded_at", source.get_extra("_at_guard_recorded_at"))
        event.set_extra("_at_guard_received_at", source.get_extra("_at_guard_received_at"))
        return event

    async def native_reply(self, event):
        """仅运行宿主 Agent/装饰/发送，不重放普通插件命令与消息监听。"""
        from astrbot.core.pipeline.context import PipelineContext
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.pipeline.process_stage.method.agent_request import AgentRequestSubStage
        from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
        from astrbot.core.pipeline.respond.stage import RespondStage
        from astrbot.core.utils.trace import TraceSpan

        context = self.plugin.context
        manager = getattr(context, "_star_manager", None)
        if manager is None:
            return False
        config = context.get_config(event.unified_msg_origin)
        if not config.get("provider_settings", {}).get("enable", True):
            return False
        config_id = context.astrbot_config_mgr.get_conf_info(event.unified_msg_origin)["id"]
        ctx = PipelineContext(config, manager, config_id, getattr(context, "_db", None))
        scheduler = PipelineScheduler(ctx)
        scheduler.stages = [AgentRequestSubStage(), ResultDecorateStage(), RespondStage()]
        for stage in scheduler.stages:
            await stage.initialize(ctx)
        event.trace = event.span = TraceSpan(name="LivingHelloRecovery", umo=event.unified_msg_origin)
        await self.plugin.parse_reply_input(event)
        # 让原生阶段自行收集附件并绑定当前会话；空 conversation 的显式请求
        # 会跳过宿主人格与历史装配，不能用它强行唤醒补答。
        try:
            await scheduler.execute(event)
        finally:
            # 超时/取消也结束本轮健康监视，避免旧 watchdog 留在后台。
            await self.plugin._reply_health().finish(event)
        return True

    async def recover(self, work):
        rows = []
        for session in work.sessions:
            key = (work.event_id, session.session_id)
            source = self.messages.get(key)
            status = BackfillStatus.UNAVAILABLE
            replay = None
            try:
                if source is not None:
                    if self.closed:
                        status = BackfillStatus.UNAVAILABLE
                    elif not self.valid(source, work):
                        status = BackfillStatus.NO_REPLY
                    elif self.attempted(source):
                        status = BackfillStatus.SEND_UNKNOWN
                    else:
                        replay = self.replay_event(source, work)
                        # 同一恢复窗口下一次尝试也检查这个副本的实际副作用。
                        self.messages[key] = replay
                        ready = await asyncio.wait_for(self.native_reply(replay), self.TIMEOUT)
                        health = replay.get_extra("_livinghello_reply_health") or {}
                        plan = replay.get_extra("_at_guard_delivery_plan") or {}
                        delivered = (replay.get_extra("_at_guard_actual_sent")
                                     and replay.get_extra("_at_guard_sent_has_content")
                                     and health.get("model_done") and health.get("sent"))
                        if delivered and not health.get("failed") and not plan.get("failed"):
                            status = BackfillStatus.COMPLETED
                        elif self.attempted(replay):
                            status = BackfillStatus.SEND_UNKNOWN
                        elif health.get("fault") or (replay.get_extra("_at_guard_decision") or {}).get("source") == "error":
                            status = BackfillStatus.RETRY_PENDING
                        elif (not self.valid(replay, work) or replay.get_extra("_livinghello_intentional_silence")
                              or getattr(replay, "is_stopped", lambda: False)()):
                            status = BackfillStatus.NO_REPLY
                        else:
                            status = BackfillStatus.RETRY_PENDING if ready else BackfillStatus.UNAVAILABLE
            except asyncio.CancelledError:
                self.plugin._outage().set_backfill_status(
                    session.session_id,
                    BackfillStatus.SEND_UNKNOWN if replay and self.attempted(replay)
                    else BackfillStatus.RETRY_PENDING)
                raise
            except (ImportError, AttributeError):
                status = (BackfillStatus.SEND_UNKNOWN if replay and self.attempted(replay)
                          else BackfillStatus.UNAVAILABLE)
            except Exception:
                status = (BackfillStatus.SEND_UNKNOWN if replay and self.attempted(replay)
                          else BackfillStatus.RETRY_PENDING)
            if status in {BackfillStatus.COMPLETED, BackfillStatus.NO_REPLY, BackfillStatus.UNAVAILABLE}:
                self.messages.pop(key, None)
            rows.append({"session_id": session.session_id, "status": status.value})
        return {"contract": CONTRACT, "event_id": work.event_id, "sessions": rows}
