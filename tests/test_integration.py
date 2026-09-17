"""Integration tests against the real AstrBot framework package.

Needs `astrbot` installed (it is a heavy dependency on purpose):

    <venv>/Scripts/python tests/test_integration.py -v

Without astrbot the whole module is skipped, so `test_core.py` stays
dependency-free. Uses REAL AstrMessageEvent / ProviderRequest / TextPart /
JSONResponse objects with a stubbed Context: this catches wiring bugs
(wrong attribute names, bad handler signatures, broken routes) that unit
tests with duck-typed fakes cannot see. A fake LLM answers provider calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# AstrBot import has a side effect: it creates ./data in CWD.
# Keep the repo clean by running from a scratch dir (all paths here are absolute).
import os
import tempfile

_IT_WORKDIR = os.path.join(tempfile.gettempdir(), "astrbot-it-workdir")
os.makedirs(_IT_WORKDIR, exist_ok=True)
os.chdir(_IT_WORKDIR)

try:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import (
        AstrBotMessage,
        MessageMember,
        MessageType,
    )
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    import main as plugin_main

    HAS_ASTRBOT = True
except Exception:  # noqa: BLE001
    HAS_ASTRBOT = False


def _facts_for_line(eid: int, text: str) -> list[dict]:
    import re

    items = []
    if "喜欢喝美式" in text and "改口" not in text:
        items.append(("likes", "美式", "我喜欢喝美式", False))
    if "改口" in text:
        items.append(("likes", "拿铁", "现在喜欢喝拿铁", True))
        items.append(("likes", "不美式", "不喝美式了", True))
    if "喜欢养猫" in text:
        items.append(("likes", "养猫", "我喜欢养猫", False))
    if "结婚" in text:
        items.append(("note", "下周结婚", "我下周要结婚了", False))
    out = []
    for attr, value, plain, corr in items:
        out.append(
            {
                "source_event_id": eid, "plain": plain, "keywords": [],
                "subject": "self", "attribute": attr, "value": value,
                "confidence": 0.85, "first_person": True,
                "explicit_correction": corr, "mention_policy": "mention",
                "write_op": "create", "ttl_seconds": 0, "topic": "",
            }
        )
    return out


class FakeContext:
    """Minimal Context: scripted LLM, recorded sends and routes."""

    def __init__(self):
        self.routes: dict[str, tuple] = {}
        self.sent: list[tuple[str, str]] = []
        self.llm_calls: list[str] = []
        self.llm_providers: list[str] = []
        self.refuse_providers: set[str] = set()
        self.stars: list = []

    def get_all_stars(self):
        return list(self.stars)

    def get_all_providers(self):
        return []

    def get_all_embedding_providers(self):
        return []

    @property
    def provider_manager(self):
        return SimpleNamespace(inst_map={})

    def get_provider_by_id(self, _pid):
        return None

    def register_web_api(self, route, handler, methods, *args, **kwargs):
        self.routes[route] = (handler, methods)

    async def get_current_chat_provider_id(self, _session):
        return "fake"

    async def llm_generate(self, chat_provider_id="", prompt=""):
        import re

        self.llm_calls.append(prompt)
        self.llm_providers.append(str(chat_provider_id or ""))
        if chat_provider_id in self.refuse_providers:
            return SimpleNamespace(completion_text="抱歉，我无法协助完成这个请求。", usage=None)
        if "一件事" in prompt:
            cues = [w for w in ("成都", "火锅", "旅游", "结婚", "美式", "养猫", "改口", "拿铁") if w in prompt]
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "kind": "life", "title": "集成测试事件",
                        "summary": "集成测试里发生的一件事",
                        "highlights": [], "keywords": cues[:4],
                        "importance": 0.7, "confidence": 0.9,
                    },
                    ensure_ascii=False,
                ),
                usage=None,
            )
        if "你是记忆审核员" in prompt:
            if "只输出 JSON 数组" in prompt:
                return SimpleNamespace(
                    completion_text=json.dumps(
                        [{"index": i, "pass": True, "reason": "", "fix_hint": ""}
                         for i in range(16)]
                    ),
                    usage=None,
                )
            return SimpleNamespace(
                completion_text=json.dumps({"pass": True, "reason": "", "fix_hint": ""}),
                usage=None,
            )
        if "你是记忆整理器" in prompt and "候选消息" in prompt:
            items = []
            for line in prompt.splitlines():
                match = re.match(r"\[(\d+)\].*?:\s*(.*)$", line.strip())
                if match:
                    items.extend(_facts_for_line(int(match.group(1)), match.group(2)))
            return SimpleNamespace(completion_text=json.dumps(items, ensure_ascii=False), usage=None)
        return SimpleNamespace(completion_text="", usage=None)

    async def send_message(self, umo, chain):
        try:
            text = chain.get_plain_text()
        except Exception:  # noqa: BLE001
            text = str(chain)
        self.sent.append((umo, text))


class FakeRequest:
    class Query(dict):
        def get(self, key, default=None, type=None):  # noqa: A002
            value = super().get(key, default)
            if type is not None and value is not default:
                try:
                    return type(value)
                except (TypeError, ValueError):
                    return default
            return value

    def __init__(self, query=None, body=None):
        self.query = FakeRequest.Query(query or {})
        self._body = body or {}

    async def json(self, default=None):
        return self._body if self._body is not None else (default or {})


def make_event(text, sid="u1", name="阿U", group="1", platform="aiocqhttp", role="member"):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id = "bot1"
    msg.sender = MessageMember(user_id=sid, nickname=name)
    msg.message = [Plain(text)]
    msg.message_str = text
    if group:
        msg.group_id = group
    meta = PlatformMetadata(name=platform, description="test", id=platform)
    event = AstrMessageEvent(text, msg, meta, group or sid)
    event.role = role
    return event


async def _collect(gen):
    return [r async for r in gen]


def _text(result):
    try:
        return result.get_plain_text()
    except Exception:  # noqa: BLE001
        return str(result)


def _json(resp):
    return json.loads(resp.body.decode("utf-8"))


@unittest.skipUnless(HAS_ASTRBOT, "astrbot package not installed")
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self._old_data_dir = plugin_main._data_dir
        root = Path(self.tmp.name)
        plugin_main._data_dir = lambda: root
        self.ctx = FakeContext()
        self.plugin = plugin_main.SavageTypePlugin(self.ctx, self._config())
        self.plugin.service.schedule_learn = lambda: None
        self.store = self.plugin.store
        self.service = self.plugin.service

    def tearDown(self):
        plugin_main._data_dir = self._old_data_dir
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _config(self):
        return {
            "owner_qq": "owner1",
            "memory_source_platforms": "aiocqhttp",
            "memory_whitelist": "",
            "pipeline_enabled": True,
            "extract_enabled": True,
            "extract_min_messages": 2,
            "event_enabled": True,
            "event_gap_minutes": 45,
            "event_min_messages": 2,
            "event_merge_minutes": 0,
            "memory_session_isolation": "strict",
        }

    # -- load & registration -------------------------------------------

    def test_plugin_loads_and_registers_routes(self):
        routes = self.ctx.routes
        for name in (
            "overview", "events", "events/update", "events/evidence", "entities",
            "config", "config/save", "memory", "memory/review", "reviews",
            "pending", "facts/update", "sleep", "diagnostics", "export", "microscope",
        ):
            self.assertIn(f"/astrbot_plugin_savagetype/{name}", routes)
        asyncio.run(self.plugin.initialize())
        backups = list((Path(self.tmp.name) / "backups").glob("*.db"))
        self.assertTrue(backups)

    # -- capture hooks ---------------------------------------------------

    def test_on_message_capture_and_command_skip(self):
        event = make_event("我喜欢喝美式", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.on_message(event))
        self.assertEqual(self.store.counts()["timeline"], 1)
        cmd = make_event("/stype status", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.on_message(cmd))
        self.assertEqual(self.store.counts()["timeline"], 1)

    def test_reply_gate_wakes_group_message(self):
        self.plugin.config.update(
            {
                "reply_gate_enabled": True,
                "reply_gate_mode": "probability",
                "reply_gate_probability": 1.0,
                "reply_gate_cooldown_seconds": 0,
            }
        )
        self.service.apply_config()
        event = make_event("在吗各位", sid="u2", group="1")
        self.assertFalse(event.is_at_or_wake_command)
        asyncio.run(self.plugin.reply_gate(event))
        self.assertTrue(event.is_at_or_wake_command)
        meta = event.get_extra("_stype_reply_gate")
        self.assertTrue(meta and meta.get("fire") is True)
        self.assertEqual(meta.get("mode"), "probability")

    def test_reply_gate_off_by_default(self):
        event = make_event("在吗各位", sid="u2", group="1")
        asyncio.run(self.plugin.reply_gate(event))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertIsNone(event.get_extra("_stype_reply_gate"))

    def test_speak_request_owner_private(self):
        from savagetype.util import ROLE_BOT_ID

        self.plugin.config.update({"speak_enabled": True})
        self.service.apply_config()
        asyncio.run(self.plugin.on_message(make_event("群里说句话", sid="u2", group="1")))
        event = make_event("去群里说：晚上八点开黑", sid="owner1", group="")
        asyncio.run(self.plugin.on_message(event))
        target_sends = [item for item in self.ctx.sent if item[0] == "aiocqhttp:GroupMessage:1"]
        self.assertTrue(target_sends)
        self.assertEqual(target_sends[-1][1], "晚上八点开黑")
        self.assertTrue(event.is_stopped())
        receipt = [item for item in self.ctx.sent if item[0] == event.unified_msg_origin]
        self.assertTrue(receipt and "已发到群 1" in receipt[-1][1])
        rows = self.store.timeline_recent(limit=5, speaker_id=ROLE_BOT_ID)
        self.assertTrue(any("晚上八点开黑" in row.content for row in rows))

    def test_owner_reply_flow(self):
        rid = self.store.add_memory_review(
            scope="owner", speaker_id="owner1", speaker_name="主人",
            raw_text="我喜欢喝茶", plain="喜欢喝茶",
            payload={
                "subject": "self", "attribute": "likes", "value": "茶",
                "content": "我喜欢喝茶",
            },
        )
        event = make_event("是", sid="owner1", name="主人", group="")
        asyncio.run(self.plugin.on_message(event))
        self.assertTrue(event.is_stopped())
        self.assertTrue(self.ctx.sent)
        self.assertIn(f"#{rid}", self.ctx.sent[-1][1])
        self.assertIn("已通过", self.ctx.sent[-1][1])

    def test_coexistence_degrade(self):
        star = SimpleNamespace(name="memory_companion", activated=True)
        self.ctx.stars = [star]
        event = make_event("我喜欢喝茶", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.on_message(event))
        self.assertEqual(self.store.counts()["timeline"], 0)
        self.ctx.stars = []

    # -- extract + events end to end -------------------------------------

    def test_extract_writes_facts(self):
        asyncio.run(self.plugin.on_message(make_event("我喜欢喝美式", sid="owner1", name="主人", group="1")))
        asyncio.run(self.plugin.on_message(make_event("我改口了，现在喜欢喝拿铁", sid="owner1", name="主人", group="1")))
        result = asyncio.run(self.service.maybe_extract(force=True))
        self.assertGreaterEqual(result.get("written") or 0, 1)
        live = self.store.live_by_speaker("owner1", speaker_ids=["owner1"], limit=20)
        self.assertTrue([f for f in live if f.value == "拿铁"])

    def test_event_flow_through_handlers(self):
        asyncio.run(self.plugin.on_message(make_event("昨天和小明去吃了火锅", sid="owner1", name="主人", group="1")))
        asyncio.run(self.plugin.on_message(make_event("决定下个月一起去旅游", sid="owner1", name="主人", group="1")))
        result = asyncio.run(self.service.maybe_extract(force=True))
        self.assertEqual((result.get("events_layer") or {}).get("created"), 1)
        event = make_event("最近有什么事", sid="owner1", name="主人", group="1")
        from astrbot.api.provider import ProviderRequest

        req = ProviderRequest(prompt="最近有什么事", session_id="x")
        asyncio.run(self.plugin.on_llm_request(event, req))
        parts = list(req.extra_user_content_parts or [])
        self.assertTrue(parts)
        self.assertIn("savagetype_memory", parts[0].text)
        self.assertIn("【事件】", parts[0].text)

    def test_on_llm_response_captures_bot(self):
        event = make_event("你好", sid="u1", name="阿U", group="1")
        resp = SimpleNamespace(completion_text="你好呀")
        asyncio.run(self.plugin.on_llm_response(event, resp))
        rows = self.store.timeline_recent(limit=5)
        self.assertTrue([r for r in rows if r.role == "assistant" and "你好呀" in r.content])

    # -- v4.9.0 谁对谁说 / 空@提醒 / 防抖 ------------------------------------

    def test_addressee_captured(self):
        from astrbot.api.message_components import At

        event = make_event("小明看下这个", sid="u2", name="阿强", group="1")
        event.message_obj.message = [At(qq="10001", name="小明"), Plain("小明看下这个")]
        asyncio.run(self.plugin.on_message(event))
        rows = self.store.timeline_recent(limit=3)
        target = [row for row in rows if "小明看下这个" in row.content]
        self.assertTrue(target)
        self.assertIn("at:10001", target[0].addressee)

    def test_blank_mention_hint_injected(self):
        from astrbot.api.message_components import At
        from astrbot.api.provider import ProviderRequest

        self.service.note_reply_target(
            make_event("在的", sid="u2", name="阿U", group="1"),
            {"window_tag": "aiocqhttp:GroupMessage:1", "speaker_id": "u2", "speaker_name": "阿U"},
        )
        event = make_event("", sid="u2", name="阿U", group="1")
        event.message_obj.message = [At(qq="bot1")]
        req = ProviderRequest(prompt="", session_id="x")
        asyncio.run(self.plugin.on_llm_request(event, req))
        texts = [getattr(part, "text", "") for part in (req.extra_user_content_parts or [])]
        self.assertTrue(any("单独 @ 提醒" in text for text in texts), texts)

    def test_debounce_hold_and_flush(self):
        self._reload_service(debounce_enabled=True, debounce_window_seconds=30, debounce_scope="both")
        flushed: dict[str, str] = {}

        async def fake_reinject(_event, text):
            flushed["text"] = text

        self.plugin._reinject = fake_reinject
        first = make_event("在吗", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.debounce_collect(first))
        self.assertTrue(first.is_stopped())
        key = self.plugin._debounce_key(first)
        self.assertIn(key, self.plugin._debounce_hold)

        second = make_event("那个", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.debounce_collect(second))
        self.assertTrue(second.is_stopped())
        self.assertEqual(self.plugin._debounce_hold[key]["count"], 2)

        asyncio.run(self.plugin._debounce_flush(key))
        self.assertEqual(flushed.get("text"), "在吗那个")
        self.assertNotIn(key, self.plugin._debounce_hold)

    def test_debounce_skips_at_and_long_text(self):
        from astrbot.api.message_components import At

        self._reload_service(debounce_enabled=True, debounce_window_seconds=30)
        at_event = make_event("在吗", sid="u1", name="阿U", group="1")
        at_event.message_obj.message = [At(qq="bot1"), Plain("在吗")]
        asyncio.run(self.plugin.debounce_collect(at_event))
        self.assertFalse(at_event.is_stopped())
        long_text = make_event("这是一条很长的消息不应该被防抖挂起", sid="u1", name="阿U", group="1")
        asyncio.run(self.plugin.debounce_collect(long_text))
        self.assertFalse(long_text.is_stopped())
        # 私聊短消息（AstrBot 里 is_at_or_wake_command 恒为 True）仍应被防抖
        private_event = make_event("在吗", sid="u1", name="阿U", group="")
        private_event.is_at_or_wake_command = True
        asyncio.run(self.plugin.debounce_collect(private_event))
        self.assertTrue(private_event.is_stopped())

    # -- v5.0.0 免@接话 v2 -------------------------------------------------

    def test_gate_name_hit_and_turn_filter(self):
        self.plugin.config.update(
            {
                "reply_gate_enabled": True,
                "reply_gate_mode": "probability",
                "reply_gate_probability": 0.0,
                "reply_gate_bot_names": "小助手",
                "reply_gate_min_interval_seconds": 0,
                "reply_gate_cooldown_seconds": 0,
            }
        )
        self.service.apply_config()
        event = make_event("小助手帮我看下", sid="u2", name="阿U", group="1")
        asyncio.run(self.plugin.reply_gate(event))
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.get_extra("_stype_reply_gate").get("reason"), "name_hit")

        from astrbot.api.message_components import At

        recorded: dict[str, dict] = {}
        original_for = self.service.reply_gate_for

        async def spy(event, **kwargs):
            fire, meta = await original_for(event, **kwargs)
            recorded["meta"] = meta
            return fire, meta

        self.service.reply_gate_for = spy
        other = make_event("小明你看", sid="u2", name="阿U", group="1")
        other.message_obj.message = [At(qq="20002", name="小明"), Plain("小明你看")]
        asyncio.run(self.plugin.reply_gate(other))
        self.assertFalse(other.is_at_or_wake_command)
        self.assertEqual(recorded["meta"].get("reason"), "turn_not_open")

    def test_gate_judge_mode(self):
        from astrbot.api.provider import ProviderRequest
        from astrbot.core.agent.message import TextPart

        self.plugin.config.update(
            {
                "reply_gate_enabled": True,
                "reply_gate_mode": "judge",
                "reply_gate_judge_threshold": 0.6,
                "reply_gate_min_interval_seconds": 0,
                "reply_gate_cooldown_seconds": 0,
            }
        )
        self.service.apply_config()

        async def fake_llm(_prompt, _provider):
            return '{"relevance":9,"willingness":8,"social":9,"timing":8}'

        self.service.llm_generate = fake_llm
        event = make_event("这个方案大家怎么看", sid="u2", name="阿U", group="1")
        asyncio.run(self.plugin.reply_gate(event))
        self.assertTrue(event.is_at_or_wake_command)
        meta = event.get_extra("_stype_reply_gate")
        self.assertEqual(meta.get("reason"), "judge_pass")
        self.assertGreaterEqual(meta.get("judge", {}).get("score", 0), 0.6)

    def test_gate_unanswered_delayed_fire(self):
        self.plugin.config.update(
            {
                "reply_gate_enabled": True,
                "reply_gate_mode": "probability",
                "reply_gate_probability": 0.0,
                "reply_gate_unanswered_enabled": True,
                "reply_gate_unanswered_seconds": 3,
                "reply_gate_min_interval_seconds": 0,
                "reply_gate_cooldown_seconds": 0,
            }
        )
        self.service.apply_config()
        fired: dict[str, str] = {}

        async def fake_reinject(_event, text):
            fired["text"] = text

        self.plugin._reinject = fake_reinject
        event = make_event("这个怎么弄？", sid="u2", name="阿U", group="1")
        asyncio.run(self.plugin.reply_gate(event))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertIn("aiocqhttp:GroupMessage:1", self.plugin._gate_pending)

        import time as _time

        asked_ts = int(_time.time()) - 2

        async def run_check():
            await self.plugin._unanswered_check(
                "aiocqhttp:GroupMessage:1", "这个怎么弄？", "u2", {}, 1, event, asked_ts
            )

        asyncio.run(run_check())
        self.assertEqual(fired.get("text"), "这个怎么弄？")

    def test_gate_unanswered_skips_when_answered(self):
        self.plugin.config.update(
            {
                "reply_gate_enabled": True,
                "reply_gate_mode": "probability",
                "reply_gate_probability": 0.0,
                "reply_gate_unanswered_enabled": True,
                "reply_gate_unanswered_seconds": 3,
                "reply_gate_min_interval_seconds": 0,
                "reply_gate_cooldown_seconds": 0,
            }
        )
        self.service.apply_config()
        fired: dict[str, str] = {}

        async def fake_reinject(_event, text):
            fired["text"] = text

        self.plugin._reinject = fake_reinject
        event = make_event("这个怎么弄？", sid="u2", name="阿U", group="1")
        asyncio.run(self.plugin.reply_gate(event))
        window = "aiocqhttp:GroupMessage:1"
        self.assertIn(window, self.plugin._gate_pending)
        from savagetype.util import now_ts as _now_ts

        self.store.add_timeline(
            {
                "ts": _now_ts(),
                "speaker_id": "u3",
                "speaker_name": "小张",
                "bot_id": "b",
                "window_tag": window,
                "role": "user",
                "content": "我知道，是这样",
                "persona_id": "",
                "fingerprint": "answered-1",
            }
        )

        import time as _time

        asked_ts = int(_time.time()) - 2

        async def run_check():
            await self.plugin._unanswered_check(window, "这个怎么弄？", "u2", {}, 1, event, asked_ts)

        asyncio.run(run_check())
        self.assertEqual(fired, {})

    # -- commands ----------------------------------------------------------

    def test_command_status_search_recent(self):
        out = _text(asyncio.run(_collect(self.plugin.cmd_status(make_event("/stype status", group="1"))))[0])
        self.assertIn("Savage Type", out)
        self.store.add_fact(
            {
                "subject": "self", "attribute": "likes", "value": "美式",
                "content": "我喜欢喝美式", "speaker_id": "u1", "speaker_name": "阿U",
                "confidence": 0.9, "first_person": 1, "window_tag": "aiocqhttp:GroupMessage:1",
            }
        )
        out = _text(asyncio.run(_collect(self.plugin.cmd_search(make_event("/stype search 美式", group="1"))))[0])
        self.assertIn("美式", out)
        asyncio.run(self.plugin.on_message(make_event("hello", group="1")))
        out = _text(asyncio.run(_collect(self.plugin.cmd_recent(make_event("/stype recent", group="1"))))[0])
        self.assertIn("hello", out)

    def test_command_events_history_explain_dossier(self):
        self.store.add_event(
            {
                "title": "火锅局", "summary": "和小明吃了火锅", "speaker_id": "u1",
                "speaker_name": "阿U", "speaker_ids": ["u1"],
                "participants": [{"id": "u1", "name": "阿U"}],
                "keywords": ["火锅"], "window_tag": "aiocqhttp:GroupMessage:1",
                "start_ts": 1000, "end_ts": 2000, "importance": 0.7,
                "confidence": 0.9, "review_status": "ai_passed",
            }
        )
        out = _text(asyncio.run(_collect(self.plugin.cmd_events(make_event("/stype events", group="1"))))[0])
        self.assertIn("火锅局", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_history(make_event("/stype history 以前", group="1"))))[0])
        self.assertIn("路线", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_explain(make_event("/stype explain 火锅", group="1"))))[0])
        self.assertIn("route=", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_dossier(make_event("/stype dossier", group="1"))))[0])
        self.assertTrue("短档案" in out or "阿U" in out)

    def test_command_microscope_and_diagnostics(self):
        out = _text(asyncio.run(_collect(self.plugin.cmd_microscope(make_event("/stype microscope", group="1"))))[0])
        self.assertIn("注入", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_diagnostics(make_event("/stype diagnostics", group="1"))))[0])
        self.assertTrue(out)

    def test_admin_commands(self):
        admin = make_event("/stype sleep", group="1")
        admin.role = "admin"
        out = _text(asyncio.run(_collect(self.plugin.cmd_sleep(admin)))[0])
        self.assertIn("merged_duplicates", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_pending(admin)))[0])
        self.assertIn("待审", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_reviews(admin)))[0])
        self.assertIn("待审", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_aliases(admin)))[0])
        self.assertIn("已映射", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_alias(admin, "old1", "u1")))[0])
        self.assertIn("已归并", out)
        rid = self.store.upsert_review("fewshot", "fp-1", "t", {"user": "a", "bot": "b"}, "r")
        out = _text(asyncio.run(_collect(self.plugin.cmd_approve(admin, rid)))[0])
        self.assertIn("approved", out)
        rid2 = self.store.upsert_review("fewshot", "fp-2", "t", {"user": "a", "bot": "b"}, "r")
        out = _text(asyncio.run(_collect(self.plugin.cmd_reject(admin, rid2)))[0])
        self.assertIn("rejected", out)
        mid = self.store.add_memory_review(scope="person", speaker_id="u1", raw_text="x", plain="y")
        out = _text(asyncio.run(_collect(self.plugin.cmd_pass(admin, mid)))[0])
        self.assertIn("已通过", out)
        mid2 = self.store.add_memory_review(scope="person", speaker_id="u1", raw_text="x2", plain="y2")
        out = _text(asyncio.run(_collect(self.plugin.cmd_drop(admin, mid2)))[0])
        self.assertIn("已删除", out)

    def test_command_export_import_roundtrip(self):
        admin = make_event("/stype export", group="1")
        admin.role = "admin"
        out = _text(asyncio.run(_collect(self.plugin.cmd_export(admin)))[0])
        path = out.split("已导出:", 1)[1].strip()
        self.assertTrue(Path(path).is_file())
        out = _text(asyncio.run(_collect(self.plugin.cmd_import(make_event(f"/stype import 预览 {path}", group="1"))))[0])
        self.assertIn("lines", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_import(make_event(f"/stype import 确认 {path}", group="1"))))[0])
        self.assertIn("'ok': True", out)

    def test_command_rollback_and_supersede(self):
        engine = self.service.contradiction
        r1 = engine.ingest(
            {
                "subject": "self", "attribute": "likes", "value": "茶",
                "content": "我喜欢喝茶", "speaker_id": "u1", "speaker_name": "阿U",
                "confidence": 0.9, "first_person": 1,
            },
            "我喜欢喝茶",
        )
        r2 = engine.ingest(
            {
                "subject": "self", "attribute": "likes", "value": "不茶",
                "content": "我改口了", "speaker_id": "u1", "speaker_name": "阿U",
                "confidence": 0.9, "first_person": 1, "explicit_correction": 1,
            },
            "我改口了",
        )
        out = _text(asyncio.run(_collect(self.plugin.cmd_rollback(make_event("/stype rollback", group="1"), r2["fact_id"])))[0])
        self.assertIn("rollback", out)
        pid = self.store.add_pending(
            r1["fact_id"],
            {
                "subject": "self", "attribute": "likes", "value": "不茶",
                "content": "我改口了", "speaker_id": "u1", "speaker_name": "阿U",
                "confidence": 0.9, "first_person": 1, "explicit_correction": 1,
            },
            "joke_or_banter",
        )
        admin = make_event("/stype supersede", group="1")
        admin.role = "admin"
        out = _text(asyncio.run(_collect(self.plugin.cmd_supersede(admin, pid)))[0])
        self.assertTrue(out)

    # -- llm tools -----------------------------------------------------------

    def test_llm_tools(self):
        self.store.add_fact(
            {
                "subject": "self", "attribute": "likes", "value": "美式",
                "content": "我喜欢喝美式", "speaker_id": "u1", "speaker_name": "阿U",
                "confidence": 0.9, "first_person": 1, "window_tag": "aiocqhttp:GroupMessage:1",
            }
        )
        out = asyncio.run(self.plugin.tool_recall(make_event("hi", group="1"), "美式"))
        self.assertIn("美式", out)
        out = asyncio.run(self.plugin.tool_remember(make_event("hi", sid="owner1", name="主人", group="1"), "主人喜欢喝茶"))
        self.assertTrue(out.startswith("ok=true"))
        out = asyncio.run(self.plugin.tool_remember(make_event("hi", group="1"), "路人喜欢喝茶"))
        self.assertIn("denied", out)
        out = asyncio.run(self.plugin.tool_navigate(make_event("hi", group="1"), "美式"))
        self.assertTrue(out)

    # -- panel apis ------------------------------------------------------------

    def test_page_overview_and_config(self):
        plugin_main.request = FakeRequest()
        try:
            resp = asyncio.run(self.plugin.page_overview())
            self.assertEqual(resp.status_code, 200)
            data = _json(resp)
            self.assertIn("counts", data)
            resp = asyncio.run(self.plugin.page_config_get())
            data = _json(resp)
            self.assertIn("event_enabled", data["schema"])
            plugin_main.request = FakeRequest(body={"values": {"memory_session_isolation": "off"}})
            data = _json(asyncio.run(self.plugin.page_config_save()))
            self.assertTrue(data["ok"])
            self.assertEqual(self.plugin.config["memory_session_isolation"], "off")
        finally:
            plugin_main.request = FakeRequest()

    def test_page_events_crud(self):
        eid = self.store.add_event(
            {
                "title": "旧标题", "summary": "旧摘要", "speaker_id": "u1",
                "speaker_name": "阿U", "window_tag": "w1",
                "start_ts": 1000, "end_ts": 2000,
            }
        )
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_events()))
            self.assertEqual(len(data["items"]), 1)
            plugin_main.request = FakeRequest(body={"id": 0})
            self.assertEqual(asyncio.run(self.plugin.page_event_update()).status_code, 400)
            plugin_main.request = FakeRequest(body={"id": eid, "title": "新标题", "importance": 0.9})
            data = _json(asyncio.run(self.plugin.page_event_update()))
            self.assertEqual(data["event"]["title"], "新标题")
            plugin_main.request = FakeRequest(body={"id": eid, "pinned": True})
            data = _json(asyncio.run(self.plugin.page_event_pin()))
            self.assertEqual(data["pinned"], 1)
            plugin_main.request = FakeRequest(body={"ids": [eid]})
            data = _json(asyncio.run(self.plugin.page_events_archive()))
            self.assertEqual(data["count"], 1)
            plugin_main.request = FakeRequest(body={"ids": [eid]})
            data = _json(asyncio.run(self.plugin.page_events_restore()))
            self.assertEqual(data["restored"], [eid])
            plugin_main.request = FakeRequest(query={"id": eid})
            data = _json(asyncio.run(self.plugin.page_event_evidence()))
            self.assertIn("items", data)
            plugin_main.request = FakeRequest(query={"id": eid, "ref": "event"})
            data = _json(asyncio.run(self.plugin.page_entities()))
            self.assertIn("items", data)
            plugin_main.request = FakeRequest(query={"id": 0})
            self.assertEqual(asyncio.run(self.plugin.page_entities()).status_code, 400)
        finally:
            plugin_main.request = FakeRequest()

    def test_page_memory_and_reviews(self):
        fid = self.store.add_fact(
            {
                "subject": "self", "attribute": "likes", "value": "美式",
                "content": "我喜欢喝美式", "speaker_id": "owner1", "speaker_name": "主人",
                "confidence": 0.9, "first_person": 1, "scope": "owner",
            }
        )
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_facts()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest(query={"q": "美式"})
            data = _json(asyncio.run(self.plugin.page_search()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_memory()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest(body={"id": fid, "importance": "bad"})
            self.assertEqual(asyncio.run(self.plugin.page_fact_update()).status_code, 400)
            plugin_main.request = FakeRequest(body={"id": fid, "importance": 0.9})
            data = _json(asyncio.run(self.plugin.page_fact_update()))
            self.assertEqual(data["fact"]["importance"], 0.9)
            plugin_main.request = FakeRequest(body={"id": fid, "pinned": True})
            data = _json(asyncio.run(self.plugin.page_fact_pin()))
            self.assertEqual(data["pinned"], 1)
            rid = self.store.upsert_review("fewshot", "fp-x", "t", {"user": "a", "bot": "b"}, "r")
            plugin_main.request = FakeRequest(body={"id": rid, "status": "approved"})
            data = _json(asyncio.run(self.plugin.page_review_set()))
            self.assertTrue(data["ok"])
            plugin_main.request = FakeRequest(body={"ids": [rid], "status": "pending"})
            data = _json(asyncio.run(self.plugin.page_review_set()))
            self.assertTrue(data["ok"])
            mid = self.store.add_memory_review(scope="person", speaker_id="u1", raw_text="x", plain="y")
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_memory_pending()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest(body={"id": mid, "status": "approved"})
            data = _json(asyncio.run(self.plugin.page_memory_review()))
            self.assertIn("已通过", str(data))
            pid = self.store.add_pending(0, {"subject": "self"}, "joke_or_banter")
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_pending()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest(body={"id": pid})
            data = _json(asyncio.run(self.plugin.page_pending_reject()))
            self.assertTrue(data["ok"])
        finally:
            plugin_main.request = FakeRequest()

    def test_page_misc_and_reset(self):
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_diagnostics()))
            self.assertIn("overview", data)
            data = _json(asyncio.run(self.plugin.page_microscope()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_aliases()))
            self.assertIn("items", data)
            plugin_main.request = FakeRequest(body={"alias": "a", "canonical_id": "b"})
            data = _json(asyncio.run(self.plugin.page_alias_set()))
            self.assertTrue(data["ok"])
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_dossiers()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_profiles()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_reviews()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_providers()))
            self.assertIn("chat", data)
            plugin_main.request = FakeRequest(body={"color": "red"})
            self.assertEqual(asyncio.run(self.plugin.page_theme_set()).status_code, 400)
            plugin_main.request = FakeRequest(
                body={"color": "#112233", "color2": "#445566", "color3": "#778899"}
            )
            data = _json(asyncio.run(self.plugin.page_theme_set()))
            self.assertTrue(data["ok"])
            plugin_main.request = FakeRequest(body={"enabled": False})
            data = _json(asyncio.run(self.plugin.page_dynamic_set()))
            self.assertFalse(data["enabled"])
            plugin_main.request = FakeRequest(
                body={"content": "panel remember", "speaker_id": "u1"}
            )
            data = _json(asyncio.run(self.plugin.page_remember()))
            self.assertIn("action", data)
            plugin_main.request = FakeRequest(body={})
            data = _json(asyncio.run(self.plugin.page_learn()))
            self.assertIn("ok", data)
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_sleep()))
            self.assertIn("merged_duplicates", data)
            plugin_main.request = FakeRequest(body={"confirm": "reset"})
            data = _json(asyncio.run(self.plugin.page_reset()))
            self.assertTrue(data["ok"])
            self.assertEqual(self.store.counts()["timeline"], 0)
            plugin_main.request = FakeRequest(body={"text": "阿U: 2026-01-01 10:00:00\n我喜欢喝茶"})
            data = _json(asyncio.run(self.plugin.page_chat_preview()))
            self.assertTrue(data["count"] >= 1)
            data = _json(asyncio.run(self.plugin.page_chat_import()))
            self.assertGreaterEqual(data.get("added", 0), 1)
            plugin_main.request = FakeRequest(body={"path": str(Path(self.tmp.name) / "nope.jsonl")})
            self.assertEqual(asyncio.run(self.plugin.page_archive_preview()).status_code, 400)
        finally:
            plugin_main.request = FakeRequest()

    def test_page_profile_and_dossier(self):
        self.store.upsert_profile("u1", "阿U", "aiocqhttp")
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_profiles()))
            self.assertTrue(data["items"])
            plugin_main.request = FakeRequest(query={})
            self.assertEqual(asyncio.run(self.plugin.page_profile()).status_code, 400)
            plugin_main.request = FakeRequest(query={"speaker_id": "u1"})
            data = _json(asyncio.run(self.plugin.page_profile()))
            self.assertIn("items", data)
            plugin_main.request = FakeRequest(
                body={"speaker_id": "u1", "speaker_name": "阿优", "note": "备注"}
            )
            data = _json(asyncio.run(self.plugin.page_profile_update()))
            self.assertTrue(data["ok"])
            plugin_main.request = FakeRequest(query={"speaker_id": "u1"})
            data = _json(asyncio.run(self.plugin.page_dossier()))
            self.assertIn("speaker_id", data)
            plugin_main.request = FakeRequest(query={})
            self.assertEqual(asyncio.run(self.plugin.page_dossier()).status_code, 400)
        finally:
            plugin_main.request = FakeRequest()

    def test_on_waiting_llm_request_warms_cache(self):
        if not hasattr(self.plugin, "on_waiting_llm_request"):
            self.skipTest("AstrBot without on_waiting_llm_request")
        event = make_event("你好", group="1")
        asyncio.run(self.plugin.on_waiting_llm_request(event))
        self.assertTrue(True)

    # -- v4.4.0 模型调用策略 -------------------------------------------------------------

    def _reload_service(self, **config):
        self.plugin.config.update(config)
        self.service.config = self.plugin.config
        self.service.apply_config()

    def test_tier_routing_and_usage_ledger(self):
        self._reload_service(quality_provider_id="p-quality", fast_provider_id="p-fast")
        asyncio.run(self.plugin.on_message(make_event("我喜欢喝美式", sid="u1", name="阿U", group="1")))
        result = asyncio.run(self.service.maybe_extract(force=True))
        self.assertGreaterEqual(result.get("written") or 0, 1)
        self.assertIn("p-quality", self.ctx.llm_providers, "缩写应走精准档")
        self.assertIn("p-fast", self.ctx.llm_providers, "审核应走快速档")
        rows = {row["task"]: row for row in self.store.usage_by_task_today()}
        self.assertEqual(rows["normalize"]["source"], "tier:quality")
        self.assertEqual(rows["verify"]["source"], "tier:fast")
        self.assertGreater(self.store.tokens_today(), 0)

    def test_explicit_provider_beats_tier(self):
        self._reload_service(
            quality_provider_id="p-quality",
            normalize_provider_id="p-explicit",
        )
        asyncio.run(self.plugin.on_message(make_event("我喜欢喝美式", sid="u1", name="阿U", group="1")))
        asyncio.run(self.service.maybe_extract(force=True))
        self.assertIn("p-explicit", self.ctx.llm_providers)
        self.assertNotIn("p-quality", self.ctx.llm_providers)

    def test_hard_budget_keeps_timeline_and_reports(self):
        self._reload_service(daily_token_limit=1)
        self.store.add_usage("llm", "p", True, tokens_in=50)
        asyncio.run(self.plugin.on_message(make_event("我喜欢喝美式", sid="u1", name="阿U", group="1")))
        result = asyncio.run(self.service.maybe_extract(force=True))
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "budget")
        self.assertEqual(self.store.counts()["unsummarized"], 1, "消息留给额度恢复后重试")
        self.assertEqual(self.store.counts()["facts_live"], 0)
        data = _json(asyncio.run(self.plugin.page_overview()))
        self.assertGreaterEqual(data["tokens"]["used"], 50)
        self.assertEqual(data["tokens"]["hard_limit"], 1)
        self.assertTrue(any(row.get("skipped") for row in data["tokens"]["by_task"]))

    def test_refusal_retry_uses_fallback(self):
        self._reload_service(quality_provider_id="p-quality", fallback_provider_id="p-backup")
        self.ctx.refuse_providers = {"p-quality"}
        asyncio.run(self.plugin.on_message(make_event("我喜欢喝美式", sid="u1", name="阿U", group="1")))
        result = asyncio.run(self.service.maybe_extract(force=True))
        self.assertGreaterEqual(result.get("written") or 0, 1, "备用模型的结果应正常入库")
        self.assertIn("p-backup", self.ctx.llm_providers)
        self.assertTrue(
            any(row["source"] == "fallback" for row in self.store.usage_by_task_today())
        )
        self.assertTrue(
            any(row["task"] == "normalize" and row["skipped"] for row in self.store.usage_by_task_today()),
            "拒答那次也要计入跳过统计",
        )


if __name__ == "__main__":
    unittest.main()
