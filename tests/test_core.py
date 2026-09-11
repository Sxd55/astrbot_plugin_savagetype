"""Offline tests for contradiction, heuristic extract, speaker filter, inject budget.

Run: python tests/test_core.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from savagetype.archive import (  # noqa: E402
    archive_low_value,
    compact_summarized_timeline,
    expire_persona_drafts,
    fold_preference_slots,
    import_jsonl,
    import_transcript_events,
    parse_transcript,
)
from savagetype.contradiction import ContradictionEngine  # noqa: E402
from savagetype.extract import Extractor  # noqa: E402
from savagetype.inject import build_pack  # noqa: E402
from savagetype.learn import LearningEngine, is_junk_term, pair_user_bot, term_in_query  # noqa: E402
from savagetype.models import Fact, LearningPack, RetrievalResult, TimelineEvent  # noqa: E402
from savagetype.retrieve import Retriever, classify_route  # noqa: E402
from savagetype.service import SavageTypeService, _unpack_llm_result  # noqa: E402
from savagetype.slots import canonical_attribute, canonical_subject  # noqa: E402
from savagetype.store import Store  # noqa: E402
from savagetype.util import now_ts  # noqa: E402


def _payload(speaker="u1", subject="用户", attribute="likes", value="茶", content=None, **kw):
    return {
        "subject": subject,
        "attribute": attribute,
        "value": value,
        "content": content or f"{subject}{attribute}{value}",
        "speaker_id": speaker,
        "speaker_name": speaker,
        "confidence": kw.pop("confidence", 0.7),
        "first_person": kw.pop("first_person", 1),
        **kw,
    }


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.engine = ContradictionEngine(self.store, high_evidence=0.8)
        self.extractor = Extractor(self.store, self.engine)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_insert_and_supersede(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.assertEqual(r1["action"], "insert")
        r2 = self.engine.ingest(
            _payload(value="咖啡", content="我改口了，喜欢咖啡", explicit_correction=1),
            "我改口了，喜欢咖啡",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes")
        self.assertEqual(live.value, "咖啡")
        self.assertIsNone(self.store.get_fact(r1["fact_id"]))
        rb = self.engine.rollback(live.id)
        self.assertFalse(rb["ok"])

    def test_joke_does_not_overwrite(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r = self.engine.ingest(_payload(value="汽油", content="我喜欢喝汽油哈哈哈开玩笑"), "我喜欢喝汽油哈哈哈开玩笑")
        self.assertEqual(r["action"], "ignored_joke")
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes").value, "茶")

    def test_dislike_flip_stays_on_likes_slot(self):
        self.store.add_timeline(
            {
                "ts": 1,
                "speaker_id": "u1",
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": "w",
                "role": "user",
                "content": "我喜欢hiphop音乐",
                "fingerprint": "t-hiphop-1",
            }
        )
        self.store.add_timeline(
            {
                "ts": 2,
                "speaker_id": "u1",
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": "w",
                "role": "user",
                "content": "我改口了我现在不喜欢听hiphop音乐",
                "fingerprint": "t-hiphop-2",
            }
        )
        facts = self.extractor.extract_heuristic(self.store.unsummarized(limit=10))
        self.assertFalse(any(f["attribute"] == "note" for f in facts))
        likes = [f for f in facts if f["attribute"] == "likes"]
        self.assertGreaterEqual(len(likes), 2)
        self.assertFalse(any(f["attribute"] == "dislikes" for f in facts if "hiphop" in f.get("value", "").lower() or "hiphop" in f.get("content", "").lower()))
        r1 = self.engine.ingest(likes[0], likes[0]["content"])
        r2 = self.engine.ingest(likes[-1], likes[-1]["content"])
        self.assertIn(r2["action"], {"supersede", "refresh"})
        live = self.store.live_by_slot("u1", "self", "likes")
        self.assertIsNotNone(live)
        self.assertTrue(live.value.startswith("不") or "不喜欢" in live.content)
        if r2["action"] == "supersede":
            self.assertIsNone(self.store.get_fact(r1["fact_id"]))

    def test_llm_dislike_alias_collides_likes(self):
        r1 = self.engine.ingest(_payload(attribute="likes", value="hiphop", content="我喜欢hiphop"), "我喜欢hiphop")
        r2 = self.engine.ingest(
            _payload(attribute="dislikes", value="hiphop", content="你不喜欢 hiphop。", explicit_correction=1),
            "我改口了我不喜欢hiphop",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes")
        self.assertTrue(live.value.startswith("不") or "不喜欢" in live.content)
        self.assertIsNone(self.store.get_fact(r1["fact_id"]))
        self.assertIsNone(self.store.live_by_slot("u1", "self", "dislikes"))

    def test_sleep_folds_old_dislike_note(self):
        self.engine.ingest(_payload(attribute="likes", value="不hiphop", content="我改口了我不喜欢hiphop"), "我改口了我不喜欢hiphop")
        now = 1
        self.store.execute(
            """INSERT INTO facts(subject, attribute, value, content, speaker_id, speaker_name, bot_id, window_tag,
                status, confidence, evidence, mention_policy, first_person, explicit_correction, source,
                created_at, updated_at, fingerprint, persona_id, slot_key)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "self",
                "dislikes",
                "hiphop",
                "你不喜欢 hiphop。",
                "u1",
                "阿U",
                "b",
                "w",
                "live",
                0.9,
                "[]",
                "mention",
                0,
                0,
                "legacy",
                now,
                now,
                "legacy-dislike",
                "",
                "legacy-dislike",
            ),
        )
        self.store.execute(
            """INSERT INTO facts(subject, attribute, value, content, speaker_id, speaker_name, bot_id, window_tag,
                status, confidence, evidence, mention_policy, first_person, explicit_correction, source,
                created_at, updated_at, fingerprint, persona_id, slot_key)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "self",
                "note",
                "不喜欢 hiphop",
                "用户明确表示不喜欢 hiphop（此前曾说过喜欢，已改口）。",
                "u1",
                "阿U",
                "b",
                "w",
                "live",
                0.8,
                "[]",
                "mention",
                0,
                0,
                "legacy",
                now,
                now,
                "legacy-note",
                "",
                "legacy-note",
            ),
        )
        n = fold_preference_slots(self.store)
        self.assertEqual(n, 2)
        self.assertIsNotNone(self.store.live_by_slot("u1", "self", "likes"))
        leftover = [f for f in self.store.facts_by_status("live", limit=20) if f.attribute in {"dislikes", "note"}]
        self.assertFalse(leftover)

    def test_hearsay_uncertain(self):
        r = self.engine.ingest(
            _payload(value="猫", content="听说他喜欢猫", first_person=0),
            "听说他喜欢猫",
        )
        self.assertEqual(r["action"], "wrote_uncertain")
        fact = self.store.get_fact(r["fact_id"])
        self.assertEqual(fact.mention_policy, "uncertain")

    def test_high_evidence_pending(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶", confidence=0.9), "我喜欢喝茶")
        self.store.update_fact(r1["fact_id"], access_count=2, confidence=0.9)
        r2 = self.engine.ingest(_payload(value="咖啡", content="我喜欢咖啡"), "我喜欢咖啡")
        self.assertEqual(r2["action"], "pending")
        self.assertEqual(r2["reason"], "high_evidence_needs_confirm")
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes").value, "茶")
        confirmed = self.engine.confirm_pending(r2["pending_id"])
        self.assertTrue(confirmed["ok"])
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes").value, "咖啡")

    def test_heuristic_likes(self):
        self.store.add_timeline(
            {
                "ts": 1,
                "speaker_id": "u1",
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": "w",
                "role": "user",
                "content": "我喜欢喝美式",
                "fingerprint": "a",
            }
        )
        events = self.store.unsummarized(10)
        facts = self.extractor.extract_heuristic(events)
        self.assertTrue(any(f["attribute"] == "likes" and "美式" in f["value"] for f in facts))

    def test_speaker_filter(self):
        self.engine.ingest(_payload(speaker="a", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.engine.ingest(_payload(speaker="b", value="酒", content="我喜欢喝酒"), "我喜欢喝酒")
        retriever = Retriever(self.store)
        import asyncio

        result = asyncio.run(retriever.retrieve("喜欢什么", "a", top_k=8))
        ids = {f.speaker_id for f in result.core + result.related}
        self.assertIn("a", ids)
        self.assertNotIn("b", ids)

    def test_low_info_skip(self):
        self.assertEqual(classify_route("哈哈"), "low_info")
        self.assertEqual(classify_route("你还记得我喜欢什么吗"), "recall")

    def test_inject_budget_and_prefix(self):
        facts = [
            Fact(
                id=i,
                subject="用户",
                attribute="likes",
                value="x" * 80,
                content="x" * 80,
                speaker_id="a",
                speaker_name="a",
                bot_id="",
                window_tag="",
                status="live",
                confidence=0.9,
            )
            for i in range(12)
        ]
        result = RetrievalResult(
            query="q",
            route="long_term",
            path="basic",
            cache="miss",
            hits=[],
            blocked=[],
            core=facts[:4],
            related=facts[4:10],
            uncertain=[],
            superseded=[],
        )
        pack = build_pack(result, budget=400)
        self.assertTrue(pack.startswith("<savagetype_memory>"))
        self.assertIn("</savagetype_memory>", pack)
        self.assertLessEqual(len(pack), 420)
        self.assertNotIn("检索: route=", pack)

    def test_slot_aliases_collide(self):
        self.assertEqual(canonical_attribute("口味"), "likes")
        self.assertEqual(canonical_subject("用户", "u1", "阿U"), "self")
        r1 = self.engine.ingest(_payload(subject="用户", attribute="likes", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r2 = self.engine.ingest(
            _payload(subject="我", attribute="口味", value="咖啡", content="我改口了，喜欢咖啡", explicit_correction=1),
            "我改口了，喜欢咖啡",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes")
        self.assertEqual(live.value, "咖啡")
        self.assertIsNone(self.store.get_fact(r1["fact_id"]))

    def test_persona_isolation(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶", persona_id="p1"), "我喜欢喝茶")
        self.engine.ingest(_payload(value="酒", content="我喜欢喝酒", persona_id="p2"), "我喜欢喝酒")
        a = self.store.live_by_slot("u1", "self", "likes", persona_id="p1")
        b = self.store.live_by_slot("u1", "self", "likes", persona_id="p2")
        self.assertEqual(a.value, "茶")
        self.assertEqual(b.value, "酒")

    def test_speaker_alias(self):
        self.store.set_alias("openid-a", "u1")
        self.assertEqual(self.store.resolve_speaker("openid-a"), "u1")
        self.assertIn("openid-a", self.store.speaker_ids_for("u1"))
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        retriever = Retriever(self.store)
        import asyncio

        result = asyncio.run(
            retriever.retrieve("喜欢什么", "u1", top_k=8, speaker_ids=self.store.speaker_ids_for("u1"))
        )
        self.assertTrue(any(f.value == "茶" for f in result.core + result.related))

    def test_remember_without_correction_pending_on_high_evidence(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶", confidence=0.9), "我喜欢喝茶")
        self.store.update_fact(r1["fact_id"], access_count=2, confidence=0.9)
        r2 = self.engine.ingest(_payload(value="咖啡", content="记住我喜欢咖啡", first_person=1), "记住我喜欢咖啡")
        self.assertEqual(r2["action"], "pending")

    def test_fewshot_pairs_go_to_review(self):
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "今天那个yyds局好顶", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "那把确实离谱", persona_id="p1"),
        ]
        pairs = pair_user_bot(events)
        self.assertEqual(len(pairs), 1)
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True, "fewshot_enabled": True, "jargon_enabled": False, "persona_draft_enabled": False})
        n = engine._queue_fewshots(events)
        self.assertEqual(n, 1)
        pending = self.store.list_reviews("pending", kind="fewshot")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["user"], "今天那个yyds局好顶")
        engine.set_status(pending[0].id, "approved")
        pack = engine.pack_for("yyds", persona_id="p1", route="long_term")
        self.assertEqual(len(pack.fewshots), 1)
        quiet = engine.pack_for("哈哈", persona_id="p1", route="low_info")
        self.assertFalse(quiet.fewshots)

    def test_jargon_inject_only_when_term_in_query(self):
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True})
        self.store.upsert_review(
            "jargon",
            "fp-yyds",
            "yyds",
            {"term": "yyds", "meaning": "永远的神"},
            reason="test",
        )
        rid = self.store.list_reviews("pending", kind="jargon")[0].id
        engine.set_status(rid, "approved")
        hit = engine.pack_for("这波 yyds", persona_id="")
        miss = engine.pack_for("你好", persona_id="")
        self.assertEqual(hit.jargon[0]["meaning"], "永远的神")
        self.assertFalse(miss.jargon)
        quiet = RetrievalResult("你好", "low_info", "skip", "miss", [], [], [], [], [], [])
        pack = build_pack(quiet, budget=400, learning=hit)
        self.assertNotIn("表达样本", pack)
        self.assertNotIn("永远的神", pack)
        mentioned = RetrievalResult("这波 yyds", "low_info", "skip", "miss", [], [], [], [], [], [])
        pack2 = build_pack(mentioned, budget=400, learning=LearningPack(jargon=hit.jargon))
        self.assertIn("永远的神", pack2)

    def test_term_boundary_and_junk(self):
        self.assertTrue(term_in_query("yyds", "这波 yyds 可以"))
        self.assertFalse(term_in_query("yy", "这波 yyds 可以"))
        self.assertTrue(is_junk_term("确实"))
        self.assertTrue(is_junk_term("1"))
        self.assertFalse(is_junk_term("yyds"))

    def test_fewshot_skips_commands(self):
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "/stype status", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "状态正常", persona_id="p1"),
            TimelineEvent(3, 10, "u1", "阿U", "b", "w", "user", "今天那个yyds局好顶", persona_id="p1"),
            TimelineEvent(4, 11, "u1", "阿U", "b", "w", "assistant", "那把确实离谱", persona_id="p1"),
        ]
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True, "fewshot_enabled": True, "jargon_enabled": False, "persona_draft_enabled": False})
        n = engine._queue_fewshots(events)
        self.assertEqual(n, 1)
        pending = self.store.list_reviews("pending", kind="fewshot")
        self.assertEqual(pending[0].payload["user"], "今天那个yyds局好顶")

    def test_fewshot_requires_same_window(self):
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w1", "user", "今天那个yyds局好顶", persona_id="p1"),
            TimelineEvent(2, 2, "u2", "阿V", "b", "w2", "assistant", "那把确实离谱", persona_id="p1"),
        ]
        self.assertEqual(pair_user_bot(events), [])

    def test_reject_jargon_clears_stats(self):
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True})
        self.store.bump_jargon("yyds")
        self.store.bump_jargon("yyds")
        self.assertTrue(self.store.hot_jargon(min_count=2))
        self.store.upsert_review("jargon", "fp-yyds-rej", "yyds", {"term": "yyds", "meaning": "永远的神"})
        rid = self.store.list_reviews("pending", kind="jargon")[0].id
        engine.set_status(rid, "rejected")
        self.assertFalse(self.store.hot_jargon(min_count=1))

    def test_parse_transcript_and_import(self):
        text = (
            "阿U: 2026-09-01 12:00:00\n我喜欢喝茶\n\n"
            "Bot: 2026-09-01 12:00:03\n记下了\n"
        )
        parsed = parse_transcript(text, user_names=["阿U"], bot_names=["Bot"])
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["events"][0]["role"], "user")
        self.assertEqual(parsed["events"][1]["role"], "assistant")
        result = import_transcript_events(self.store, parsed["events"])
        self.assertEqual(result["added"], 2)
        again = import_transcript_events(self.store, parsed["events"])
        self.assertEqual(again["skipped"], 2)

    def test_jsonl_roundtrip_skips_duplicate_fingerprint(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        svc = SavageTypeService(self.store, {}, llm_generate=None, get_provider=None, logger=None)
        dest = Path(self.tmp.name) / "out.jsonl"
        svc.export_jsonl(dest)
        other_dir = tempfile.TemporaryDirectory()
        other = Store(Path(other_dir.name) / "b.db")
        first = import_jsonl(other, dest)
        self.assertGreaterEqual(first["facts"], 1)
        second = import_jsonl(other, dest)
        self.assertGreaterEqual(second["skipped"], 1)
        other.close()
        other_dir.cleanup()

    def test_sleep_compacts_and_archives(self):
        old = now_ts() - 40 * 86400
        self.store.add_timeline(
            {
                "ts": old,
                "speaker_id": "u1",
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": "w",
                "role": "user",
                "content": "旧消息",
                "fingerprint": "old-tl",
            }
        )
        rows = self.store.query("SELECT id FROM timeline")
        self.store.mark_summarized([int(rows[0]["id"])])
        self.store.execute("UPDATE timeline SET ts=? WHERE id=?", (old, int(rows[0]["id"])))
        compacted = compact_summarized_timeline(self.store, retain_days=30)
        self.assertEqual(compacted, 1)
        r = self.engine.ingest(_payload(value="汽水", content="可能喜欢汽水", confidence=0.2, first_person=0), "可能喜欢汽水")
        self.store.update_fact(r["fact_id"], updated_at=old, access_count=0, confidence=0.2)
        archived = archive_low_value(self.store, min_age_days=30, max_confidence=0.45)
        self.assertEqual(archived, 1)
        self.store.upsert_review("persona", "old-draft", "旧草稿", {"draft": "说话短", "created_at": old})
        rid = self.store.list_reviews("pending", kind="persona")[0].id
        self.store.set_review_status(rid, "approved")
        expired = expire_persona_drafts(self.store, ttl_seconds=14 * 86400)
        self.assertEqual(expired, 1)

    def test_alias_suggestions_same_name_different_ids(self):
        for sid, n in (("id-a", 3), ("id-b", 1)):
            for i in range(n):
                self.store.add_timeline(
                    {
                        "ts": 100 + i,
                        "speaker_id": sid,
                        "speaker_name": "阿U",
                        "bot_id": "b",
                        "window_tag": "w",
                        "role": "user",
                        "content": f"hello {sid} {i}",
                        "fingerprint": f"{sid}-{i}",
                    }
                )
        svc = SavageTypeService(self.store, {}, llm_generate=None, get_provider=None, logger=None)
        suggestions = svc.alias_suggestions()
        self.assertTrue(any(s["alias"] == "id-b" and s["canonical_id"] == "id-a" for s in suggestions))
        self.store.set_alias("id-b", "id-a")
        self.assertFalse(any(s["alias"] == "id-b" for s in svc.alias_suggestions()))

    def test_review_quality_and_rejected_fewshot_skipped(self):
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True, "fewshot_enabled": True, "jargon_enabled": False, "persona_draft_enabled": False})
        self.assertGreater(engine.score_review("jargon", {"term": "yyds", "meaning": "永远的神"}), 60)
        self.assertLess(engine.score_review("jargon", {"term": "确实", "meaning": "语气词"}), 50)
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "今天那个yyds局好顶", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "那把确实离谱", persona_id="p1"),
        ]
        engine._queue_fewshots(events)
        pending = self.store.list_reviews("pending", kind="fewshot")
        self.assertEqual(len(pending), 1)
        self.assertIn("quality", pending[0].payload)
        engine.set_status(pending[0].id, "rejected")
        n = engine._queue_fewshots(events)
        self.assertEqual(n, 0)

    def test_unpack_llm_tokens_and_navigate(self):
        text, tin, tout = _unpack_llm_result(("hello", 12, 4))
        self.assertEqual((text, tin, tout), ("hello", 12, 4))
        text, tin, tout = _unpack_llm_result("plain")
        self.assertEqual(text, "plain")
        self.assertEqual(tin, 0)
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.engine.ingest(_payload(attribute="habit", value="晚睡", content="我习惯晚睡"), "我习惯晚睡")
        svc = SavageTypeService(self.store, {}, llm_generate=None, get_provider=None, logger=None)
        import asyncio

        result = asyncio.run(svc.navigate("喝茶", "u1", max_steps=2))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["steps"]), 1)
        self.assertLessEqual(len(result["steps"]), 2)

    def test_persona_draft_rejects_identity_and_overlap(self):
        engine = LearningEngine(self.store, llm=None, config={"learning_enabled": True})
        self.assertTrue(engine._draft_too_close("说话简短直接，不绕弯。", "说话简短直接，不绕弯。喜欢茶。"))
        import asyncio

        async def ident_llm(_prompt: str) -> str:
            return "你是17岁宅家少女，说话阴暗。"

        engine.llm = ident_llm
        for i in range(4):
            self.store.upsert_review(
                "fewshot",
                f"fs-{i}",
                f"u{i}",
                {"user": f"你好{i}啊今天", "bot": f"嗯嗯收到{i}"},
            )
            rid = self.store.list_reviews("pending", kind="fewshot")[0].id
            engine.set_status(rid, "approved")
        n = asyncio.run(engine._queue_persona_draft(force=True, persona_text="随便"))
        self.assertEqual(n, 0)

    def test_inject_drops_extras_not_tags(self):
        facts = [
            Fact(
                id=1,
                subject="self",
                attribute="likes",
                value="茶",
                content="喜欢喝茶",
                speaker_id="a",
                speaker_name="a",
                bot_id="",
                window_tag="",
                status="live",
                confidence=0.9,
            )
        ]
        result = RetrievalResult("q", "long_term", "basic", "miss", [], [], facts, [], [], [])
        pack = build_pack(
            result,
            budget=220,
            learning=LearningPack(
                fewshots=[{"user": "x" * 80, "bot": "y" * 80}],
                persona_draft="z" * 80,
            ),
        )
        self.assertTrue(pack.startswith("<savagetype_memory>"))
        self.assertTrue(pack.endswith("</savagetype_memory>"))
        self.assertIn("喜欢喝茶", pack)
        self.assertNotIn("表达样本", pack)
        self.assertNotIn("人格补丁草稿", pack)

    def test_injection_microscope_records_without_debug(self):
        import asyncio

        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        service = SavageTypeService(
            store=self.store,
            config={"inject_budget_chars": 800, "top_k": 8, "related_fact_limit": 4, "core_fact_limit": 2},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        pack, result, snapshot = asyncio.run(service.build_injection("我喜欢什么", "u1"))
        self.assertTrue(pack)
        self.assertEqual(snapshot["route"], result.route)
        self.assertIn("core", snapshot)
        injects = [x for x in self.store.recent_diag(10) if x["kind"] == "inject"]
        self.assertTrue(injects)
        self.assertEqual(injects[0]["payload"]["query"], "我喜欢什么")

    def test_embedding_wanted_threshold_does_not_flip_config(self):
        service = SavageTypeService(
            store=self.store,
            config={"embedding_enabled": False, "embedding_auto_threshold": 3},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        self.assertFalse(service.embedding_wanted())
        for attr, value in (("likes", "茶"), ("dislikes", "烟"), ("habit", "早起")):
            self.engine.ingest(_payload(attribute=attr, value=value, content=f"我{attr}{value}"), f"我{attr}{value}")
        status = service.embedding_status()
        self.assertTrue(status["wanted"])
        self.assertFalse(status["config_enabled"])
        self.assertEqual(status["reason"], "need_provider")
        self.assertFalse(status["active"])
        self.assertIsNone(service.retriever.embed)

        service.config["embedding_enabled"] = True

        class Dummy:
            pass

        service.get_provider = lambda *_a, **_k: Dummy()
        service._sync_embed_fn()
        self.assertTrue(service.embedding_status()["active"])
        self.assertIsNotNone(service.retriever.embed)

    def test_archive_facts_batch(self):
        a = self.engine.ingest(_payload(attribute="likes", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        b = self.engine.ingest(_payload(attribute="habit", value="早起", content="我习惯早起"), "我习惯早起")
        result = self.store.archive_facts([a["fact_id"], b["fact_id"]], reason="ui_delete")
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.store.get_fact(a["fact_id"]).status, "archived")
        self.assertEqual(self.store.get_fact(b["fact_id"]).status, "archived")
        self.assertEqual(self.store.counts()["facts_archived"], 2)

    def test_store_reopens_after_close(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.store.close()
        live = self.store.live_by_slot("u1", "self", "likes")
        self.assertIsNotNone(live)
        self.assertEqual(live.value, "茶")

    def test_capture_skips_non_directive(self):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config={},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        self.assertTrue(service.is_self_directive("我喜欢喝茶"))
        self.assertTrue(service.is_self_directive("记住我叫小明"))
        self.assertFalse(service.is_self_directive("今天天气真好"))

    def test_dossier_card_from_live_facts(self):
        from savagetype.profiles import build_profile

        self.engine.ingest(_payload(attribute="likes", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.engine.ingest(_payload(attribute="name", value="阿U", content="叫我阿U"), "叫我阿U")
        facts = self.store.live_by_speaker("u1")
        card = build_profile("u1", facts, speaker_name="阿U")
        self.assertIn("u1", card["card"])
        self.assertTrue(any("茶" in line for line in card["lines"]))
        other = build_profile("u2", facts)
        self.assertFalse(other["lines"])

    def test_status_ttl_and_close_ignore(self):
        from savagetype.archive import expire_status_facts
        from savagetype.util import now_ts

        r = self.engine.ingest(
            _payload(attribute="status", value="加班", content="我这周加班", ttl_seconds=60),
            "我这周加班",
        )
        self.assertEqual(r["action"], "insert")
        fact = self.store.get_fact(r["fact_id"])
        self.assertGreater(fact.expires_at, now_ts())
        self.store.update_fact(fact.id, expires_at=now_ts() - 10)
        self.assertEqual(expire_status_facts(self.store), 1)
        self.assertEqual(self.store.get_fact(fact.id).status, "archived")

        p = self.engine.ingest(_payload(attribute="promise", value="寄快递", content="记住我要寄快递"), "记住我要寄快递")
        closed = self.engine.ingest(
            _payload(attribute="promise", value="寄快递", content="快递做完了", write_op="close"),
            "快递做完了",
        )
        self.assertEqual(closed["action"], "closed")
        self.assertEqual(self.store.get_fact(p["fact_id"]).status, "archived")
        ignored = self.engine.ingest(_payload(value="x", content="哈哈", write_op="ignore"), "哈哈")
        self.assertEqual(ignored["action"], "ignored")


if __name__ == "__main__":
    unittest.main()
