import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from savagetype.coexistence import Coexistence
from savagetype.presets import (
    PRESET_DEFINITIONS,
    get_preset_defaults,
    resolve_effective_config,
    diff_preset,
    format_preset_diff_text,
    PRESET_METADATA,
)


class MockEvent:
    def __init__(self, message_str: str | dict = "", extra=None, admin: bool = True):
        if isinstance(message_str, dict) and extra is None:
            extra = message_str
            message_str = ""
        self.message_str = str(message_str or "")
        self._extra = dict(extra or {})
        self._admin = admin

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str):
        return text


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

    def test_diff_preset_daily_to_frugal(self):
        cfg = {"config_preset": "daily"}
        diffs = diff_preset(cfg, "frugal")
        self.assertEqual(len(diffs), len(PRESET_METADATA))

        # 验证 inject_budget_chars 降低
        budget_diff = next(d for d in diffs if d["key"] == "inject_budget_chars")
        self.assertEqual(budget_diff["current_value"], 1200)
        self.assertEqual(budget_diff["target_value"], 400)
        self.assertTrue(budget_diff["changed"])
        self.assertEqual(budget_diff["direction"], "down")
        self.assertEqual(budget_diff["symbol"], "🔽")

        # 验证 cross_window_enabled 关闭
        cross_diff = next(d for d in diffs if d["key"] == "cross_window_enabled")
        self.assertEqual(cross_diff["current_value"], True)
        self.assertEqual(cross_diff["target_value"], False)
        self.assertTrue(cross_diff["changed"])
        self.assertEqual(cross_diff["direction"], "toggle")
        self.assertEqual(cross_diff["symbol"], "🔄")

        # 验证 retrieval_bm25 保持一致
        bm25_diff = next(d for d in diffs if d["key"] == "retrieval_bm25")
        self.assertEqual(bm25_diff["current_value"], True)
        self.assertEqual(bm25_diff["target_value"], True)
        self.assertFalse(bm25_diff["changed"])
        self.assertEqual(bm25_diff["direction"], "same")
        self.assertEqual(bm25_diff["symbol"], "➖")

    def test_diff_preset_to_custom_preserves_user_override(self):
        # 用户在磁盘上配置了自定义值，但在 daily 预设下被遮罩接管
        cfg = {
            "config_preset": "daily",
            "top_k": 88,
            "inject_budget_chars": 3500,
        }
        # 当前在 daily 下生效的是预设推荐值
        self.assertEqual(resolve_effective_config(cfg, "top_k", 5), 3)
        self.assertEqual(resolve_effective_config(cfg, "inject_budget_chars", 800), 1200)

        # 比对切回 custom：目标值精准反映用户保存在磁盘的手动微调值
        diffs = diff_preset(cfg, "custom")
        topk_diff = next(d for d in diffs if d["key"] == "top_k")
        self.assertEqual(topk_diff["target_value"], 88)
        self.assertEqual(topk_diff["target_display"], "88条")

        budget_diff = next(d for d in diffs if d["key"] == "inject_budget_chars")
        self.assertEqual(budget_diff["target_value"], 3500)
        self.assertEqual(budget_diff["target_display"], "3500字")

        # 当用户实际切换到 custom 模式时，手动值 100% 找回生效
        cfg["config_preset"] = "custom"
        self.assertEqual(resolve_effective_config(cfg, "top_k", 5), 88)
        self.assertEqual(resolve_effective_config(cfg, "inject_budget_chars", 800), 3500)

    def test_format_preset_diff_text(self):
        cfg = {"config_preset": "daily"}
        diffs = diff_preset(cfg, "frugal")

        # 1. 预览模式（未应用）
        preview_text = format_preset_diff_text("daily", "frugal", diffs, is_applied=False)
        self.assertIn("预设变更清单预览", preview_text)
        self.assertIn("日常陪伴", preview_text)
        self.assertIn("极致省Token", preview_text)
        self.assertIn("注入字符预算", preview_text)
        self.assertIn("安全机制", preview_text)
        self.assertIn("apply frugal", preview_text)

        # 2. 应用模式（已生效）
        applied_text = format_preset_diff_text("daily", "frugal", diffs, is_applied=True)
        self.assertIn("预设切换成功", applied_text)
        self.assertIn("/stype preset custom", applied_text)


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


class TestPresetCommand(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_preset_workflows(self):
        import tempfile
        import os
        from main import SavageTypePlugin

        class MockContext:
            def get_all_stars(self):
                return []

            def register_web_api(self, *args, **kwargs):
                pass

            def register_page(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_data_dir = os.environ.get("ASTRBOT_DATA_PATH")
            os.environ["ASTRBOT_DATA_PATH"] = tmpdir
            try:
                cfg = {"config_preset": "daily"}
                plugin = SavageTypePlugin(MockContext(), config=cfg)

                # 1. 查看当前预设列表
                ev_view = MockEvent("/stype preset", admin=True)
                results = [r async for r in plugin.cmd_preset(ev_view)]
                self.assertEqual(len(results), 1)
                self.assertIn("当前生效为 [日常陪伴", results[0])
                self.assertIn("可选档位：", results[0])

                # 2. 预览 diff 清单
                ev_diff = MockEvent("/stype preset diff frugal", admin=True)
                results = [r async for r in plugin.cmd_preset(ev_diff)]
                self.assertEqual(len(results), 1)
                self.assertIn("预设变更清单预览", results[0])
                self.assertIn("400字", results[0])
                # 配置本身不被修改
                self.assertEqual(plugin.config["config_preset"], "daily")

                # 3. 权限不足拦截
                ev_no_perm = MockEvent("/stype preset apply frugal", admin=False)
                results = [r async for r in plugin.cmd_preset(ev_no_perm)]
                self.assertEqual(len(results), 1)
                self.assertIn("权限不足", results[0])
                self.assertEqual(plugin.config["config_preset"], "daily")

                # 4. 执行切换
                ev_apply = MockEvent("/stype preset apply frugal", admin=True)
                results = [r async for r in plugin.cmd_preset(ev_apply)]
                self.assertEqual(len(results), 1)
                self.assertIn("预设切换成功", results[0])
                self.assertEqual(plugin.config["config_preset"], "frugal")

                # 5. 切换回 custom
                ev_custom = MockEvent("/stype preset apply custom", admin=True)
                results = [r async for r in plugin.cmd_preset(ev_custom)]
                self.assertEqual(len(results), 1)
                self.assertIn("专家自定义", results[0])
                self.assertEqual(plugin.config["config_preset"], "custom")

            finally:
                if orig_data_dir is not None:
                    os.environ["ASTRBOT_DATA_PATH"] = orig_data_dir
                else:
                    os.environ.pop("ASTRBOT_DATA_PATH", None)


if __name__ == "__main__":
    unittest.main()
