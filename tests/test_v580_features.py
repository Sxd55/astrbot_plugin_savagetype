"""v5.8.0 新特性单元测试：图片与工具历史上下文优化、群身份工具、自动清理缓存、指令白名单及预设协调。"""

import unittest
from types import SimpleNamespace

from savagetype.contexthistory import (
    IMAGE_HISTORY_PLACEHOLDER,
    TOOL_HISTORY_PLACEHOLDER,
    find_completed_tool_result_groups,
    sanitize_history_contexts,
    sanitize_request_history,
)
from savagetype.groupidentity import (
    QueryGroupMemberIdentityTool,
    QueryGroupManagementIdentityTool,
    QueryGroupMemberBirthdayTool,
    QueryGroupUpcomingBirthdaysTool,
    build_member_result,
    format_tool_result,
    GroupQueryContext,
)
from savagetype.autoclean import AutoCacheCleanupModule
from savagetype.builtinallow import (
    BuiltinCommandAllowlistModule,
    sanitize_command_allowlist,
    should_keep_handler,
)
from savagetype.presets import (
    PRESET_DEFINITIONS,
    PRESET_METADATA,
    diff_preset,
    get_preset_defaults,
    resolve_effective_config,
)


class TestV580Features(unittest.TestCase):
    def test_image_history_sanitization(self):
        # 包含 base64 图片的消息
        b64_img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        contexts = [
            {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": b64_img}}]},
            {"role": "assistant", "content": "好的看到了"},
            {"role": "user", "content": "上一张图是什么？"},
        ]
        sanitized, changed = sanitize_history_contexts(contexts, clean_images=True, clean_tools=False)
        self.assertTrue(changed)
        self.assertEqual(sanitized[0]["content"][1]["type"], "text")
        self.assertEqual(sanitized[0]["content"][1]["text"], IMAGE_HISTORY_PLACEHOLDER)

    def test_tool_history_sanitization(self):
        # 工具调用且已被后续 assistant 回复消费（闭环）
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_123"}]},
            {"role": "tool", "tool_call_id": "call_123", "content": "非常非常长的网页搜索结果，占用大量 Token"},
            {"role": "assistant", "content": "搜索完毕，答案是 42"},
            {"role": "user", "content": "谢谢"},
        ]
        groups = find_completed_tool_result_groups(messages)
        self.assertEqual(groups, [[1]])

        sanitized, changed = sanitize_history_contexts(messages, clean_images=False, clean_tools=True)
        self.assertTrue(changed)
        self.assertEqual(sanitized[1]["content"], TOOL_HISTORY_PLACEHOLDER)

    def test_tool_history_not_completed_keeps_content(self):
        # 正在执行中的工具调用（还未被 assistant 消费闭环）不能被替换
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_999"}]},
            {"role": "tool", "tool_call_id": "call_999", "content": "进行中的中间结果"},
        ]
        groups = find_completed_tool_result_groups(messages)
        self.assertEqual(groups, [])
        sanitized, changed = sanitize_history_contexts(messages, clean_images=False, clean_tools=True)
        self.assertFalse(changed)
        self.assertEqual(sanitized[1]["content"], "进行中的中间结果")

    def test_group_identity_formatting(self):
        ctx = GroupQueryContext(event=None, group_id="123456", group_name="测试群")
        raw_member = {
            "user_id": 10001,
            "role": "admin",
            "nickname": "群友A",
            "card": "小A",
            "title": "龙王",
            "level": 5,
        }
        res = build_member_result(ctx, raw_member)
        self.assertEqual(res["group_id"], "123456")
        self.assertEqual(res["member"]["role"], "管理员")
        self.assertEqual(res["member"]["card"], "小A")
        self.assertEqual(res["member"]["special_title"], "龙王")

        tool_json = format_tool_result({"ok": True, **res})
        self.assertIn('"ok":true', tool_json)
        self.assertIn('"role":"管理员"', tool_json)

    def test_autoclean_idle_logic(self):
        cleaner = AutoCacheCleanupModule(logger=None)
        # 刚开机时应该处于保护缓冲期
        idle, reason = cleaner._is_idle()
        self.assertFalse(idle)
        self.assertIn("开机等待缓冲中", reason)

        # 模拟运行超 10 分钟且长期无活动
        cleaner._started_at -= 700
        cleaner._last_activity_at -= 700
        idle, reason = cleaner._is_idle()
        self.assertTrue(idle)

        # 有正在处理的 LLM 请求时不清理
        cleaner.begin_request_activity()
        idle, reason = cleaner._is_idle()
        self.assertFalse(idle)
        self.assertIn("存在 1 个正在处理的请求", reason)
        cleaner.end_request_activity()

    def test_builtin_allowlist_filtering(self):
        allowlist = sanitize_command_allowlist(["help", "/reset", "stats", "unknown_cmd"])
        self.assertEqual(allowlist, {"help", "reset", "stats"})

        h_help = SimpleNamespace(
            handler_module_path="astrbot.builtin_stars.builtin_commands.main",
            handler_name="help",
        )
        h_stop = SimpleNamespace(
            handler_module_path="astrbot.builtin_stars.builtin_commands.main",
            handler_name="stop",
        )
        h_other_plugin = SimpleNamespace(
            handler_module_path="other_plugin.main",
            handler_name="stop",
        )

        self.assertTrue(should_keep_handler(h_help, allowlist))
        self.assertFalse(should_keep_handler(h_stop, allowlist))
        self.assertTrue(should_keep_handler(h_other_plugin, allowlist))  # 非内置插件不拦截

    def test_presets_coordination(self):
        # 验证所有预设都正确声明了 5 个新参数
        new_keys = [
            "clean_image_history_context",
            "clean_tool_history_context",
            "group_identity_tools_enabled",
            "auto_cache_cleanup_enabled",
            "builtin_command_allowlist_enabled",
        ]
        for key in new_keys:
            self.assertIn(key, PRESET_METADATA)

        for p_name in ("daily", "frugal", "assistant", "rpg"):
            defaults = get_preset_defaults(p_name)
            for key in new_keys:
                self.assertIn(key, defaults)

        # 验证差异与解析
        cfg_daily = {"config_preset": "daily"}
        self.assertTrue(resolve_effective_config(cfg_daily, "clean_image_history_context", False))
        self.assertTrue(resolve_effective_config(cfg_daily, "group_identity_tools_enabled", False))

        cfg_frugal = {"config_preset": "frugal"}
        self.assertFalse(resolve_effective_config(cfg_frugal, "group_identity_tools_enabled", True))

        diffs = diff_preset(cfg_daily, "frugal")
        changed_keys = [d["key"] for d in diffs if d["changed"]]
        self.assertIn("group_identity_tools_enabled", changed_keys)


if __name__ == "__main__":
    unittest.main()
