// 真实页面与真实配置处理器，延迟/失败由隔离 bridge 注入；禁止外部请求。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fileURLToPath(
  new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url)));
const { server, url } = await startServer();
const browser = await chromium.launch({ headless: true });
const report = { scope: "隔离 fixture；真实页面/配置处理器；不调用真实模型", cases: [],
  timings: [], pageErrors: [], externalRequests: [], maxPendingRefresh: 0 };

async function open() {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on("pageerror", error => report.pageErrors.push(error.message));
  await page.route("**/*", route => {
    if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
    report.externalRequests.push(route.request().url());
    return route.abort();
  });
  await page.addInitScript(() => {
    const control = window.__saveTest = {
      holdGet: new Set(), holdPost: new Set(), failPost: new Set(), pending: [], ackAt: 0,
      samples: [], configCalls: 0, nextId: 0,
      release(id, error = "") {
        const item = this.pending.find(value => value.id === id);
        this.pending = this.pending.filter(value => value.id !== id);
        if (error) item.reject(new Error(error)); else item.resolve(item.result);
      },
    };
    let bridge;
    Object.defineProperty(window, "AstrBotPluginView", {
      get: () => bridge,
      set(value) {
        const get = value.apiGet.bind(value), post = value.apiPost.bind(value);
        const defer = (path, kind, result) => new Promise((resolve, reject) => {
          control.pending.push({ id: ++control.nextId, path, kind, result, resolve, reject });
        });
        bridge = {
          ...value,
          async apiGet(path, params) {
            const result = await get(path, params);
            return control.holdGet.has(path) ? defer(path, "get", result) : result;
          },
          async apiPost(path, payload) {
            if (path === "console/config") control.configCalls++;
            if (control.failPost.has(path)) throw new Error("合成写入失败");
            if (control.holdPost.has(path)) await defer(path, "post", null);
            const result = await post(path, payload);
            if (path === "console/config" && result.status === "ok") control.ackAt = performance.now();
            return result;
          },
        };
      },
    });
    document.addEventListener("DOMContentLoaded", () => {
      const node = document.getElementById("saveMsg");
      new MutationObserver(() => {
        if (node.className === "msg ok" && control.ackAt) {
          control.samples.push(performance.now() - control.ackAt);
          control.ackAt = 0;
        }
      }).observe(node, { childList: true, attributes: true, subtree: true });
    });
  });
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.getElementById("ver").textContent.includes("fixture")
    && document.getElementById("nativeStatus").textContent.includes("已读取")
    && !document.getElementById("nativeScope").disabled);
  await page.locator('.page-tabs [data-page="settings"]').click();
  return { page, context };
}
async function holdRefresh(page) {
  await page.evaluate(() => ["console/state", "console/flow"].forEach(path => __saveTest.holdGet.add(path)));
}
async function success(page) {
  await page.locator("#saveMsg.msg.ok").filter({ hasText: "回复参数已保存" }).waitFor();
  assert.equal(await page.locator("#saveBarSave").isEnabled(), true);
}
async function pending(page, count) {
  await page.waitForFunction(n => __saveTest.pending.filter(item => item.kind === "get").length === n, count);
  report.maxPendingRefresh = Math.max(report.maxPendingRefresh, count);
}
async function releaseAll(page, error = "") {
  await page.evaluate(error => [...__saveTest.pending].forEach(item => __saveTest.release(item.id, error)), error);
}
async function editSegments(page) {
  await page.locator('.page-tabs [data-page="reply"]').click();
  const nativeTab = page.locator('#replySubTabs [data-sub="native"]');
  if (await nativeTab.count()) await nativeTab.click();
  if (!await page.locator("#native-enable").isVisible()) {
    await page.locator("#nativeSegments summary").filter({hasText:"原生分段回复"}).click();
  }
  await page.locator("#native-enable").uncheck();
  await page.locator('.page-tabs [data-page="settings"]').click();
}
async function run(name, task) {
  const { page, context } = await open();
  try {
    await task(page);
    report.cases.push({ name, passed: true });
    console.log(`通过：${name}`);
  } catch (error) {
    report.cases.push({ name, passed: false, error: error.stack });
    console.error(`失败：${name}：${error.message}`);
  } finally {
    await context.close();
  }
}
try {
  await run("状态和流程无限等待时先显示成功；12次回执到提示延迟", async page => {
    await holdRefresh(page);
    for (let index = 0; index < 12; index++) {
      await page.locator("#field-classifier_timeout_seconds").fill(index % 2 ? "5" : "4");
      await page.locator("#saveBarSave").click();
      await success(page);
      await pending(page, 2);
      await releaseAll(page);
      await page.waitForFunction(() => __saveTest.samples.length === __saveTest.configCalls);
    }
    report.timings = await page.evaluate(() => __saveTest.samples);
    const sorted = [...report.timings].sort((a, b) => a - b);
    report.ackToMessageP95Ms = sorted[Math.ceil(sorted.length * .95) - 1];
    assert.ok(report.ackToMessageP95Ms < 200, "回执到提示超过离线验收上限200ms");
  });
  await run("后台读取失败保持保存成功与已保存值", async page => {
    await holdRefresh(page);
    await page.locator("#field-classifier_timeout_seconds").fill("4");
    await page.locator("#saveBarSave").click();
    await success(page);
    await pending(page, 2);
    await releaseAll(page, "合成读取失败");
    await page.locator("#flowStatus").filter({ hasText: "流程读取失败" }).waitFor({state:"attached"});
    await page.locator("#dirtyMsg").filter({ hasText: "读取失败" }).waitFor();
    await success(page);
    assert.equal(await page.evaluate(() => __fixture.state.config.classifier_timeout_seconds), 4);
  });
  await run("保存中恢复原值、新增字段和切换模式均保留；可再次保存", async page => {
    await holdRefresh(page);
    await page.evaluate(() => __saveTest.holdPost.add("console/config"));
    await page.locator("#field-classifier_timeout_seconds").fill("4");
    await page.locator("#saveBarSave").click();
    await page.waitForFunction(() => __saveTest.pending.some(item => item.kind === "post"));
    await page.locator("#field-classifier_timeout_seconds").fill("3.5");
    await page.locator("#field-gate_min_length").fill("9");
    await page.locator('[data-mode="shadow"]').click();
    await page.evaluate(() => {
      __saveTest.holdPost.clear();
      __saveTest.pending.filter(item => item.kind === "post").forEach(item => __saveTest.release(item.id));
    });
    await success(page);
    await pending(page, 2);
    assert.equal(await page.locator("#field-classifier_timeout_seconds").inputValue(), "3.5");
    assert.equal(await page.locator("#field-gate_min_length").inputValue(), "9");
    assert.equal(await page.locator('[data-mode="shadow"]').getAttribute("aria-pressed"), "true");
    assert.match(await page.locator("#saveBarText").textContent(), /3/);
    await page.locator("#saveBarSave").click();
    await page.waitForFunction(() => __saveTest.configCalls === 2
      && __fixture.state.config.classifier_timeout_seconds === 3.5);
    await success(page);
    assert.equal(await page.evaluate(() => __fixture.state.config.gate_min_length), 9);
    assert.equal(await page.locator("#modeBadge").textContent(), "观察中");
  });
  await run("分段失败且参数成功时显示部分成功，失败草稿保留", async page => {
    await editSegments(page);
    await page.locator("#field-classifier_timeout_seconds").fill("4");
    await page.evaluate(() => __saveTest.failPost.add("console/native-segments"));
    await page.locator("#saveBarSave").click();
    await page.locator("#saveMsg.msg.err").filter({ hasText: "回复参数已保存" }).waitFor();
    assert.match(await page.locator("#saveMsg").textContent(), /分段保存失败/);
    assert.equal(await page.locator("#saveBarSave").isEnabled(), true);
    assert.match(await page.locator("#saveBarText").textContent(), /未保存/);
    assert.equal(await page.locator("#native-enable").isChecked(), false);
    assert.equal(await page.evaluate(() => __fixture.state.config.classifier_timeout_seconds), 4);
  });
  await run("参数写入失败且分段成功时保留参数草稿，无全成功提示", async page => {
    await editSegments(page);
    await page.locator("#field-classifier_timeout_seconds").fill("4");
    await page.evaluate(() => __saveTest.failPost.add("console/config"));
    await page.locator("#saveBarSave").click();
    await page.locator("#saveMsg.msg.err").filter({ hasText: "参数保存失败" }).waitFor();
    assert.match(await page.locator("#saveMsg").textContent(), /分段已保存/);
    assert.equal(await page.locator("#field-classifier_timeout_seconds").inputValue(), "4");
    assert.equal(await page.evaluate(() => __fixture.state.config.classifier_timeout_seconds), 3.5);
    assert.equal(await page.locator("#saveBarSave").isEnabled(), true);
  });
  await run("后一次保存刷新先返回时，丢弃前一次状态和流程响应", async page => {
    await holdRefresh(page);
    await page.locator("#field-classifier_timeout_seconds").fill("4");
    await page.locator("#saveBarSave").click();
    await success(page);
    await pending(page, 2);
    await page.locator("#field-classifier_timeout_seconds").fill("5");
    await page.locator("#saveBarSave").click();
    await page.waitForFunction(() => __saveTest.configCalls === 2);
    await pending(page, 4);
    await page.evaluate(() => {
      const rows = __saveTest.pending.filter(item => item.kind === "get");
      rows.forEach((item, index) => {
        if (item.path === "console/flow") item.result.data.note = index < 2 ? "旧流程" : "新流程";
        else item.result.data.today.judged = index < 2 ? 111 : 222;
      });
      rows.slice(2).forEach(item => __saveTest.release(item.id));
    });
    await page.locator("#flowStatus").filter({ hasText: "新流程" }).waitFor({state:"attached"});
    await page.waitForFunction(() => document.querySelector("#stats strong").textContent === "222");
    await releaseAll(page);
    await page.locator("#field-classifier_timeout_seconds").fill("6");
    assert.equal(await page.locator("#flowStatus").textContent(), "新流程");
    assert.equal(await page.locator("#stats strong").first().textContent(), "222");
    assert.equal(await page.locator("#field-classifier_timeout_seconds").inputValue(), "6");
  });
  assert.equal(report.pageErrors.length, 0);
  assert.equal(report.externalRequests.length, 0);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  await mkdir(new URL("./evidence/", import.meta.url), { recursive: true });
  await writeFile(new URL("./evidence/save-latency-results.json", import.meta.url), JSON.stringify(report, null, 2));
}
if (report.cases.some(item => !item.passed)) process.exitCode = 1;
