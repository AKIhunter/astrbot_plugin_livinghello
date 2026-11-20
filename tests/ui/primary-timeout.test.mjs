// 真实LH页面，合成原生模型字段；只验证交互，不访问宿主或付费Provider。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fileURLToPath(
  new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url)));
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) });
const { server, url } = await startServer();
const report = { scope: "真实页面/合成原生字段；模型调用0；无线上配置写入",
  cases: [], errors: [], externalRequests: [], port: server.address().port };
const output = process.env.REPORT_DIR || "/private/tmp/lh-timeout-ui-20261120/ui";
await mkdir(output, { recursive: true });

async function open(width = 1280) {
  const context = await browser.newContext({ viewport: { width, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(3000);
  page.on("pageerror", error => report.errors.push(error.message));
  await page.route("**/*", route => {
    if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
    report.externalRequests.push(route.request().url());
    return route.abort();
  });
  await page.addInitScript(() => {
    window.__timeout = { seconds: 15, posts: [], hold: false, pending: [], fail: false,
      release() { this.hold = false; this.pending.splice(0).forEach(resolve => resolve()); } };
    let bridge;
    Object.defineProperty(window, "AstrBotPluginView", {
      get: () => bridge,
      set(value) {
        const get = value.apiGet.bind(value), post = value.apiPost.bind(value);
        bridge = {
          ...value,
          async apiGet(route, params) {
            const result = await get(route, params);
            if (route === "console/state") result.data.config.reply_primary_timeout_seconds = __timeout.seconds;
            if (route === "console/native-settings") Object.assign(result.data, {
              local_runner: true,
              fields: [
                { key: "provider_id", group: "模型", title: "主对话模型", kind: "provider" },
                { key: "fallback_provider_ids", group: "模型", title: "回退模型（按顺序）", kind: "providers" },
                { key: "request_max_retries", group: "模型", title: "异常最大尝试次数", kind: "int", min: 1, max: 10 },
                { key: "streaming_response", group: "模型", title: "流式回复", kind: "bool" }
              ],
              values: { provider_id: "", fallback_provider_ids: [], request_max_retries: 5, streaming_response: false }
            });
            return result;
          },
          async apiPost(route, payload) {
            __timeout.posts.push({ route, payload: structuredClone(payload) });
            if (route === "console/config") {
              if (__timeout.hold) await new Promise(resolve => __timeout.pending.push(resolve));
              if (__timeout.fail) throw new Error("合成保存失败");
              __timeout.seconds = payload.reply_primary_timeout_seconds;
              const result = await post(route, payload);
              return { status: "ok", data: { saved: true, config: structuredClone(payload),
                mode: result.data.mode, message_policy: result.data.message_policy, changed: Object.keys(payload) } };
            }
            return post(route, payload);
          }
        };
      }
    });
    // 加速既有自动轮询，不改变页面刷新或保存逻辑。
    const timeout = window.setTimeout;
    window.setTimeout = (fn, ms, ...args) => timeout(fn, ms === 15000 ? 100 : ms, ...args);
  });
  await page.goto(url);
  await page.locator('.page-tabs [data-page="reply"]').click();
  await page.locator('#replySubTabs [data-sub="native"]').click();
  await page.locator("#field-reply_primary_timeout_seconds").waitFor();
  return { page, context };
}
const input = page => page.locator("#field-reply_primary_timeout_seconds");
const configPosts = page => page.evaluate(() =>
  __timeout.posts.filter(item => item.route === "console/config").map(item => item.payload));
async function save(page) {
  await page.locator("#saveBarSave").click();
  await page.locator("#saveMsg.msg.ok").filter({ hasText: "回复参数已保存" }).waitFor();
}
async function run(name, task, width) {
  const { page, context } = await open(width);
  try {
    await task(page);
    report.cases.push({ name, passed: true });
    console.log("PASS " + name);
  } catch (error) {
    report.cases.push({ name, passed: false, detail: error.stack });
    console.error("FAIL " + name + ": " + error.message);
  } finally {
    assert.equal(await page.evaluate(() => __fixture.modelCalls), 0);
    await context.close();
  }
}
try {
  await run("截图位置、唯一输入框、范围与问号说明", async page => {
    assert.equal(await input(page).count(), 1);
    assert.equal(await input(page).inputValue(), "15");
    assert.deepEqual(await input(page).evaluate(node => [node.min, node.max, node.step]), ["1", "60", "1"]);
    assert.deepEqual(await page.locator('[data-group="模型"] .row').evaluateAll(rows =>
      rows.map(row => row.querySelector("input,select")?.id)), [
      "setting-provider_id", undefined, "setting-request_max_retries",
      "field-reply_primary_timeout_seconds", "setting-streaming_response"
    ]);
    await page.screenshot({ path: path.join(output, "model-section.png"), fullPage: true });
    await page.locator('[data-help-key="reply_primary_timeout_seconds"]').click();
    await page.getByText(/对所有LH接管的本地回复生效/).first().waitFor();
  });
  await run("保存仅提交LH秒数，读回8秒", async page => {
    await input(page).fill("8");
    await save(page);
    assert.deepEqual(await configPosts(page), [{ reply_primary_timeout_seconds: 8 }]);
    assert.equal(await input(page).inputValue(), "8");
    assert.equal(await page.locator("#dirtyMsg").textContent(), "");
    assert.equal(await page.evaluate(() => __timeout.posts.some(item => item.route === "console/native-settings")), false);
  });
  await run("外部已保存值自动显示，未保存草稿和焦点保留", async page => {
    await page.evaluate(() => { __timeout.seconds = 12; });
    await page.waitForFunction(() => document.querySelector("#field-reply_primary_timeout_seconds").value === "12");
    await input(page).fill("9");
    await page.evaluate(() => { __timeout.seconds = 25; });
    await page.waitForFunction(() => __fixture.calls.filter(call => call.path === "console/state").length >= 4);
    assert.equal(await input(page).inputValue(), "9");
    assert.equal(await input(page).evaluate(node => node === document.activeElement), true);
  });
  await run("保存失败保留草稿", async page => {
    await page.evaluate(() => { __timeout.fail = true; });
    await input(page).fill("8");
    await page.locator("#saveBarSave").click();
    await page.locator("#saveMsg.msg.err").waitFor();
    assert.equal(await input(page).inputValue(), "8");
    assert.match(await page.locator("#saveBarText").textContent(), /仍有改动未保存/);
  });
  await run("空值、小数与超范围拒绝提交且定位输入框", async page => {
    for (const value of ["", "0", "61", "1.5"]) {
      await input(page).fill(value);
      await page.locator("#saveBarSave").click();
      await page.getByText(value === "" ? "主模型超时切换需要有效数字，不能留空"
        : "主模型超时切换需要1–60秒的整数").waitFor();
      assert.equal((await configPosts(page)).length, 0);
      assert.equal(await input(page).evaluate(node => node === document.activeElement), true);
    }
  });
  await run("保存中继续编辑保留新草稿", async page => {
    await page.evaluate(() => { __timeout.hold = true; });
    await input(page).fill("8");
    await page.locator("#saveBarSave").click();
    await page.waitForFunction(() => __timeout.pending.length === 1);
    await input(page).fill("10");
    await page.evaluate(() => __timeout.release());
    await page.locator("#saveMsg.msg.ok").waitFor();
    assert.equal(await input(page).inputValue(), "10");
    assert.match(await page.locator("#saveBarText").textContent(), /1/);
  });
  await run("窄屏字段可见且无横向溢出", async page => {
    assert.equal(await input(page).isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }, 390);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  report.passed = report.cases.every(item => item.passed) && !report.errors.length && !report.externalRequests.length;
  await writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
}
assert.equal(report.passed, true, "参见隔离UI报告");
