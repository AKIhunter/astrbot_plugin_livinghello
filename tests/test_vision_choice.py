"""视觉模型共用选择项的解析顺序与向后兼容。"""
import unittest

from guard_vision_choice import (
    GATE_OVERRIDE_KEY,
    SHARED_KEY,
    UNDERSTAND_OVERRIDE_KEY,
    effective_vision_provider,
)


class VisionChoiceTest(unittest.TestCase):
    def test_override_wins_over_shared(self):
        config = {SHARED_KEY: "shared-vl", GATE_OVERRIDE_KEY: "gate-vl",
                  UNDERSTAND_OVERRIDE_KEY: "understand-vl"}
        self.assertEqual(effective_vision_provider(config, GATE_OVERRIDE_KEY), "gate-vl")
        self.assertEqual(effective_vision_provider(config, UNDERSTAND_OVERRIDE_KEY), "understand-vl")

    def test_blank_override_follows_shared(self):
        config = {SHARED_KEY: " shared-vl ", GATE_OVERRIDE_KEY: "  ",
                  UNDERSTAND_OVERRIDE_KEY: ""}
        self.assertEqual(effective_vision_provider(config, GATE_OVERRIDE_KEY), "shared-vl")
        self.assertEqual(effective_vision_provider(config, UNDERSTAND_OVERRIDE_KEY), "shared-vl")

    def test_shared_covers_both_roles(self):
        config = {SHARED_KEY: "multi_api_router:upstream_5"}
        for key in (GATE_OVERRIDE_KEY, UNDERSTAND_OVERRIDE_KEY):
            self.assertEqual(effective_vision_provider(config, key), "multi_api_router:upstream_5")

    def test_both_blank_keeps_legacy_follow_session(self):
        # 三个键都为空时必须返回空串，调用方才会沿用原来的「跟随会话模型」逻辑。
        for config in ({}, {SHARED_KEY: ""}, {SHARED_KEY: None, GATE_OVERRIDE_KEY: None}):
            self.assertEqual(effective_vision_provider(config, GATE_OVERRIDE_KEY), "")
            self.assertEqual(effective_vision_provider(config, UNDERSTAND_OVERRIDE_KEY), "")

    def test_online_upgrade_keeps_same_upstream(self):
        # 线上升级前两个覆盖键都填了 upstream_5、共用项缺省，升级后必须解析到同一上游。
        config = {GATE_OVERRIDE_KEY: "multi_api_router:upstream_5",
                  UNDERSTAND_OVERRIDE_KEY: "multi_api_router:upstream_5"}
        self.assertEqual(effective_vision_provider(config, GATE_OVERRIDE_KEY),
                         "multi_api_router:upstream_5")
        self.assertEqual(effective_vision_provider(config, UNDERSTAND_OVERRIDE_KEY),
                         "multi_api_router:upstream_5")


if __name__ == "__main__":
    unittest.main()
