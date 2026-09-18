import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from savagetype.coexistence import Coexistence
from savagetype.presets import (
    PRESET_DEFINITIONS,
    get_preset_defaults,
    resolve_effective_config,
)


class MockEvent:
    def __init__(self, extra=None):
        self._extra = dict(extra or {})

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)


class MockStar:
    def __init__(self, name: str, config=None, activated: bool = True):
        self.name = name
        self.config = dict(config or {})
        self.activated = activated


class TestPresets(unittest.TestCase):
    def test_preset_definitions_structure(self):
        for preset_name in ("daily", "frugal", "assistant", "rpg"):
            defaults = get_preset_defaults(preset_name)
            self.assertIn("top_k", defaults)
            self.assertIn("inject_max_chars", defaults)
            self.assertIn("extract_cooldown_seconds", defaults)

    def test_resolve_daily_preset(self):
        cfg = {"config_preset": "daily"}
        # 用户未配置 top_k 时，采用 daily 的推荐值 3
        top_k = resolve_effective_config(cfg, "top_k", default=5)
        self.assertEqual(top_k, 3)

    def test_resolve_frugal_preset(self):
        cfg = {"config_preset": "frugal"}
        # frugal 档位压缩到 2
        top_k = resolve_effective_config(cfg, "top_k", default=5)
        self.assertEqual(top_k, 2)
        # inject_max_chars 压缩到 400
        chars = resolve_effective_config(cfg, "inject_max_chars", default=1000)
        self.assertEqual(chars, 400)
        # 关闭跨窗口与流
        self.assertFalse(resolve_effective_config(cfg, "cross_window_enabled", default=True))

    def test_resolve_custom_preset_and_user_override(self):
        # 1. custom 模式：用户自定义参数直接生效
        cfg_custom = {"config_preset": "custom", "top_k": 8}
        self.assertEqual(resolve_effective_config(cfg_custom, "top_k", default=3), 8)

        # 2. 预设模式：托管参数由预设接管（daily 推荐值为 3）
        cfg_daily = {"config_preset": "daily", "top_k": 10}
        self.assertEqual(resolve_effective_config(cfg_daily, "top_k", default=5), 3)

        # 3. 预设模式：非托管项（如 owner_qq）100% 保持用户配置
        cfg_owner = {"config_preset": "daily", "owner_qq": "12345678"}
        self.assertEqual(resolve_effective_config(cfg_owner, "owner_qq", default=""), "12345678")


class TestCoexistenceAvoidance(unittest.TestCase):
    def test_avoidance_on_event_flag(self):
        coex = Coexistence(enabled=True)
        # 普通事件不避让
        ev1 = MockEvent()
        self.assertFalse(coex.should_skip_reply_gate(ev1))

        # 带有 savagereply 标记的事件自动避让
        ev2 = MockEvent({"_savage_active_reply": True})
        self.assertTrue(coex.should_skip_reply_gate(ev2))

    def test_avoidance_on_star_detection(self):
        coex = Coexistence(enabled=True)
        # 假装发现了开启了 active_reply_enabled 的 savagereply
        reply_star = MockStar(
            name="astrbot_plugin_savagereply",
            config={"active_reply_enabled": True},
        )
        coex.refresh([reply_star])

        self.assertTrue(coex.skip_reply_gate)
        self.assertTrue(coex.should_skip_reply_gate(None))
        self.assertIn("active reply yielded to savagereply", coex.reasons)


if __name__ == "__main__":
    unittest.main()
