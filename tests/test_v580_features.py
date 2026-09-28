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

    def test_group_identity_tools_dataclass_and_resilient_creation(self):
        from savagetype.groupidentity import (
            get_all_group_identity_tools,
            GROUP_MEMBER_TOOL_NAME,
            GROUP_MANAGEMENT_TOOL_NAME,
            GROUP_MEMBER_BIRTHDAY_TOOL_NAME,
            GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME,
        )
        tools = get_all_group_identity_tools()
        # 即使在没有真实 AstrBot 依赖的离线环境下，如果 FunctionTool 不存在，返回空；
        # 但我们单独测试各 tool 类的 dataclass 默认参数生成
        tool1 = QueryGroupMemberIdentityTool()
        self.assertEqual(tool1.name, GROUP_MEMBER_TOOL_NAME)
        self.assertIn("target", tool1.parameters["properties"])

        tool2 = QueryGroupManagementIdentityTool()
        self.assertEqual(tool2.name, GROUP_MANAGEMENT_TOOL_NAME)
        self.assertIn("scope", tool2.parameters["properties"])

        tool3 = QueryGroupMemberBirthdayTool()
        self.assertEqual(tool3.name, GROUP_MEMBER_BIRTHDAY_TOOL_NAME)
        self.assertIn("target", tool3.parameters["properties"])

        tool4 = QueryGroupUpcomingBirthdaysTool()
        self.assertEqual(tool4.name, GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME)
        self.assertIn("days", tool4.parameters["properties"])

    def test_debounce_qualifies_wake_and_bot_name_bypass(self):
        import sys
        from unittest.mock import MagicMock

        mock_keys = []
        for mod_name in [
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.provider",
            "astrbot.api.star", "astrbot.api.web", "astrbot.core.agent.message",
            "astrbot.core.provider.provider", "astrbot.core.utils.astrbot_path"
        ]:
            if mod_name not in sys.modules:
                m = MagicMock()
                if mod_name == "astrbot.api.star":
                    m.register = lambda *a, **kw: (lambda cls: cls)
                    class _BaseStar:
                        def __init__(self, context=None, *a, **kw):
                            self.context = context
                    m.Star = _BaseStar
                sys.modules[mod_name] = m
                mock_keys.append(mod_name)

        try:
            import importlib
            if "main" in sys.modules:
                del sys.modules["main"]
            import main as plugin_main
            SavageTypePlugin = plugin_main.SavageTypePlugin

            class FakeContext:
                def __init__(self, bot_name="小萨"):
                    self._cfg = {"bot_name": bot_name, "wake_words": ["萨维奇"]}
                def get_config(self):
                    return self._cfg
                def register_web_api(self, *a, **kw):
                    pass

            plugin = SavageTypePlugin(FakeContext(), config={"debounce_enabled": True, "debounce_skip_wake": True})

            # 模拟“小萨来色色”事件
            event_wake_name = SimpleNamespace(
                message_str="小萨来色色",
                unified_msg_origin="group:123456",
                message_obj=SimpleNamespace(self_id="9999", message_id="101", message=[]),
                get_sender_id=lambda: "2412260046",
            )
            # 因为叫了名字“小萨”，哪怕只有 5 个字，也不能被防抖拦截（必须放行立即回复）
            self.assertFalse(plugin._debounce_qualifies(event_wake_name))

            # 模拟带原生 is_wake=True 标记的事件
            event_wake_flag = SimpleNamespace(
                message_str="来色色",
                is_wake=True,
                unified_msg_origin="group:123456",
                message_obj=SimpleNamespace(self_id="9999", message_id="102", message=[]),
                get_sender_id=lambda: "2412260046",
            )
            self.assertFalse(plugin._debounce_qualifies(event_wake_flag))

            # 模拟普通没带名字、未唤醒的断句碎片（例如“在吗”），应该被防抖收集
            event_incomplete = SimpleNamespace(
                message_str="在吗",
                unified_msg_origin="group:123456",
                message_obj=SimpleNamespace(self_id="9999", message_id="103", message=[]),
                get_sender_id=lambda: "2412260046",
            )
            self.assertTrue(plugin._debounce_qualifies(event_incomplete))
        finally:
            for k in mock_keys:
                sys.modules.pop(k, None)


if __name__ == "__main__":
    unittest.main()

