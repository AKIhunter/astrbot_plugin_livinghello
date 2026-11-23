import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

// One worker per plugin scope. JSON lines carry IDs; stdout never carries logs.
export class WorkerClient {
  constructor(python, dataDir, callback = async () => { throw Error('unsupported_callback'); }, config = {}) {
    this.child = spawn(python, ['-B', fileURLToPath(new URL('./worker.py', import.meta.url)), dataDir, JSON.stringify(config)],
      { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
    this.pending = new Map();
    this.nextId = 0;
    this.closed = false;
    this.callback = callback;
    this.child.stderr.on('data', () => {}); // Do not expose user text or exception payloads.
    this.child.on('error', () => this.fail('worker_start_failed'));
    this.child.on('exit', () => this.fail('worker_exited'));
    this.child.stdin.on('error', () => this.fail('worker_pipe_failed'));
    this.lines = createInterface({ input: this.child.stdout });
    this.lines.on('line', line => {
      let message;
      try { message = JSON.parse(line); } catch { this.fail('worker_protocol'); return; }
      if (message.callback) {
        const request = this.pending.get(message.parent);
        if (!request) return;
        Promise.resolve().then(() => this.callback(message.args, request.signal)).then(
          result => this.write({ callbackResult: message.id, result }),
          () => this.write({ callbackResult: message.id, error: 'model_failed' }));
        return;
      }
      const request = this.pending.get(message.id);
      if (!request) return;
      this.pending.delete(message.id);
      request.cleanup();
      if (message.error) request.reject(Error(message.error));
      else request.resolve(message.result);
    });
  }
  write(message) {
    if (!this.closed && !this.child.stdin.destroyed)
      this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }
  fail(code) {
    this.closed = true;
    for (const request of this.pending.values()) {
      request.cleanup();
      request.reject(Error(code));
    }
    this.pending.clear();
  }
  call(op, args = {}, signal, timeout = 20000) {
    if (this.closed || signal?.aborted) return Promise.reject(Error('worker_unavailable'));
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const abort = () => {
        const request = this.pending.get(id);
        if (!request) return;
        this.pending.delete(id);
        request.cleanup();
        this.write({ cancel: id });
        reject(Error('worker_canceled'));
      };
      const timer = setTimeout(abort, timeout);
      this.pending.set(id, { resolve, reject, signal,
        cleanup: () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); } });
      signal?.addEventListener('abort', abort, { once: true });
      this.write({ id, op, args });
    });
  }
  async stop() {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    this.fail('worker_disposed');
    this.lines.close();
    this.child.stdin.end();
    await new Promise(resolve => {
      const timer = setTimeout(() => this.child.kill('SIGKILL'), 1500);
      this.child.once('exit', () => { clearTimeout(timer); resolve(); });
      this.child.kill('SIGTERM');
    });
  }
}
