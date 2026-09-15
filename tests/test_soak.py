"""Adversarial + scale + migration tests (offline, stdlib only).

Run: python tests/test_soak.py
Slower than test_core.py by design. A failure here is a real robustness bug,
not a unit nit: hostile model output, concurrent writes, big libraries and
old databases must all degrade gracefully, never crash or silently lose data.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from savagetype.contradiction import ContradictionEngine  # noqa: E402
from savagetype.events import EventPipeline  # noqa: E402
from savagetype.extract import Extractor  # noqa: E402
from savagetype.models import TimelineEvent  # noqa: E402
from savagetype.retrieve import classify_route  # noqa: E402
from savagetype.service import SavageTypeService  # noqa: E402
from savagetype.store import Store  # noqa: E402
from savagetype.util import now_ts, parse_time_range  # noqa: E402


def _timeline_row(store, i, ts, content, window="w1", sid="u1", role="user"):
    store.add_timeline(
        {
            "ts": ts, "speaker_id": sid, "speaker_name": sid, "bot_id": "b",
            "window_tag": window, "role": role, "content": content,
            "fingerprint": f"soak-{i}-{ts}-{content[:8]}",
        }
    )


class PoisonLLMTest(unittest.TestCase):
    """Hostile model output must never crash the pipeline or wedge the cursor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "poison.db")
        self.base = now_ts() - 7200
        _timeline_row(self.store, 1, self.base, "我昨天去了成都")
        _timeline_row(self.store, 2, self.base + 60, "吃了火锅，很好吃")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _pipe(self, summary, verify='{"pass": true, "reason": "", "fix_hint": ""}'):
        async def fake_llm(_prompt):
            return summary

        async def fake_verify(_prompt):
            return verify

        return EventPipeline(
            self.store,
            {
                "event_enabled": True, "event_gap_minutes": 30,
                "event_min_messages": 2, "event_max_per_run": 5,
            },
            None, fake_llm, fake_verify,
        )

    def test_markdown_wrapped_json_parses(self):
        body = json.dumps(
            {
                "kind": "life", "title": "成都之行", "summary": "去了成都吃火锅",
                "highlights": ["吃火锅"], "keywords": ["成都"],
                "importance": 0.7, "confidence": 0.9,
            },
            ensure_ascii=False,
        )
        pipe = self._pipe(f"以下是整理结果：\n```json\n{body}\n```\n完毕")
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        event = self.store.events_by_status("live")[0]
        self.assertEqual(event.review_status, "ai_passed")

    def test_empty_list_falls_back_without_raise(self):
        pipe = self._pipe("[]")
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        event = self.store.events_by_status("live")[0]
        self.assertEqual(event.review_status, "needs_review")

    def test_wrong_types_coerced_safely(self):
        pipe = self._pipe(
            json.dumps(
                {
                    "kind": "movie", "title": 12345, "summary": "",
                    "highlights": "吃火锅", "keywords": "成都",
                    "importance": "高", "confidence": None,
                },
                ensure_ascii=False,
            )
        )
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        event = self.store.events_by_status("live")[0]
        self.assertEqual(event.kind, "life")
        self.assertEqual(event.title, "12345")
        self.assertEqual(event.highlights, ["吃火锅"])
        self.assertEqual(event.keywords, ["成都"])
        self.assertAlmostEqual(event.importance, 0.5)
        # confidence 取不到合法值时回退到兜底值，而不是崩。
        self.assertAlmostEqual(event.confidence, 0.3)

    def test_verify_without_pass_field_passes_through(self):
        pipe = self._pipe(
            json.dumps({"kind": "talk", "title": "闲聊", "summary": "聊了天气"}),
            verify='{"ok": true}',
        )
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        self.assertEqual(self.store.events_by_status("live")[0].review_status, "ai_passed")

    def test_normalize_item_bad_numbers_do_not_kill_batch(self):
        from savagetype.contradiction import ContradictionEngine

        async def fake_llm(_prompt):
            return json.dumps(
                [
                    {
                        "source_event_id": 1, "plain": "喜欢喝茶", "keywords": [],
                        "subject": "self", "attribute": "likes", "value": "茶",
                        "confidence": "高", "first_person": "yes",
                        "explicit_correction": None, "mention_policy": "mention",
                        "write_op": "create", "ttl_seconds": "三天", "topic": "",
                    }
                ]
            )

        engine = ContradictionEngine(self.store, high_evidence=0.8)
        extractor = Extractor(self.store, engine, llm=fake_llm)
        events = self.store.unsummarized(10)
        payloads = asyncio.run(extractor.normalize_llm(events))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["confidence"], 0.6)

    def test_always_failing_llm_retries_without_loss(self):
        calls = {"n": 0}
        real_add = self.store.add_event

        async def boom(_prompt):
            calls["n"] += 1
            raise RuntimeError("provider down")

        async def ok_verify(_prompt):
            return '{"pass": true}'

        pipe = EventPipeline(
            self.store,
            {
                "event_enabled": True, "event_gap_minutes": 30,
                "event_min_messages": 2, "event_max_per_run": 5,
                "event_merge_minutes": 0,
            },
            None, boom, ok_verify,
        )
        # LLM 挂了走确定性兜底：事件照写（待审），不丢不卡。
        fallback_result = asyncio.run(pipe.run())
        self.assertEqual(fallback_result.get("created"), 1)
        self.assertGreaterEqual(calls["n"], 1)

        # 再来一段新的；写库失败（非 LLM 问题）才记 failed，且游标不动、下轮重试。
        _timeline_row(self.store, 3, self.base + 3600, "我决定下个月去成都")
        _timeline_row(self.store, 4, self.base + 3660, "还要去吃火锅")

        def boom_add(_payload, **_kw):
            raise sqlite3.OperationalError("disk hiccup")

        self.store.add_event = boom_add
        try:
            first = asyncio.run(pipe.run())
        finally:
            self.store.add_event = real_add
        self.assertEqual(first.get("failed"), 1)
        self.assertEqual(first.get("created"), 0)
        cursor = self.store.get_meta("event_cursor")
        # 恢复后同一段被重新处理且只写一次（不丢、不重复）。
        second = asyncio.run(pipe.run())
        self.assertEqual(second.get("created"), 1)
        self.assertNotEqual(self.store.get_meta("event_cursor"), cursor)
        self.assertEqual(len(self.store.events_by_status("live")), 2)
        third = asyncio.run(pipe.run())
        self.assertTrue(third.get("skipped"))
        self.assertEqual(len(self.store.events_by_status("live")), 2)


class BoundaryPhraseTest(unittest.TestCase):
    def test_time_range_rejects_impossible_months(self):
        now = now_ts()
        self.assertEqual(parse_time_range("13月", now), (0, 0, ""))
        self.assertEqual(parse_time_range("2026年13月", now), (0, 0, ""))
        # "3-4月" 里 4 的前面是 "-"，不能误解析。
        self.assertEqual(parse_time_range("3-4月", now), (0, 0, ""))

    def test_chinese_numerals_do_not_match(self):
        now = now_ts()
        self.assertEqual(parse_time_range("最近两周", now), (0, 0, ""))

    def test_discourse_marker_routes_history(self):
        # "原来是这样" 只是语气词，但按历史路由走也无害（仍返回 live 结果）。
        self.assertEqual(classify_route("原来是这样"), "history")
        self.assertEqual(classify_route(""), "low_info")
        self.assertEqual(classify_route("!!!"), "long_term")

class DossierPrivacyTest(unittest.TestCase):
    """档案卡不能成为隐私过滤的旁路。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dossier.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dossier_card_respects_privacy(self):
        from savagetype.service import SavageTypeService

        self.store.add_fact(
            {
                "subject": "self", "attribute": "note", "value": "养了两只猫",
                "content": "我养了两只猫", "speaker_id": "u1", "speaker_name": "阿U",
                "scope": "person", "confidence": 0.9, "first_person": 1,
                "window_tag": "default:FriendMessage:u1-1",
            }
        )
        service = SavageTypeService(
            store=self.store, config={"memory_session_isolation": "strict"},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None, logger=None,
        )
        service.apply_config()
        group_card = service.dossier_for(
            "u1", window_tag="aiocqhttp:GroupMessage:1", query="我", isolation="strict"
        )
        self.assertNotIn("两只猫", group_card.get("card") or "")
        dm_card = service.dossier_for(
            "u1", window_tag="default:FriendMessage:u1-1", query="我", isolation="strict"
        )
        self.assertIn("两只猫", dm_card.get("card") or "")

        async def check():
            pack, _r, _s = await service.build_injection("我养了什么", "u1", window_tag="aiocqhttp:GroupMessage:1")
            return pack

        self.assertNotIn("两只猫", asyncio.run(check()))

    def test_empty_and_garbage_queries_do_not_crash(self):
        service = SavageTypeService(
            store=Store(Path(tempfile.mkdtemp()) / "x.db"),
            config={}, llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None, logger=None,
        )
        for query in ["", "!!!", "，，，", "a", "我"]:
            result = asyncio.run(service.retrieve_for(query, "u1", window_tag="w1"))
            self.assertTrue(result.route)
        service.store.close()


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_writes_do_not_corrupt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "conc.db")
        self.addCleanup(store.close)
        fact_id = store.add_fact(
            {
                "subject": "self", "attribute": "note", "value": "并发",
                "content": "并发", "speaker_id": "u1", "speaker_name": "u1",
                "confidence": 0.8, "first_person": 1, "window_tag": "w1",
            }
        )
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(i: int):
            try:
                barrier.wait(timeout=10)
                for n in range(50):
                    store.add_timeline(
                        {
                            "ts": now_ts(), "speaker_id": f"u{i}", "speaker_name": f"u{i}",
                            "bot_id": "b", "window_tag": "w1", "role": "user",
                            "content": f"消息 {n}", "fingerprint": f"conc-{i}-{n}",
                        }
                    )
                    store.bump_access(fact_id)
                    store.add_recall("w1", [fact_id], now_ts())
                    store.add_event_recall("w1", [fact_id], now_ts())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(store.counts()["timeline"], 400)
        self.assertEqual(store.recent_recall_ids("w1", 0), {fact_id})


class ScaleTest(unittest.TestCase):
    FACTS = 2000
    EVENTS = 100

    def test_bulk_library_retrieval_latency(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "scale.db")
        self.addCleanup(store.close)
        now = now_ts()
        for i in range(self.FACTS):
            store.add_fact(
                {
                    "subject": "self",
                    "attribute": "note" if i % 3 else "likes",
                    "value": f"话题{i % 50}条目{i}",
                    "content": f"第{i}条记忆，关于话题{i % 50}和小明",
                    "speaker_id": f"u{i % 50}",
                    "speaker_name": f"人{i % 50}",
                    "confidence": 0.6 + (i % 4) * 0.1,
                    "first_person": 1,
                    "window_tag": f"w{i % 10}",
                    "keywords": ["小明", f"话题{i % 50}"],
                    "created_at": now - (i % 300) * 86400,
                    "updated_at": now - (i % 300) * 86400,
                },
                bump=False,
            )
        for i in range(self.EVENTS):
            store.add_event(
                {
                    "title": f"事件{i}", "summary": f"和小明去做了事情{i}",
                    "speaker_id": f"u{i % 50}", "speaker_name": f"人{i % 50}",
                    "speaker_ids": [f"u{i % 50}"],
                    "participants": [
                        {"id": f"u{i % 50}", "name": f"人{i % 50}"},
                        {"id": "u9", "name": "小明"},
                    ],
                    "keywords": ["小明", f"事件{i % 10}"],
                    "window_tag": f"w{i % 10}",
                    "start_ts": now - (i % 200) * 86400,
                    "end_ts": now - (i % 200) * 86400 + 3600,
                    "importance": 0.6, "confidence": 0.8,
                    "review_status": "ai_passed", "evidence": [1],
                },
                bump=False,
            )
        store.bump_revision()
        service = SavageTypeService(
            store=store, config={"memory_session_isolation": "off"},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None, logger=None,
        )
        service.apply_config()

        started = time.perf_counter()
        entity_hits = store.entities_in_text("小明最近在成都干嘛")
        entity_ms = (time.perf_counter() - started) * 1000
        self.assertIn("小明", entity_hits)

        started = time.perf_counter()
        result = asyncio.run(service.retrieve_for("小明最近在成都干嘛", "u3", window_tag="w3"))
        pack, _result, snapshot = asyncio.run(
            service.build_injection("小明最近在成都干嘛", "u3", window_tag="w3")
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"\nscale: entities={entity_ms:.1f}ms retrieve+inject={elapsed_ms:.1f}ms "
              f"hits={len(result.hits)} events={len(result.events)} pack={len(pack)}")
        self.assertLess(elapsed_ms, 5000)
        self.assertLess(entity_ms, 2000)
        self.assertTrue(pack)


class MigrationTest(unittest.TestCase):
    """Simulate upgrading from a pre-4.3 database file."""

    def _old_db(self, path: Path, facts: int = 60) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE facts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, attribute TEXT NOT NULL, "
            "value TEXT NOT NULL, content TEXT NOT NULL, speaker_id TEXT NOT NULL, "
            "speaker_name TEXT NOT NULL DEFAULT '', bot_id TEXT NOT NULL DEFAULT '', "
            "window_tag TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5, "
            "evidence TEXT NOT NULL DEFAULT '[]', mention_policy TEXT NOT NULL DEFAULT 'mention', "
            "first_person INTEGER NOT NULL DEFAULT 0, explicit_correction INTEGER NOT NULL DEFAULT 0, "
            "source TEXT NOT NULL DEFAULT 'extract', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "superseded_by INTEGER, supersedes INTEGER, fingerprint TEXT NOT NULL, embedding TEXT, "
            "access_count INTEGER NOT NULL DEFAULT 0, last_accessed INTEGER NOT NULL DEFAULT 0, "
            "reason TEXT NOT NULL DEFAULT '', persona_id TEXT NOT NULL DEFAULT '', "
            "slot_key TEXT NOT NULL DEFAULT '', importance REAL NOT NULL DEFAULT 0, "
            "kind TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('revision', '1')")
        now = now_ts()
        for i in range(facts):
            # 旧版单槽 key：约定没有主题后缀。
            conn.execute(
                "INSERT INTO facts(subject, attribute, value, content, speaker_id, speaker_name, "
                "status, confidence, created_at, updated_at, fingerprint, importance, kind, slot_key) "
                "VALUES('self','promise',?,?,'u1','阿U','live',0.8,?,?,?,0.6,'promise',?)",
                (
                    f"带饭{i}", f"答应带饭{i}", now - 86400, now - 3600,
                    f"fp-old-{i}", "|u1|self|promise",
                ),
            )
        conn.commit()
        conn.close()

    def test_old_db_migrates_with_slot_backfill_and_entities(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "old.db"
        self._old_db(path)
        started = time.perf_counter()
        store = Store(path)
        elapsed = time.perf_counter() - started
        try:
            self.assertEqual(store.get_meta("slot_topic_v420"), "1")
            self.assertEqual(store.get_meta("entity_links_v430"), "1")
            print(f"\nmigration: 60 facts in {elapsed:.2f}s")
            self.assertLess(elapsed, 60)
            rows = store.query(
                "SELECT slot_key FROM facts WHERE attribute='promise' LIMIT 5"
            )
            for row in rows:
                # 回填后约定带主题分槽。
                self.assertIn("|", row["slot_key"])
                self.assertGreater(len(row["slot_key"].split("|")), 4)
            linked = store.query("SELECT COUNT(*) AS n FROM entities")[0]["n"]
            self.assertGreater(linked, 0)
            # 新写入继续链接。
            fid = store.add_fact(
                {
                    "subject": "self", "attribute": "note", "value": "和小明爬山",
                    "content": "和小明爬山", "speaker_id": "u1", "speaker_name": "阿U",
                    "confidence": 0.8, "first_person": 1, "keywords": ["小明"],
                }
            )
            self.assertIn("小明", store.entities_in_text("小明去哪"))
            self.assertIn(fid, store.entity_refs(["小明"])[0])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
