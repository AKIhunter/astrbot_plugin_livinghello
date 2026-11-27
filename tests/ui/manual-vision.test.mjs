// 真实页面、离线桥接：同名线路可以分别选择，视频的候选仍需确认图片能力。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const fallback = fileURLToPath(new URL(
  "../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url,
));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fallback);
const browser = await chromium.launch({ headless: true });
const { server, url } = await startServer();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => new URL(route.request().url()).origin === new URL(url).origin
    ? route.continue() : route.abort());
  await page.addInitScript(() => {
    let bridge;
    Object.defineProperty(window, "AstrBotPluginView", {
      get: () => bridge,
      set(value) {
        bridge = { ...value, async apiGet(path, params) {
          const result = await value.apiGet(path, params);
          if (path === "console/state") {
            result.data.providers = [
              { id: "router:upstream_12", model: "deepseek-v4.1-flash", display: "线路甲",
                kind: "router_upstream", role: "chat", supports_images: false },
              { id: "router:upstream_13", model: "deepseek-v4.1-flash", display: "线路乙",
                kind: "router_upstream", role: "chat", supports_images: true },
              { id: "auto-text", kind: "router", role: "chat", supports_images: false },
              { id: "speech", kind: "provider", role: "stt", supports_images: false },
            ];
          }
          return result;
        } };
      },
    });
  });
  await page.goto(url);
  await page.locator("#ver").filter({ hasText: "fixture-task9" }).waitFor();
  await page.getByRole("tab", { name: "判定设置" }).click();
  const image = page.locator("#field-vision_shared_provider_id");
  const video = page.locator("#field-understand_video_provider_id");
  assert.deepEqual(await image.locator("option").evaluateAll(options => options.map(o => o.value)),
    ["", "router:upstream_12", "router:upstream_13"]);
  assert.deepEqual(await video.locator("option").evaluateAll(options => options.map(o => o.value)),
    ["", "router:upstream_13"]);
  assert.match(await image.locator('option[value="router:upstream_12"]').textContent(), /图片能力未确认/);
  await image.selectOption("router:upstream_12");
  await page.locator("#saveBarSave").click();
  await page.locator("#saveMsg").filter({ hasText: "回复参数已保存" }).waitFor();
  assert.deepEqual(await page.evaluate(() => __fixture.calls.filter(call =>
    call.path === "console/config").at(-1).payload), { vision_shared_provider_id: "router:upstream_12" });
  assert.equal(await image.inputValue(), "router:upstream_12");
  assert.equal(await page.evaluate(() => __fixture.modelCalls), 0);
  assert.deepEqual(errors, []);
  console.log("通过：手选未确认图片能力的同名上游，保存与回显正确；视频仍筛选图片能力");
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
