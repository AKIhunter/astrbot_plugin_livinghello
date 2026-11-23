import path from 'node:path';
import z from '@deepseek-ai/schemastery';
import { WorkerClient } from './worker-client.mjs';

export const name = 'dsh-livinghello';
export const inject = ['agents', 'llm'];
export const Config = z.object({
  enabled: z.boolean().default(true).description('开启基础准入。配置在本插件；关闭后所有步骤交给 DSH。'),
  python: z.string().default('python3').description('本机 Python 3.11+ 可执行文件。'),
  dataDir: z.string().default('.dsh/livinghello').description('DSH 独立目录，不使用 AstrBot 配置。'),
  classifierEnabled: z.boolean().default(false).description('开启后在本插件填写 gateProvider / gateModel；会增加判定模型消费。关闭只走规则。'),
  gateProvider: z.string().default('').description('DSH 已登记的判定 Provider ID。'),
  gateModel: z.string().default('').description('判定模型 ID；不会改变主回答模型。'),
  minLength: z.number().min(0).max(20).step(1).default(2).description('最短有效文字长度，媒体输入保留。'),
  threshold: z.number().min(0).max(100).step(1).default(65).description('分类参与阈值。'),
  timeoutSeconds: z.number().min(1).max(15).default(3.5).description('分类超时秒数；失败关闭本次准入。'),
});
export function normalizeConfig(input = {}) {
  const config = { enabled: true, python: 'python3', dataDir: '.dsh/livinghello',
    classifierEnabled: false, gateProvider: '', gateModel: '',
    minLength: 2, threshold: 65, timeoutSeconds: 3.5, ...input };
  for (const key of ['enabled', 'classifierEnabled'])
    if (typeof config[key] !== 'boolean') throw Error(`invalid_${key}`);
  for (const key of ['python', 'dataDir', 'gateProvider', 'gateModel'])
    if (typeof config[key] !== 'string') throw Error(`invalid_${key}`);
  if (!config.python.trim() || !config.dataDir.trim()) throw Error('invalid_runtime_path');
  for (const [key, low, high, integer] of [
    ['minLength', 0, 20, true], ['threshold', 0, 100, true], ['timeoutSeconds', 1, 15, false]]) {
    if (!Number.isFinite(config[key]) || config[key] < low || config[key] > high
      || (integer && !Number.isInteger(config[key]))) throw Error(`invalid_${key}`);
  }
  if (config.classifierEnabled && (!config.gateProvider.trim() || !config.gateModel.trim()))
    throw Error('classifier_provider_required');
  config.dataDir = path.resolve(config.dataDir);
  return config;
}

export async function apply(ctx, input = {}) {
  const config = normalizeConfig(input);
  if (!config.enabled) return;
  const logger = ctx.logger(name);
  if (config.classifierEnabled && !ctx.llm?.stream) throw Error('llm_required');
  const controllers = new Set();
  const worker = new WorkerClient(config.python, config.dataDir, async (args, signal) => {
    const controller = new AbortController();
    controllers.add(controller);
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, config.timeoutSeconds * 1000);
    let text = '';
    let finished = false;
    try {
      if (signal?.aborted) throw Error('canceled');
      for await (const chunk of ctx.llm.stream({
        provider: config.gateProvider, model: config.gateModel,
        messages: [{ role: 'user', content: [{ type: 'text', text: args.prompt }] }],
        system: args.system, maxTokens: Math.min(1024, args.maxTokens || 512),
        signal: controller.signal,
      })) {
        if (chunk.type === 'text-delta') text += chunk.text;
        if (chunk.type === 'error' || (chunk.type === 'finish' && ['error', 'aborted'].includes(chunk.reason)))
          throw Error('classifier_failed');
        if (chunk.type === 'finish') finished = true;
        if (text.length > 16000) throw Error('classifier_too_large');
      }
      if (!finished || controller.signal.aborted) throw Error('classifier_incomplete');
      return text;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      controllers.delete(controller);
    }
  }, config);
  let disposed = false;
  ctx.effect(() => async () => {
    disposed = true;
    for (const controller of controllers) controller.abort();
    await worker.stop();
  });
  await worker.call('ping');
  const history = new Map();
  const cache = new Map();
  ctx.on('agent/disposed', ({ agent }) => {
    history.delete(agent.id);
    cache.delete(agent.id);
  });
  ctx.on('agent/pre-step', async (payload, next) => {
    const downstream = await next();
    if (downstream.kind !== 'enter') return downstream;
    if (disposed || payload.signal?.aborted) return { kind: 'reject' };
    const messages = (downstream.messages ?? []).filter(message =>
      message?.role === 'user' && message.source?.kind === 'user');
    if (!messages.length) return downstream; // Tools and internal continuations retain their admission.
    const text = messages.map(message => (message.content ?? [])
      .filter(block => block.type === 'text').map(block => block.text).join('\n')).join('\n');
    const hasMedia = messages.some(message => (message.content ?? []).some(block => block.type !== 'text'));
    const before = history.get(payload.agent.id) ?? [];
    // Alpha text classifier cannot claim to understand attachments. DSH keeps original blocks.
    if (hasMedia) {
      history.set(payload.agent.id, [...before, text.slice(0, 800)].slice(-20));
      return downstream;
    }
    const key = JSON.stringify(messages.map(message => [message.id, message.content]));
    const sessionCache = cache.get(payload.agent.id) ?? new Map();
    cache.set(payload.agent.id, sessionCache);
    let pending = sessionCache.get(key);
    if (!pending) {
      pending = worker.call('decide', {
        scope: payload.agent.id, text: text.slice(0, 32000), hasMedia, history: before,
        isPrivate: true, classifierEnabled: config.classifierEnabled, model: config.gateModel,
      }, payload.signal, (config.timeoutSeconds + 2) * 1000);
      sessionCache.set(key, pending);
      if (sessionCache.size > 256) sessionCache.delete(sessionCache.keys().next().value);
    }
    let decision;
    try {
      decision = await pending;
    } catch {
      logger.warn('LH admission unavailable');
      return { kind: 'reject' };
    }
    if (!disposed && !payload.signal?.aborted)
      history.set(payload.agent.id, [...before, text.slice(0, 800)].slice(-20));
    logger.debug(`LH ${payload.agent.id} ${decision.action} ${decision.reason}`);
    return decision.action === 'reply' && !payload.signal?.aborted && !disposed
      ? downstream : { kind: 'reject' };
  });
}
