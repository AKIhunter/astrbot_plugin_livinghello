"""回复源头契约与实际分段路径；Provider、发送和配置保存均不联网。"""
import ast
import os
from pathlib import Path
import unittest
from types import SimpleNamespace as NS

from astrbot.api.message_components import Image, Plain
from astrbot.core.agent.message import TextPart

from prompt_bundle import offer
from reply import guard_prompt_injection as prompt
from reply.guard_native_reply import managed_regex
from reply.guard_reply_merge import deduplicate_text_chain, merge_text_parts
from reply.guard_segments import ReplySegments


# 合成回放保留“相同首句、改写句、重复末段”结构，不发布真实聊天原文。
PRE_TOOL = "原来的设置保留。开启新功能的时候，会自动刷新，不用手动操作。\n\n设置已确认。按这个方案执行～"
POST_TOOL = "原来的设置保留。开启新功能后，会自动刷新，不用手动操作。\n\n设置已确认。按这个方案执行～"
DELTA = "开启新功能后，会自动刷新，不用手动操作。"
PUNCT_PRE = "记下了，以后新功能都自动刷新。"
PUNCT_POST = "记下了。以后新功能，都自动刷新。"


class Event:
    unified_msg_origin = "offline:private:reply-merge"
    def __init__(self):
        self.extra = {}
    def get_extra(self, key, default=None):
        return self.extra.get(key, default)
    def get_platform_name(self):
        return "offline"


class Chain:
    def __init__(self, chain=None, llm=True):
        self.chain, self.llm = list(chain or []), llm
    def is_llm_result(self):
        return self.llm
    def derive(self, chain):
        return Chain(chain, self.llm)


def guard(**raw):
    native = dict(enable=True, only_llm_result=True, split_mode="regex",
                  regex=managed_regex(), split_words=[], content_cleanup_rule="")
    native.update(raw)
    return NS(config={"expression_punct_cleanup": False},
              context=NS(get_config=lambda _: {"platform_settings": {"segmented_reply": native}},
                         get_registered_star=lambda _: None))


def request(*tools):
    return NS(func_tool=NS(tools=[NS(name=name) for name in tools]),
              extra_user_content_parts=[], system_prompt="原有人格")


def texts(result):
    return [c.text for c in result.chain if isinstance(c, Plain)]


class ReplyMergeTests(unittest.TestCase):
    def test_reproduced_tool_round_keeps_changed_wording(self):
        merged = merge_text_parts([PRE_TOOL, POST_TOOL])
        self.assertEqual(merged, PRE_TOOL + "\n\n" + DELTA)
        self.assertEqual(merged.count("设置已确认。按这个方案执行～"), 1)
        self.assertEqual(merged.count("原来的设置保留。"), 1)

    def test_exact_duplicate_full_response(self):
        self.assertEqual(merge_text_parts(["第一段\n\n第二段"] * 3), "第一段\n\n第二段")

    def test_new_paragraphs_and_new_sentences_remain_in_order(self):
        for parts, expected in (
            (["已查到结果。", "已查到结果。\n\n另有补充。"], "已查到结果。\n\n另有补充。"),
            (["已查到结果。", "已查到结果。另有补充。"], "已查到结果。\n\n另有补充。"),
            (["甲。乙。", "乙。丙。", "甲。丙。丁。"], "甲。乙。\n\n丙。\n\n丁。"),
            (["原句。", "原句还有补充。"], "原句。原句还有补充。"),
        ):
            with self.subTest(parts=parts):
                self.assertEqual(merge_text_parts(parts), expected)

    def test_single_response_intentional_repetition_is_preserved(self):
        text = "再说一次。再说一次。\n\n同一段\n\n同一段"
        self.assertEqual(merge_text_parts([text]), text)
        # 去重集合只能在整份响应处理完后更新。
        self.assertEqual(merge_text_parts(["开头。", "再说一次。再说一次。"]),
                         "开头。再说一次。再说一次。")

    def test_structure_and_technical_content_are_preserved(self):
        for text in (
            "```python\nprint('同样内容')\nprint('同样内容')\n```",
            "~~~text\n同样内容\n同样内容\n~~~",
            "|名称|值|\n|---|---|\n|甲|重复|\n|乙|重复|",
            "1. 重复操作。\n2. 重复操作。",
            "- 重复操作。\n- 重复操作。",
            "> 原样引用。\n> 原样引用。",
            "他说「重复。重复。」",
            '{"text": "重复。", "items": ["重复。", "重复。"]}',
            "详见 https://example.invalid/a.b",
            "文件位于 /tmp/example.json",
            "数值是 1.25。",
        ):
            with self.subTest(text=text):
                self.assertEqual(merge_text_parts([text, text]), text + text)

    def test_long_content_uses_conservative_budget(self):
        text = "很长的正文。" * 4000
        self.assertEqual(merge_text_parts([text, text]), text + text)

    def test_no_fuzzy_deletion_or_incomplete_sentence_deletion(self):
        for first, second in (
            ("我会认真学。", "我会认真学习。"),
            ("还没完成", "还没完成，现在继续"),
            ("有两个结果。", "有三个结果。"),
        ):
            with self.subTest(first=first):
                self.assertEqual(merge_text_parts([first, second]), first + second)

    def test_empty_parts_do_not_create_empty_bubbles(self):
        self.assertEqual(merge_text_parts(["", "正文。", "正文。", ""]), "正文。")

    def test_same_text_on_new_turn_is_not_suppressed(self):
        for _ in range(2):
            self.assertEqual(merge_text_parts(["正文。", "正文。"]), "正文。")

    def test_punctuation_and_whitespace_only_changes_are_duplicates(self):
        for second in (PUNCT_POST, "记下了 以后新功能都自动刷新",
                       " 记下了。\t以后新功能， 都自动刷新！\n"):
            with self.subTest(second=second):
                self.assertEqual(merge_text_parts([PUNCT_PRE, second]), PUNCT_PRE)

    def test_changed_sentence_boundaries_still_keep_new_tail(self):
        for second in (PUNCT_POST + "另有补充。",
                       "记下了！以后新功能都自动刷新。\n\n另有补充。"):
            with self.subTest(second=second):
                self.assertEqual(merge_text_parts([PUNCT_PRE, second]),
                                 PUNCT_PRE + "\n\n另有补充。")

    def test_duplicate_sentence_keeps_original_first_wording(self):
        self.assertEqual(merge_text_parts(["已确认！", "已 确认。还有补充。"]),
                         "已确认！\n\n还有补充。")
        self.assertEqual(merge_text_parts(["已确认。", "已确认还有补充。"]),
                         "已确认。已确认还有补充。")

    def test_single_body_and_punctuation_only_parts_remain_untouched(self):
        text = PUNCT_PRE + PUNCT_POST
        self.assertEqual(merge_text_parts([text]), text)
        self.assertEqual(merge_text_parts(["！？", "！！"]), "！？！！")

    def test_words_numbers_and_quoted_variants_are_not_normalized_away(self):
        for first, second in (
            (PUNCT_PRE, "记下了。以后新功能都手动刷新。"),
            ("数值是 1.25。", "数值是 12.5。"),
            ("他说「已确认。」", "他说「已确认！」"),
            ("curl https://example.invalid/a.b", "curl https://example.invalid/ab"),
        ):
            with self.subTest(first=first):
                self.assertEqual(merge_text_parts([first, second]), first + second)


class SegmentMergeTests(unittest.TestCase):
    def prepare(self, components, *, streaming=False, **raw):
        result = Chain(components)
        segments = ReplySegments(guard(**raw))
        self.assertTrue(segments.prepare(Event(), result, streaming=streaming))
        return result

    def test_reproduced_three_bubbles_all_become_distinct(self):
        result = self.prepare([Plain(PRE_TOOL), Plain(POST_TOOL)])
        self.assertEqual(texts(result), [
            PRE_TOOL.split("\n\n")[0], PRE_TOOL.split("\n\n")[1], DELTA])
        self.assertEqual(len(set(texts(result))), len(texts(result)))

    @unittest.skipUnless(os.environ.get("ASTRBOT_NATIVE_SOURCE"), "需提供实际宿主源码路径")
    def test_actual_host_buffered_merge_then_plugin_deduplication(self):
        path = Path(os.environ["ASTRBOT_NATIVE_SOURCE"]) / "core/astr_agent_run_util.py"
        nodes = [n for n in ast.parse(path.read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_merge_buffered_llm_chains"]
        if not nodes:
            self.skipTest("该宿主源码没有缓冲正文合并扩展")
        self.assertEqual(len(nodes), 1)
        namespace = {"MessageChain": Chain}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        buffered = [Chain([Plain(PRE_TOOL)]), Chain([Plain(POST_TOOL)])]
        merged = namespace["_merge_buffered_llm_chains"](buffered)
        self.assertEqual(buffered, [])
        self.assertEqual(texts(merged), [PRE_TOOL, POST_TOOL])
        result = self.prepare(merged.chain)
        self.assertEqual(texts(result), [
            PRE_TOOL.split("\n\n")[0], PRE_TOOL.split("\n\n")[1], DELTA])

    def test_duplicate_response_and_fresh_content(self):
        result = self.prepare([Plain("旧段"), Plain("旧段\n\n新增段")])
        self.assertEqual(texts(result), ["旧段", "新增段"])

    def test_three_tool_steps_keep_all_new_content(self):
        result = self.prepare([Plain("甲。乙。"), Plain("乙。丙。"), Plain("甲。丙。丁。")])
        self.assertEqual(texts(result), ["甲。乙。", "丙。", "丁。"])

    def test_disabled_bubbles_still_deduplicate(self):
        result = self.prepare([Plain("旧段"), Plain("旧段\n\n新增段")], enable=False)
        self.assertEqual(texts(result), ["旧段\n\n新增段"])

    def test_maximum_compaction_runs_after_deduplication(self):
        result = self.prepare([Plain("甲。\n\n乙。"), Plain("乙。\n\n丙。\n\n丁。")],
                              regex=managed_regex(maximum=2))
        self.assertEqual(texts(result), ["甲。乙。", "丙。丁。"])

    def test_media_position_and_identical_captions_are_preserved(self):
        image = Image.fromURL("https://example.invalid/media")
        result = self.prepare([Plain("图的说明"), image,
                               Plain("图的说明"), Plain("图的说明\n\n新增说明")])
        self.assertEqual(result.chain[1], image)
        self.assertEqual(texts(result), ["图的说明", "图的说明", "新增说明"])

    def test_stream_tokens_are_neither_deduplicated_nor_separated(self):
        result = self.prepare([Plain("重复"), Plain("重复")], streaming=True)
        self.assertEqual(texts(result), ["重复重复"])
        result = self.prepare([Plain("<thi"), Plain("nk>思考</think>正文")], streaming=True)
        self.assertEqual(texts(result), ["正文"])

    def test_nonduplicate_plain_fragments_keep_original_join(self):
        result = self.prepare([Plain("你"), Plain("好")])
        self.assertEqual(texts(result), ["你好"])

    def test_prepare_is_idempotent(self):
        segments, event = ReplySegments(guard()), Event()
        result = Chain([Plain(PRE_TOOL), Plain(POST_TOOL)])
        segments.prepare(event, result)
        first = texts(result)
        segments.prepare(event, result)
        self.assertEqual(texts(result), first)

    def test_host_managed_and_non_llm_results_are_untouched(self):
        for result, raw in (
            (Chain([Plain("重复"), Plain("重复")], llm=False), {}),
            (Chain([Plain("重复"), Plain("重复")]), {"regex": "(?s).+"}),
        ):
            self.assertFalse(ReplySegments(guard(**raw)).prepare(Event(), result))
            self.assertEqual(texts(result), ["重复", "重复"])

    def test_early_merge_keeps_first_component_and_new_content(self):
        first = Plain(PUNCT_PRE)
        result = Chain([first, Plain(PUNCT_POST + "另有补充。")])
        segments, event = ReplySegments(guard()), Event()
        self.assertTrue(segments.deduplicate(event, result))
        self.assertIs(result.chain[0], first)
        self.assertFalse(segments.deduplicate(event, result))
        self.assertTrue(segments.prepare(event, result))
        self.assertEqual(texts(result), [PUNCT_PRE, "另有补充。"])

    def test_early_merge_does_not_change_nonduplicates_or_media(self):
        first, second = Plain("新"), Plain("内容")
        chain = [first, second]
        self.assertFalse(deduplicate_text_chain(chain))
        self.assertEqual(chain, [first, second])
        image = Image.fromURL("https://example.invalid/media")
        chain = [Plain(PUNCT_PRE), image, Plain(PUNCT_POST), Plain(PUNCT_PRE)]
        self.assertTrue(deduplicate_text_chain(chain))
        self.assertIs(chain[1], image)
        self.assertEqual([c.text for c in chain if isinstance(c, Plain)],
                         [PUNCT_PRE, PUNCT_POST])

    def test_early_merge_leaves_single_body_and_host_managed_result_alone(self):
        text = PUNCT_PRE + PUNCT_POST
        for result, raw in (
            (Chain([Plain(text)]), {}),
            (Chain([Plain(PUNCT_PRE), Plain(PUNCT_POST)], llm=False), {}),
            (Chain([Plain(PUNCT_PRE), Plain(PUNCT_POST)]), {"regex": "(?s).+"}),
        ):
            before = list(result.chain)
            self.assertFalse(ReplySegments(guard(**raw)).deduplicate(Event(), result))
            self.assertEqual(result.chain, before)


class ToolReplyPromptTests(unittest.TestCase):
    def test_contract_injected_once_for_all_tool_types(self):
        for name in ("store_memory", "web_search", "livinghello_generate_image",
                     "send_message_to_user", "add_cron"):
            with self.subTest(name=name):
                app, event, req = guard(), Event(), request(name)
                for _ in range(3):
                    prompt.attach_shared(app, event, req, lambda text: TextPart(text=text))
                body = "\n".join(p.text for p in req.extra_user_content_parts)
                self.assertEqual(body.count("[lh.tool_reply]"), 1)
                self.assertIn(prompt.TOOL_REPLY_CONTRACT, body)
                self.assertTrue(all(p.model_dump_for_context()["_no_save"]
                                    for p in req.extra_user_content_parts))
                self.assertEqual(req.system_prompt, "原有人格")

    def test_no_tools_do_not_add_contract(self):
        for tools in ([], None):
            req = request()
            req.func_tool.tools = tools
            prompt.attach_shared(guard(), Event(), req, lambda text: TextPart(text=text))
            self.assertNotIn("[lh.tool_reply]", "\n".join(p.text for p in req.extra_user_content_parts))

    def test_custom_and_disabled_shared_prompts_are_respected(self):
        app = guard()
        app.config.update(prompt_core_text="手写通用说明", prompt_memory_enabled=False)
        req = request("store_memory")
        prompt.attach_shared(app, Event(), req, lambda text: TextPart(text=text))
        body = "\n".join(p.text for p in req.extra_user_content_parts)
        self.assertIn("手写通用说明", body)
        self.assertNotIn("[shared.memory]", body)
        self.assertEqual(app.config["prompt_core_text"], "手写通用说明")
        self.assertFalse(app.config["prompt_memory_enabled"])
        app.config["prompt_core_enabled"] = False
        other = request("store_memory")
        prompt.attach_shared(app, Event(), other, lambda text: TextPart(text=text))
        self.assertNotIn("[shared.context]", "\n".join(p.text for p in other.extra_user_content_parts))

    def test_peer_offer_keeps_tool_contract_once(self):
        req = request("store_memory")
        prompt.attach_shared(guard(), Event(), req, lambda text: TextPart(text=text))
        offer(req, "LE", {"le.state": "本轮情绪"}, lambda text: TextPart(text=text))
        prompt.attach_shared(guard(), Event(), req, lambda text: TextPart(text=text))
        body = "\n".join(p.text for p in req.extra_user_content_parts)
        self.assertEqual(body.count("[lh.tool_reply]"), 1)
        self.assertEqual(body.count("[le.state]"), 1)

    def test_image_specific_contract_is_preserved(self):
        req = request("livinghello_generate_image")
        req.system_prompt += "\n" + prompt.IMAGE_CONTRACT
        original = req.system_prompt
        prompt.attach_shared(guard(), Event(), req, lambda text: TextPart(text=text))
        self.assertEqual(req.system_prompt, original)
        body = "\n".join(p.text for p in req.extra_user_content_parts)
        self.assertIn("生图仍遵循专用的简短承接规则", body)

    def test_new_request_has_own_tool_contract(self):
        app = guard()
        with_tool, without_tool = request("store_memory"), request()
        for req in (with_tool, without_tool):
            prompt.attach_shared(app, Event(), req, lambda text: TextPart(text=text))
        self.assertIn("[lh.tool_reply]", "\n".join(p.text for p in with_tool.extra_user_content_parts))
        self.assertNotIn("[lh.tool_reply]", "\n".join(p.text for p in without_tool.extra_user_content_parts))


if __name__ == "__main__":
    unittest.main()
