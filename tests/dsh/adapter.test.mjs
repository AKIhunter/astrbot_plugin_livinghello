import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import { AgentRegistry, agentEvents } from '@deepseek-ai/dsh-agent';
import { LlmRuntime, LlmAdapter } from '@deepseek-ai/dsh-llm';
import { createScope } from '@deepseek-ai/dsh-scope';
import * as LH from '../../dsh/index.mjs';
import { WorkerClient } from '../../dsh/worker-client.mjs';

const reply = '{"act":"reply","conf":90,"ground":"none","understood":true,"reason":"directed"}';
const silence = '{"act":"silent","conf":10,"ground":"none","understood":true,"reason":"no_value"}';
const message = (id, text, extra = []) => ({
  id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }, ...extra],
});
async function fixture(t, input = {}, responses = [reply]) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'lh-dsh-test-'));
  const ctx = new Context();
  const registry = await ctx.plugin(AgentRegistry);
  const llm = await ctx.plugin(LlmRuntime);
  const owner = ctx.plugin({ inject: ['agents'], apply() {} });
  await owner.await();
  let calls = 0;
  const requests = [];
  class FakeAdapter extends LlmAdapter {
    async *stream(options) {
      requests.push(options);
      const response = responses[Math.min(calls++, responses.length - 1)];
      if (response === 'timeout') {
        await new Promise(resolve => options.signal.addEventListener('abort', resolve, { once: true }));
        yield { type: 'finish', reason: 'aborted' };
        return;
      }
      if (response === 'failure') throw Error('synthetic_failure');
      yield { type: 'block-start', index: 0, blockType: 'text' };
      yield { type: 'text-delta', index: 0, text: response };
      yield { type: 'block-end', index: 0, block: { type: 'text', text: response } };
      yield { type: 'finish', reason: 'stop' };
    }
  }
  const remove = ctx.llm.registerAdapter(['fake'], new FakeAdapter());
  const config = { dataDir: directory, ...input };
  const plugin = await ctx.plugin(LH, config);
  const scopes = [];
  t.after(async () => {
    await plugin.dispose();
    for (const scope of scopes) await scope.dispose();
    await owner.dispose();
    remove();
    await llm.dispose();
    await registry.dispose();
    await rm(directory, { recursive: true, force: true });
  });
  await plugin.await();
  assert.equal(plugin.state, 2);
  const agent = async id => {
    const value = { id, session: { id } };
    const scope = createScope(owner.ctx, value);
    value.ctx = scope.ctx;
    scopes.push(scope);
    await ctx.agents.register(value);
    return value;
  };
  const step = (agent, messages, next = async () => ({ kind: 'enter', messages }), signal) =>
    agentEvents(ctx, agent).waterfall('agent/pre-step', {
      messages, turn: 1, step: 1, signal: signal ?? new AbortController().signal,
    }, next);
  return { ctx, plugin, agent, step, directory, requests, calls: () => calls };
}

test('real Cordis + original GateEngine; basic rejects junk without model cost', async t => {
  const f = await fixture(t);
  const a = await f.agent('a');
  assert.equal((await f.step(a, [message('blank', '')])).kind, 'reject');
  assert.equal((await f.step(a, [message('short', 'a')])).kind, 'reject');
  assert.equal((await f.step(a, [message('noise', '!!!')])).kind, 'reject');
  const messages = [message('valid', '你好')];
  assert.strictEqual((await f.step(a, messages)).messages, messages);
  assert.equal(f.calls(), 0);
});

test('real LlmRuntime callbacks; classifier allow/block and cached replay', async t => {
  const f = await fixture(t, { classifierEnabled: true, gateProvider: 'fake', gateModel: 'offline' },
    [reply, silence]);
  const a = await f.agent('a');
  const allow = [message('yes', '请回答我')];
  assert.equal((await f.step(a, allow)).kind, 'enter');
  assert.equal((await f.step(a, allow)).kind, 'enter');
  assert.equal(f.calls(), 1);
  assert.equal((await f.step(a, [message('no', '结束了')])).kind, 'reject');
  assert.equal(f.calls(), 2);
  assert.equal(f.requests[0].provider, 'fake');
  assert.equal(f.requests[0].model, 'offline');
});

test('downstream replacement/rejection; tools; attachments keep original objects', async t => {
  const f = await fixture(t, { classifierEnabled: true, gateProvider: 'fake', gateModel: 'offline' });
  const a = await f.agent('a');
  const rejected = { kind: 'reject' };
  assert.strictEqual(await f.step(a, [message('1', 'hello')], async () => rejected), rejected);
  assert.equal(f.calls(), 0);
  const internal = [{ ...message('tool', ''), source: { kind: 'model' } }];
  assert.equal((await f.step(a, internal)).kind, 'enter');
  const media = [message('image', '', [{ type: 'image', image: { ref: 'synthetic' } }])];
  assert.strictEqual((await f.step(a, media)).messages, media);
  assert.equal(f.calls(), 0);
  const replacement = { kind: 'enter', messages: [message('replace', '有意义的文字')], startsRequestSeries: true };
  assert.strictEqual(await f.step(a, [], async () => replacement), replacement);
});

test('parallel sessions isolate history and callback IDs', async t => {
  const f = await fixture(t, { classifierEnabled: true, gateProvider: 'fake', gateModel: 'offline' });
  const a = await f.agent('a');
  const b = await f.agent('b');
  await Promise.all([f.step(a, [message('a1', 'alpha-only')]), f.step(b, [message('b1', 'beta-only')])]);
  await f.step(a, [message('a2', 'alpha-next')]);
  assert.ok(!JSON.stringify(f.requests.at(-1)).includes('beta-only'));
  assert.equal(f.calls(), 3);
});

for (const failure of ['failure', 'not a decision', 'timeout']) {
  test(`classifier ${failure} fails closed and bounds retries`, async t => {
    const f = await fixture(t, { classifierEnabled: true, gateProvider: 'fake', gateModel: 'offline',
      timeoutSeconds: 1 }, [failure]);
    const a = await f.agent('a');
    assert.equal((await f.step(a, [message('1', '请回答')])).kind, 'reject');
    assert.ok(f.calls() >= 1 && f.calls() <= 2);
  });
}

test('cancel and worker shutdown settle requests and release PID', async t => {
  const f = await fixture(t, { classifierEnabled: true, gateProvider: 'fake', gateModel: 'offline',
    timeoutSeconds: 1 }, ['timeout']);
  const a = await f.agent('a');
  const controller = new AbortController();
  const running = f.step(a, [message('c', '请回答')], undefined, controller.signal);
  controller.abort();
  assert.equal((await running).kind, 'reject');
  const worker = new WorkerClient('python3', f.directory, undefined, LH.normalizeConfig());
  await worker.call('ping');
  const pid = worker.child.pid;
  await worker.stop();
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
});

test('config schema, runtime ranges and classifier dependencies agree', () => {
  assert.equal(LH.normalizeConfig(LH.Config({})).minLength, 2);
  for (const config of [{ threshold: 101 }, { minLength: -1 }, { timeoutSeconds: 0 },
    { classifierEnabled: true }, { enabled: 'true' }, { minLength: 1.5 }])
    assert.throws(() => LH.normalizeConfig(config));
});
