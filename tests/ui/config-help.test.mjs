// 真实页面 / 隔离桥：验证说明覆盖、自动读回与草稿/版本保护，不调用模型。
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import {startServer} from "./server.mjs";

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || fileURLToPath(
  new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright",import.meta.url)));
const {server,url}=await startServer();
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage();
const errors=[];
page.on("pageerror",error=>errors.push(error.message));
await page.route("**/*",route=>{
  assert.equal(new URL(route.request().url()).origin,new URL(url).origin);
  return route.continue();
});
await page.addInitScript(()=>{
  const timer=window.setTimeout;
  window.setTimeout=(fn,delay,...args)=>{
    if(delay===15000) window.__poll=fn;
    return timer(fn,delay,...args);
  };
  let bridge;
  Object.defineProperty(window,"AstrBotPluginView",{
    get:()=>bridge,set(value){
      const get=value.apiGet.bind(value);
      const state=window.__nativeTest={revision:"external-1",maxSegments:4,gets:0};
      bridge={...value,async apiGet(path,params){
        const result=await get(path,params);
        if(path==="console/native-segments"){
          state.gets++;
          result.data.revision=state.revision;
          result.data.values.max_segments=state.maxSegments;
        }
        return result;
      }};
    },
  });
});
try {
  await page.goto(url+"?coupling=connected");
  await page.waitForFunction(()=>document.getElementById("ver").textContent.includes("fixture")
    && !document.getElementById("nativeScope").disabled && typeof __poll==="function");
  const switches=await page.locator('main input[type="checkbox"]').evaluateAll(inputs=>inputs.map(input=>input.id));
  let checked=0;
  for(const id of switches){
    const input=page.locator("#"+id);
    const target=await input.evaluate(element=>({
      page:element.closest('[role="tabpanel"]').id.replace("panel-",""),
      sub:element.closest("section[data-sub]")?.dataset.sub,
    }));
    await page.locator(`.page-tabs [data-page="${target.page}"]`).click();
    if(target.sub)await page.locator(`#replySubTabs [data-sub="${target.sub}"]`).click();
    const help=await input.evaluate(element=>{
      const row=element.closest(".row,.prompt-entry,.coupling-row")||element.closest(".field-title");
      return row?.querySelector(".field-help-trigger")?.outerHTML;
    });
    assert.ok(help,"缺少问号："+id);
    await input.evaluate(element=>{
      const row=element.closest(".row,.prompt-entry,.coupling-row")||element.closest(".field-title");
      row.querySelector(".field-help-trigger").click();
    });
    const text=await page.locator("#fieldHelpModalBody").textContent();
    for(const label of ["开启后","关闭后","去哪设置"])assert.ok(text.includes(label),id+" 缺少 "+label);
    assert.ok(!text.includes("undefined"),id+" 出现无效说明");
    await page.keyboard.press("Escape");
    checked++;
  }
  console.log(`通过：${checked} 个开关均有用途、开启、关闭及设置位置说明`);
  await page.locator('.page-tabs [data-page="reply"]').click();
  await page.locator('#replySubTabs [data-sub="native"]').click();
  await page.evaluate(()=>{__nativeTest.revision="external-2";__nativeTest.maxSegments=7;});
  // 焦点离开原生编辑区后，触发页面本身的 15 秒刷新处理器。
  await page.locator('#replySubTabs [data-sub="native"]').focus();
  await page.evaluate(()=>__poll());
  await page.waitForFunction(()=>document.getElementById("native-max_segments").value==="7");
  console.log("通过：外部原生配置更改自动读回");
  const control=page.locator("#native-max_segments");
  await control.fill("9");
  await page.evaluate(()=>{__nativeTest.revision="external-3";__nativeTest.maxSegments=2;});
  const before=await page.evaluate(()=>__nativeTest.gets);
  await page.evaluate(()=>__poll());
  await page.waitForFunction(value=>__nativeTest.gets>value,before);
  assert.equal(await control.inputValue(),"9");
  assert.equal(await page.evaluate(()=>document.activeElement.id),"native-max_segments");
  assert.match(await page.locator("#nativeStatus").textContent(),/保留你的编辑/);
  console.log("通过：原生配置更新保留草稿和焦点，不静默推进保存版本");
  assert.deepEqual(errors,[]);
} finally {
  await context.close();
  await browser.close();
  await new Promise(resolve=>server.close(resolve));
}
