"""首次调用、垫场回环和取消终态的隔离回归；不调用真实模型或平台。"""
import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import test_reply_fallback as fixtures

class RetryContextFixTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ReplyFallbackTests.asyncSetUp
    asyncTearDown = fixtures.ReplyFallbackTests.asyncTearDown
    make_event = fixtures.ReplyFallbackTests.make_event
    run_model = fixtures.ReplyFallbackTests.run_model
    deliver = fixtures.ReplyFallbackTests.deliver

    def judged(self):
        self.event.set_extra('_at_guard_fragment_judged', True)
        self.event.set_extra('_at_guard_judged_revision', 0)
        self.guard.context_revision['1001'] = 0
        self.guard._trace(self.event)

    async def test_first_call_and_delivery_survive_new_group_message(self):
        self.judged()
        self.guard.context_revision['1001'] = 1
        runner, result = await self.run_model([fixtures.response()])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(result), 1)
        await self.deliver(result[0])
        self.assertEqual(len(self.event.sent), 1)

    async def test_fresh_tool_step_does_not_inherit_previous_retry_context_check(self):
        self.judged()
        runner, result = await self.run_model([TimeoutError(), fixtures.response()])
        self.assertEqual(result[-1].role, 'assistant')
        self.guard.context_revision['1001'] += 1
        runner.outcomes = [fixtures.response('工具后的正常回答')]
        result = [item async for item in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(result[-1].completion_text, '工具后的正常回答')
        self.assertEqual(len(runner.calls), 3)

    async def test_failed_retry_context_cancel_is_single_terminal_transition(self):
        self.judged()
        runner = fixtures.Runner([TimeoutError(), fixtures.response()])
        self.retry.bind(self.event, runner)

        async def changed(_):
            self.guard.context_revision['1001'] += 1
            await asyncio.sleep(0)
        self.retry.sleep = changed
        result = [item async for item in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(result, [])
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(runner._is_stop_requested())
        again = [item async for item in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(again, [])
        row = await self.guard._trace().get(self.event.get_extra('_livinghello_request_id'))
        cancels = [e for e in row['events'] if e['feature'] == 'reply_retry' and e['status'] == 'cancelled']
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]['reason'], 'context_changed')
        self.assertEqual((cancels[0]['judged'], cancels[0]['before'], cancels[0]['after'], cancels[0]['count']), (0, 0, 1, 0))
        await self.health.finish(self.event)
        self.guard.context.send_message.assert_not_called()

    async def test_first_call_keeps_recall_policy_and_expiry_protection(self):
        for reason in ('message_recalled', 'blacklist_user', 'message_expired'):
            with self.subTest(reason=reason):
                self.event = self.make_event()
                self.judged()
                if reason == 'message_recalled':
                    self.event.set_extra('_at_guard_source_recalled', True)
                elif reason == 'blacklist_user':
                    self.event.set_extra('_livinghello_policy_send_check', lambda: reason)
                else:
                    import time
                    self.event.set_extra('_at_guard_recorded_at', time.monotonic() - 100)
                runner, result = await self.run_model([fixtures.response()])
                self.assertEqual((runner.calls, result), ([], []))
                self.assertEqual(self.event.get_extra('_livinghello_reply_retry')['reason'], reason)

    async def test_actual_host_step_terminates_after_cancel(self):
        root = os.environ.get('ASTRBOT_HOST_SOURCE')
        if not root:
            self.skipTest('需要线上宿主源码快照')
        path = Path(root) / 'astrbot/core/agent/runners/tool_loop_agent_runner.py'
        tree = ast.parse(path.read_text())
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ToolLoopAgentRunner'))
        cls.bases, cls.decorator_list = ([], [])
        cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {'step', 'request_stop', '_is_stop_requested', 'done'}]
        namespace = {'override': lambda f: f, 'AgentState': SimpleNamespace(IDLE='idle', RUNNING='running', DONE='done', ERROR='error'), 'logger': self.guard.logger}
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        native = namespace['ToolLoopAgentRunner']()
        fixture = fixtures.Runner([TimeoutError(), fixtures.response()])
        native.__dict__.update(fixture.__dict__)
        native._iter_llm_responses = fixture._iter_llm_responses
        native._iter_llm_responses_with_fallback = fixture._iter_llm_responses_with_fallback
        native._sanitize_malformed_tool_calls = lambda _: None
        native._abort_signal = asyncio.Event()
        native._state, native._step_budget_max = ('idle', None)
        native.req = SimpleNamespace(conversation=None)
        native._transition_state = lambda state: setattr(native, '_state', state)
        native._simple_print_message_role = lambda *_: None
        native.agent_hooks = SimpleNamespace(on_agent_begin=AsyncMock())
        native.request_context_manager = SimpleNamespace(process=AsyncMock(return_value=native.run_context.messages))
        native._await_or_stop = lambda operation: operation

        async def finalize():
            native._state = 'done'
            return 'aborted-fixture'
        native._finalize_aborted_step = AsyncMock(side_effect=finalize)
        self.judged()
        self.retry.bind(self.event, native)

        async def changed(_):
            self.guard.context_revision['1001'] += 1
            await asyncio.sleep(0)
        self.retry.sleep = changed
        steps = 0
        while not native.done() and steps < 3:
            steps += 1
            async for _ in native.step():
                pass
        self.assertTrue(native.done())
        self.assertEqual(steps, 1)
        native._finalize_aborted_step.assert_awaited_once()
        self.assertEqual(len(fixture.calls), 1)
