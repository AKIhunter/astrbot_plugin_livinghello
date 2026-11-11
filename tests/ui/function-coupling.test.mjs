import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import {startServer} from "./server.mjs";

const require=createRequire(import.meta.url);
const {chromium}=require(fileURLToPath(new URL("../../../astrbot_plugin_livingemotion/tests/ui/node_modules/playwright",import.meta.url)));
const {server,url}=await startServer();
const browser=await chromium.launch({headless:true});
const report={scope:"真实页面/保存处理器，隔离插件注册表；无真实模型或外网调用",cases:[],pageErrors:[],externalRequests:[]};
const output="/tmp/lh-compact-coupling-20261110/local-ui";
async function run(name, task, connected=true, width=1280) {
  const context=await browser.newContext({viewport:{width,height:1000}});
  const page=await context.newPage();
  page.setDefaultTimeout(7000);
  page.on("pageerror",error=>report.pageErrors.push(error.message));
  await page.route("**/*",route=>{
    if(new URL(route.request().url()).origin===new URL(url).origin) return route.continue();
    report.externalRequests.push(route.request().url());return route.abort();
  });
  await page.addInitScript(()=>{
    for(const method of ["setInterval","setTimeout"]) {
      const original=window[method];
      window[method]=(fn,delay,...args)=>{
        if(delay===15000) window.__couplingPoll=fn;
        return original(fn,delay,...args);
      };
    }
  });
  try {
    await page.goto(url+(connected?"?coupling=connected":""),{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>document.getElementById("ver").textContent.includes("fixture"));
    await page.locator('.page-tabs [data-page="reply"]').click();
    await page.locator("#integration").scrollIntoViewIfNeeded();
    await task(page);
    report.cases.push({name,passed:true});
    console.log("通过："+name);
  } catch(error) {
    report.cases.push({name,passed:false,error:error.stack});console.error("失败："+name+"："+error.message);
  } finally {await context.close();}
}
const emotion="#field-coupling_livingemotion_enabled", memory="#field-coupling_livingmemory_enabled";
try {
  await mkdir(output,{recursive:true});
  await run("未接入禁用两个开关，链接文本与目标准确",async page=>{
    for(const kind of ["livingemotion","livingmemory"]) {
      const row=page.locator(`[data-coupling="${kind}"]`);
      assert.equal(await row.locator(".coupling-status").textContent(),"未接入");
      assert.equal(await row.locator("input").isDisabled(),true);
      assert.equal(await row.locator(".coupling-info > label").textContent(),kind==="livingemotion"?"LivingEmotion":"LivingMemory");
      assert.equal(await row.locator("a").textContent(),"插件主页 ↗");
      assert.equal(await row.locator("a").getAttribute("href"),"https://github.com/AKIhunter/astrbot_plugin_"+kind);
    }
  },false);
  await run("接入后键盘启用、保存两个开关并再次关闭",async page=>{
    await page.locator(emotion).focus();await page.keyboard.press("Space");
    await page.locator(memory).check();
    await page.locator("#saveBarSave").click();
    await page.locator("#saveMsg.msg.ok").waitFor();
    assert.equal(await page.evaluate(()=>__fixture.state.config.coupling_livingemotion_enabled),true);
    assert.equal(await page.evaluate(()=>__fixture.state.config.coupling_livingmemory_enabled),true);
    await page.locator(emotion).uncheck();await page.locator(memory).uncheck();
    await page.locator("#saveBarSave").click();
    await page.waitForFunction(()=>__fixture.state.config.coupling_livingemotion_enabled===false &&
      __fixture.state.config.coupling_livingmemory_enabled===false);
  });
  await run("动态断开保留另一开关草稿和焦点，重新接入恢复草稿",async page=>{
    await page.locator(emotion).check();
    await page.locator(memory).focus();
    await page.evaluate(()=>{__fixture.setConnected("livingemotion",false);__couplingPoll();});
    await page.locator('[data-coupling="livingemotion"] .coupling-status').filter({hasText:"未接入"}).waitFor();
    assert.equal(await page.locator(emotion).isDisabled(),true);
    assert.equal(await page.evaluate(()=>document.activeElement.id),"field-coupling_livingmemory_enabled");
    await page.evaluate(()=>{__fixture.setConnected("livingemotion",true);__couplingPoll();});
    await page.waitForFunction(()=>!document.getElementById("field-coupling_livingemotion_enabled").disabled);
    assert.equal(await page.locator(emotion).isChecked(),true);
    await page.locator('[data-coupling-help="livingemotion"]').click();
    assert.match(await page.locator("#fieldHelpModalBody").textContent(),/待保存/);
    await page.keyboard.press("Escape");
  });
  await run("合并位置与独立说明按钮",async page=>{
    assert.equal(await page.locator("#functionCoupling").count(),0);
    assert.equal(await page.locator("#integration").evaluate(e=>e.previousElementSibling.id),"recall");
    assert.equal(await page.locator("#integration p.sub").count(),0);
    await page.getByRole("button",{name:"爬楼是什么",exact:true}).click();
    assert.equal(await page.locator("#fieldHelpTitle").textContent(),"爬楼是什么");
    await page.keyboard.press("Escape");
  });
  for(const width of [1280,390]) {
    await run(`${width}px 合并卡片紧凑布局无溢出`,async page=>{
      const card=await page.locator("#integration").boundingBox();
      assert.ok(card.width>=Math.min(320,width-24),"功能卡片必须保有可读宽度");
      for(const selector of [emotion,memory]) {
        const box=await page.locator(selector).boundingBox();
        assert.ok(box.x>=card.x && box.x+box.width<=card.x+card.width);
      }
      await page.locator("#integration").screenshot({path:`${output}/coupling-${width}.png`});
    },true,width);
  }
  assert.deepEqual(report.pageErrors,[]);
  assert.deepEqual(report.externalRequests,[]);
} finally {
  await writeFile(`${output}/ui-report.json`,JSON.stringify(report,null,2)+"\n");
  await browser.close();await new Promise(resolve=>server.close(resolve));
}
if(report.cases.some(value=>!value.passed)) process.exitCode=1;
