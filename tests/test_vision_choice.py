"""视觉模型合并后：一个视觉模型项，视频理解另有独立项。"""
import unittest

from images.guard_vision_choice import (
    ROLE_KEYS,
    SHARED_KEY,
    VIDEO_KEY,
    effective_vision_for,
    effective_vision_provider,
)


class VisionChoiceTest(unittest.TestCase):
    def test_gate_and_image_share_one_model(self):
        config = {SHARED_KEY: "multi_api_router:upstream_5"}
        # vision 是内容理解侧传进来的角色名，与 image 同义。
        for role in ("gate", "image", "vision"):
            self.assertEqual(effective_vision_for(config, role), "multi_api_router:upstream_5")

    def test_video_has_own_switch(self):
        config = {SHARED_KEY: "shared-vl", VIDEO_KEY: "video-vl"}
        self.assertEqual(effective_vision_for(config, "video"), "video-vl")
        self.assertEqual(effective_vision_for(config, "gate"), "shared-vl")

    def test_video_falls_back_to_shared(self):
        config = {SHARED_KEY: " shared-vl ", VIDEO_KEY: "   "}
        self.assertEqual(effective_vision_for(config, "video"), "shared-vl")

    def test_blank_everything_keeps_follow_session(self):
        # 都为空时必须返回空串，调用方才会沿用「跟随会话模型」的兜底。
        for config in ({}, {SHARED_KEY: ""}, {SHARED_KEY: None, VIDEO_KEY: None}):
            for role in ("gate", "image", "video"):
                self.assertEqual(effective_vision_for(config, role), "")

    def test_role_table_only_maps_video(self):
        # 除视频外不再有覆盖项；以后拆角色只改这张表。
        self.assertEqual(ROLE_KEYS, {"gate": "", "image": "", "video": VIDEO_KEY})

    def test_unknown_role_follows_image_side(self):
        config = {SHARED_KEY: "shared-vl", VIDEO_KEY: "video-vl"}
        self.assertEqual(effective_vision_for(config, "something-new"), "shared-vl")

    def test_override_key_can_still_be_passed_directly(self):
        config = {SHARED_KEY: "shared-vl", "custom_key": "custom-vl"}
        self.assertEqual(effective_vision_provider(config, "custom_key"), "custom-vl")
        self.assertEqual(effective_vision_provider(config), "shared-vl")


if __name__ == "__main__":
    unittest.main()
