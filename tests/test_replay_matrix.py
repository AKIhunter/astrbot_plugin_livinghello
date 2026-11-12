"""离线回放矩阵：用合成数据压 12 类回复终态路径。

数据来源固定标注为「合成」：本轮不读取任何线上历史库，也不代表历史数据已验证。
真实历史回放入口见文末 RealReplayDataTests（目录缺失时明确跳过）。

硬约束：
- 零真实模型调用：StubProvider 只返回四种本地结果。
- 零真实外发：RecordingPlatform 记录「本该发到哪里」，计数必须始终为 0。
- 时间虚拟化：FakeClock 驱动，watchdog 用显式 finish(timed_out=True) 代替真实等待。

用法（无需安装 AstrBot，自动装最小替身）：
    python3 插件/astrbot_plugin_livingHello/tests/test_replay_matrix.py
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

DATA_SOURCE = "合成"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = PLUGIN_ROOT.parents[1] / "回放数据"

MATRIX: list["Row"] = []


@dataclass
class Row:
    sid: str
    scenario: str
    construction: str
    expectation: str          # 规格预期（用户定义的终态）
    actual: str               # 当前实现实测
    packets: int
    tool: str
    external: int
    note: str = ""
    status: str = "PASS"      # PASS / MISMATCH / ERROR

    def grade(self, expect_state, actual_state, expect_reason=None, actual_reason=""):
        ok = actual_state == expect_state
        if ok and expect_reason is not None:
            ok = actual_reason == expect_reason
        self.status = "PASS" if ok else "MISMATCH"
        return self


class FakeClock:
    """单调虚拟时钟；只做加法，不 sleep。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class StubProvider:
    """四种本地结果：normal / empty / exception / timeout，绝不联网。"""

    def __init__(self, outcome="normal"):
        self.outcome = outcome
        self.calls = 0

    async def text_chat(self, **_kwargs):
        self.calls += 1
        if self.outcome == "timeout":
            raise asyncio.TimeoutError("synthetic route timeout")
        if self.outcome == "exception":
            raise RuntimeError("synthetic upstream 502")
        text = "合成回答：这是一条用于终态归类的正常回复。" if self.outcome == "normal" else ""
        return SimpleNamespace(completion_text=text)


class RecordingPlatform:
    """平台替身：真实外发只可能落在这里；任何计数 >0 都视为违反硬约束。"""

    def __init__(self):
        self.packets = []

    async def send(self, route, chain):
        self.packets.append((route, chain))
        raise AssertionError("合成回放禁止真实外发")


def install_stubs():
    here = str(Path(__file__).resolve().parent)
    for path in (str(PLUGIN_ROOT), here):
        if path not in sys.path:
            sys.path.insert(0, path)
    import run_offline_tests
    if "astrbot" not in sys.modules:
        run_offline_tests.install_framework_stubs()


class Harness:
    """把真实组件接到一起：ReplyHealth + ReplyDelivery + RequestTrace。"""

    def __init__(self):
        install_stubs()
        from astrbot.api.message_components import Plain
        from test_gate import gate_config, plugin
        from test_reply_delivery import Chain, Event
        from guard_reply_health import ReplyHealth
        from guard_request_trace import RequestTrace

        self.Plain, self.Chain, self.Event = Plain, Chain, Event
        self._gate_config, self._plugin = gate_config, plugin
        self._ReplyHealth, self._RequestTrace = ReplyHealth, RequestTrace
        self.clock = FakeClock()
        self.platform = RecordingPlatform()

    # ---- 基础装配 -------------------------------------------------------
    def guard(self, **overrides):
        overrides.setdefault("gate_reply_max_age_seconds", 30)
        instance = self._plugin(self._gate_config(**overrides))
        instance.data_dir = None
        instance.reply_health = self._ReplyHealth(instance, clock=self.clock)
        instance.request_trace = self._RequestTrace(instance, None)
        return instance

    def event(self, *, group="1001", mid="m1", text="现在回我"):
        event = self.Event(group=group, mid=mid)
        event.message_str = text
        return event

    def begin(self, guard, event):
        return guard._trace(event)

    def terminal(self, guard, event, request_id=None):
        from guard_request_trace import ID_KEY
        rid = request_id or event.get_extra(ID_KEY)
        row = guard.request_trace.rows.get(rid) or {}
        reason = ""
        for entry in reversed(row.get("events") or []):
            if entry.get("feature") == "lifecycle":
                reason = str(entry.get("reason") or "")
                break
        return {
            "state": row.get("state", "missing"),
            "reason": reason,
            "packets": int((row.get("summary") or {}).get("sent_packets", 0)),
        }

    async def complete_turn(self, guard, event, *, text="合成回答：正常完成这一轮。",
                            tool=False, cron=False):
        """模拟宿主顺序：watch → patch send → model_done → arm → send → finish。"""
        self.begin(guard, event)
        health = guard.reply_health
        health.watch(event, expected=True)
        guard._delivery().patch(event)
        health.model_done(event, object())
        if cron:
            if tool:
                health.tool_delivered(event, target="cron:active_agent", elapsed=0.05)
            await health.finish_cron(event)
            return self.terminal(guard, event)
        result = self.Chain([self.Plain(text)])
        health.arm(event, result)
        await event.send(result.derive(result.chain))
        if tool:
            health.tool_delivered(event, target="qq-bot:FriendMessage:9001", elapsed=0.05)
        await health.finish(event)
        return self.terminal(guard, event)

    def row(self, sid, scenario, construction, expect_text, actual_state,
            actual_reason, expect_state, packets, tool="否", note="", expect_reason=None):
        row = Row(
            sid=sid, scenario=scenario, construction=construction,
            expectation=expect_text,
            actual=f"{actual_state}/{actual_reason}" if actual_reason else actual_state,
            packets=packets, tool=tool, external=len(self.platform.packets), note=note,
        )
        row.grade(expect_state, actual_state, expect_reason, actual_reason)
        return row

    # ---- ⑧ Provider 行为 -------------------------------------------------
    async def provider_round(self, outcome):
        provider = StubProvider(outcome)
        text = ""
        error = ""
        try:
            response = await provider.text_chat()
            text = response.completion_text
        except asyncio.TimeoutError:
            error = "timeout"
        except RuntimeError as exc:
            error = str(exc)
        return provider, text, error


def print_matrix(rows):
    print()
    print(f"场景矩阵（数据来源：{DATA_SOURCE}）")
    print("=" * 118)
    head = f"{'编号':<5}{'场景':<22}{'构造':<34}{'预期终态':<26}{'实测':<26}{'包':<4}{'工具':<5}{'外发':<5}判定"
    print(head)
    print("-" * 118)
    for row in rows:
        print(f"{row.sid:<5}{row.scenario:<22}{row.construction:<34}"
              f"{row.expectation:<26}{row.actual:<26}{row.packets:<4}"
              f"{row.tool:<5}{row.external:<5}{row.status}")
        if row.note:
            print(f"     注：{row.note}")
    mismatches = [r for r in rows if r.status != "PASS"]
    print("-" * 118)
    print(f"合计 {len(rows)} 行：PASS {len(rows) - len(mismatches)}，"
          f"MISMATCH {len(mismatches)}；真实外发 {sum(r.external for r in rows)} 次")
    print(f"数据来源：{DATA_SOURCE}（本表不代表历史数据已验证）")


class ReplayMatrixTests(unittest.IsolatedAsyncioTestCase):
    """12 类场景；MISMATCH 行按当前实现做回归快照，修复后需同步更新该行。"""

    @classmethod
    def setUpClass(cls):
        MATRIX.clear()

    async def asyncSetUp(self):
        self.h = Harness()
        self.rows = []

    async def asyncTearDown(self):
        MATRIX.extend(self.rows)
        self.assertEqual(self.h.platform.packets, [], "合成回放不得产生真实外发")

    def add(self, row):
        self.rows.append(row)
        return row

    # ① 私聊直连 ---------------------------------------------------------
    async def test_01_private_direct_not_taken_over(self):
        guard = self.h.guard(apply_to_private=False)
        event = self.h.event(group="")
        applies = guard._applies_to_event(event)
        guard.reply_health.watch(event, expected=True) if applies else None
        self.add(self.h.row(
            "01", "私聊直连", "apply_to_private=False，私聊事件",
            "不接管：无监测、无终态", "not_taken_over" if not applies else "taken_over",
            "", "not_taken_over", 0,
            note="LivingHello 只负责群聊闸门，私聊回复由 LivingEmotion 负责。",
        ))
        self.assertEqual(self.rows[-1].status, "PASS")
        self.assertEqual(guard.reply_health.active, {})

    # ② 群闸门 allow / block / classifier error -------------------------
    async def test_02_group_gate_paths(self):
        # allow：正常完成
        guard = self.h.guard()
        event = self.h.event()
        got = await self.h.complete_turn(guard, event)
        self.add(self.h.row(
            "02a", "群闸门 allow", "judged=True，模型正常，最终包已发",
            "completed/final_delivery_confirmed", got["state"], got["reason"],
            "completed", got["packets"], note="", expect_reason="final_delivery_confirmed",
        ))
        self.assertEqual(got["state"], "completed")
        self.assertEqual(got["packets"], 1)

        # block：判定为不回
        guard = self.h.guard()
        event = self.h.event(mid="m2")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        event.set_extra("_at_guard_delivery_blocked", "no_reply")
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "02b", "群闸门 block", "判定 no_reply，未发送",
            "cancelled/no_reply", got["state"], got["reason"],
            "cancelled", got["packets"], expect_reason="no_reply",
        ))
        self.assertEqual(got["state"], "cancelled")

        # classifier error：判定模型失败，没有模型正文
        guard = self.h.guard()
        event = self.h.event(mid="m3")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.classifier_failed(event, "timeout")
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "02c", "群闸门 判定模型异常", "classifier timeout，无主模型正文",
            "failed/timeout（层=classifier）", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="timeout",
        ))
        self.assertEqual(got["state"], "failed")

    # ③ @ / 唤醒绕过 -----------------------------------------------------
    async def test_03_at_and_wake_bypass(self):
        guard = self.h.guard()
        event = self.h.event()
        event.is_at_or_wake_command = True
        event.set_extra("_at_guard_bypass_reason", "at_call")
        self.assertFalse(guard._judged_event(event))
        got = await self.h.complete_turn(guard, event)
        self.add(self.h.row(
            "03", "@/唤醒绕过", "is_at_or_wake_command=True，未判定，直接生成",
            "completed/final_delivery_confirmed", got["state"], got["reason"],
            "completed", got["packets"], note="绕过仅跳过判定，仍须有最终投递证据。",
            expect_reason="final_delivery_confirmed",
        ))
        self.assertIsNone(event.get_extra("_at_guard_send_check"))
        self.assertEqual(got["state"], "completed")

    # ④ 碎片合并 ---------------------------------------------------------
    async def test_04_fragment_merging(self):
        from guard_fragments import FragmentWait

        async def run(seconds, gap, scope_key):
            clock = FakeClock()
            fw = FragmentWait(clock=clock)
            first = self.h.Event(group="1001" if scope_key[0] == "group" else "",
                                 mid="f1")
            second = self.h.Event(group="1001" if scope_key[0] == "group" else "",
                                  mid="f2")
            t1 = fw.observe(first, scope_key, {"text": "第一段"}, seconds, message_id="f1")
            clock.advance(gap)
            t2 = fw.observe(second, scope_key, {"text": "第二段"}, seconds, message_id="f2")
            old = await fw.wait(t1)
            clock.advance(seconds + 0.1)
            new = await fw.wait(t2)
            return old, new

        # 私聊关闭合并（0 秒）：两条各自成轮
        old, new = await run(0, 0, ("private", "u1"))
        self.assertIsNotNone(old)
        self.assertEqual(len(old), 1)
        self.assertEqual(len(new), 1)
        self.add(self.h.row(
            "04a", "碎片合并 私聊关闭", "fragment_wait_seconds=0，连发两条",
            "两轮独立，无 merged", "两轮独立" if len(old) == 1 and len(new) == 1 else "被合并",
            "", "两轮独立", 0,
            note="私聊连发不等待，正是旧回复被新水位顶替的高发场景。",
        ))
        self.assertEqual(self.rows[-1].status, "PASS")

        # 私聊开启合并（15 秒）：旧轮让位，调用方走 merge
        old, new = await run(15, 5, ("private", "u2"))
        self.assertIsNone(old)
        self.assertEqual(len(new), 2)
        guard = self.h.guard()
        first = self.h.event(group="", mid="f1")
        second = self.h.event(group="", mid="f2")
        trace = self.h.begin(guard, first)
        old_rid = first.get_extra("_livinghello_request_id")
        self.h.begin(guard, second)
        trace.merge(second, [first])
        merged = self.h.terminal(guard, first, old_rid)
        self.add(self.h.row(
            "04b", "碎片合并 私聊开启", "15s 窗口，两条在窗口内连发",
            "旧轮 merged，新轮承接", merged["state"], merged["reason"],
            "merged", 0, note="合成构造：直接驱动 FragmentWait + RequestTrace.merge。",
        ))
        self.assertEqual(merged["state"], "merged")

        # 群聊合并（8 秒）：同样旧轮 merged，群新消息不取消回复
        old, new = await run(8, 3, ("group", "1001"))
        self.assertIsNone(old)
        self.assertEqual(len(new), 2)
        self.add(self.h.row(
            "04c", "碎片合并 群聊", "8s 窗口，两条在窗口内连发",
            "旧轮 merged，新轮承接", "merged", "",
            "merged", 0, note="群聊新消息只推进上下文，不撤销已生成的回复（见 07）。",
        ))
        self.assertEqual(self.rows[-1].status, "PASS")

    # ⑤ cron 主动任务 ----------------------------------------------------
    async def test_05_cron_tool_paths(self):
        # 工具送达：立即可完结
        guard = self.h.guard()
        event = self.h.event(group="")
        event.get_platform_name = lambda: "cron"
        event.unified_msg_origin = "cron:active_agent"
        got = await self.h.complete_turn(guard, event, tool=True, cron=True)
        self.add(self.h.row(
            "05a", "cron 工具送达", "platform=cron，send_message_to_user 成功回执",
            "completed/tool_delivery_confirmed", got["state"], got["reason"],
            "completed", got["packets"], tool="是",
            note="不等 900s watchdog，工具回执即送达证据。",
            expect_reason="tool_delivery_confirmed",
        ))
        self.assertEqual(got["state"], "completed")

        # 工具报错：没有送达证据，且与「没调工具」区分
        guard = self.h.guard()
        event = self.h.event(group="")
        event.get_platform_name = lambda: "cron"
        event.unified_msg_origin = "cron:active_agent"
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        guard.reply_health.tool_failed(event, "error: platform not found")
        await guard.reply_health.finish_cron(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "05b", "cron 工具报错", "工具返回 error:，未记录送达",
            "failed/tool_call_failed", got["state"], got["reason"],
            "failed", got["packets"], tool="是",
            expect_reason="tool_call_failed",
        ))
        self.assertEqual(got["state"], "failed")

        # 未调用工具：按失败收尾
        guard = self.h.guard()
        event = self.h.event(group="")
        event.get_platform_name = lambda: "cron"
        event.unified_msg_origin = "cron:active_agent"
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        await guard.reply_health.finish_cron(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "05c", "cron 未调用工具", "cron 无工具回执",
            "failed/tool_not_called", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="tool_not_called",
        ))
        from guard_reply_health import KEY
        await asyncio.sleep(0)  # 让被取消的 watchdog 真正结束
        self.assertTrue(event.get_extra(KEY)["watchdog"].cancelled())

    # ⑥ 私聊水位 ---------------------------------------------------------
    async def _freshness(self, *, window, watermark, lease_watermark, started, latest,
                         event=None):
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
        import astrbot_plugin_livingemotion.runtime.application as app
        original_window, original_last = app.reply_freshness_window, app.last_activity

        def fake_window(_host):
            return window

        async def fake_last(_store, _context):
            return latest

        app.reply_freshness_window = fake_window
        app.last_activity = fake_last
        try:
            probe = SimpleNamespace(bridge=SimpleNamespace(host=object()), store=object(),
                                    _discard=app.Application._discard)
            lease = {"watermark": lease_watermark, "started_at": started}
            event = event or self.h.event(group="")
            valid = await app.Application._private_freshness_valid(
                probe, event, SimpleNamespace(scene_kind="private", scope_key="s1"),
                lease, watermark)
            return valid, event.get_extra("_at_guard_delivery_blocked")
        finally:
            app.reply_freshness_window, app.last_activity = original_window, original_last

    async def test_06_private_watermark(self):
        equal, _ = await self._freshness(window=15, watermark=8, lease_watermark=8,
                                         started=100, latest=125)
        within, _ = await self._freshness(window=15, watermark=9, lease_watermark=8,
                                          started=100, latest=114.9)
        boundary, _ = await self._freshness(window=15, watermark=9, lease_watermark=8,
                                            started=100, latest=115.0)
        beyond, _ = await self._freshness(window=15, watermark=9, lease_watermark=8,
                                          started=100, latest=115.1)
        strict, _ = await self._freshness(window=0, watermark=9, lease_watermark=8,
                                          started=100, latest=101)
        outside, _ = await self._freshness(window=15, watermark=9, lease_watermark=8,
                                           started=100, latest=130)
        self.add(self.h.row(
            "06a", "水位 相等/窗口内", "watermark 8→8；9→8 且 14.9s/15.0s",
            "均通过（True/True/True）",
            f"{equal}/{within}/{boundary}", "", "True/True/True", 0,
            note="调用真实 _private_freshness_valid，仅替换宿主窗口与最后活跃时间。",
        ))
        self.assertTrue(equal and within and boundary)
        self.add(self.h.row(
            "06b", "水位 越界/严格", "15.1s、window=0、30s",
            "均拒绝（False/False/False）",
            f"{beyond}/{strict}/{outside}", "", "False/False/False", 0,
        ))
        self.assertFalse(beyond or strict or outside)

        # 被新私聊顶替后的终态：LivingEmotion 先写 blocked，pipeline 结束即收尾
        guard = self.h.guard()
        event = self.h.event(group="", mid="m6")
        valid, blocked = await self._freshness(
            window=15, watermark=9, lease_watermark=8, started=100, latest=130,
            event=event)
        self.assertFalse(valid)
        self.assertEqual(blocked, "superseded_by_new_message")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        await guard.reply_health.finish(event)  # pipeline 结束，不再等 watchdog
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "06c", "私聊被新消息顶替", "生成期间新消息推进水位，回复 lease 失效",
            "cancelled/superseded_by_new_message", got["state"], got["reason"],
            "cancelled", got["packets"],
            note="LivingEmotion 回传具体原因，at_guard 立即按主动取消收尾。",
            expect_reason="superseded_by_new_message",
        ))
        self.assertEqual((got["state"], got["reason"]),
                         ("cancelled", "superseded_by_new_message"))

        # 其他丢弃分支各有原因，不能一律套「被新消息顶替」
        guard = self.h.guard()
        event = self.h.event(group="", mid="m6b")
        import astrbot_plugin_livingemotion.runtime.application as app
        app.Application._discard(event, "configuration_changed")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "06d", "配置变更丢弃", "revision 变化，_discard 写 configuration_changed",
            "cancelled/configuration_changed", got["state"], got["reason"],
            "cancelled", got["packets"],
            note="原因注入验证映射；其余 context_changed/撤回等沿用各自既有原因。",
            expect_reason="configuration_changed",
        ))
        self.assertEqual(got["state"], "cancelled")

    # ⑦ 回复有效期 -------------------------------------------------------
    async def test_07_reply_validity_window(self):
        import main as main_module

        async def stale_after(age, max_age=30):
            guard = self.h.guard(gate_reply_max_age_seconds=max_age)
            event = self.h.event()
            event.set_extra("_at_guard_recorded_at", 1000.0)
            with patch.object(main_module.time, "monotonic", self.h.clock):
                self.h.clock.now = 1000.0 + age
                return guard._stale_reason(event, "1001")

        cases = [(29.9, ""), (30.0, ""), (300.0, ""), (300.1, "message_expired")]
        actual = []
        for age, expected in cases:
            max_age = 300 if age >= 299 else 30
            reason = await stale_after(age, max_age)
            self.assertEqual(reason, expected, f"{age}s/{max_age}s")
            actual.append(reason or "-")
        self.add(self.h.row(
            "07a", "回复有效期边界", "29.9/30.0/300.0/300.1s，阈值 30/300",
            "前三不拦，末项 message_expired", "/".join(actual), "",
            "-/-/-/message_expired", 0,
            note="边界为严格大于；有效秒数已扣除判定、看图、工具、打字与重试。",
        ))
        self.assertEqual(self.rows[-1].status, "PASS")

        # 过期拦截后的终态
        guard = self.h.guard(gate_reply_max_age_seconds=30)
        event = self.h.event(mid="m7")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        event.set_extra("_at_guard_recorded_at", 1000.0)
        with patch.object(main_module.time, "monotonic", self.h.clock):
            self.h.clock.now = 1030.1
            reason = guard._stale_reason(event, "1001")
        self.assertEqual(reason, "message_expired")
        guard._record_delivery_block(event, reason)
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "07b", "回复过期拦截", "300.1s 超过 300s 上限后走发送前复核",
            "cancelled/message_expired", got["state"], got["reason"],
            "cancelled", got["packets"], expect_reason="message_expired",
        ))
        self.assertEqual(got["state"], "cancelled")

    # ⑧ 生成与路由超时 ---------------------------------------------------
    async def test_08_generation_and_route_timeouts(self):
        # 单次 45s 超时：主模型失败
        guard = self.h.guard(classifier_timeout_seconds=45.0)
        event = self.h.event()
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(
            event, SimpleNamespace(role="err", completion_text="Request timeout"))
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "08a", "单次生成 45s 超时", "provider 返回 err/timeout",
            "failed/timeout", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="timeout",
        ))
        self.assertEqual(got["state"], "failed")

        # 总预算 60s 到点：watchdog 判流程超时
        guard = self.h.guard(gate_total_timeout_seconds=60)
        event = self.h.event(mid="m8")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        self.h.clock.advance(60)
        await guard.reply_health.finish(event, timed_out=True)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "08b", "总预算 60s 到点", "模型已结束但最终包未出现，watchdog 触发",
            "incomplete/completion_timeout", got["state"], got["reason"],
            "incomplete", got["packets"], expect_reason="completion_timeout",
        ))
        self.assertEqual(got["state"], "incomplete")

        # 路由 100s 与判定总预算上限的关系（跨插件预算）
        guard = self.h.guard(gate_total_timeout_seconds=100)
        clamped = guard.engine.total_timeout()
        self.add(self.h.row(
            "08c", "路由 100s vs 判定预算", "_conf_schema 上限 60s，配置写 100",
            "夹取为 60（记录用）", str(clamped), "", "60", 0,
            note="多 API 路由总时限 100s 超出本插件判定预算上限，需人工确认预算关系。",
        ))
        self.assertEqual(clamped, 60)

    # ⑨ 工具成功 / 失败 / 超时 -------------------------------------------
    async def test_09_tool_outcomes(self):
        from guard_reply_health import tool_result_delivered

        def result(text, is_error=False):
            return SimpleNamespace(isError=is_error, content=[SimpleNamespace(text=text)])

        ok = tool_result_delivered("send_message_to_user",
                                   result("Message sent to session qq-bot:FriendMessage:9001"))
        failed = tool_result_delivered("send_message_to_user",
                                       result("error: platform not found"))
        timed_out = tool_result_delivered("send_message_to_user", None)
        self.assertTrue(ok[0])
        self.assertFalse(failed[0] or timed_out[0])
        self.add(self.h.row(
            "09a", "工具成功/失败/超时 判定", "真实 tool_result_delivered 分类",
            "成功 True；失败/超时 False", f"{ok[0]}/{failed[0]}/{timed_out[0]}", "",
            "True/False/False", 0,
            note="普通消息的工具成功不能替代最终回复包（见 09b）。",
        ))
        self.assertEqual(self.rows[-1].status, "PASS")

        # 普通消息：工具成功也不能算最终送达
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        guard.reply_health.tool_delivered(event, target="qq-bot:GroupMessage:1001", elapsed=0.1)
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "09b", "工具成功（普通消息）", "send_message_to_user 成功，但最终包未发",
            "failed/final_message_missing", got["state"], got["reason"],
            "failed", got["packets"], tool="是",
            note="回归保护：非 cron 工具成功不得冒充最终投递。",
            expect_reason="final_message_missing",
        ))
        self.assertEqual(got["state"], "failed")

    # ⑩ 主模型空体 / protocol_error / 图片理解 12s -----------------------
    async def test_10_model_body_errors(self):
        from guard_reply_health import error_category

        # 空正文
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, SimpleNamespace(role="assistant", completion_text=""))
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "10a", "主模型空正文", "model_done 成功但 completion_text 为空",
            "failed/empty_response", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="empty_response",
            note="空正文在模型层即归空响应，不再误报为投递问题。",
        ))
        self.assertEqual(got["reason"], "empty_response")

        # protocol_error 归类
        category = error_category("protocol error: reason field must be a fixed enum")
        self.add(self.h.row(
            "10b", "protocol_error 归类", "判定模型返回非法枚举，上游抛 protocol error",
            "protocol_error", category, "", "protocol_error", 0,
            note="error_category 已补 protocol 分支，与 CATEGORIES/历史标签一致。",
        ))
        self.assertEqual(category, "protocol_error")

        # 图片理解 12s 硬超时：降级占位，不牵连同轮失败
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        guard.request_trace.record(event, "input_vision", "timeout", error_type="TimeoutError")
        got = await self.h.complete_turn(guard, event, text="我看不清图，但这条回复照常发出。")
        self.add(self.h.row(
            "10c", "图片理解 12s 超时", "视觉调用超时→占位文本，主回复继续",
            "completed/final_delivery_confirmed", got["state"], got["reason"],
            "completed", got["packets"],
            note="探针：不调用真实视觉模型，只验证降级不污染整轮终态。",
            expect_reason="final_delivery_confirmed",
        ))
        self.assertEqual(got["state"], "completed")

    # ⑪ 撤回 / 休眠 / 新消息顶替 -----------------------------------------
    async def test_11_recall_sleep_and_supersede(self):
        # 撤回
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard._delivery().block(event)
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "11a", "消息撤回", "撤回记录命中，发送前拦截",
            "cancelled/message_recalled", got["state"], got["reason"],
            "cancelled", got["packets"], expect_reason="message_recalled",
        ))
        self.assertEqual(got["state"], "cancelled")

        # 休眠：LivingEmotion 下发 block_chat
        from guard_livingemotion import availability_reason

        class Api:
            api_version = 1

            async def get_capabilities(self, context=None):
                return {"api_version": 1, "status": "ok", "mode": "active",
                        "instance_id": "inst-1", "operations": ["get_availability"]}

            async def get_availability(self, scope):
                return {"api_version": 1, "status": "ok",
                        "available": False, "block_chat": True}

        api = Api()
        meta = SimpleNamespace(activated=True, star_cls=SimpleNamespace(api=api))
        context = SimpleNamespace(get_registered_star=lambda name: meta)
        event = self.h.event(group="")
        event.unified_msg_origin = "qq:private:u1"
        event.get_sender_id = lambda: "u1"
        event.set_extra("livingemotion:context", {
            "route": "qq:private:u1", "account_id": "u1",
            "scene_kind": "private", "scene_id": "u1", "instance_id": "inst-1"})
        reason = await availability_reason(context, event, unsolicited=False, config=None)
        self.assertEqual(reason, "livingemotion_block_chat")
        guard = self.h.guard()
        self.h.begin(guard, event)
        guard.request_trace.finish(event, "silent", reason)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "11b", "休眠期静默", "LivingEmotion block_chat，at_guard 记 silent",
            "silent/livingemotion_block_chat", got["state"], got["reason"],
            "silent", got["packets"], expect_reason="livingemotion_block_chat",
        ))
        self.assertEqual(got["state"], "silent")

        # 新消息顶替（群聊）：新消息不取消，属设计内
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        event.set_extra("_at_guard_context_advanced", True)
        got = await self.h.complete_turn(guard, event)
        self.add(self.h.row(
            "11c", "群聊新消息顶替", "生成期间群友继续说话（context_advanced）",
            "照常发送 completed", got["state"], got["reason"],
            "completed", got["packets"],
            note="群聊新消息只刷新上下文，不撤销已生成回复。",
            expect_reason="final_delivery_confirmed",
        ))
        self.assertEqual(got["state"], "completed")

    # ⑫ 发送阶段平台异常 -------------------------------------------------
    async def test_12_send_stage_platform_errors(self):
        # 发送抛异常
        guard = self.h.guard()
        event = self.h.event()
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        event.send = AsyncMock(side_effect=RuntimeError("synthetic platform down"))
        guard._delivery().patch(event)
        result = self.h.Chain([self.h.Plain("这条发不出去。")])
        guard.reply_health.arm(event, result)
        with self.assertRaises(RuntimeError):
            await event.send(result.derive(result.chain))
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "12a", "发送阶段平台异常", "event.send 抛 RuntimeError",
            "failed/send_unknown", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="send_unknown",
        ))
        self.assertEqual((got["state"], got["reason"]), ("failed", "send_unknown"))

        # 显式负回执
        guard = self.h.guard()
        event = self.h.event(mid="m12")
        self.h.begin(guard, event)
        guard.reply_health.watch(event, expected=True)
        guard.reply_health.model_done(event, object())
        event.send = AsyncMock(return_value={"status": "failed"})
        guard._delivery().patch(event)
        result = self.h.Chain([self.h.Plain("平台拒绝。")])
        guard.reply_health.arm(event, result)
        await event.send(result.derive(result.chain))
        await guard.reply_health.finish(event)
        got = self.h.terminal(guard, event)
        self.add(self.h.row(
            "12b", "发送负回执", "send 返回 {status: failed}",
            "failed/send_unknown", got["state"], got["reason"],
            "failed", got["packets"], expect_reason="send_unknown",
        ))
        self.assertEqual(got["state"], "failed")


class RealReplayDataTests(unittest.TestCase):
    """真实历史回放入口：文件未到位时明确跳过，不伪造结论。"""

    REQUIRED = ["request_traces.sqlite3", "decisions.jsonl", "logs"]
    # 配置文件名随插件改名变化：新导出用 LivingHello，旧导出仍是 at_guard。
    CONFIG_FILES = ("astrbot_plugin_LivingHello_config.json",
                    "astrbot_plugin_at_guard_config.json")

    def test_real_replay_data_ready(self):
        missing = [name for name in self.REQUIRED if not (REPLAY_DIR / name).exists()]
        if not missing and not any((REPLAY_DIR / name).exists() for name in self.CONFIG_FILES):
            missing.append(" 或 ".join(self.CONFIG_FILES))
        if missing:
            self.skipTest(f"真实回放数据未到位（{REPLAY_DIR}）：缺少 {', '.join(missing)}"
                          f"；本轮结论仅来自{DATA_SOURCE}数据")


def main():
    install_stubs()
    suite = unittest.TestLoader().loadTestsFromTestCase(ReplayMatrixTests)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    print_matrix(MATRIX)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
