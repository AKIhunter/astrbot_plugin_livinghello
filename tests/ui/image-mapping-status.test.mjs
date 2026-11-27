// 真实配置页面；同一配置revision下热更新映射状态，保留编辑DOM与草稿。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fileURLToPath(
  new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url)));
const { server, url } = await startServer();
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) });
const errors = [], external = [];
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => {
    if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
    external.push(route.request().url());
    return route.abort();
  });
  await page.clock.install();
  await page.addInitScript(() => {
    window.__mappingFixture = {
      contract: "livinghello.image-prompt-mapping.v1", enabled: true,
      rule_count: 2, rules_source: "file", load_status: "loaded", learned_count: 0, warning: "",
    };
    window.__textUsageFixture = { used: 0, limit: 48, migration_pending: true };
    let bridge;
    Object.defineProperty(window, "AstrBotPluginView", {
      get: () => bridge,
      set(value) {
        const get = value.apiGet.bind(value);
        bridge = { ...value, async apiGet(path, params) {
          if (path !== "console/image-settings") return get(path, params);
          return { status: "ok", data: {
            available: true, scope: "离线映射夹具", revision: "fixed-revision",
            usage: { used: 0, limit: 20 }, providers: [],
            fields: [
              { key: "style_prompt", title: "画面风格", kind: "text", help: "离线草稿" },
              { key: "prompt_text_provider_id", title: "提示词调整模型", kind: "image_text_provider", help: "承诺生图" },
              { key: "prompt_text_daily_limit", title: "每日提示词模型调用上限", kind: "int", help: "调用上限" },
            ],
            values: { style_prompt: "", prompt_text_provider_id: "@legacy", prompt_text_daily_limit: null },
            prompt_mapping: structuredClone(window.__mappingFixture),
            prompt_text_usage: structuredClone(window.__textUsageFixture),
            prompt_text_providers: [{ id: "chat-1", display: "提示词聊天模型", model: "Chat" }],
          }};
        }};
      },
    });
  });
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.getElementById("imageSettingsStatus").textContent.includes("生效规则 2 条"));
  const status = page.locator("#imageSettingsStatus");
  assert.match(await status.textContent(), /当前映射表/);
  await page.evaluate(() => Object.assign(window.__mappingFixture, {
    rules_source: "previous", load_status: "invalid", warning: "第1条规则：正则不支持分组；使用上一份有效规则。",
  }));
  await page.clock.runFor(15100);
  assert.match(await status.textContent(), /上一份有效表/);
  assert.match(await status.textContent(), /第1条规则/);

  await page.locator('.page-tabs [data-page="reply"]').click();
  const tab = page.locator('#replySubTabs [data-sub="native"]');
  if (await tab.count()) await tab.click();
  const input = page.locator("#image-style_prompt");
  if (!await input.isVisible()) await page.locator("#imageSettings summary").click();
  const provider = page.locator("#image-prompt_text_provider_id");
  const limit = page.locator("#image-prompt_text_daily_limit");
  assert.equal(await provider.inputValue(), "@legacy");
  assert.deepEqual(await provider.locator("option").evaluateAll(options => options.map(o => o.value)),
    ["@legacy", "", "chat-1"]);
  assert.equal(await limit.inputValue(), "");
  assert.equal(await limit.getAttribute("placeholder"), "48");
  assert.equal(await limit.getAttribute("min"), "0");
  assert.equal(await limit.getAttribute("max"), "1000");
  assert.match(await status.textContent(), /首次需要起稿或改词时迁移/);
  await provider.selectOption("chat-1");
  await limit.fill("0");
  await input.fill("本地未保存草稿");
  const editor = await input.elementHandle();
  await page.evaluate(() => Object.assign(window.__mappingFixture, {
    rules_source: "builtin", rule_count: 1, warning: "映射表无效；使用内置规则。",
  }));
  await page.evaluate(() => Object.assign(window.__textUsageFixture, {
    used: 2, limit: 37, migration_pending: false, legacy_providers: ["chat-1"],
  }));
  await page.clock.runFor(15100);
  assert.match(await status.textContent(), /生效规则 1 条（内置规则）/);
  assert.match(await status.textContent(), /提示词模型已用 2 \/ 37 次/);
  assert.match(await status.textContent(), /迁移首选：提示词聊天模型/);
  assert.doesNotMatch(await status.textContent(), /首次需要起稿/);
  assert.equal(await provider.inputValue(), "chat-1");
  assert.equal(await limit.inputValue(), "0");
  assert.equal(await input.inputValue(), "本地未保存草稿");
  assert.equal(await page.evaluate(node => node === document.getElementById("image-style_prompt"), editor), true);
  await page.evaluate(() => Object.assign(window.__mappingFixture, { enabled: false, rule_count: 0 }));
  await page.clock.runFor(15100);
  assert.match(await status.textContent(), /映射已关闭，生效规则 0 条/);
  assert.equal(await input.inputValue(), "本地未保存草稿");
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log("PASS：映射和提示词用量自动读回；聊天模型选项、额度输入和编辑草稿保留；无外部请求。");
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
