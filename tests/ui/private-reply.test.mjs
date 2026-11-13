import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const fallback = fileURLToPath(new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright", import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || fallback);
const { server, url } = await startServer();
const browser = await chromium.launch({ headless: true });
const results = [];
const evidence = new URL("./evidence/", import.meta.url);
await mkdir(evidence, { recursive: true });
try {
  for (const width of [320, 390, 1280]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    const external = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      external.push(route.request().url()); return route.abort();
    });
    await page.goto(url);
    await page.locator("#ver").filter({ hasText: "fixture-task9" }).waitFor();
    await page.getByRole("tab", { name: "回复与私聊" }).click();
    await page.locator('#replySubTabs [data-sub="reply"]').click();
    const control = page.locator("#field-expression_private_reuse");
    assert.equal(await control.isChecked(), false);
    assert.equal(await control.isEnabled(), true);
    for (const value of [true, false]) {
      await control.setChecked(value);
      await page.locator("#saveBarSave").click();
      await page.locator("#saveMsg").filter({ hasText: "回复参数已保存" }).waitFor();
      const saved = await page.evaluate(() => window.__fixture.calls.filter(c => c.path === "console/config").at(-1).payload);
      assert.deepEqual(saved, { expression_private_reuse: value });
      const readback = await page.evaluate(() => window.__fixture.state.config.expression_private_reuse);
      assert.equal(readback, value);
      assert.equal(await control.isChecked(), value);
    }
    const owner = page.locator("#field-expression_takeover_enabled");
    await owner.uncheck();
    assert.equal(await control.isEnabled(), false);
    await owner.check();
    assert.equal(await control.isEnabled(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.evaluate(() => window.__fixture.modelCalls), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    await page.screenshot({ path: fileURLToPath(new URL(`private-reply-${width}.png`, evidence)) });
    results.push({ width, defaultOff: true, savedOnAndOff: true, noModelCalls: true });
    await page.close();
  }
  await writeFile(new URL("private-reply-result.json", evidence), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
