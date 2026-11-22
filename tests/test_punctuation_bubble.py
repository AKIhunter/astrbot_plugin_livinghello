"""气泡级标点保留：代码、链接、括号、场景动作、多段长文不参与标点清理。"""
import unittest

from reply.guard_expression import preserve_punctuation, normalize_punctuation


class PunctuationBubbleTests(unittest.TestCase):
    def test_short_chat_still_gets_spaces(self):
        self.assertFalse(preserve_punctuation("普通，聊天。"))
        self.assertEqual(normalize_punctuation("普通，聊天。", space="1"), "普通 聊天")

    def test_code_link_and_bracket_bubbles_keep_punctuation(self):
        bt = chr(96)
        for text in (
            "看 " + bt + "pip install x" + bt + " 就行。",
            bt * 3 + "python\nprint(1)\n" + bt * 3,
            "看这个 https://example.com/a?b=1。",
            "（笑）",
        ):
            with self.subTest(text=text):
                self.assertTrue(preserve_punctuation(text))

    def test_scene_action_and_multiline_keep_punctuation(self):
        for text in ("*抬手挡住。* 别闹。", "第一段。\n\n第二段！", "很长的一段话。" * 20):
            with self.subTest(text=text):
                self.assertTrue(preserve_punctuation(text))


if __name__ == "__main__":
    unittest.main()
