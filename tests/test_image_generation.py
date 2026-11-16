"""独立生图的配置/共享额度/重复与取消，以及本地HTTP协议回归。"""
import asyncio
import base64
import importlib.util
import json
import logging
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Image, Plain
from main import Main
from images.guard_image_settings import (DEFAULTS, MAX_PROMPT_CHARS, STYLE_MAX_CHARS,
                                         ImageSettings, MAX_IMAGE_BYTES)
from reply.guard_native_reply import NativeReplyError


class Event:
    unified_msg_origin="test:group:one"
    def __init__(self,mid="message-one",images=None):
        self.message_obj=SimpleNamespace(message_id=mid)
        self.extra={}
        self.images=images or []
        self.send=AsyncMock()
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return "user-one"
    def get_messages(self):return self.images
    def chain_result(self,chain):return SimpleNamespace(chain=chain)
    def get_result(self):return self.result


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.native=SimpleNamespace(provider_config=dict(id="native-image",type="openai",
            api_base="https://example.invalid/v1",key=["secret-marker"]),
            get_model=lambda:"image-one")
        self.router=SimpleNamespace(provider_config=dict(id="router",type="multi_api_router"),
            upstreams=[SimpleNamespace(client=self.native,slot=2,name="绘图上游")])
        self.context=SimpleNamespace(get_all_providers=lambda:[self.native,self.router],
                                     get_registered_star=lambda _:self.fail("不得依赖BiliBot"))
        self.plugin=SimpleNamespace(context=self.context,data_dir=Path(self.folder.name),
            _delivery=lambda:SimpleNamespace(recalled=lambda e:False),
            _judged_event=lambda e:False,_gate_mode=lambda:"enforce")
        self.service=ImageSettings(self.plugin)
        self.service.ensure_tool=lambda:object()
        self.service.values.update(enabled=True,provider_id="native-image",daily_limit=2)
        self.output=Path(self.folder.name)/"generated.png"
        self.output.write_bytes(b"\x89PNG\r\n\x1a\n"+b"test-image")
        self.service._request=AsyncMock(return_value=(self.output, "一只橙色猫"))
        self.plugin._image_generation=lambda:self.service
        self.plugin.logger=logging.getLogger("livinghello-image-offline")

    async def deliver(self, event):
        event.result=event.chain_result([Plain("文字已发")])
        await Main.deliver_pending_image(self.plugin, event)

    async def test_deferred_contract_generates_without_sending_or_pending_leak(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        event = Event(mid="promise:fixed-id")
        result = await Main.generate_deferred_image(self.plugin, event, "橙色猫的自拍")
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["path"], str(self.output))
        self.assertEqual(result["revised"], "一只橙色猫")
        self.assertIsNone(event.get_extra("_livinghello_pending_image"))
        event.send.assert_not_awaited()
        Main.complete_deferred_image(self.plugin, result["request_id"], "sent")
        with self.service._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls").fetchone()[0], "sent")

    async def test_deferred_disabled_does_not_request_image(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.service.values["enabled"] = False
        result = await Main.generate_deferred_image(self.plugin, Event(), "猫")
        self.assertEqual(result, {"status": "unavailable"})
        self.service._request.assert_not_awaited()

    async def test_deferred_unknown_never_claims_delivery(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.service._request.side_effect = TimeoutError()
        result = await Main.generate_deferred_image(self.plugin, Event(), "猫")
        self.assertEqual(result, {"status": "unknown"})
        self.service._request.assert_awaited_once()

    async def test_deferred_fixed_id_keeps_paid_call_idempotent(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        first = await Main.generate_deferred_image(self.plugin, Event(mid="promise:fixed"), "猫")
        second = await Main.generate_deferred_image(self.plugin, Event(mid="promise:fixed"), "猫")
        self.assertEqual(first["status"], "generated")
        self.assertEqual(second["status"], "unknown")
        self.service._request.assert_awaited_once()

    def deferred_plugin(self):
        self.plugin.get_deferred_image_contract = lambda: Main.get_deferred_image_contract(self.plugin)
        self.plugin.generate_deferred_image = lambda event, prompt: Main.generate_deferred_image(
            self.plugin, event, prompt)
        self.plugin.complete_deferred_image = lambda rid, status: Main.complete_deferred_image(
            self.plugin, rid, status)
        self.plugin.execute_deferred_image = lambda event, prompt: Main.execute_deferred_image(
            self.plugin, event, prompt)
        self.plugin.deliver_deferred_image = lambda event, artifact, **kwargs: Main.deliver_deferred_image(
            self.plugin, event, artifact, **kwargs)
        self.context.send_message = AsyncMock(return_value=True)

    async def generated_resource(self):
        self.deferred_plugin()
        event = Event(mid="promise:delivery-contract")
        artifact = await self.plugin.generate_deferred_image(event, "猫")
        return event, artifact

    async def test_delivery_restart_replays_receipt_without_second_send(self):
        event, artifact = await self.generated_resource()
        result = await self.plugin.deliver_deferred_image(event, artifact)
        self.assertEqual(result["status"], "sent")
        # 重建服务实例，仍使用同一个磁盘账本。
        restored = ImageSettings(self.plugin)
        restored.values.update(self.service.values)
        self.plugin._image_generation = lambda: restored
        replay = await self.plugin.deliver_deferred_image(Event("new-event"), artifact)
        self.assertEqual(replay["status"], "sent")
        self.assertEqual(replay["reason"], "already_sent")
        self.context.send_message.assert_awaited_once()

    async def test_late_legacy_callback_cannot_reopen_sent_delivery(self):
        event, artifact = await self.generated_resource()
        await self.plugin.deliver_deferred_image(event, artifact)
        self.plugin.complete_deferred_image(artifact["request_id"], "unknown")
        self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"], "sent")
        self.context.send_message.assert_awaited_once()

    async def test_delivery_false_retries_only_original_image(self):
        event, artifact = await self.generated_resource()
        self.context.send_message.return_value = False
        result = await self.plugin.deliver_deferred_image(event, artifact)
        self.assertEqual((result["status"], result["retryable"]), ("rejected", True))
        self.context.send_message.return_value = True
        self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"], "sent")
        self.assertEqual(self.context.send_message.await_count, 2)
        self.service._request.assert_awaited_once()

    async def test_delivery_exception_or_nonbool_never_retries(self):
        for index, response in enumerate((OSError("isolated"), None)):
            with self.subTest(response=type(response).__name__):
                self.deferred_plugin()
                event = Event(mid=f"promise:unknown-{index}")
                artifact = await self.plugin.generate_deferred_image(event, "猫")
                if isinstance(response, Exception):
                    self.context.send_message.side_effect = response
                else:
                    self.context.send_message.return_value = response
                self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"],
                                 "unknown")
                self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"],
                                 "unknown")
                self.context.send_message.assert_awaited_once()

    async def test_delivery_concurrent_attempts_have_one_transport_call(self):
        event, artifact = await self.generated_resource()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*_):
            entered.set()
            await release.wait()
            return True
        self.context.send_message.side_effect = send
        first = asyncio.create_task(self.plugin.deliver_deferred_image(event, artifact))
        await entered.wait()
        try:
            second = await self.plugin.deliver_deferred_image(Event("second"), artifact)
            self.assertEqual(second["status"], "unknown")
        finally:
            release.set()
        self.assertEqual((await first)["status"], "sent")
        self.context.send_message.assert_awaited_once()

    async def test_delivery_source_authorization_is_checked_before_transport(self):
        event, artifact = await self.generated_resource()
        authorize = AsyncMock(return_value=False)
        result = await self.plugin.deliver_deferred_image(event, artifact, authorize=authorize)
        self.assertEqual(result["reason"], "source_authorization_denied")
        self.context.send_message.assert_not_awaited()
        authorize.return_value = True
        self.assertEqual((await self.plugin.deliver_deferred_image(
            event, artifact, authorize=authorize))["status"], "sent")

    async def test_delivery_rejects_changed_resource_identity(self):
        event, artifact = await self.generated_resource()
        for change in ({"session": "other"}, {"request_id": "invented"},
                       {"path": str(self.output.parent / "other.png")}):
            other = self.output.parent / "other.png"
            other.write_bytes(self.output.read_bytes())
            result = await self.plugin.deliver_deferred_image(event, {**artifact, **change})
            self.assertEqual((result["status"], result["retryable"]), ("rejected", False))
        self.context.send_message.assert_not_awaited()
        self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"], "sent")

    async def test_delivery_disabled_after_generation_retains_original_image(self):
        event, artifact = await self.generated_resource()
        self.service.values["enabled"] = False
        result = await self.plugin.deliver_deferred_image(event, artifact)
        self.assertEqual(result["status"], "rejected")
        self.context.send_message.assert_not_awaited()
        self.service.values["enabled"] = True
        self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"], "sent")
        self.service._request.assert_awaited_once()

    async def test_delivery_rechecks_local_switch_after_source_authorization(self):
        event, artifact = await self.generated_resource()
        async def authorize():
            self.service.values["enabled"] = False
            return True
        result = await self.plugin.deliver_deferred_image(event, artifact, authorize=authorize)
        self.assertEqual(result["status"], "rejected")
        self.context.send_message.assert_not_awaited()

    async def test_delivery_interrupted_submission_is_persisted_as_unknown(self):
        event, artifact = await self.generated_resource()
        self.context.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.deliver_deferred_image(event, artifact)
        self.assertEqual((await self.plugin.deliver_deferred_image(event, artifact))["status"], "unknown")
        self.context.send_message.assert_awaited_once()

    async def test_cron_tool_delivers_without_after_sent_stage(self):
        self.deferred_plugin()
        from images.guard_image_tool import GenerateImageTool
        tool = GenerateImageTool(service=self.service)
        event = Event(mid="cron:one")
        event.set_extra("cron_job", {"id": "one"})
        result = await tool.call(SimpleNamespace(context=SimpleNamespace(event=event)), prompt="猫")
        self.assertIn("图片已发送", result)
        self.context.send_message.assert_awaited_once()
        self.assertIsNone(event.get_extra("_livinghello_pending_image"))
        with self.service._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls").fetchone()[0], "sent")

    async def test_cron_send_failure_cannot_retry_with_changed_prompt(self):
        self.deferred_plugin()
        self.context.send_message.side_effect = OSError("isolated")
        from images.guard_image_tool import GenerateImageTool
        tool = GenerateImageTool(service=self.service)
        event = Event(mid="cron:failure")
        event.set_extra("cron_job", {"id": "failure"})
        result = await tool.call(event, prompt="猫")
        self.assertIn("未确认发送", result)
        again = await tool.call(event, prompt="狗")
        self.assertIn("未确认发送", again)
        self.service._request.assert_awaited_once()
        self.context.send_message.assert_awaited_once()

    async def test_one_time_cron_restart_and_rewording_do_not_charge_twice(self):
        self.deferred_plugin()
        first = Event(mid="before-restart")
        first.set_extra("cron_job", {"id": "durable-job", "run_once": True})
        self.assertEqual((await self.plugin.execute_deferred_image(first, "猫"))["status"], "sent")
        newer = Event(mid="after-restart")
        newer.set_extra("cron_job", {"id": "durable-job", "run_once": True})
        self.assertEqual((await self.plugin.execute_deferred_image(newer, "狗"))["status"], "unknown")
        self.service._request.assert_awaited_once()
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.service.read()["usage"]["used"], 1)

    def body(self,**values):
        return dict(expected_revision=self.service.read()["revision"],values=values)

    def test_public_targets_include_router_and_never_secrets(self):
        payload=self.service.read()
        self.assertEqual({p["id"] for p in payload["providers"]},{"native-image","router:upstream_2"})
        self.assertNotIn("secret-marker",json.dumps(payload))
        self.assertNotIn("api_base",json.dumps(payload))
        self.assertEqual(self.service.connection({**self.service.values,
            "provider_id":"router:upstream_2"})[2],"image-one")

    async def test_typed_image_router_outside_chat_registry_can_save_and_reload(self):
        config = dict(enabled=True, provider_id="typed-router",
                      upstream_7=dict(api_type="image_generation", name="专用生图API",
                                      api_base="https://images.invalid/v1", api_key="typed-secret",
                                      model="image-seven", route_enabled=False),
                      upstream_8=dict(api_type="embedding", name="向量API",
                                      api_base="https://embedding.invalid/v1", api_key="embedding-secret",
                                      model="embedding-eight"))
        self.context.get_registered_star = lambda name: SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config=config)) if name == "astrbot_plugin_multi_api_router" else None
        self.context.get_all_providers = lambda: [self.native]  # 生图上游没有聊天Provider。
        result = await self.service.save(self.body(provider_id="typed-router:upstream_7"))
        row = next(p for p in result["providers"] if p["id"] == "typed-router:upstream_7")
        self.assertEqual((row["display"], row["model"]), ("专用生图API", "image-seven"))
        self.assertNotIn("typed-router:upstream_8", {p["id"] for p in result["providers"]})
        self.assertNotIn("typed-secret", json.dumps(result))
        self.assertEqual(self.service.connection(self.service.values),
                         ("https://images.invalid/v1", "typed-secret", "image-seven"))
        replacement = ImageSettings(self.plugin)
        self.assertEqual(replacement.values["provider_id"], "typed-router:upstream_7")
        config["enabled"] = False
        with self.assertRaises(NativeReplyError):
            replacement.connection(replacement.values)

    async def test_existing_custom_size_and_extended_timeout_survive_reload(self):
        await self.service.save(self.body(size="2048x2048", timeout_seconds=240))
        replacement = ImageSettings(self.plugin)
        self.assertFalse(replacement.error)
        self.assertEqual(replacement.values["size"], "2048x2048")
        self.assertEqual(replacement.values["timeout_seconds"], 240)
        for patch in (dict(size="2049x2048"), dict(size="511x1024"), dict(timeout_seconds=501)):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(**patch))

    def test_legacy_config_without_style_prompt_is_migrated_on_load(self):
        legacy = {key: value for key, value in self.service.values.items() if key != "style_prompt"}
        legacy.update(enabled=True, provider_id="native-image")
        self.service.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        loaded = ImageSettings(self.plugin)
        self.assertFalse(loaded.error)
        self.assertEqual(loaded.values["style_prompt"], DEFAULTS["style_prompt"])
        # 迁移只在内存补默认值，旧文件保持原样；下一次保存写回完整键集合。
        self.assertNotIn("style_prompt", json.loads(self.service.path.read_text(encoding="utf-8")))

    def test_legacy_config_without_follow_server_time_is_migrated_on_load(self):
        legacy = {key: value for key, value in self.service.values.items()
                  if key != "follow_server_time"}
        legacy.update(enabled=True, provider_id="native-image")
        self.service.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        loaded = ImageSettings(self.plugin)
        self.assertFalse(loaded.error)
        self.assertFalse(loaded.values["follow_server_time"])

    async def test_save_on_legacy_disk_config_is_not_rejected(self):
        legacy = {key: value for key, value in self.service.values.items() if key != "style_prompt"}
        self.service.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        service = ImageSettings(self.plugin)
        service.ensure_tool = lambda: object()
        result = await service.save(dict(expected_revision=service.read()["revision"],
                                         values={"style_prompt": "统一水彩风格"}))
        self.assertEqual(result["values"]["style_prompt"], "统一水彩风格")
        self.assertIn("style_prompt", json.loads(self.service.path.read_text(encoding="utf-8")))

    async def test_external_disk_change_returns_conflict_not_unreadable(self):
        await self.service.save(self.body(style_prompt="水彩"))
        # 页面草稿先取得版本；外部修改后再read()会热重载，已不再是旧草稿。
        draft = self.body(style_prompt="油画")
        stored = json.loads(self.service.path.read_text(encoding="utf-8"))
        stored["daily_limit"] = 7
        self.service.path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(NativeReplyError) as raised:
            await self.service.save(draft)
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("已变化", str(raised.exception))

    async def test_corrupt_disk_config_reports_unreadable(self):
        self.service.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(NativeReplyError) as raised:
            await self.service.save(self.body(style_prompt="水彩"))
        self.assertEqual(raised.exception.status, 503)
        self.assertIn("不可读", str(raised.exception))

    async def test_style_prompt_saves_reloads_and_rejects_oversize(self):
        await self.service.save(self.body(style_prompt="统一水彩风格"))
        replacement = ImageSettings(self.plugin)
        self.assertEqual(replacement.values["style_prompt"], "统一水彩风格")
        with self.assertRaises(NativeReplyError):
            await self.service.save(self.body(style_prompt="风" * (STYLE_MAX_CHARS + 1)))
        with self.assertRaises(NativeReplyError):
            await self.service.save(self.body(style_prompt=False))

    def test_style_prompt_is_appended_after_description_with_length_guard(self):
        self.assertEqual(ImageSettings.styled_prompt("画猫", ""), "画猫")
        self.assertEqual(ImageSettings.styled_prompt("画猫", "   "), "画猫")
        self.assertEqual(ImageSettings.styled_prompt("画猫", " 水彩 "), "画猫\n水彩")
        # 描述本身过长时保持原样，不因为风格再被截断。
        oversized = "猫" * MAX_PROMPT_CHARS
        self.assertEqual(ImageSettings.styled_prompt(oversized, "水彩"), oversized)
        # 合计超限只截断风格，用户描述完整保留。
        combined = ImageSettings.styled_prompt("猫" * (MAX_PROMPT_CHARS - 2), "风" * STYLE_MAX_CHARS)
        self.assertTrue(combined.startswith("猫" * (MAX_PROMPT_CHARS - 2) + "\n"))
        self.assertEqual(len(combined), MAX_PROMPT_CHARS)

    def test_follow_server_time_is_prepended_and_skipped_near_limit(self):
        now = datetime(2026, 11, 16, 20, 30)
        self.assertEqual(ImageSettings.timed_prompt("画猫", False, now), "画猫")
        self.assertEqual(
            ImageSettings.timed_prompt("画猫", True, now),
            "当前服务器时间：2026年11月16日 20:30（星期一）。"
            "请按这个时间理解现实中的昼夜、季节和节日。\n画猫")
        # 描述接近上限时跳过注入，保证用户原描述完整。
        oversized = "猫" * (MAX_PROMPT_CHARS - 10)
        self.assertEqual(ImageSettings.timed_prompt(oversized, True, now), oversized)

    async def test_follow_server_time_saves_and_rejects_non_bool(self):
        await self.service.save(self.body(follow_server_time=True))
        replacement = ImageSettings(self.plugin)
        self.assertTrue(replacement.values["follow_server_time"])
        with self.assertRaises(NativeReplyError):
            await self.service.save(self.body(follow_server_time="yes"))

    def test_dashscope_image_response_is_preserved(self):
        self.assertEqual(ImageSettings.extract({"output": {"choices": [{"message": {
            "content": [{"type": "image", "image": "https://example.invalid/image.png"}]}}]}}),
            "https://example.invalid/image.png")

    async def test_atomic_save_reload_and_stale_revision(self):
        before=self.service.read()["revision"]
        await self.service.save(self.body(model="image-two",daily_limit=10))
        new=ImageSettings(self.plugin)
        self.assertEqual(new.values["model"],"image-two")
        with self.assertRaises(NativeReplyError):
            await self.service.save(dict(expected_revision=before,values={"daily_limit":20}))

    async def test_bad_model_unknown_field_and_bounds_rejected(self):
        for values in (dict(provider_id="missing"),dict(api_key="cannot-edit"),
                       dict(daily_limit=-1),dict(daily_limit=True),dict(protocol={}),
                       dict(timeout_seconds=9),dict(size="huge")):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(**values))
        self.assertFalse(self.service.path.exists())

    async def test_disk_failure_preserves_running_configuration(self):
        before=dict(self.service.values)
        with patch.object(self.service,"_write",side_effect=OSError("fixture")):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(daily_limit=9))
        self.assertEqual(self.service.values,before)

    async def test_duplicate_event_does_not_call_or_send_twice(self):
        event=Event()
        self.assertIn("图片已生成",await self.service.generate(event,"画一只猫"))
        event.send.assert_not_awaited()
        await self.deliver(event)
        self.assertIn("已请求过",await self.service.generate(event,"画一只猫"))
        self.assertEqual(self.service._request.await_count,1)
        self.assertEqual(event.send.await_count,1)
        self.assertEqual(self.service.used(),1)

    async def test_concurrent_calls_respect_global_daily_limit(self):
        self.service.values["daily_limit"]=1
        results=await asyncio.gather(*(self.service.generate(Event(str(i)),"画猫") for i in range(8)))
        self.assertEqual(sum("图片已生成" in r for r in results),1)
        self.assertEqual(self.service._request.await_count,1)
        self.assertEqual(self.service.used(),1)

    async def test_pending_image_waits_for_last_text_and_is_sent_once(self):
        event=Event()
        await self.service.generate(event,"拍个自拍")
        text=Plain("第二段")
        event.result=event.chain_result([text])
        event.set_extra("_at_guard_delivery_plan", {"failed":False,"attempted":{}})
        await Main.deliver_pending_image(self.plugin,event)
        event.send.assert_not_awaited()
        self.assertIsNotNone(event.get_extra("_livinghello_pending_image"))
        event.get_extra("_at_guard_delivery_plan")["attempted"][id(text)]=True
        await Main.deliver_pending_image(self.plugin,event)
        await Main.deliver_pending_image(self.plugin,event)
        self.assertEqual(event.send.await_count,1)
        self.assertIsNone(event.get_extra("_livinghello_pending_image"))

    async def test_pending_image_is_cancelled_when_reply_is_recalled(self):
        event=Event()
        await self.service.generate(event,"画猫")
        event.set_extra("_at_guard_delivery_blocked","message_recalled")
        await self.deliver(event)
        event.send.assert_not_awaited()
        self.assertIsNone(event.get_extra("_livinghello_pending_image"))
        self.assertEqual(self.service.used(),1)

    async def test_pending_image_send_error_does_not_retry(self):
        event=Event()
        await self.service.generate(event,"画猫")
        event.send.side_effect=OSError("offline-send-failure")
        await self.deliver(event)
        await self.deliver(event)
        self.assertEqual(event.send.await_count,1)
        self.assertIsNone(event.get_extra("_livinghello_pending_image"))
        self.assertEqual(self.service.used(),1)

    async def test_image_job_retries_delivery_of_generated_image(self):
        event=Event()
        request_id=self.service.reserve(event,"画猫",False)
        job=dict(status="running",prompt="画猫",use_reference=False,
                 values=dict(self.service.values),reply="",emotion=None)
        event.set_extra("_livinghello_pending_image",
                        {"path":str(self.output),"request_id":request_id})
        event.send.side_effect=[OSError("offline-once"),None]
        self.service.generate=AsyncMock(return_value="图片已生成。")
        with patch("images.guard_image_settings.asyncio.sleep",new=AsyncMock()):
            await self.service._run_image_job(event,job)
        self.assertEqual(event.send.await_count,2)
        self.assertEqual(job["status"],"sent")
        with self.service._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls").fetchone()[0],"sent")

    async def test_image_job_apology_after_delivery_retries_exhausted(self):
        event=Event()
        request_id=self.service.reserve(event,"画猫",False)
        job=dict(status="running",prompt="画猫",use_reference=False,
                 values=dict(self.service.values),reply="",emotion=None)
        event.set_extra("_livinghello_pending_image",
                        {"path":str(self.output),"request_id":request_id})
        event.send.side_effect=[OSError("offline-one"),OSError("offline-two"),None]
        self.service.generate=AsyncMock(return_value="图片已生成。")
        with patch("images.guard_image_settings.asyncio.sleep",new=AsyncMock()):
            await self.service._run_image_job(event,job)
        self.assertEqual(event.send.await_count,3)
        self.assertEqual(job["status"],"unknown")
        apology=event.send.await_args_list[-1].args[0]
        self.assertIn("没顺利",apology.chain[0].text)
        with self.service._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM calls").fetchone()[0],"unknown")

    def test_revised_prompt_accepts_both_protocols_and_caps_length(self):
        self.assertEqual(ImageSettings.revised(
            {"data":[{"revised_prompt":" 橙色猫 "}]}),"橙色猫")
        self.assertEqual(ImageSettings.revised(
            {"output":{"choices":[{"message":{"prompt":"蓝色猫"}}]}}),"蓝色猫")
        self.assertEqual(ImageSettings.revised({"choices":[None]}),"")
        self.assertEqual(ImageSettings.revised(None),"")
        self.assertEqual(len(ImageSettings.revised(
            {"data":[{"revised_prompt":"猫"*1000}]})),600)

    def test_ledger_shared_across_instances_and_reload(self):
        self.service.values["daily_limit"]=1
        self.service.reserve(Event("first"),"画猫",False)
        new=ImageSettings(self.plugin)
        new.values.update(daily_limit=1)
        self.assertEqual(new.used(),1)
        with self.assertRaises(NativeReplyError):
            new.reserve(Event("second"),"画狗",False)
        with patch.object(new,"today",return_value="2099-01-01"):
            self.assertEqual(new.used(),0)
            self.assertTrue(new.reserve(Event("tomorrow"),"画猫",False))

    async def test_upstream_failure_keeps_quota_and_does_not_retry(self):
        self.service._request.side_effect=TimeoutError("secret-marker")
        result=await self.service.generate(Event(),"画猫")
        self.assertNotIn("secret-marker",result)
        self.assertEqual(self.service.used(),1)
        self.assertEqual(self.service._request.await_count,1)

    async def test_missing_reference_never_requests_or_consumes(self):
        self.assertIn("补充图片",await self.service.generate(Event(),"换背景",True))
        self.service._request.assert_not_awaited()
        self.assertEqual(self.service.used(),0)

    async def test_reference_is_taken_from_current_event(self):
        event=Event(images=[Image(file=str(self.output))])
        result=await self.service.generate(event,"换成蓝色",True)
        self.assertIn("图片已生成",result)
        reference=self.service._request.await_args.args[-1]
        self.assertEqual(reference[0],self.output.read_bytes())

    async def test_cancelled_after_generation_never_sends(self):
        event=Event()
        async def generate(*_):
            event.set_extra("_at_guard_delivery_blocked","context_changed")
            return self.output, ""
        self.service._request.side_effect=generate
        self.assertIn("未发送",await self.service.generate(event,"画猫"))
        event.send.assert_not_awaited()
        self.assertEqual(self.service.used(),1)

    async def test_disabled_and_zero_limit_make_no_request(self):
        self.service.values["enabled"]=False
        await self.service.generate(Event(),"画猫")
        self.service.values.update(enabled=True,daily_limit=0)
        await self.service.generate(Event("another"),"画猫")
        self.service._request.assert_not_awaited()

    async def test_disabling_while_generating_suppresses_delivery(self):
        event=Event()
        async def generate(*_):
            self.service.values["enabled"]=False
            return self.output, ""
        self.service._request.side_effect=generate
        self.assertIn("未发送",await self.service.generate(event,"画猫"))
        event.send.assert_not_awaited()
        self.assertEqual(self.service.used(),1)

    async def test_shutdown_cancels_inflight_without_refunding_or_sending(self):
        event=Event()
        started=asyncio.Event()
        async def generate(*_):
            started.set()
            await asyncio.Event().wait()
        self.service._request.side_effect=generate
        task=asyncio.create_task(self.service.generate(event,"画猫"))
        await started.wait()
        await self.service.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.service.used(),1)
        event.send.assert_not_awaited()

    async def test_pre_cancel_and_corrupt_quota_fail_closed(self):
        event=Event()
        event.set_extra("_at_guard_delivery_blocked","message_recalled")
        await self.service.generate(event,"画猫")
        self.service.ledger.write_bytes(b"not sqlite")
        self.assertFalse(self.service.read()["available"])
        await self.service.generate(Event("new"),"画猫")
        self.service._request.assert_not_awaited()

    def test_decode_rejects_nonimage_and_handles_standard_chat_shapes(self):
        image=base64.b64encode(self.output.read_bytes()).decode()
        for payload in ({"data":[{"b64_json":image}]},
                        {"choices":[{"message":{"images":[{"image_url":{"url":"data:image/png;base64,"+image}}]}}]}):
            value=ImageSettings.extract(payload)
            self.assertEqual(ImageSettings._image_bytes(value)[0],self.output.read_bytes())
        for value in ("not base64",base64.b64encode(b"<html>not image</html>").decode()):
            with self.assertRaises(NativeReplyError):ImageSettings._image_bytes(value)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"),"HTTP协议测试需宿主已有aiohttp")
class ImageHttpTests(unittest.IsolatedAsyncioTestCase):
    setUp = ImageGenerationTests.setUp
    deliver = ImageGenerationTests.deliver
    async def asyncSetUp(self):
        from aiohttp import web
        self.received=[]
        self.http_status = 200
        self.response_payload = None
        async def handler(request):
            if request.content_type=="multipart/form-data":
                form=await request.post()
                self.received.append((request.path,dict(model=form["model"],n=form["n"],
                                                      image=form["image"].file.read())))
            else:self.received.append((request.path,await request.json()))
            return web.json_response(self.response_payload or
                {"data":[{"b64_json":base64.b64encode(self.output.read_bytes()).decode()}]},
                status=self.http_status)
        app=web.Application()
        app.router.add_post("/v1/images/generations",handler)
        app.router.add_post("/v1/images/edits",handler)
        app.router.add_post("/v1/chat/completions",handler)
        async def download(request):
            return web.Response(status=503)
        app.router.add_get("/image.png", download)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        site=web.TCPSite(self.runner,"127.0.0.1",0)
        await site.start()
        self.base="http://127.0.0.1:"+str(site._server.sockets[0].getsockname()[1])+"/v1"

    async def asyncTearDown(self):await self.runner.cleanup()

    async def test_generation_edit_and_chat_requests_use_real_http_protocol(self):
        values=self.service.values
        connection=(self.base,"local-fixture-key","image-model")
        for reference in (None,(self.output.read_bytes(),"image/png")):
            generated,revised=await ImageSettings._request(self.service,values,connection,"画猫",reference)
            self.assertEqual(generated.read_bytes(),self.output.read_bytes())
            self.assertEqual(revised, "")
        values={**values,"protocol":"chat_images"}
        await ImageSettings._request(self.service,values,connection,"换背景",
                                     (self.output.read_bytes(),"image/png"))
        self.assertEqual([r[0] for r in self.received],
                         ["/v1/images/generations","/v1/images/edits","/v1/chat/completions"])
        self.assertEqual(self.received[0][1]["n"],1)
        self.assertEqual(self.received[1][1]["image"],self.output.read_bytes())
        self.assertEqual(self.received[2][1]["modalities"],["image"])

    async def test_style_prompt_reaches_both_generation_protocols(self):
        values={**self.service.values,"style_prompt":"统一水彩风格"}
        connection=(self.base,"local-fixture-key","image-model")
        await ImageSettings._request(self.service,values,connection,"画猫",None)
        values={**values,"protocol":"chat_images"}
        await ImageSettings._request(self.service,values,connection,"画猫",
                                     (self.output.read_bytes(),"image/png"))
        self.assertEqual(self.received[0][1]["prompt"],"画猫\n统一水彩风格")
        self.assertEqual(self.received[1][1]["messages"][0]["content"][0]["text"],
                         "画猫\n统一水彩风格")

    async def test_follow_server_time_reaches_both_generation_protocols(self):
        values={**self.service.values,"follow_server_time":True}
        connection=(self.base,"local-fixture-key","image-model")
        await ImageSettings._request(self.service,values,connection,"画猫",None)
        values={**values,"protocol":"chat_images"}
        await ImageSettings._request(self.service,values,connection,"画猫",
                                     (self.output.read_bytes(),"image/png"))
        for prompt in (self.received[0][1]["prompt"],
                       self.received[1][1]["messages"][0]["content"][0]["text"]):
            self.assertTrue(prompt.startswith("当前服务器时间："))
            self.assertTrue(prompt.endswith("\n画猫"))

    async def test_trace_counts_real_http_api_and_successful_image_delivery(self):
        from observability.guard_request_trace import RequestTrace, ID_KEY
        trace = RequestTrace(self.plugin, self.plugin.data_dir)
        event = Event()
        trace.begin(event)
        self.service.connection = lambda values: (self.base, "fixture-key", "image-model")
        self.service._request = lambda *args: ImageSettings._request(self.service, *args)
        try:
            result = await self.service.generate(event, "仅本地HTTP生图")
            self.assertIn("图片已生成", result)
            event.send.assert_not_awaited()
            await self.deliver(event)
            row = await trace.get(event.get_extra(ID_KEY))
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.assertEqual(row["summary"]["tool_calls"], 1)
            self.assertEqual(row["features"]["image_api"]["status"], "success")
            self.assertEqual(row["features"]["image_delivery"]["status"], "success")
            self.assertEqual(len(self.received), 1)
        finally:
            await trace.close()

    async def test_trace_distinguishes_http_failure_from_download_failure(self):
        from observability.guard_request_trace import RequestTrace, ID_KEY
        trace = RequestTrace(self.plugin, self.plugin.data_dir)
        self.service.connection = lambda values: (self.base, "fixture-key", "image-model")
        self.service._request = lambda *args: ImageSettings._request(self.service, *args)
        try:
            self.http_status = 503
            first = Event(mid="http-failure")
            trace.begin(first)
            await self.service.generate(first, "API失败")
            row = await trace.get(first.get_extra(ID_KEY))
            self.assertEqual(row["features"]["image_api"]["status"], "failed")
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.http_status = 200
            self.response_payload = {"data": [{"url": self.base.removesuffix("/v1") + "/image.png"}]}
            second = Event(mid="download-failure")
            trace.begin(second)
            await self.service.generate(second, "下载失败")
            row = await trace.get(second.get_extra(ID_KEY))
            self.assertEqual(row["features"]["image_api"]["status"], "success")
            self.assertEqual(row["features"]["image_download"]["status"], "failed")
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.assertNotIn("image_delivery", row["features"])
            self.assertEqual(self.service.used(), 2)
        finally:
            await trace.close()
