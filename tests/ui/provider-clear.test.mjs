// 执行真实页面保存过滤逻辑；仅验证请求体，不启动宿主或调用模型。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../pages/guard/index.html", import.meta.url), "utf8");
const start = source.indexOf("    state.dirty.forEach((key) => {", source.indexOf("  async function save()"));
const end = source.indexOf("\n    if(policyDirty())", start);
assert.ok(start >= 0 && end > start, "未找到页面的实际保存逻辑");
const code = source.slice(start, end);

function payloadFor(draft, listKeys = []) {
  const payload = {};
  vm.runInNewContext(code, {
    state: { dirty: new Set(Object.keys(draft)), draft }, payload,
    MESSAGE_POLICY_FIELDS: [], LIST_KEYS: new Set(listKeys), el: () => null,
  }, { timeout: 1000 });
  return JSON.parse(JSON.stringify(payload));
}

for (const key of [
  "classifier_provider_id", "vision_provider_id", "vision_shared_provider_id",
  "understand_image_provider_id", "understand_video_provider_id", "understand_voice_provider_id",
]) {
  test(`${key} 清空后仍提交到后端`, () => {
    assert.deepEqual(payloadFor({ [key]: "" }), { [key]: "" });
  });
}

test("保存模型和其他改动时，不丢弃清空的视频或语音选择", () => {
  const draft = { understand_video_provider_id: "", understand_voice_provider_id: "",
    understand_image_provider_id: "image-vl", fragment_wait_seconds: 4 };
  assert.deepEqual(payloadFor(draft), draft);
});

test("列表清空仍提交空数组，未填写的数值仍跳过", () => {
  assert.deepEqual(payloadFor({ bot_names: "", fragment_wait_seconds: null }, ["bot_names"]), { bot_names: [] });
});
