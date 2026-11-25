import test from "node:test";
import assert from "node:assert/strict";
import { bindDiagnosticExport } from "../pages/guard/diagnostics.mjs";

function controls(bridge) {
  const button = { disabled: false, textContent: "", dataset: {}, attributes: new Map(),
    addEventListener(_, action) { this.click = action; },
    setAttribute(key, value) { this.attributes.set(key, value); },
    removeAttribute(key) { this.attributes.delete(key); } };
  const status = { textContent: "", dataset: {} };
  bindDiagnosticExport(button, status, () => bridge);
  return { button, status };
}
test("native binary download, duplicate click prevention and recovery", async () => {
  let resolve, calls = 0;
  const { button, status } = controls({ download(endpoint, params) {
    assert.equal(endpoint, "console/diagnostics/export");
    assert.deepEqual(params, {});
    calls++;
    return new Promise(done => { resolve = done; });
  } });
  const first = button.click();
  await Promise.resolve();
  await button.click();
  assert.equal(calls, 1);
  assert.equal(button.disabled, true);
  assert.equal(button.dataset.i18n, "正在打包…");
  resolve({ filename: "diagnostics.zip" });
  await first;
  assert.equal(button.disabled, false);
  assert.equal(button.dataset.i18n, "导出诊断日志（近3天）");
  assert.equal(button.attributes.has("aria-busy"), false);
  assert.match(status.textContent, /已发起/);
});
for (const [error, expected] of [["401 unauthorized", /登录/], ["403 forbidden", /权限/],
  ["409 busy", /稍后重试/], ["storage failed", /导出失败/]]) {
  test(`failure ${error} restores button`, async () => {
    const { button, status } = controls({ download: async () => { throw new Error(error); } });
    await button.click();
    assert.equal(button.disabled, false);
    assert.match(status.textContent, expected);
  });
}
test("old host shows unsupported download without starting a task", async () => {
  const { button, status } = controls({});
  await button.click();
  assert.match(status.textContent, /不支持/);
  assert.equal(button.disabled, false);
});
test("timeout warns about uncertainty and restores the button", async () => {
  const original = globalThis.setTimeout;
  globalThis.setTimeout = action => original(action, 0);
  try {
    const { button, status } = controls({ download: () => new Promise(() => {}) });
    await button.click();
    assert.match(status.textContent, /尚未确认/);
    assert.equal(button.disabled, false);
  } finally {
    globalThis.setTimeout = original;
  }
});
