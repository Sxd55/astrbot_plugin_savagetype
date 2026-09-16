"""Offline tests for contradiction, heuristic extract, speaker filter, inject budget.

Run: python tests/test_core.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
from savagetype.pipeline import MemoryPipeline, candidate_reason  # noqa: E402
from savagetype.retrieve import Retriever, classify_route  # noqa: E402
from savagetype.service import SavageTypeService, _unpack_llm_result  # noqa: E402
from savagetype.crosswin import (  # noqa: E402
    build_cross_window,
    direction_allowed,
    window_kind,
)
from savagetype.profile import build_profile_card  # noqa: E402
from savagetype.slots import canonical_attribute, canonical_subject  # noqa: E402
from savagetype.store import Store  # noqa: E402
from savagetype.util import ROLE_ASSISTANT, ROLE_BOT_ID, now_ts  # noqa: E402


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
            _payload(value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes", value="不茶")
        self.assertEqual(live.value, "不茶")
        old = self.store.get_fact(r1["fact_id"])
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.superseded_by, live.id)
        self.assertEqual(live.supersedes, r1["fact_id"])
        rb = self.engine.rollback(live.id)
        self.assertTrue(rb["ok"])
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "live")
        rolled = self.store.get_fact(live.id)
        self.assertEqual(rolled.status, "archived")
        self.assertEqual(rolled.reason, "rolled_back")

    def test_superseded_chain_survives_for_recall_and_rollback(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r2 = self.engine.ingest(
            _payload(value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        r3 = self.engine.ingest(
            _payload(value="茶", content="我又喜欢茶了", explicit_correction=1),
            "我又喜欢茶了",
        )
        self.assertEqual(r3["action"], "supersede")
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "superseded")
        self.assertEqual(self.store.get_fact(r2["fact_id"]).status, "superseded")
        self.assertEqual(self.store.get_fact(r3["fact_id"]).status, "live")
        recalls = {f.id for f in self.store.recent_superseded(["u1"])}
        self.assertIn(r1["fact_id"], recalls)
        self.assertIn(r2["fact_id"], recalls)

        self.assertTrue(self.engine.rollback(r3["fact_id"])["ok"])
        self.assertEqual(self.store.get_fact(r2["fact_id"]).status, "live")
        self.assertEqual(self.store.get_fact(r3["fact_id"]).status, "archived")
        self.assertTrue(self.engine.rollback(r2["fact_id"])["ok"])
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "live")

    def test_rollback_blocks_when_slot_taken_by_third(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r2 = self.engine.ingest(
            _payload(value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        slot_key = self.store.get_fact(r2["fact_id"]).slot_key()
        self.store.add_fact(
            {
                "subject": "self",
                "attribute": "likes",
                "value": "重新喝茶",
                "content": "旁路写入",
                "speaker_id": "u1",
                "speaker_name": "u1",
                "status": "live",
                "confidence": 0.99,
                "slot_key": slot_key,
            }
        )
        res = self.engine.rollback(r2["fact_id"])
        self.assertFalse(res["ok"])
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "superseded")
        self.assertEqual(self.store.get_fact(r2["fact_id"]).status, "live")

    def test_compact_superseded_expires_rollback_window(self):
        from savagetype.archive import compact_superseded

        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r2 = self.engine.ingest(
            _payload(value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        self.store.execute("UPDATE facts SET updated_at=1 WHERE id=?", (r1["fact_id"],))
        self.assertEqual(compact_superseded(self.store, retain_days=30), 1)
        self.assertIsNone(self.store.get_fact(r1["fact_id"]))
        res = self.engine.rollback(r2["fact_id"])
        self.assertFalse(res["ok"])
        self.assertIn("清理", res["error"])

    def test_multiple_preferences_coexist(self):
        r1 = self.engine.ingest(_payload(value="猫", content="我喜欢猫"), "我喜欢猫")
        r2 = self.engine.ingest(_payload(value="狗", content="我喜欢狗"), "我喜欢狗")
        r3 = self.engine.ingest(_payload(value="咖啡", content="我喜欢咖啡"), "我喜欢咖啡")
        self.assertEqual([r1["action"], r2["action"], r3["action"]], ["insert", "insert", "insert"])
        live = self.store.facts_by_status("live", limit=20)
        values = {f.value for f in live if f.attribute == "likes"}
        self.assertEqual(values, {"猫", "狗", "咖啡"})

    def test_joke_does_not_overwrite(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r = self.engine.ingest(_payload(value="汽油", content="我喜欢喝汽油哈哈哈开玩笑"), "我喜欢喝汽油哈哈哈开玩笑")
        self.assertEqual(r["action"], "ignored_joke")
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes", value="茶").value, "茶")

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
        live = self.store.live_by_slot("u1", "self", "likes", value=likes[-1]["value"])
        self.assertIsNotNone(live)
        self.assertTrue(live.value.startswith("不") or "不喜欢" in live.content)
        if r2["action"] == "supersede":
            old = self.store.get_fact(r1["fact_id"])
            self.assertIsNotNone(old)
            self.assertEqual(old.status, "superseded")

    def test_llm_dislike_alias_collides_likes(self):
        r1 = self.engine.ingest(_payload(attribute="likes", value="hiphop", content="我喜欢hiphop"), "我喜欢hiphop")
        r2 = self.engine.ingest(
            _payload(attribute="dislikes", value="hiphop", content="你不喜欢 hiphop。", explicit_correction=1),
            "我改口了我不喜欢hiphop",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes", value="不hiphop")
        self.assertTrue(live.value.startswith("不") or "不喜欢" in live.content)
        old = self.store.get_fact(r1["fact_id"])
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "superseded")
        self.assertIsNone(self.store.live_by_slot("u1", "self", "dislikes", value="hiphop"))

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
        self.assertIsNotNone(self.store.live_by_slot("u1", "self", "likes", value="不hiphop"))
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
        r2 = self.engine.ingest(_payload(value="不茶", content="我现在不喜欢茶了"), "我现在不喜欢茶了")
        self.assertEqual(r2["action"], "pending")
        self.assertEqual(r2["reason"], "high_evidence_needs_confirm")
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes", value="茶").value, "茶")
        confirmed = self.engine.confirm_pending(r2["pending_id"])
        self.assertTrue(confirmed["ok"])
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes", value="不茶").value, "不茶")

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
            _payload(subject="我", attribute="口味", value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        self.assertEqual(r2["action"], "supersede")
        live = self.store.live_by_slot("u1", "self", "likes", value="不茶")
        self.assertEqual(live.value, "不茶")
        old = self.store.get_fact(r1["fact_id"])
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "superseded")

    def test_persona_isolation(self):
        self.engine.ingest(_payload(value="茶", content="我喜欢喝茶", persona_id="p1"), "我喜欢喝茶")
        self.engine.ingest(_payload(value="酒", content="我喜欢喝酒", persona_id="p2"), "我喜欢喝酒")
        a = self.store.live_by_slot("u1", "self", "likes", persona_id="p1", value="茶")
        b = self.store.live_by_slot("u1", "self", "likes", persona_id="p2", value="酒")
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
        r2 = self.engine.ingest(_payload(value="不茶", content="记住我不喜欢茶", first_person=1), "记住我不喜欢茶")
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

    def test_bm25_prefers_rare_term_matches(self):
        from savagetype.bm25 import BM25Index
        from savagetype.tokenize import builtin_tokens

        latte = Fact(id=2, subject="self", attribute="likes", value="拿铁", content="我喜欢拿铁", speaker_id="u1", speaker_name="阿U", bot_id="", window_tag="", status="live", confidence=0.9)
        coffee = Fact(id=1, subject="self", attribute="likes", value="咖啡", content="我喜欢咖啡", speaker_id="u1", speaker_name="阿U", bot_id="", window_tag="", status="live", confidence=0.9)
        extra = [
            Fact(id=3, subject="self", attribute="likes", value="咖啡", content="喜欢喝咖啡", speaker_id="u2", speaker_name="阿V", bot_id="", window_tag="", status="live", confidence=0.9),
            Fact(id=4, subject="self", attribute="likes", value="咖啡", content="每天咖啡", speaker_id="u3", speaker_name="阿W", bot_id="", window_tag="", status="live", confidence=0.9),
        ]
        index = BM25Index([coffee, latte, *extra], tokenize_fn=builtin_tokens)
        index.prepare("拿铁咖啡")
        self.assertGreater(index.score(latte), index.score(coffee))
        index.prepare("完全不相干")
        self.assertEqual(index.score(latte), 0.0)

    def test_bm25_length_normalization(self):
        from savagetype.bm25 import BM25Index
        from savagetype.tokenize import builtin_tokens

        short = Fact(id=1, subject="self", attribute="likes", value="abc", content="abc", speaker_id="u1", speaker_name="阿U", bot_id="", window_tag="", status="live", confidence=0.9)
        long_ = Fact(id=2, subject="self", attribute="likes", value="abc", content="abc " + "item " * 30, speaker_id="u1", speaker_name="阿U", bot_id="", window_tag="", status="live", confidence=0.9)
        index = BM25Index([short, long_], tokenize_fn=builtin_tokens)
        index.prepare("abc")
        self.assertGreater(index.score(short), index.score(long_))

    def test_tokenizer_fallback_and_custom_terms(self):
        from savagetype import tokenize as tokenize_mod

        builtin = tokenize_mod.builtin_tokens("我喜欢 OpenAI 的 GPT-4")
        self.assertIn("我", builtin)
        self.assertIn("openai", builtin)
        self.assertIn("gpt", builtin)
        self.assertIn("4", builtin)
        self.assertIn(tokenize_mod.name(), {"jieba", "builtin"})
        tokenize_mod.add_terms(["测试专用词条xqz"])
        self.assertEqual(tokenize_mod.add_terms(["测试专用词条xqz"]), 0)

    def test_retriever_bm25_flag_and_scoring(self):
        from savagetype.retrieve import Retriever

        fact = Fact(id=1, subject="self", attribute="likes", value="咖啡", content="我喜欢咖啡", speaker_id="u1", speaker_name="阿U", bot_id="", window_tag="", status="live", confidence=0.9)
        on = Retriever(self.store, bm25=True)
        off = Retriever(self.store, bm25=False)
        self.assertNotEqual(on.cache_key("拿铁咖啡", "u1"), off.cache_key("拿铁咖啡", "u1"))
        with_bm25 = on._local_score("拿铁咖啡", fact, "u1", "long_term", bm25_scores={fact.id: 1.0})
        with_keyword = on._local_score("拿铁咖啡", fact, "u1", "long_term", bm25_scores=None)
        self.assertGreater(with_bm25, with_keyword)

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
        n = engine._queue_fewshots(events, force=True)
        self.assertEqual(n, 0)

    def test_fewshot_cooldown_blocks_then_force_queues(self):
        engine = LearningEngine(
            self.store,
            llm=None,
            config={
                "learning_enabled": True,
                "fewshot_enabled": True,
                "jargon_enabled": False,
                "persona_draft_enabled": False,
                "fewshot_cooldown_seconds": 600,
                "fewshot_max_per_run": 0,
            },
        )
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "今天那个yyds局好顶啊啊", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "那把确实离谱到家了", persona_id="p1"),
        ]
        self.assertEqual(engine._queue_fewshots(events), 1)
        more = events + [
            TimelineEvent(3, 3, "u1", "阿U", "b", "w", "user", "这家店的环境也不错", persona_id="p1"),
            TimelineEvent(4, 4, "u1", "阿U", "b", "w", "assistant", "记下了，下次一起去", persona_id="p1"),
        ]
        self.assertEqual(engine._queue_fewshots(more), 0)
        self.assertEqual(len(self.store.list_reviews("pending", kind="fewshot")), 1)
        self.assertEqual(engine._queue_fewshots(more, force=True), 2)
        self.assertEqual(len(self.store.list_reviews("pending", kind="fewshot")), 2)

    def test_fewshot_max_per_run_prefers_quality(self):
        engine = LearningEngine(
            self.store,
            llm=None,
            config={
                "learning_enabled": True,
                "fewshot_enabled": True,
                "jargon_enabled": False,
                "persona_draft_enabled": False,
                "fewshot_cooldown_seconds": 0,
                "fewshot_max_per_run": 1,
            },
        )
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "我觉得还行", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "还行吧嗯嗯", persona_id="p1"),
            TimelineEvent(3, 3, "u1", "阿U", "b", "w", "user", "今天那个yyds局真的好顶啊", persona_id="p1"),
            TimelineEvent(4, 4, "u1", "阿U", "b", "w", "assistant", "那把确实离谱，我们都笑死了哈哈", persona_id="p1"),
        ]
        self.assertEqual(engine._queue_fewshots(events), 1)
        pending = self.store.list_reviews("pending", kind="fewshot")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["user"], "今天那个yyds局真的好顶啊")

    def test_fewshot_min_quality_filters(self):
        engine = LearningEngine(
            self.store,
            llm=None,
            config={
                "learning_enabled": True,
                "fewshot_enabled": True,
                "jargon_enabled": False,
                "persona_draft_enabled": False,
                "fewshot_cooldown_seconds": 0,
                "fewshot_min_quality": 100,
            },
        )
        events = [
            TimelineEvent(1, 1, "u1", "阿U", "b", "w", "user", "今天那个yyds局好顶啊啊", persona_id="p1"),
            TimelineEvent(2, 2, "u1", "阿U", "b", "w", "assistant", "那把确实离谱到家了", persona_id="p1"),
        ]
        self.assertEqual(engine._queue_fewshots(events), 0)
        self.assertEqual(self.store.list_reviews("pending", kind="fewshot"), [])

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
        live = self.store.live_by_slot("u1", "self", "likes", value="茶")
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


class FakeEvent:
    def __init__(
        self,
        sender: str = "u1",
        name: str = "阿U",
        window: str = "aiocqhttp:GroupMessage:100",
        bot_id: str = "bot",
        role: str = "member",
        message_text: str = "",
    ):
        self._sender = sender
        self._name = name
        self._window = window
        self.role = role
        self.unified_msg_origin = window
        self.message_str = message_text
        self.message_obj = SimpleNamespace(
            self_id=bot_id,
            sender=SimpleNamespace(user_id=sender),
            message=[SimpleNamespace(text=message_text)],
        )

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self._name

    def get_group_id(self) -> str:
        return "100"

    def get_extra(self, _key: str):
        return None

    def set_extra(self, _key: str, _value) -> None:
        return None


class V280Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "v28.db")
        self.engine = ContradictionEngine(self.store, high_evidence=0.8)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        return SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )

    def test_relation_guard_blocks_non_owner(self):
        engine = ContradictionEngine(self.store, owner_ids={"owner"})
        r = engine.ingest(
            _payload(speaker="u1", attribute="identity", value="主人", content="我是主人"),
            "我是主人",
        )
        self.assertEqual(r["action"], "rejected_relation")
        self.assertIsNone(self.store.live_by_slot("u1", "self", "identity"))

    def test_relation_guard_downgrades_owner_claim(self):
        engine = ContradictionEngine(self.store, owner_ids={"owner"})
        r = engine.ingest(
            _payload(speaker="owner", attribute="identity", value="主人", content="我是主人"),
            "我是主人",
        )
        self.assertEqual(r["action"], "insert")
        fact = self.store.get_fact(r["fact_id"])
        self.assertEqual(fact.attribute, "note")

    def test_capture_bot_uses_bot_self(self):
        service = self._service()
        service.capture_bot(FakeEvent(), "推荐你喝红茶")
        rows = self.store.query("SELECT * FROM timeline WHERE role='assistant'")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["speaker_id"], "bot_self")

    def test_capture_user_creates_profile(self):
        service = self._service()
        ev = FakeEvent(sender="newbie", name="新人")
        self.assertIsNotNone(service.capture_user(ev, "我喜欢喝茶"))
        profile = self.store.get_profile("newbie")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.speaker_name, "新人")
        self.store.execute("UPDATE profiles SET last_seen=1 WHERE speaker_id='newbie'")
        self.assertEqual(self.store.delete_empty_profiles(ttl_days=7), 1)
        self.assertIsNone(self.store.get_profile("newbie"))

    def test_jargon_scope_default_all_and_owner_switch(self):
        service = self._service()
        ev = FakeEvent(sender="u9", name="路人")
        ev.get_platform_name = lambda: "aiocqhttp"
        self.assertIsNotNone(service.capture_user(ev, "这波操作太yyds了吧"))
        self.assertIn("yyds", {h["term"] for h in self.store.hot_jargon(min_count=1)})

        service2 = self._service(jargon_scope="owner")
        ev2 = FakeEvent(sender="u10", name="另一个路人")
        ev2.get_platform_name = lambda: "aiocqhttp"
        self.assertIsNotNone(service2.capture_user(ev2, "这也太awsl了吧"))
        terms = {h["term"] for h in self.store.hot_jargon(min_count=1)}
        self.assertIn("yyds", terms)
        self.assertNotIn("awsl", terms)

        owner_ev = FakeEvent(window="webchat:FriendMessage:web", sender="admin-web", name="主人")
        owner_ev.get_platform_name = lambda: "webchat"
        self.assertIsNotNone(service2.capture_user(owner_ev, "这也太tql了吧"))
        self.assertIn("tql", {h["term"] for h in self.store.hot_jargon(min_count=1)})

    def test_platform_type_and_alias(self):
        service = self._service(memory_source_platforms="aiocqhttp,qq_official")
        ev = FakeEvent(window="mybot:GroupMessage:100")
        ev.get_platform_name = lambda: "qq_official_webhook"
        ident = service.identity_from_event(ev)
        self.assertEqual(ident["platform"], "qq_official")
        self.assertTrue(service.platform_allowed(ident))

        web = FakeEvent(window="webchat:FriendMessage:web", sender="admin-web", name="主人")
        web.get_platform_name = lambda: "webchat"
        web_ident = service.identity_from_event(web)
        self.assertTrue(web_ident["is_owner"])
        self.assertEqual(service.capture_skip_reason(web, web_ident), "")
        self.assertIsNotNone(service.capture_user(web, "我喜欢喝茶"))
        self.assertEqual(self.store.counts()["timeline"], 1)
        self.assertIsNone(self.store.get_profile("admin-web"))

        qq = FakeEvent(sender="u9", name="路人")
        qq.get_platform_name = lambda: "aiocqhttp"
        self.assertIsNotNone(service.capture_user(qq, "我喜欢喝茶"))
        self.assertIsNotNone(self.store.get_profile("u9"))

    def test_command_text_is_skipped(self):
        service = self._service()
        ev = FakeEvent(sender="u1", name="阿U", message_text="/stype status")
        self.assertTrue(service.is_command_text("stype status", ev))
        self.assertIsNone(service.capture_user(ev, "stype status"))
        self.assertEqual(self.store.counts()["timeline"], 0)

        plain = FakeEvent(sender="u1", name="阿U", message_text="我喜欢喝茶")
        self.assertFalse(service.is_command_text("我喜欢喝茶", plain))
        self.assertIsNotNone(service.capture_user(plain, "我喜欢喝茶"))
        self.assertEqual(self.store.counts()["timeline"], 1)

    def test_manual_remember_keeps_speaker(self):
        service = self._service()
        speaker = {"speaker_id": "u2", "speaker_name": "老二", "window_tag": "aiocqhttp:FriendMessage:2"}
        r = service.remember(speaker, "我喜欢咖啡")
        fact = self.store.get_fact(r["fact_id"])
        self.assertEqual(fact.speaker_id, "u2")
        self.assertEqual(fact.scope, "person")

    def test_candidate_gate_owner_directive(self):
        owner_ev = TimelineEvent(
            id=1, ts=1, speaker_id="owner", speaker_name="主人", bot_id="b",
            window_tag="w", role="user", content="以后回复短一点",
        )
        self.assertEqual(candidate_reason(owner_ev, True), "owner_directive")
        self.assertEqual(candidate_reason(owner_ev, False), "")
        like_ev = TimelineEvent(
            id=2, ts=1, speaker_id="u1", speaker_name="阿U", bot_id="b",
            window_tag="w", role="user", content="我喜欢喝茶",
        )
        self.assertEqual(candidate_reason(like_ev, False), "self")

    def test_candidate_gate_adverbs_and_new_verbs(self):
        # 真实语料里最自然的说法：主语和谓语之间夹副词，或换用别的谓词。
        cases = [
            "我平时喜欢喝气泡水。",
            "我最近迷上了爬山。",
            "我其实不喜欢猫了。",
            "我家的猫叫团子。",
            "我不太喜欢甜食。",
            "我不怎么吃辣。",
            "我也爱喝咖啡。",
            "我养了一只叫毛球的猫。",
            "我住在成都。",
        ]
        for index, content in enumerate(cases):
            ev = TimelineEvent(
                id=index + 10, ts=1, speaker_id="u1", speaker_name="阿U", bot_id="b",
                window_tag="w", role="user", content=content,
            )
            self.assertEqual(candidate_reason(ev, False), "self", content)
            self.assertEqual(candidate_reason(ev, True), "owner_self", content)

    def test_candidate_gate_keeps_noise_out(self):
        cases = [
            "我可爱吗？",
            "我朋友叫小明。",
            "我叫什么名字？",
            "你是谁？",
            "今天天气不错。",
        ]
        for index, content in enumerate(cases):
            ev = TimelineEvent(
                id=index + 50, ts=1, speaker_id="u1", speaker_name="阿U", bot_id="b",
                window_tag="w", role="user", content=content,
            )
            self.assertEqual(candidate_reason(ev, False), "", content)

    def test_heuristic_covers_new_phrasings(self):
        extractor = Extractor(self.store, self.engine)
        payloads = []
        for index, content in enumerate(("我平时喜欢喝气泡水。", "我最近迷上了爬山。")):
            ev = TimelineEvent(
                id=index + 70, ts=1, speaker_id="u1", speaker_name="阿U", bot_id="b",
                window_tag="w", role="user", content=content,
            )
            payloads.extend(extractor.extract_heuristic([ev]))
        values = {(p.get("attribute"), p.get("value")) for p in payloads}
        self.assertIn(("likes", "气泡水"), values)
        self.assertIn(("likes", "爬山"), values)
        # 「我最近迷上了爬山」不该被写成 3 天有效的 status。
        self.assertNotIn("status", {attr for attr, _ in values})

    def test_inject_returns_blank_when_empty(self):
        result = RetrievalResult(
            query="q",
            route="long_term",
            path="basic",
            cache="miss",
            hits=[],
            blocked=[],
            core=[],
            related=[],
            uncertain=[],
            superseded=[],
        )
        self.assertEqual(build_pack(result, budget=400), "")

    def test_owner_scope_visible_to_others(self):
        self.engine.ingest(
            _payload(speaker="owner", value="喝茶", content="我喜欢喝茶", scope="owner"),
            "我喜欢喝茶",
        )
        retriever = Retriever(self.store)
        result = asyncio.run(retriever.retrieve("喜欢什么", "u1", top_k=8))
        ids = {f.speaker_id for f in result.core + result.related}
        self.assertIn("owner", ids)

    def test_pipeline_passes_and_writes(self):
        store, engine, pipe, holder = self._fresh_pipeline(verify_pass=True)
        try:
            event_id = self._add_like(store, holder)
            result = asyncio.run(pipe.run(force=True))
            self.assertEqual(result["written"], 1)
            fact = store.live_by_slot("u1", "self", "likes", value="茶")
            self.assertIsNotNone(fact)
            self.assertEqual(fact.review_status, "ai_passed")
            self.assertEqual(fact.scope, "person")
            self.assertEqual(fact.source_event_id, event_id)
        finally:
            store.close()

    def test_pipeline_failure_goes_pending(self):
        store, engine, pipe, holder = self._fresh_pipeline(verify_pass=False, max_revisions=0)
        try:
            self._add_like(store, holder)
            result = asyncio.run(pipe.run(force=True))
            self.assertEqual(result["pending"], 1)
            self.assertIsNone(store.live_by_slot("u1", "self", "likes"))
            reviews = store.list_memory_reviews("pending")
            self.assertEqual(len(reviews), 1)
            self.assertIn("我喜欢喝茶", reviews[0].raw_text)
        finally:
            store.close()

    def test_owner_reply_approves_and_deletes(self):
        service = self._service(owner_qq="owner")
        payload = _payload(speaker="owner", value="茶", content="我喜欢喝茶")
        pass_id = self.store.add_memory_review(
            scope="owner",
            speaker_id="owner",
            speaker_name="主人",
            raw_text="我喜欢喝茶",
            plain="喜欢喝茶",
            payload=payload,
        )
        reply = asyncio.run(service.handle_owner_reply(f"是 {pass_id}"))
        self.assertIn("已通过", reply)
        self.assertIsNotNone(self.store.live_by_slot("owner", "self", "likes", value="茶"))

        drop_payload = _payload(speaker="owner", value="酒", content="我喜欢喝酒")
        drop_id = self.store.add_memory_review(
            scope="owner",
            speaker_id="owner",
            speaker_name="主人",
            raw_text="我喜欢喝酒",
            plain="喜欢喝酒",
            payload=drop_payload,
        )
        reply = asyncio.run(service.handle_owner_reply(f"否 {drop_id}"))
        self.assertIn("已删除", reply)
        self.assertIsNone(self.store.get_memory_review(drop_id))

    def _fresh_pipeline(self, verify_pass: bool, max_revisions: int = 1):
        store = Store(Path(self.tmp.name) / f"pipe-{verify_pass}-{max_revisions}.db")
        engine = ContradictionEngine(store)
        event_holder: dict[str, int] = {}

        async def fake_normalize(_prompt: str) -> str:
            return json.dumps(
                [
                    {
                        "source_event_id": event_holder["id"],
                        "plain": "喜欢喝茶",
                        "keywords": ["喝茶"],
                        "subject": "self",
                        "attribute": "likes",
                        "value": "茶",
                        "confidence": 0.9,
                        "write_op": "create",
                    }
                ],
                ensure_ascii=False,
            )

        async def fake_verify(_prompt: str) -> str:
            return json.dumps(
                [{"index": 0, "pass": verify_pass, "reason": "" if verify_pass else "加戏", "fix_hint": ""}],
                ensure_ascii=False,
            )

        extractor = Extractor(store, engine, llm=fake_normalize)
        pipe = MemoryPipeline(
            store,
            engine,
            extractor,
            {
                "pipeline_enabled": True,
                "extract_min_messages": 1,
                "pipeline_batch_size": 8,
                "pipeline_max_revisions": max_revisions,
            },
            None,
            llm=fake_normalize,
            verify_llm=fake_verify,
            is_owner_speaker=lambda sid: sid == "owner",
        )
        return store, engine, pipe, event_holder

    def _add_like(self, store: Store, holder: dict[str, int]) -> int:
        from savagetype.util import now_ts

        store.add_timeline(
            {
                "ts": now_ts(),
                "speaker_id": "u1",
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": "aiocqhttp:GroupMessage:1",
                "role": "user",
                "content": "我喜欢喝茶",
                "fingerprint": "pipe-like",
            }
        )
        event = store.unsummarized(10)[0]
        holder["id"] = event.id
        return event.id


class V320Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "v32.db")
        self.engine = ContradictionEngine(self.store, high_evidence=0.8)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_importance_by_origin(self):
        from savagetype.util import default_importance

        self.assertAlmostEqual(default_importance({"origin": "manual"}), 1.0)
        self.assertAlmostEqual(default_importance({"review_status": "ai_passed"}), 0.8)
        self.assertAlmostEqual(default_importance({}), 0.5)
        boosted = default_importance({"review_status": "ai_passed", "explicit_correction": 1, "first_person": 1})
        self.assertGreater(boosted, 0.8)

    def test_fact_weight_decay_and_reinforce(self):
        from savagetype.util import fact_weight, now_ts

        ts = now_ts()
        fact = Fact(
            id=1, subject="self", attribute="likes", value="茶", content="c",
            speaker_id="u1", speaker_name="u", bot_id="", window_tag="",
            status="live", confidence=0.9, importance=0.8, created_at=ts, updated_at=ts,
        )
        self.assertAlmostEqual(fact_weight(fact, now=ts), 0.8, places=2)
        decayed = fact_weight(fact, now=ts + 30 * 86400)
        self.assertLess(decayed, 0.45)
        fact.access_count = 4
        reinforced = fact_weight(fact, now=ts + 30 * 86400)
        self.assertGreater(reinforced, decayed)
        fact.pinned = 1
        self.assertGreaterEqual(fact_weight(fact, now=ts + 365 * 86400), 0.8)

    def test_fact_kind_classification(self):
        from savagetype.slots import apply_slot

        likes = apply_slot({"subject": "self", "attribute": "likes", "value": "茶", "content": "我喜欢茶", "speaker_id": "u1"})
        self.assertEqual(likes["kind"], "preference")
        promise = apply_slot({"subject": "self", "attribute": "promise", "value": "寄快递", "content": "记住我要寄快递", "speaker_id": "u1"})
        self.assertEqual(promise["kind"], "promise")
        identity = apply_slot({"subject": "self", "attribute": "身份", "value": "学生", "content": "我是学生", "speaker_id": "u1"})
        self.assertEqual(identity["kind"], "identity")
        status = apply_slot({"subject": "self", "attribute": "status", "value": "加班", "content": "我这周加班", "speaker_id": "u1"})
        self.assertEqual(status["kind"], "status")

    def test_retrieve_dedup_and_pin_bypass(self):
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        fact = self.store.live_by_slot("u1", "self", "likes", value="茶")
        retriever = Retriever(self.store)

        r1 = asyncio.run(retriever.retrieve("喜欢什么", "u1", skip_ids={fact.id}))
        ids = {f.id for f in r1.core + r1.related}
        self.assertNotIn(fact.id, ids)
        self.assertIn("recently_injected", {h.filter_reason for h in r1.blocked})

        r2 = asyncio.run(retriever.retrieve("你还记得我喜欢什么吗", "u1", skip_ids={fact.id}))
        self.assertIn(fact.id, {f.id for f in r2.core + r2.related})

        self.store.set_pinned(fact.id, True)
        r3 = asyncio.run(retriever.retrieve("喜欢什么", "u1", skip_ids={fact.id}))
        self.assertIn(fact.id, {f.id for f in r3.core + r3.related})

    def test_inject_promise_and_status_sections(self):
        promise = Fact(
            id=1, subject="self", attribute="promise", value="寄快递", content="要寄快递",
            speaker_id="u1", speaker_name="u", bot_id="", window_tag="", status="live",
            confidence=0.9, kind="promise", plain="要寄快递",
        )
        status = Fact(
            id=2, subject="self", attribute="status", value="加班", content="最近加班",
            speaker_id="u1", speaker_name="u", bot_id="", window_tag="", status="live",
            confidence=0.9, kind="status", plain="最近加班",
        )
        result = RetrievalResult(
            query="约定的蛋糕最近怎么样了", route="long_term", path="basic", cache="miss",
            hits=[], blocked=[], core=[], related=[promise, status],
            uncertain=[], superseded=[],
        )
        pack = build_pack(result, budget=800)
        self.assertIn("【约定】", pack)
        self.assertIn("【近况】", pack)
        self.assertIn("不可信数据", pack)

    def test_restore_blocks_conflict(self):
        a = self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.store.archive_facts([a["fact_id"]])
        b = self.engine.ingest(_payload(speaker="u1", value="不茶", content="我现在不喜欢茶了"), "我现在不喜欢茶了")
        out = self.store.restore_facts([a["fact_id"]])
        self.assertEqual(out["restored"], [])
        self.assertEqual(len(out["blocked"]), 1)
        self.store.archive_facts([b["fact_id"]])
        out2 = self.store.restore_facts([a["fact_id"]])
        self.assertIn(a["fact_id"], out2["restored"])

    def test_archive_decayed_and_pin_protection(self):
        from savagetype.archive import archive_decayed
        from savagetype.util import now_ts

        old_ts = now_ts() - 90 * 86400
        first = self.engine.ingest(_payload(speaker="u1", value="旧", content="我喜欢旧东西"), "我喜欢旧东西")
        self.store.update_fact(first["fact_id"], importance=0.05, updated_at=old_ts)
        self.assertEqual(
            archive_decayed(self.store, min_age_days=30, threshold=0.12, half_life_days=30),
            1,
        )
        self.assertEqual(self.store.get_fact(first["fact_id"]).status, "archived")

        second = self.engine.ingest(_payload(speaker="u2", value="旧2", content="我喜欢旧东西2"), "我喜欢旧东西2")
        self.store.update_fact(second["fact_id"], importance=0.05, updated_at=old_ts)
        self.store.set_pinned(second["fact_id"], True)
        self.assertEqual(
            archive_decayed(self.store, min_age_days=30, threshold=0.12, half_life_days=30),
            0,
        )
        self.assertEqual(self.store.get_fact(second["fact_id"]).status, "live")

    def test_keyword_score_uses_keywords(self):
        from savagetype.retrieve import keyword_score

        fact = Fact(
            id=1, subject="self", attribute="note", value="咨询", content="问了一下午",
            speaker_id="u1", speaker_name="u", bot_id="", window_tag="", status="live",
            confidence=0.8, keywords=["OpenAI", "万事达卡"],
        )
        self.assertGreater(keyword_score("OpenAI 能用吗", fact), 0)
        self.assertEqual(keyword_score("完全不相干", fact), 0.0)

    def test_search_facts_matches_keywords(self):
        self.engine.ingest(
            _payload(speaker="u1", value="境外支付", content="咨询境外支付", keywords=["万事达卡", "OpenAI"]),
            "咨询境外支付",
        )
        self.assertEqual(len(self.store.search_facts("万事达")), 1)

    def test_migration_adds_new_columns(self):
        import sqlite3

        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, attribute TEXT NOT NULL,
                value TEXT NOT NULL, content TEXT NOT NULL, speaker_id TEXT NOT NULL,
                speaker_name TEXT NOT NULL DEFAULT '', bot_id TEXT NOT NULL DEFAULT '',
                window_tag TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5, evidence TEXT NOT NULL DEFAULT '[]',
                mention_policy TEXT NOT NULL DEFAULT 'mention', first_person INTEGER NOT NULL DEFAULT 0,
                explicit_correction INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'extract',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, superseded_by INTEGER,
                supersedes INTEGER, fingerprint TEXT NOT NULL, embedding TEXT,
                access_count INTEGER NOT NULL DEFAULT 0, last_accessed INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', persona_id TEXT NOT NULL DEFAULT '',
                slot_key TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()
        conn.close()
        store = Store(path)
        try:
            cols = {r["name"] for r in store.query("PRAGMA table_info(facts)")}
            self.assertIn("importance", cols)
            self.assertIn("kind", cols)
            self.assertIn("pinned", cols)
        finally:
            store.close()

    def test_idle_pending(self):
        from savagetype.service import SavageTypeService
        from savagetype.util import now_ts

        service = SavageTypeService(
            store=self.store,
            config={"extract_idle_seconds": 60},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        self.assertFalse(service.idle_pending())
        self.store.add_timeline(
            {
                "ts": now_ts() - 3600, "speaker_id": "u1", "speaker_name": "u",
                "bot_id": "b", "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "我喜欢喝茶", "fingerprint": "idle-1",
            }
        )
        self.assertTrue(service.idle_pending())
        self.store.add_timeline(
            {
                "ts": now_ts(), "speaker_id": "u1", "speaker_name": "u",
                "bot_id": "b", "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "在吗", "fingerprint": "idle-2",
            }
        )
        self.assertFalse(service.idle_pending())


class V330Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "v33.db")
        self.engine = ContradictionEngine(self.store, high_evidence=0.8)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        return SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )

    def test_heuristic_splits_multiple_preferences(self):
        from savagetype.extract import Extractor

        self.store.add_timeline(
            {
                "ts": 1, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "我喜欢喝美式，不喜欢拿铁。", "fingerprint": "split-1",
            }
        )
        facts = Extractor(self.store, self.engine).extract_heuristic(self.store.unsummarized(10))
        values = {f["value"] for f in facts if f["attribute"] == "likes"}
        self.assertIn("美式", values)
        self.assertIn("不拿铁", values)

    def test_topic_key_strips_particles(self):
        from savagetype.util import topic_key

        self.assertEqual(topic_key("不美式了"), "美式")
        self.assertEqual(topic_key("不喝咖啡了"), "咖啡")
        self.assertEqual(topic_key("我不喜欢喝美式了"), "美式")
        self.assertEqual(topic_key("hiphop"), "hiphop")

    def test_sentence_value_remap_covers_same_topic(self):
        from savagetype.slots import apply_slot

        cleaned = apply_slot(
            {
                "subject": "self",
                "attribute": "note",
                "value": "我不喜欢喝美式了",
                "content": "我不喜欢喝美式了",
                "speaker_id": "u1",
            }
        )
        self.assertEqual(cleaned["attribute"], "likes")
        self.assertEqual(cleaned["value"], "不美式")

        r1 = self.engine.ingest(_payload(value="美式", content="我喜欢喝美式"), "我喜欢喝美式")
        r2 = self.engine.ingest(
            {
                "subject": "self",
                "attribute": "note",
                "value": "我不喜欢喝美式了",
                "content": "我不喜欢喝美式了",
                "speaker_id": "u1",
                "speaker_name": "u1",
                "confidence": 0.9,
                "first_person": 1,
            },
            "我不喜欢喝美式了",
        )
        self.assertEqual(r2["action"], "supersede")
        old = self.store.get_fact(r1["fact_id"])
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "superseded")

    def test_heuristic_extracts_repeated_likes(self):
        from savagetype.extract import Extractor

        self.store.add_timeline(
            {
                "ts": 1, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "我喜欢猫，我喜欢狗，我喜欢羊", "fingerprint": "repeat-1",
            }
        )
        facts = Extractor(self.store, self.engine).extract_heuristic(self.store.unsummarized(10))
        values = {f["value"] for f in facts if f["attribute"] == "likes"}
        self.assertEqual(values, {"猫", "狗", "羊"})

    def test_correction_with_particle_covers_same_topic(self):
        r1 = self.engine.ingest(_payload(value="美式", content="我喜欢喝美式"), "我喜欢喝美式")
        r2 = self.engine.ingest(_payload(value="不美式了", content="我不喜欢喝美式了"), "我不喜欢喝美式了")
        self.assertEqual(r2["action"], "supersede")
        old = self.store.get_fact(r1["fact_id"])
        self.assertIsNotNone(old)
        self.assertEqual(old.status, "superseded")
        live = self.store.live_by_slot("u1", "self", "likes", value="不美式了")
        self.assertIsNotNone(live)
        self.assertEqual(live.value, "不美式了")

    def test_identity_merge_resolves_conflict(self):
        old = self.engine.ingest(_payload(speaker="savage", value="香蕉", content="我喜欢吃香蕉"), "我喜欢吃香蕉")
        self.store.update_fact(old["fact_id"], updated_at=100)
        self.engine.ingest(_payload(speaker="2412260046", value="不香蕉", content="我不喜欢吃香蕉"), "我不喜欢吃香蕉")
        moved = self.store.reassign_speaker("savage", "2412260046", "主人")
        self.assertEqual(moved, 1)
        live_values = {
            f.value
            for f in self.store.facts_by_status("live", limit=20)
            if f.speaker_id == "2412260046"
        }
        self.assertIn("不香蕉", live_values)
        self.assertNotIn("香蕉", live_values)
        archived = self.store.facts_by_status("archived", limit=20)
        self.assertTrue(any(f.reason == "identity_merge_conflict" for f in archived))

    def test_webchat_owner_identity_merge(self):
        service = self._service(owner_qq="2412260046")
        self.engine.ingest(_payload(speaker="savage", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        event = FakeEvent(sender="savage", name="savage", window="webchat:FriendMessage:webchat!savage!x")
        event.get_platform_name = lambda: "webchat"
        ident = service.identity_from_event(event)
        self.assertEqual(ident["speaker_id"], "2412260046")
        self.assertEqual(self.store.resolve_speaker("savage"), "2412260046")
        moved = [f for f in self.store.facts_by_status("live", limit=10) if f.speaker_id == "2412260046"]
        self.assertTrue(moved)

    def test_pipeline_per_entry_verdicts(self):
        from savagetype.extract import Extractor
        from savagetype.pipeline import MemoryPipeline
        from savagetype.util import now_ts

        store = Store(Path(self.tmp.name) / "idx.db")
        try:
            engine = ContradictionEngine(store)
            store.add_timeline(
                {
                    "ts": now_ts(), "speaker_id": "u1", "speaker_name": "u", "bot_id": "b",
                    "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                    "content": "我喜欢美式，不喜欢拿铁", "fingerprint": "idx-1",
                }
            )
            event = store.unsummarized(10)[0]

            async def fake_normalize(_prompt):
                return json.dumps(
                    [
                        {"source_event_id": event.id, "plain": "喜欢美式", "keywords": [],
                         "subject": "self", "attribute": "likes", "value": "美式", "write_op": "create"},
                        {"source_event_id": event.id, "plain": "不喜欢拿铁", "keywords": [],
                         "subject": "self", "attribute": "likes", "value": "不拿铁", "write_op": "create"},
                    ],
                    ensure_ascii=False,
                )

            async def fake_verify(_prompt):
                return json.dumps(
                    [
                        {"index": 0, "pass": True, "reason": "", "fix_hint": ""},
                        {"index": 1, "pass": False, "reason": "加戏", "fix_hint": ""},
                    ],
                    ensure_ascii=False,
                )

            extractor = Extractor(store, engine, llm=fake_normalize)
            pipe = MemoryPipeline(
                store, engine, extractor,
                {"extract_min_messages": 1, "pipeline_batch_size": 8, "pipeline_max_revisions": 0},
                None, llm=fake_normalize, verify_llm=fake_verify, is_owner_speaker=lambda _s: False,
            )
            result = asyncio.run(pipe.run(force=True))
            self.assertEqual(result["written"], 1)
            self.assertEqual(result["pending"], 1)
        finally:
            store.close()

    def test_dedup_bypasses_cache(self):
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        fact = self.store.live_by_slot("u1", "self", "likes", value="茶")
        retriever = Retriever(self.store)
        r1 = asyncio.run(retriever.retrieve("喜欢什么", "u1"))
        self.assertIn(fact.id, {f.id for f in r1.core + r1.related})
        r2 = asyncio.run(retriever.retrieve("喜欢什么", "u1", skip_ids={fact.id}))
        self.assertNotIn(fact.id, {f.id for f in r2.core + r2.related})
        self.assertIn("recently_injected", {h.filter_reason for h in r2.blocked})

    def test_ingest_resolves_alias(self):
        self.store.set_alias("savage", "2412260046")
        r = self.engine.ingest(_payload(speaker="savage", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.assertEqual(self.store.get_fact(r["fact_id"]).speaker_id, "2412260046")

    def test_reassign_moves_pending_reviews(self):
        rid = self.store.add_memory_review(
            scope="owner", speaker_id="savage", speaker_name="savage",
            raw_text="x", plain="x",
        )
        self.store.reassign_speaker("savage", "2412260046", "主人")
        self.assertEqual(self.store.get_memory_review(rid).speaker_id, "2412260046")

    def test_pinned_conflict_goes_pending(self):
        r1 = self.engine.ingest(_payload(speaker="u1", value="美式", content="我喜欢喝美式"), "我喜欢喝美式")
        self.store.set_pinned(r1["fact_id"], True)
        r2 = self.engine.ingest(
            _payload(speaker="u1", value="不美式", content="我不喜欢喝美式了", explicit_correction=1),
            "我不喜欢喝美式了",
        )
        self.assertEqual(r2["action"], "pending")
        self.assertEqual(r2["reason"], "pinned_needs_confirm")
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "live")
        self.assertEqual(self.store.live_by_slot("u1", "self", "likes", value="美式").value, "美式")

    def test_pinned_survives_slot_conflict_resolution(self):
        from savagetype.slots import apply_slot

        r1 = self.engine.ingest(_payload(speaker="u1", value="美式", content="我喜欢喝美式"), "我喜欢喝美式")
        self.store.update_fact(r1["fact_id"], updated_at=100)
        self.store.set_pinned(r1["fact_id"], True)
        payload = apply_slot(
            {
                "subject": "self", "attribute": "likes", "value": "不美式",
                "content": "我不喜欢喝美式了", "speaker_id": "u1", "speaker_name": "u1",
                "confidence": 0.9, "first_person": 1,
            }
        )
        b_id = self.store.add_fact(payload)
        self.store.resolve_all_slot_conflicts()
        self.assertEqual(self.store.get_fact(r1["fact_id"]).status, "live")
        self.assertEqual(self.store.get_fact(b_id).status, "archived")

    def test_pipeline_conflict_pending_not_counted(self):
        from savagetype.extract import Extractor
        from savagetype.pipeline import MemoryPipeline
        from savagetype.util import now_ts

        store = Store(Path(self.tmp.name) / "pc.db")
        try:
            engine = ContradictionEngine(store, high_evidence=0.8)
            r1 = engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶", confidence=0.9), "我喜欢喝茶")
            store.update_fact(r1["fact_id"], access_count=2, confidence=0.9)
            store.add_timeline(
                {
                    "ts": now_ts(), "speaker_id": "u1", "speaker_name": "u", "bot_id": "b",
                    "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                    "content": "我不喜欢喝茶", "fingerprint": "pc-1",
                }
            )
            event = store.unsummarized(10)[0]

            async def fake_normalize(_prompt):
                return json.dumps(
                    [{"source_event_id": event.id, "plain": "不喜欢喝茶", "keywords": [],
                      "subject": "self", "attribute": "likes", "value": "不茶", "write_op": "create"}],
                    ensure_ascii=False,
                )

            async def fake_verify(_prompt):
                return json.dumps([{"index": 0, "pass": True, "reason": "", "fix_hint": ""}], ensure_ascii=False)

            extractor = Extractor(store, engine, llm=fake_normalize)
            pipe = MemoryPipeline(
                store, engine, extractor,
                {"extract_min_messages": 1, "pipeline_batch_size": 8, "pipeline_max_revisions": 0},
                None, llm=fake_normalize, verify_llm=fake_verify, is_owner_speaker=lambda _s: False,
            )
            result = asyncio.run(pipe.run(force=True))
            self.assertEqual(result["written"], 0)
            self.assertEqual(store.counts()["pending"], 1)
            self.assertEqual(store.live_by_slot("u1", "self", "likes", value="茶").value, "茶")
        finally:
            store.close()

    def test_pipeline_revise_drop_goes_pending(self):
        from savagetype.extract import Extractor
        from savagetype.pipeline import MemoryPipeline
        from savagetype.util import now_ts

        store = Store(Path(self.tmp.name) / "revise.db")
        try:
            engine = ContradictionEngine(store)
            store.add_timeline(
                {
                    "ts": now_ts(), "speaker_id": "u1", "speaker_name": "u", "bot_id": "b",
                    "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                    "content": "我喜欢喝茶", "fingerprint": "revise-1",
                }
            )
            event = store.unsummarized(10)[0]

            async def fake_normalize(_prompt):
                return json.dumps(
                    [{"source_event_id": event.id, "plain": "喜欢喝茶", "keywords": [],
                      "subject": "self", "attribute": "likes", "value": "茶", "write_op": "create"}],
                    ensure_ascii=False,
                )

            async def fake_verify(_prompt):
                return json.dumps(
                    [{"index": 0, "pass": False, "reason": "加戏", "fix_hint": "删掉"}],
                    ensure_ascii=False,
                )

            async def fake_revise(_prompt):
                return "[]"

            extractor = Extractor(store, engine, llm=fake_normalize)
            pipe = MemoryPipeline(
                store, engine, extractor,
                {"extract_min_messages": 1, "pipeline_batch_size": 8, "pipeline_max_revisions": 1},
                None, llm=fake_revise, verify_llm=fake_verify, is_owner_speaker=lambda _s: False,
            )
            result = asyncio.run(pipe.run(force=True))
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(len(store.list_memory_reviews("pending")), 1)
        finally:
            store.close()

    def test_heuristic_skips_hearsay_preference(self):
        from savagetype.extract import Extractor

        self.store.add_timeline(
            {
                "ts": 1, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "我喜欢猫，朋友说不喜欢狗", "fingerprint": "hearsay-1",
            }
        )
        facts = Extractor(self.store, self.engine).extract_heuristic(self.store.unsummarized(10))
        values = {f["value"] for f in facts if f["attribute"] == "likes"}
        self.assertIn("猫", values)
        self.assertNotIn("不狗", values)
        self.assertNotIn("狗", values)

    def test_profile_includes_alias_facts(self):
        from savagetype.profiles import build_profile

        self.store.set_alias("old-id", "new-id")
        self.engine.ingest(_payload(speaker="old-id", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        ids = self.store.speaker_ids_for("new-id")
        facts = self.store.live_by_speaker("new-id", speaker_ids=ids, limit=20)
        card = build_profile("new-id", facts, speaker_name="某人", speaker_ids=ids)
        self.assertTrue(any("茶" in line for line in card["lines"]))

    def test_llm_likes_with_negated_content_flips(self):
        from savagetype.slots import apply_slot

        cleaned = apply_slot(
            {
                "subject": "self",
                "attribute": "likes",
                "value": "拿铁",
                "content": "我不喜欢拿铁",
                "speaker_id": "u1",
            }
        )
        self.assertEqual(cleaned["attribute"], "likes")
        self.assertTrue(cleaned["value"].startswith("不"))

    def test_empty_profile_cleanup_keeps_archived_facts(self):
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.store.upsert_profile("u1", "阿U", "aiocqhttp")
        fact = self.store.live_by_slot("u1", "self", "likes", value="茶")
        self.store.archive_facts([fact.id])
        self.store.execute("UPDATE profiles SET last_seen=1 WHERE speaker_id='u1'")
        self.assertEqual(self.store.delete_empty_profiles(ttl_days=7), 0)
        self.assertIsNotNone(self.store.get_profile("u1"))

    def test_archive_decayed_targets_oldest(self):
        from savagetype.archive import archive_decayed
        from savagetype.util import now_ts

        ids = []
        for i, value in enumerate(["a", "b", "c"]):
            r = self.engine.ingest(_payload(speaker="u1", value=value, content=f"我喜欢{value}"), f"我喜欢{value}")
            self.store.update_fact(
                r["fact_id"], importance=0.05, updated_at=now_ts() - (90 - i) * 86400
            )
            ids.append(r["fact_id"])
        self.assertEqual(
            archive_decayed(self.store, min_age_days=30, threshold=0.12, limit=1), 1
        )
        self.assertEqual(self.store.get_fact(ids[0]).status, "archived")
        self.assertEqual(self.store.get_fact(ids[2]).status, "live")

    def test_memory_review_dedupe_by_source(self):
        rid1 = self.store.add_memory_review(
            scope="person", speaker_id="u1", source_event_id=5, raw_text="x", plain="y"
        )
        rid2 = self.store.add_memory_review(
            scope="person", speaker_id="u1", source_event_id=5, raw_text="x", plain="y"
        )
        self.assertEqual(rid1, rid2)
        self.assertEqual(len(self.store.list_memory_reviews("pending")), 1)

    def test_idle_trigger_processes_small_batch(self):
        from savagetype.util import now_ts

        service = self._service(
            extract_idle_seconds=1,
            pipeline_enabled=False,
            extract_min_messages=8,
        )
        self.store.add_timeline(
            {
                "ts": now_ts() - 3600, "speaker_id": "u1", "speaker_name": "u",
                "bot_id": "b", "window_tag": "aiocqhttp:GroupMessage:1", "role": "user",
                "content": "我喜欢喝茶", "fingerprint": "idle-int",
            }
        )
        result = asyncio.run(service.maybe_extract())
        self.assertTrue(result.get("idle"))
        self.assertIsNotNone(self.store.live_by_slot("u1", "self", "likes", value="茶"))

    def test_dedup_is_per_window(self):
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        service = self._service()
        r1 = asyncio.run(service.build_injection("我喜欢什么", "u1", window_tag="w-a"))
        self.assertTrue(r1[1].core or r1[1].related)
        r2 = asyncio.run(service.build_injection("我喜欢什么", "u1", window_tag="w-b"))
        self.assertTrue(r2[1].core or r2[1].related)
        r3 = asyncio.run(service.build_injection("我喜欢什么", "u1", window_tag="w-a"))
        self.assertEqual(r3[2]["dedup"], 1)
        self.assertEqual([f.id for f in r3[1].core], [])

    def test_sleep_prunes_jargon_and_stale_pending(self):
        from savagetype.archive import expire_pending_overrides, prune_jargon_stats

        self.store.bump_jargon("孤词", persona_id="p")
        self.store.execute("UPDATE jargon_stats SET last_seen=1 WHERE term='孤词'")
        self.assertEqual(prune_jargon_stats(self.store, min_age_days=30), 1)
        self.assertEqual(self.store.hot_jargon(min_count=1), [])

        pending_id = self.store.add_pending(0, {"subject": "self"}, "joke_or_banter")
        self.store.execute("UPDATE pending_overrides SET created_at=1 WHERE id=?", (pending_id,))
        self.assertEqual(expire_pending_overrides(self.store, max_age_days=30), 1)
        self.assertEqual(self.store.pending_open(), [])

    def test_both_pinned_duplicates_stay_live(self):
        from savagetype.slots import apply_slot

        a = self.engine.ingest(_payload(speaker="u1", value="美式", content="我喜欢喝美式"), "我喜欢喝美式")
        self.store.set_pinned(a["fact_id"], True)
        payload = apply_slot(
            {
                "subject": "self", "attribute": "likes", "value": "不美式",
                "content": "我不喜欢喝美式了", "speaker_id": "u1", "speaker_name": "u1",
                "confidence": 0.9, "first_person": 1,
            }
        )
        b_id = self.store.add_fact(payload)
        self.store.set_pinned(b_id, True)
        self.store.resolve_all_slot_conflicts()
        self.assertEqual(self.store.get_fact(a["fact_id"]).status, "live")
        self.assertEqual(self.store.get_fact(b_id).status, "live")

    def test_reset_clears_transient_meta(self):
        self.store.set_meta("capture_skip", '{"reason":"platform:webchat"}')
        self.store.set_meta("notify_last_at", "123")
        self.store.set_meta("owner_umo", "default:FriendMessage:1")
        self.store.clear_dirty_v280()
        self.assertIsNone(self.store.get_meta("capture_skip"))
        self.assertIsNone(self.store.get_meta("notify_last_at"))
        self.assertEqual(self.store.get_meta("owner_umo"), "default:FriendMessage:1")

    def test_alias_chain_resolves(self):
        self.store.set_alias("a", "b")
        self.store.set_alias("b", "c")
        self.assertEqual(self.store.resolve_speaker("a"), "c")
        self.assertEqual(self.store.resolve_speaker("b"), "c")
        self.assertEqual(self.store.resolve_speaker("c"), "c")

    def test_apply_config_keeps_cache_until_change(self):
        import time as _time

        service = self._service()
        service.retriever._cache["k"] = (_time.time(), None)
        service.apply_config()
        self.assertIn("k", service.retriever._cache)
        service.config["retrieval_mode"] = "basic"
        service.apply_config()
        self.assertNotIn("k", service.retriever._cache)

    def test_export_import_roundtrip_new_tables(self):
        from savagetype.archive import import_jsonl

        service = self._service()
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        self.store.upsert_profile("u1", "阿U", "aiocqhttp")
        self.store.add_memory_review(
            scope="person", speaker_id="u1", raw_text="我喜欢喝茶", plain="喜欢喝茶"
        )
        path = Path(self.tmp.name) / "exp.jsonl"
        service.export_jsonl(path)
        store2 = Store(Path(self.tmp.name) / "imported.db")
        try:
            result = import_jsonl(store2, path)
            self.assertGreaterEqual(result["facts"], 1)
            self.assertGreaterEqual(result["profiles"], 1)
            self.assertGreaterEqual(result["memory_reviews"], 1)
            self.assertIsNotNone(store2.get_profile("u1"))
        finally:
            store2.close()

    def test_embedding_auto_threshold_zero_disables(self):
        service = self._service(embedding_auto_threshold=0)
        self.assertFalse(service.embedding_wanted())

    def test_owner_reply_bare_words(self):
        service = self._service(owner_qq="owner")
        rid = self.store.add_memory_review(
            scope="owner", speaker_id="owner", speaker_name="主人",
            raw_text="我喜欢喝茶", plain="喜欢喝茶",
            payload=_payload(speaker="owner", value="茶", content="我喜欢喝茶"),
        )
        self.assertIsNone(asyncio.run(service.handle_owner_reply("删除")))
        self.assertIsNotNone(self.store.get_memory_review(rid))
        reply = asyncio.run(service.handle_owner_reply("是"))
        self.assertIn("已通过", reply)

    def test_dedup_persists_across_service_instances(self):
        self.engine.ingest(_payload(speaker="u1", value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        first = self._service()
        r1 = asyncio.run(first.build_injection("我喜欢什么", "u1", window_tag="w1"))
        self.assertTrue(r1[1].core or r1[1].related)
        second = self._service()  # 模拟插件重载：新实例，同一个库
        r2 = asyncio.run(second.build_injection("我喜欢什么", "u1", window_tag="w1"))
        self.assertEqual(r2[2]["dedup"], 1)
        self.assertEqual([f.id for f in r2[1].core], [])


class EventLayerTest(unittest.TestCase):
    """v4.2.0 episodic layer: segmentation, summary, merge, lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "events.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _ev(i, ts, window="w1", role="user", content="x", sid="u1"):
        return TimelineEvent(
            id=i, ts=ts, speaker_id=sid, speaker_name=sid, bot_id="b",
            window_tag=window, role=role, content=content, persona_id="",
        )

    def _add(self, i, ts, window="w1", role="user", content="x", sid="u1"):
        self.store.add_timeline(
            {
                "ts": ts, "speaker_id": sid, "speaker_name": sid, "bot_id": "b",
                "window_tag": window, "role": role, "content": content,
                "fingerprint": f"ev-{i}",
            }
        )

    def _pipeline(self, store=None, summary=None, verify=None, summary_llm=None, **config):
        from savagetype.events import EventPipeline

        calls = {"summary": 0, "verify": 0}
        verdict = verify or '{"pass": true, "reason": "", "fix_hint": ""}'
        payload = summary or json.dumps(
            {
                "kind": "life", "title": "成都之行", "summary": "去成都玩了，吃了火锅",
                "highlights": ["吃火锅"], "keywords": ["成都"], "importance": 0.7, "confidence": 0.9,
            },
            ensure_ascii=False,
        )

        async def default_summary(_prompt):
            calls["summary"] += 1
            return payload

        async def default_verify(_prompt):
            calls["verify"] += 1
            return verdict

        cfg = {
            "event_enabled": True,
            "event_gap_minutes": 30,
            "event_min_messages": 2,
            "event_merge_minutes": 120,
            "event_max_per_run": 5,
        }
        cfg.update(config)
        pipe = EventPipeline(
            store or self.store, cfg, None,
            summary_llm if summary_llm is not None else default_summary,
            default_verify,
        )
        return pipe, calls

    def test_split_episodes_by_gap_and_window(self):
        from savagetype.events import split_episodes

        rows = [
            self._ev(1, 1000, window="w1"),
            self._ev(2, 1100, window="w2"),
            self._ev(3, 1200, window="w1"),
            self._ev(4, 5000, window="w1"),
        ]
        episodes = split_episodes(rows, gap_seconds=1800, max_span_seconds=6 * 3600)
        self.assertEqual([[e.id for e in ep] for ep in episodes], [[1, 3], [2], [4]])

    def test_split_episodes_hard_span(self):
        from savagetype.events import split_episodes

        rows = [self._ev(1, 0), self._ev(2, 100), self._ev(3, 200)]
        episodes = split_episodes(rows, gap_seconds=10 ** 9, max_span_seconds=150)
        self.assertEqual([[e.id for e in ep] for ep in episodes], [[1, 2], [3]])

    def test_episode_worthy_gate(self):
        from savagetype.events import episode_worthy

        chatter = [
            self._ev(1, 1, content="哈哈哈"),
            self._ev(2, 2, content="嗯嗯"),
            self._ev(3, 3, content="好的"),
        ]
        self.assertFalse(episode_worthy(chatter, min_messages=4))
        narrative = [
            self._ev(1, 1, content="我昨天去了成都"),
            self._ev(2, 2, content="嗯"),
        ]
        self.assertTrue(episode_worthy(narrative, min_messages=4))
        plain = [self._ev(i, i, content="今天天气不错") for i in range(1, 5)]
        self.assertTrue(episode_worthy(plain, min_messages=4))
        bot_only = [self._ev(1, 1, role="assistant", sid="bot_self")]
        self.assertFalse(episode_worthy(bot_only, min_messages=1))

    def test_chunk_episode_by_count_and_chars(self):
        from savagetype.events import chunk_episode

        rows = [self._ev(i, i, content="x" * 10) for i in range(10)]
        chunks = chunk_episode(rows, max_messages=4, max_chars=10 ** 6)
        self.assertEqual([len(c) for c in chunks], [4, 4, 2])
        wide = [self._ev(i, i, content="x" * 60) for i in range(4)]
        chunks = chunk_episode(wide, max_messages=10, max_chars=100)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1, 1])

    def test_pipeline_creates_and_extends_one_event(self):
        base = now_ts() - 6 * 3600
        self._add("a1", base, content="我昨天去了成都")
        self._add("a2", base + 60, content="吃了火锅，很好吃")
        self._add("a3", base + 70, role="assistant", sid="bot_self", content="听起来不错")
        pipe, calls = self._pipeline()
        first = asyncio.run(pipe.run())
        self.assertEqual(first["created"], 1)
        events = self.store.events_by_status("live")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.kind, "life")
        self.assertEqual(event.title, "成都之行")
        self.assertEqual(event.review_status, "ai_passed")
        self.assertEqual(len(event.evidence), 3)
        self.assertEqual(event.scope, "person")

        self._add("b1", base + 7200, content="第二天又去爬山了")
        self._add("b2", base + 7260, content="累死了")
        second = asyncio.run(pipe.run())
        self.assertEqual(second["extended"], 1)
        events = self.store.events_by_status("live")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, event.id)
        self.assertEqual(len(events[0].evidence), 5)
        self.assertEqual(events[0].end_ts, base + 7260)
        self.assertEqual(calls["summary"], 2)

    def test_pipeline_verify_failure_marks_needs_review(self):
        base = now_ts() - 7200
        self._add("v1", base, content="我昨天去了成都")
        self._add("v2", base + 10, content="吃了火锅")
        pipe, _calls = self._pipeline(verify='{"pass": false, "reason": "加了原文没有的细节", "fix_hint": "删掉"}')
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        event = self.store.events_by_status("live")[0]
        self.assertEqual(event.review_status, "needs_review")
        self.assertLessEqual(event.confidence, 0.4)

    def test_pipeline_fallback_without_llm_keeps_evidence(self):
        base = now_ts() - 7200
        self._add("f1", base, content="我昨天去了成都")
        self._add("f2", base + 10, content="吃了火锅")
        pipe, _calls = self._pipeline(summary_llm=None)
        pipe.llm = None
        result = asyncio.run(pipe.run())
        self.assertEqual(result["created"], 1)
        event = self.store.events_by_status("live")[0]
        self.assertEqual(event.review_status, "needs_review")
        self.assertIn("成都", event.summary)
        self.assertEqual(len(event.evidence), 2)

    def test_event_recall_log_is_separate_from_facts(self):
        self.store.add_recall("w1", [7], 1000)
        self.store.add_event_recall("w1", [7], 1000)
        self.assertEqual(self.store.recent_recall_ids("w1", 0), {7})
        self.assertEqual(self.store.recent_event_recall_ids("w1", 0), {7})

    def test_archive_decayed_events(self):
        from savagetype.archive import archive_decayed_events

        eid = self.store.add_event(
            {
                "title": "旧事", "summary": "很久以前的事", "speaker_id": "u1",
                "start_ts": now_ts() - 400 * 86400, "end_ts": now_ts() - 400 * 86400,
                "importance": 0.05, "evidence": [1],
            }
        )
        self.assertEqual(archive_decayed_events(self.store, min_age_days=90, threshold=0.12), 1)
        self.assertEqual(self.store.get_event(eid).status, "archived")

    def test_referenced_timeline_ids_includes_event_evidence(self):
        self._add("r1", now_ts() - 7200, content="我去了成都")
        self.store.add_event(
            {
                "title": "成都", "summary": "去了成都", "speaker_id": "u1",
                "start_ts": now_ts() - 7200, "end_ts": now_ts() - 7200, "evidence": [1],
            }
        )
        self.assertIn(1, self.store.referenced_timeline_ids())

    def test_events_export_import_roundtrip(self):
        from savagetype.archive import import_jsonl
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store, config={}, llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None, logger=None,
        )
        self.store.add_event(
            {
                "title": "成都之行", "summary": "去成都玩了", "speaker_id": "u1",
                "start_ts": 1000, "end_ts": 1200, "evidence": [1], "fingerprint": "ev-fp-1",
            }
        )
        path = Path(self.tmp.name) / "events.jsonl"
        service.export_jsonl(path)
        store2 = Store(Path(self.tmp.name) / "imported.db")
        try:
            result = import_jsonl(store2, path)
            self.assertGreaterEqual(result["events"], 1)
            self.assertEqual(len(store2.events_by_status("live")), 1)
            again = import_jsonl(store2, path)
            self.assertEqual(again["events"], 0)
            self.assertGreaterEqual(again["skipped"], 1)
        finally:
            store2.close()

    def test_maybe_extract_runs_event_layer(self):
        from savagetype.service import SavageTypeService

        base = now_ts() - 7200
        self._add("s1", base, content="我昨天去了成都")
        self._add("s2", base + 30, content="吃了火锅")
        service = SavageTypeService(
            store=self.store,
            config={
                "event_enabled": True,
                "event_gap_minutes": 30,
                "event_min_messages": 2,
                "event_merge_minutes": 120,
                "extract_enabled": True,
            },
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        result = asyncio.run(service.maybe_extract(force=True))
        self.assertIn("events_layer", result)
        self.assertEqual(result["events_layer"]["created"], 1)

    # ------------------------------------------------------------------
    # P2: retrieval, injection, privacy isolation
    # ------------------------------------------------------------------

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        service.apply_config()
        return service

    def _add_event(self, title, summary, window="w1", sid="u1", days_ago=1.0, **kw):
        now = now_ts()
        end = int(now - days_ago * 86400)
        payload = {
            "title": title,
            "summary": summary,
            "speaker_id": sid,
            "speaker_name": sid,
            "speaker_ids": [sid],
            "window_tag": window,
            "start_ts": end - 600,
            "end_ts": end,
            "kind": kw.pop("kind", "life"),
            "importance": kw.pop("importance", 0.8),
            "confidence": kw.pop("confidence", 0.9),
            "review_status": kw.pop("review_status", "ai_passed"),
            "evidence": kw.pop("evidence", [1]),
        }
        payload.update(kw)
        return self.store.add_event(payload)

    def test_event_injected_once_per_window(self):
        eid = self._add_event("成都之行", "去成都玩了三天，吃了火锅")
        service = self._service(memory_session_isolation="strict", event_enabled=True)
        pack, _result, snapshot = asyncio.run(
            service.build_injection("最近怎么样", "u1", window_tag="w1")
        )
        self.assertIn("【事件】", pack)
        self.assertIn("成都之行", pack)
        self.assertEqual(snapshot["injected_event_ids"], [eid])
        self.assertEqual(self.store.get_event(eid).access_count, 1)
        again, _r2, snap2 = asyncio.run(
            service.build_injection("最近怎么样", "u1", window_tag="w1")
        )
        self.assertNotIn("成都之行", again)
        self.assertEqual(snap2["injected_event_ids"], [])

    def test_event_blocked_in_other_session_when_isolated(self):
        self._add_event("成都之行", "去成都玩了三天", window="w2")
        strict = self._service(memory_session_isolation="strict", event_enabled=True)
        pack, _r, _s = asyncio.run(strict.build_injection("成都", "u1", window_tag="w1"))
        self.assertNotIn("成都之行", pack)

        open_service = self._service(memory_session_isolation="off", event_enabled=True)
        pack2, _r2, snap2 = asyncio.run(
            open_service.build_injection("成都", "u1", window_tag="w1")
        )
        self.assertIn("成都之行", pack2)
        self.assertTrue(snap2["injected_event_ids"])

    def test_owner_facts_not_leaked_to_others(self):
        self.store.add_fact(
            {
                "subject": "self", "attribute": "likes", "value": "咖啡",
                "content": "我喜欢咖啡", "speaker_id": "owner", "speaker_name": "主人",
                "scope": "owner", "confidence": 0.9, "first_person": 1,
                "window_tag": "default:FriendMessage:owner",
            }
        )
        service = self._service(memory_session_isolation="strict", owner_qq="owner")
        result = asyncio.run(
            service.retrieve_for("主人喜欢什么", "u1", window_tag="aiocqhttp:GroupMessage:1")
        )
        self.assertTrue(any(h.filter_reason == "owner_private" for h in result.blocked))
        result2 = asyncio.run(
            service.retrieve_for("我喜欢什么", "owner", window_tag="default:FriendMessage:owner")
        )
        self.assertTrue(result2.core or result2.related)

    def test_private_origin_fact_hidden_in_group(self):
        self.store.add_fact(
            {
                "subject": "self", "attribute": "note", "value": "养了两只猫",
                "content": "我养了两只猫", "speaker_id": "u1", "speaker_name": "阿U",
                "scope": "person", "confidence": 0.9, "first_person": 1,
                "window_tag": "default:FriendMessage:u1-1",
            }
        )
        service = self._service(memory_session_isolation="strict")
        group = asyncio.run(
            service.retrieve_for("我养了什么", "u1", window_tag="aiocqhttp:GroupMessage:1")
        )
        self.assertTrue(any(h.filter_reason == "private_origin" for h in group.blocked))
        private = asyncio.run(
            service.retrieve_for("我养了什么", "u1", window_tag="default:FriendMessage:u1-1")
        )
        self.assertTrue(private.core or private.related)

    def test_time_window_route_limits_event_age(self):
        recent = self._add_event("新事件", "上周去了成都", days_ago=3)
        self._add_event("老事件", "很久以前去了成都", days_ago=40)
        service = self._service(memory_session_isolation="strict", event_enabled=True)
        result = asyncio.run(
            service.retrieve_for("上周去成都做了什么", "u1", window_tag="w1")
        )
        self.assertEqual(result.route, "time_window")
        self.assertEqual([e.id for e in result.events], [recent])

    def test_event_budget_zero_keeps_facts_only(self):
        self._add_event("成都之行", "去成都玩了三天")
        service = self._service(
            memory_session_isolation="strict", event_enabled=True, event_budget_chars=1
        )
        pack, _r, snap = asyncio.run(service.build_injection("成都", "u1", window_tag="w1"))
        self.assertNotIn("【事件】", pack)
        self.assertEqual(snap["injected_event_ids"], [])

    def test_needs_review_event_not_injected(self):
        self._add_event("乱写的", "模型编的内容", review_status="needs_review")
        service = self._service(memory_session_isolation="strict", event_enabled=True)
        pack, _r, _s = asyncio.run(service.build_injection("成都", "u1", window_tag="w1"))
        self.assertNotIn("乱写的", pack)

    # ------------------------------------------------------------------
    # P4: promise / habit per-topic slots
    # ------------------------------------------------------------------

    def test_promise_and_habit_topics_coexist(self):
        engine = ContradictionEngine(self.store, high_evidence=0.8)
        a = engine.ingest(
            _payload(attribute="promise", value="带饭", content="答应小明带饭"),
            "答应小明带饭",
        )
        b = engine.ingest(
            _payload(attribute="promise", value="交作业", content="答应周五交作业"),
            "答应周五交作业",
        )
        self.assertEqual(a["action"], "insert")
        self.assertEqual(b["action"], "insert")
        lives = [f for f in self.store.person_facts("u1") if f.attribute == "promise"]
        self.assertEqual(len(lives), 2)

        h1 = engine.ingest(
            _payload(attribute="habit", value="早起", content="我习惯早起"),
            "我习惯早起",
        )
        h2 = engine.ingest(
            _payload(attribute="habit", value="戒烟", content="我戒烟了"),
            "我戒烟了",
        )
        self.assertEqual(h1["action"], "insert")
        self.assertEqual(h2["action"], "insert")
        habits = [f for f in self.store.person_facts("u1") if f.attribute == "habit"]
        self.assertEqual(len(habits), 2)

    def test_close_targets_latest_promise(self):
        engine = ContradictionEngine(self.store, high_evidence=0.8)
        a = engine.ingest(
            _payload(attribute="promise", value="带饭", content="答应小明带饭"),
            "答应小明带饭",
        )
        b = engine.ingest(
            _payload(attribute="promise", value="交作业", content="答应周五交作业"),
            "答应周五交作业",
        )
        self.store.update_fact(b["fact_id"], updated_at=now_ts() + 5)
        close = engine.ingest(
            _payload(attribute="promise", value="交作业做完了", content="交作业做完了", write_op="close"),
            "交作业做完了",
        )
        self.assertEqual(close["action"], "closed")
        self.assertEqual(self.store.get_fact(b["fact_id"]).status, "archived")
        self.assertEqual(self.store.get_fact(a["fact_id"]).status, "live")

    def test_close_without_existing_is_ignored(self):
        engine = ContradictionEngine(self.store, high_evidence=0.8)
        result = engine.ingest(
            _payload(attribute="promise", value="没写过的约定", content="做完了", write_op="close"),
            "做完了",
        )
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(result["reason"], "close_without_existing")


class EntityHistoryTest(unittest.TestCase):
    """v4.3.0: entity linking weights and time-travel queries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "eh.db")
        self.engine = ContradictionEngine(self.store, high_evidence=0.8)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        service.apply_config()
        return service

    def _note(self, value, content, keywords=None, speaker="u1", window="w1", attribute="note"):
        return self.engine.ingest(
            _payload(
                speaker=speaker,
                attribute=attribute,
                value=value,
                content=content,
                keywords=keywords or [],
                window_tag=window,
            ),
            content,
        )

    def test_entity_links_and_rename(self):
        result = self._note("爬山", "和小明约了周末爬山", keywords=["小明", "爬山"])
        names = {item["name"]: item["kind"] for item in self.store.entities_for_ref("fact", result["fact_id"])}
        self.assertIn("小明", names)
        self.assertIn("爬山", names)
        self.assertEqual(names["小明"], "keyword")

        found = self.store.entities_in_text("小明这周末干嘛")
        self.assertIn("小明", found)
        fact_ids, event_ids = self.store.entity_refs(found)
        self.assertIn(result["fact_id"], fact_ids)
        self.assertEqual(event_ids, set())

        self.store.update_fact(result["fact_id"], speaker_name="阿优")
        renamed = {item["name"] for item in self.store.entities_for_ref("fact", result["fact_id"])}
        self.assertIn("阿优", renamed)

    def test_entity_boost_changes_score(self):
        from savagetype.retrieve import Retriever

        result = self._note("爬山", "和小明约了周末爬山", keywords=["小明"])
        fact = self.store.get_fact(result["fact_id"])
        retriever = Retriever(self.store)
        query = "小明最近去哪了"
        base = retriever._local_score(query, fact, "u1", "long_term")
        boosted = retriever._local_score(
            query, fact, "u1", "long_term", entity_ids={fact.id}, entity_boost=0.2
        )
        self.assertAlmostEqual(boosted - base, 0.2, places=6)
        custom = retriever._local_score(
            query, fact, "u1", "long_term", entity_ids={fact.id}, entity_boost=0.5
        )
        self.assertAlmostEqual(custom - base, 0.5, places=6)

        service = self._service(entity_linking_enabled=True)
        retrieved = asyncio.run(service.retrieve_for(query, "u1", window_tag="w1"))
        self.assertIn(result["fact_id"], [hit.fact.id for hit in retrieved.hits])

    def test_parse_time_range_cases(self):
        from savagetype.util import parse_time_range

        now = int(now_ts())
        start, end, label = parse_time_range("2025年3月的事", now)
        self.assertEqual(label, "2025-03")
        self.assertEqual(
            time.strftime("%Y-%m", time.localtime(start)), "2025-03"
        )
        start, end, label = parse_time_range("最近3天", now)
        self.assertEqual(label, "最近3天")
        self.assertLess(start, end)
        start, end, label = parse_time_range("以前喜欢什么", now)
        self.assertEqual(label, "当时")
        self.assertEqual(start, 0)
        self.assertGreater(end, 0)
        self.assertEqual(parse_time_range("今天天气不错", now), (0, 0, ""))

    def test_facts_in_window_respects_validity(self):
        first = self._note("美式", "我喜欢喝美式", attribute="likes")
        second = self.engine.ingest(
            _payload(
                speaker="u1", attribute="likes", value="不美式",
                content="我改口了，不喜欢美式", explicit_correction=1, window_tag="w1",
            ),
            "我改口了，不喜欢美式",
        )
        self.assertEqual(second["action"], "supersede")
        start = now_ts() - 60
        end = now_ts() + 60
        window = self.store.facts_in_window(start, end, speaker_ids=["u1"], include_owner=True)
        ids = {f.id for f in window}
        self.assertIn(first["fact_id"], ids)
        empty = self.store.facts_in_window(0, start - 120, speaker_ids=["u1"], include_owner=True)
        self.assertNotIn(first["fact_id"], {f.id for f in empty})

    def test_history_retrieval_and_injection(self):
        first = self._note("美式", "我喜欢喝美式", attribute="likes")
        self.engine.ingest(
            _payload(
                speaker="u1", attribute="likes", value="不美式",
                content="我改口了，不喜欢美式", explicit_correction=1, window_tag="w1",
            ),
            "我改口了，不喜欢美式",
        )
        service = self._service(memory_session_isolation="strict")
        result = asyncio.run(
            service.retrieve_for("我以前喜欢喝什么", "u1", window_tag="w1")
        )
        self.assertEqual(result.route, "history")
        self.assertIn(first["fact_id"], [f.id for f in result.history])
        self.assertTrue(result.history_current.get(first["fact_id"]))

        pack, _r, snapshot = asyncio.run(
            service.build_injection("我以前喜欢喝什么", "u1", window_tag="w1")
        )
        self.assertIn("【当时】", pack)
        self.assertIn("现在：", pack)
        self.assertIn(first["fact_id"], snapshot["history"])

    def test_history_events_filtered_by_time_range(self):
        import datetime

        def ts(year, month, day):
            return int(datetime.datetime(year, month, day, 12, 0).timestamp())

        in_range = self.store.add_event(
            {
                "title": "成都之行", "summary": "去成都玩了", "speaker_id": "u1",
                "speaker_ids": ["u1"], "window_tag": "w1",
                "start_ts": ts(2025, 3, 5), "end_ts": ts(2025, 3, 5) + 3600,
            }
        )
        self.store.add_event(
            {
                "title": "旧事", "summary": "很久以前", "speaker_id": "u1",
                "speaker_ids": ["u1"], "window_tag": "w1",
                "start_ts": ts(2024, 1, 2), "end_ts": ts(2024, 1, 2) + 3600,
            }
        )
        service = self._service(memory_session_isolation="strict")
        result = asyncio.run(
            service.retrieve_for("2025年3月发生了什么", "u1", window_tag="w1")
        )
        self.assertEqual(result.route, "history")
        self.assertEqual([e.id for e in result.events], [in_range])

    def test_history_owner_private_not_leaked(self):
        owner = self.engine.ingest(
            {
                "subject": "self", "attribute": "likes", "value": "咖啡",
                "content": "我喜欢咖啡", "speaker_id": "owner", "speaker_name": "主人",
                "scope": "owner", "confidence": 0.9, "first_person": 1,
                "window_tag": "default:FriendMessage:owner",
            },
            "我喜欢咖啡",
        )
        self.store.update_fact(owner["fact_id"], status="superseded", updated_at=now_ts())
        service = self._service(memory_session_isolation="strict", owner_qq="owner")
        result = asyncio.run(
            service.retrieve_for("主人以前喜欢什么", "u1", window_tag="aiocqhttp:GroupMessage:1")
        )
        self.assertEqual(result.route, "history")
        self.assertEqual(result.history, [])

    def test_events_between_window(self):
        now = now_ts()
        inside = self.store.add_event(
            {
                "title": "近事", "summary": "刚发生", "speaker_id": "u1",
                "speaker_ids": ["u1"], "window_tag": "w1",
                "start_ts": now - 3600, "end_ts": now - 60,
            }
        )
        self.store.add_event(
            {
                "title": "远事", "summary": "很久以前", "speaker_id": "u1",
                "speaker_ids": ["u1"], "window_tag": "w1",
                "start_ts": now - 400 * 86400, "end_ts": now - 400 * 86400 + 3600,
            }
        )
        ids = [e.id for e in self.store.events_between(now - 7200, now, speaker_ids=["u1"])]
        self.assertEqual(ids, [inside])

    def test_archived_event_visible_on_history_route(self):
        now = now_ts()
        eid = self.store.add_event(
            {
                "title": "老聚会", "summary": "很久以前的聚会", "speaker_id": "u1",
                "speaker_ids": ["u1"], "window_tag": "w1",
                "start_ts": now - 400 * 86400, "end_ts": now - 400 * 86400 + 3600,
            }
        )
        self.store.update_event(eid, status="archived")
        service = self._service(memory_session_isolation="strict")
        plain = asyncio.run(service.retrieve_for("去年聚会了吗", "u1", window_tag="w1"))
        self.assertIn(eid, [e.id for e in plain.events])
        live_only = asyncio.run(service.retrieve_for("最近怎么样", "u1", window_tag="w1"))
        self.assertNotIn(eid, [e.id for e in live_only.events])

    def test_history_ask_other_sees_named_person(self):
        self.engine.ingest(
            _payload(speaker="u2", value="咖啡", content="我喜欢喝咖啡"),
            "我喜欢喝咖啡",
        )
        old = self.engine.ingest(
            {
                "subject": "self", "attribute": "likes", "value": "茶",
                "content": "我喜欢喝茶", "speaker_id": "u2", "speaker_name": "小明",
                "confidence": 0.9, "first_person": 1, "window_tag": "w1",
            },
            "我喜欢喝茶",
        )
        self.store.update_fact(old["fact_id"], status="superseded", updated_at=now_ts())
        service = self._service(memory_session_isolation="strict")
        result = asyncio.run(
            service.retrieve_for("小明以前喜欢什么", "u1", window_tag="w1")
        )
        self.assertIn(old["fact_id"], [f.id for f in result.history])

    def test_history_current_walks_supersede_chain(self):
        r1 = self.engine.ingest(_payload(value="茶", content="我喜欢喝茶"), "我喜欢喝茶")
        r2 = self.engine.ingest(
            _payload(value="不茶", content="我改口了，不喜欢茶了", explicit_correction=1),
            "我改口了，不喜欢茶了",
        )
        r3 = self.engine.ingest(
            _payload(value="茶", content="我又喜欢茶了", explicit_correction=1),
            "我又喜欢茶了",
        )
        service = self._service(memory_session_isolation="strict")
        result = asyncio.run(
            service.retrieve_for("我以前喜欢什么", "u1", window_tag="w1")
        )
        latest = self.store.get_fact(r3["fact_id"])
        self.assertIn(r1["fact_id"], [f.id for f in result.history])
        self.assertEqual(
            result.history_current.get(r1["fact_id"]), latest.plain or latest.value
        )

    def test_history_novelty_filter_skips_mentioned_value(self):
        self._note("美式", "我喜欢喝美式", attribute="likes")
        self.engine.ingest(
            _payload(
                speaker="u1", attribute="likes", value="不美式",
                content="我改口了，不喜欢美式", explicit_correction=1, window_tag="w1",
            ),
            "我改口了，不喜欢美式",
        )
        service = self._service(memory_session_isolation="strict")
        pack, _r, _s = asyncio.run(
            service.build_injection("我以前喜欢美式吗", "u1", window_tag="w1")
        )
        self.assertNotIn("【当时】", pack)

    def test_event_participant_name_follows_rename(self):
        now = now_ts()
        eid = self.store.add_event(
            {
                "title": "爬山", "summary": "一起去爬山", "speaker_id": "u1",
                "speaker_name": "阿U", "speaker_ids": ["u1"],
                "participants": [{"id": "u1", "name": "阿U"}],
                "window_tag": "w1", "start_ts": now - 3600, "end_ts": now - 60,
            }
        )
        self.assertEqual(self.store.sync_event_participant("u1", "阿优"), 1)
        event = self.store.get_event(eid)
        self.assertEqual(event.participants[0]["name"], "阿优")
        fact_ids, event_ids = self.store.entity_refs(["阿优"])
        self.assertIn(eid, event_ids)

    def test_events_run_despite_fact_pipeline_failure(self):
        service = self._service(
            event_enabled=True,
            event_gap_minutes=30,
            event_min_messages=2,
            extract_enabled=True,
        )

        async def boom(**_kw):
            raise RuntimeError("normalize provider down")

        service.pipeline.run = boom
        base = now_ts()
        self.store.add_timeline(
            {
                "ts": base, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "w1", "role": "user", "content": "我决定下个月去成都",
                "fingerprint": "isolate-1",
            }
        )
        self.store.add_timeline(
            {
                "ts": base + 10, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "w1", "role": "user", "content": "还要去吃火锅",
                "fingerprint": "isolate-2",
            }
        )
        result = asyncio.run(service.maybe_extract(force=True))
        self.assertEqual(result["reason"], "extract_fail")
        self.assertIn("events_layer", result)
        self.assertEqual(result["events_layer"]["created"], 1)

    def test_entity_refs_respect_persona(self):
        self.store.add_fact(
            {
                "subject": "self", "attribute": "note", "value": "和小明爬山",
                "content": "和小明爬山", "speaker_id": "u1", "speaker_name": "u1",
                "confidence": 0.8, "first_person": 1, "persona_id": "persona-a",
                "keywords": ["小明"], "window_tag": "w1",
            }
        )
        fact_ids, _events = self.store.entity_refs(["小明"])
        self.assertTrue(fact_ids)
        scoped, _events2 = self.store.entity_refs(["小明"], persona_id="persona-b")
        self.assertEqual(scoped, set())

    def test_event_pipeline_force_processes_fresh_rows(self):
        from savagetype.events import EventPipeline

        now = now_ts()
        self.store.add_timeline(
            {
                "ts": now, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "w1", "role": "user", "content": "我决定下个月去成都",
                "fingerprint": "force-1",
            }
        )
        self.store.add_timeline(
            {
                "ts": now + 5, "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "w1", "role": "user", "content": "还要去吃火锅",
                "fingerprint": "force-2",
            }
        )
        pipe = EventPipeline(
            self.store,
            {"event_enabled": True, "event_gap_minutes": 45, "event_min_messages": 2},
            None,
            None,
        )
        idle_result = asyncio.run(pipe.run())
        self.assertTrue(idle_result.get("skipped"))
        forced = asyncio.run(pipe.run(force=True))
        self.assertEqual(forced.get("created"), 1)


class LLMStrategyTest(unittest.TestCase):
    """v4.4.0 模型调用策略：任务分档、回退链、Token 预算闸、拒答识别、用量账。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "llm.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_resolve_provider_precedence(self):
        from savagetype.llm import resolve_provider

        config = {
            "normalize_provider_id": "p-norm",
            "verify_provider_id": "p-verify",
            "summary_provider_id": "p-summary",
            "quality_provider_id": "p-quality",
            "fast_provider_id": "p-fast",
        }
        self.assertEqual(resolve_provider("normalize", config), ("p-norm", "explicit:normalize_provider_id"))
        self.assertEqual(resolve_provider("verify", config), ("p-verify", "explicit:verify_provider_id"))
        # event 走旧回退链（event → normalize → summary）
        self.assertEqual(resolve_provider("event", config), ("p-norm", "explicit:normalize_provider_id"))
        # learn 走 fast 档（normalize/summary 都清掉之后）
        config["normalize_provider_id"] = ""
        config["summary_provider_id"] = ""
        self.assertEqual(resolve_provider("learn", config), ("p-fast", "tier:fast"))
        self.assertEqual(resolve_provider("verify", config), ("p-verify", "explicit:verify_provider_id"))
        # 全清空后跟随会话模型
        config["verify_provider_id"] = ""
        config["quality_provider_id"] = ""
        config["fast_provider_id"] = ""
        self.assertEqual(resolve_provider("normalize", config), ("", "default"))
        self.assertEqual(resolve_provider("learn", config), ("", "default"))

    def test_estimate_and_refusal(self):
        from savagetype.llm import estimate_tokens, looks_refusal

        self.assertEqual(estimate_tokens("中文四个字"), 5)
        self.assertEqual(estimate_tokens(""), 0)
        self.assertTrue(looks_refusal("抱歉，我无法协助完成这个请求。"))
        self.assertTrue(looks_refusal("As an AI, I cannot provide that."))
        self.assertFalse(looks_refusal("标题：测试事件\n分区：科技"))

    def test_budget_hard_soft_single_cap(self):
        from savagetype.llm import BudgetGuard

        used = {"n": 0}
        guard = BudgetGuard({"daily_token_limit": 100, "soft_token_limit": 50}, lambda: used["n"])
        self.assertTrue(guard.check("normalize", "x").allowed)
        used["n"] = 60
        self.assertTrue(guard.check("normalize", "x").allowed, "高优先级任务不受软限")
        blocked = guard.check("learn", "x")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "soft_token_limit")
        used["n"] = 100
        hard = guard.check("normalize", "x")
        self.assertFalse(hard.allowed)
        self.assertEqual(hard.reason, "daily_token_limit")

        cap = BudgetGuard(
            {"single_call_token_cap": 10, "fallback_provider_id": "p-backup"},
            lambda: 0,
        )
        decision = cap.check("normalize", "很长的提示词" * 50)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.provider_override, "p-backup")
        no_fb = BudgetGuard({"single_call_token_cap": 10}, lambda: 0).check(
            "normalize", "很长的提示词" * 50
        )
        self.assertTrue(no_fb.allowed)
        self.assertEqual(no_fb.reason, "single_call_cap_no_fallback")

    def test_usage_daily_accounting(self):
        self.store.add_usage(
            "llm", "p1", True, 10, 20, tokens_in=100, tokens_out=50,
            task="normalize", source="tier:quality",
        )
        self.store.add_usage("llm", "", False, task="learn", source="tier:fast", reason="soft_token_limit")
        self.store.add_usage("embed", "e1", True, 30, 0, tokens_in=40, task="embed")
        self.assertEqual(self.store.tokens_today(), 190)
        rows = {row["task"]: row for row in self.store.usage_by_task_today()}
        self.assertEqual(rows["normalize"]["tokens"], 150)
        self.assertEqual(rows["normalize"]["source"], "tier:quality")
        self.assertEqual(rows["learn"]["skipped"], 1)
        self.assertEqual(rows["learn"]["skip_reason"], "soft_token_limit")
        self.assertEqual(rows["embed"]["tokens_in"], 40)

    def test_budget_blocked_extraction_keeps_timeline(self):
        """硬限额下抽取被跳过：时间线不标记已总结，也没有新事实。"""
        from savagetype.service import SavageTypeService

        self.store.add_timeline(
            {
                "ts": now_ts(), "speaker_id": "u1", "speaker_name": "阿U", "bot_id": "b",
                "window_tag": "w1", "role": "user", "content": "我喜欢喝美式",
                "fingerprint": "budget-1",
            }
        )
        service = SavageTypeService(
            store=self.store,
            config={"daily_token_limit": 1, "extract_enabled": True, "event_enabled": False},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        service.apply_config()
        self.store.add_usage("llm", "p", True, tokens_in=10)
        result = asyncio.run(service.maybe_extract(force=True))
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "budget")
        self.assertEqual(self.store.counts()["unsummarized"], 1, "消息必须留着重试")
        self.assertEqual(self.store.counts()["facts_live"], 0)
        rows = {row["task"]: row for row in self.store.usage_by_task_today()}
        self.assertGreaterEqual(rows.get("normalize", {}).get("skipped", 0), 1)

    def test_tokens_status_shape(self):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config={"soft_token_limit": 500, "daily_token_limit": 1000},
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=None,
        )
        service.apply_config()
        status = service.tokens_status()
        self.assertEqual(status["hard_limit"], 1000)
        self.assertEqual(status["soft_limit"], 500)
        self.assertIn("by_task", status)
        self.assertEqual(service.overview()["tokens"]["hard_limit"], 1000)


class CrossSessionTest(unittest.TestCase):
    """A 层画像卡 + B 层跨窗口衔接。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "cross.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fact(self, **kwargs):
        payload = {
            "subject": "self",
            "attribute": "likes",
            "value": "美式咖啡",
            "plain": "喜欢美式咖啡",
            "content": "我喜欢美式咖啡",
            "speaker_id": "u1",
            "speaker_name": "阿U",
            "status": "live",
            "confidence": 0.9,
        }
        payload.update(kwargs)
        return self.store.add_fact(payload)

    def _msg(self, ts, window, text, speaker="u1", role="user", persona="", fp=None):
        self.store.add_timeline(
            {
                "ts": ts,
                "speaker_id": speaker,
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": window,
                "role": role,
                "content": text,
                "persona_id": persona,
                "fingerprint": fp or f"{window}-{ts}-{text[:6]}",
            }
        )

    # ---- A 层：画像卡 -------------------------------------------------

    def test_profile_card_sections(self):
        self._fact(attribute="name", value="鳄鱼", plain="以后叫我鳄鱼")
        self._fact(attribute="likes", value="美式咖啡", plain="喜欢美式咖啡")
        self._fact(attribute="dislikes", value="拿铁", plain="不喜欢拿铁")
        self._fact(attribute="habit", value="每天练吉他", plain="每天练半小时吉他")
        card, meta = build_profile_card(self.store, "u1")
        self.assertIn("【画像】", card)
        self.assertIn("称呼：鳄鱼", card)
        self.assertIn("偏好：美式咖啡", card)
        self.assertIn("不喜欢：拿铁", card)
        self.assertIn("习惯：每天练吉他", card)
        self.assertEqual(meta["name"], "鳄鱼")
        self.assertFalse(meta["is_owner"])

    def test_profile_card_privacy_by_window(self):
        """A 层画像卡也要遵守会话隔离：strict 下私聊事实不进群聊。"""
        self._fact(attribute="note", value="养了两只猫", window_tag="default:FriendMessage:u1-1")
        card, _meta = build_profile_card(
            self.store,
            "u1",
            window_tag="aiocqhttp:GroupMessage:1",
            isolation="strict",
        )
        self.assertNotIn("两只猫", card)
        card2, _meta2 = build_profile_card(
            self.store,
            "u1",
            window_tag="default:FriendMessage:u1-1",
            isolation="strict",
        )
        self.assertIn("两只猫", card2)
        card3, _meta3 = build_profile_card(
            self.store,
            "u1",
            window_tag="aiocqhttp:GroupMessage:1",
            isolation="off",
        )
        self.assertIn("两只猫", card3)

    def test_profile_card_owner_marked(self):
        self._fact(scope="owner", speaker_id="owner1", speaker_name="主人")
        card, meta = build_profile_card(self.store, "owner1")
        self.assertIn("（主人）", card)
        self.assertTrue(meta["is_owner"])

    def test_profile_card_tone_only_in_hint_line(self):
        self._fact(value="私下很黏人", plain="私下很黏人", mention_policy="tone")
        card, meta = build_profile_card(self.store, "u1")
        # 语气类只在「语气」行出现一次，不会被当成可复述的事实行。
        self.assertEqual(card.count("私下很黏人"), 1)
        tone_line = [line for line in card.splitlines() if line.startswith("语气：")]
        self.assertEqual(len(tone_line), 1)
        self.assertIn("私下很黏人", tone_line[0])
        for line in card.splitlines():
            if line.startswith(("称呼：", "身份：", "偏好：", "不喜欢：", "习惯：", "约定：", "近况：", "备注：")):
                self.assertNotIn("私下很黏人", line)
        self.assertEqual(meta["tone"], 1)

    def test_profile_card_skips_expired_status(self):
        self._fact(attribute="status", value="加班", plain="在加班", expires_at=1)
        card, _ = build_profile_card(self.store, "u1")
        self.assertNotIn("加班", card)
        self._fact(attribute="status", value="出差", plain="在出差", expires_at=now_ts() + 3600)
        card, _ = build_profile_card(self.store, "u1")
        self.assertIn("出差", card)

    def test_profile_card_empty_and_budget(self):
        card, meta = build_profile_card(self.store, "nobody")
        self.assertEqual(card, "")
        self.assertEqual(meta["facts"], 0)
        for index in range(6):
            self._fact(attribute="likes", value=f"饮品{index}", plain=f"喜欢饮品{index}")
        card, _ = build_profile_card(self.store, "u1", max_chars=40)
        self.assertLessEqual(len(card), 40)

    def test_profile_card_is_window_independent(self):
        self._fact(attribute="name", value="鳄鱼", plain="叫我鳄鱼")
        first, _ = build_profile_card(self.store, "u1")
        second, _ = build_profile_card(self.store, "u1")  # 与窗口无关：同一份
        self.assertEqual(first, second)

    # ---- B 层：方向规则与解析 ------------------------------------------

    def test_window_kind(self):
        self.assertEqual(window_kind("aiocqhttp:GroupMessage:123"), "group")
        self.assertEqual(window_kind("aiocqhttp:FriendMessage:456"), "private")
        self.assertEqual(window_kind("webchat:FriendMessage:webchat!a!b"), "private")
        self.assertEqual(window_kind(""), "unknown")
        self.assertEqual(window_kind("import"), "unknown")

    def test_direction_matrix(self):
        self.assertTrue(direction_allowed("group", "private"))
        self.assertTrue(direction_allowed("private", "private"))
        self.assertFalse(direction_allowed("private", "group"))
        self.assertFalse(direction_allowed("group", "group"))
        self.assertTrue(direction_allowed("private", "group", private_to_group=True))
        self.assertTrue(direction_allowed("group", "group", group_to_group=True))
        self.assertFalse(direction_allowed("unknown", "private"))
        self.assertFalse(direction_allowed("private", "unknown"))

    # ---- B 层：组装 ---------------------------------------------------

    def test_cross_window_group_to_private(self):
        now = int(time.time())
        self._msg(now - 600, "aiocqhttp:GroupMessage:123", "周末要加班")
        self._msg(now - 540, "aiocqhttp:GroupMessage:123", "想换个键盘")
        block, meta = build_cross_window(
            self.store,
            ["u1"],
            "aiocqhttp:FriendMessage:456",
            minutes=30,
        )
        self.assertIn("周末要加班", block)
        self.assertIn("想换个键盘", block)
        self.assertIn("在群里说过", block)
        self.assertEqual(meta["items"], 2)
        self.assertEqual(meta["sources"], ["group"])

    def test_cross_window_private_to_group_blocked_by_default(self):
        now = int(time.time())
        self._msg(now - 300, "aiocqhttp:FriendMessage:456", "我偷偷准备了生日礼物")
        block, meta = build_cross_window(
            self.store,
            ["u1"],
            "aiocqhttp:GroupMessage:123",
            minutes=30,
        )
        self.assertEqual(block, "")
        self.assertEqual(meta["items"], 0)
        self.assertGreaterEqual(meta["skipped_direction"], 1)
        allowed, _ = build_cross_window(
            self.store,
            ["u1"],
            "aiocqhttp:GroupMessage:123",
            minutes=30,
            private_to_group=True,
        )
        self.assertIn("生日礼物", allowed)

    def test_cross_window_group_to_group_blocked_by_default(self):
        now = int(time.time())
        self._msg(now - 300, "aiocqhttp:GroupMessage:111", "A 群的事")
        block, _ = build_cross_window(self.store, ["u1"], "aiocqhttp:GroupMessage:222", minutes=30)
        self.assertEqual(block, "")
        allowed, _ = build_cross_window(
            self.store, ["u1"], "aiocqhttp:GroupMessage:222", minutes=30, group_to_group=True
        )
        self.assertIn("A 群的事", allowed)

    def test_cross_window_filters_and_limits(self):
        now = int(time.time())
        self._msg(now - 400, "aiocqhttp:GroupMessage:123", "/stype status")
        self._msg(now - 399, "aiocqhttp:GroupMessage:123", "[图片]")
        self._msg(now - 398, "aiocqhttp:GroupMessage:123", "嗯")
        self._msg(now - 397, "aiocqhttp:GroupMessage:123", "Bot 的回复", speaker="bot_self", role="assistant")
        self._msg(now - 200, "aiocqhttp:GroupMessage:123", "第三人说的话", speaker="u2")
        self._msg(now - 60 * 60, "aiocqhttp:GroupMessage:123", "一小时前的旧话")
        for index in range(8):
            self._msg(now - 100 + index, "aiocqhttp:GroupMessage:123", f"近况第{index}条")
        block, meta = build_cross_window(
            self.store,
            ["u1"],
            "aiocqhttp:FriendMessage:456",
            minutes=30,
            max_items=3,
        )
        self.assertEqual(meta["items"], 3)
        self.assertNotIn("stype", block)
        self.assertNotIn("[图片]", block)
        self.assertNotIn("第三人说的话", block)
        self.assertNotIn("一小时前的旧话", block)
        self.assertNotIn("Bot 的回复", block)

    def test_cross_window_persona_isolation(self):
        now = int(time.time())
        self._msg(now - 120, "aiocqhttp:GroupMessage:123", "别的人格的发言", persona="other")
        block, meta = build_cross_window(
            self.store, ["u1"], "aiocqhttp:FriendMessage:456", minutes=30, persona_id="default"
        )
        self.assertNotIn("别的人格的发言", block)
        self.assertEqual(meta["items"], 0)

    def test_cross_window_ordering_and_budget(self):
        now = int(time.time())
        self._msg(now - 300, "aiocqhttp:GroupMessage:123", "先说的")
        self._msg(now - 200, "aiocqhttp:GroupMessage:123", "后说的")
        block, _ = build_cross_window(self.store, ["u1"], "aiocqhttp:FriendMessage:456", minutes=30)
        self.assertLess(block.index("先说的"), block.index("后说的"))
        tight, meta = build_cross_window(
            self.store, ["u1"], "aiocqhttp:FriendMessage:456", minutes=30, max_chars=60
        )
        self.assertLessEqual(len(tight), 90)
        self.assertLessEqual(meta["items"], 2)

    def test_cross_window_unknown_window(self):
        now = int(time.time())
        self._msg(now - 60, "aiocqhttp:GroupMessage:123", "某句话")
        block, meta = build_cross_window(self.store, ["u1"], "import", minutes=30)
        self.assertEqual(block, "")
        self.assertEqual(meta["reason"], "target_unknown")

    # ---- 注入包集成 ---------------------------------------------------

    def test_pack_carries_profile_and_cross_window(self):
        result = RetrievalResult(
            query="q", route="long_term", path="basic", cache="miss",
            hits=[], blocked=[], core=[], related=[], uncertain=[], superseded=[],
        )
        pack = build_pack(
            result,
            budget=800,
            profile="【画像】阿U\n称呼：鳄鱼",
            cross_window="【衔接·同一个人在别处刚说的】\n- 10:00 在群里说过：周末要加班",
        )
        self.assertIn("【画像】", pack)
        self.assertIn("衔接·同一个人在别处刚说的", pack)
        self.assertIn("savagetype_memory", pack)

    def test_pack_low_info_keeps_profile(self):
        result = RetrievalResult(
            query="你好", route="low_info", path="basic", cache="miss",
            hits=[], blocked=[], core=[], related=[], uncertain=[], superseded=[],
        )
        pack = build_pack(result, budget=800, profile="【画像】阿U\n称呼：鳄鱼")
        self.assertIn("称呼：鳄鱼", pack)
        self.assertEqual(build_pack(result, budget=800), "")

    def test_pack_respects_profile_budget(self):
        result = RetrievalResult(
            query="q", route="long_term", path="basic", cache="miss",
            hits=[], blocked=[], core=[], related=[], uncertain=[], superseded=[],
        )
        pack = build_pack(result, budget=800, profile="画像" * 200, profile_budget=50)
        self.assertIn("画像", pack)
        self.assertLess(len(pack), 400)


    def test_service_toggles_and_wrappers(self):
        """Service 层：两个开关能关掉，开了能拿到内容。"""
        config = {
            "profile_inject_enabled": True,
            "profile_max_chars": 300,
            "cross_window_enabled": True,
            "cross_window_minutes": 30,
            "cross_window_max_items": 6,
            "cross_window_max_chars": 320,
            "cross_window_private_to_group": False,
            "cross_window_group_to_group": False,
            "memory_session_isolation": "strict",
        }
        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        )
        service.apply_config()
        self._fact(attribute="name", value="鳄鱼", plain="叫我鳄鱼")
        card, meta = service.profile_card_for("u1")
        self.assertIn("鳄鱼", card)
        self.assertTrue(meta["enabled"])

        now = int(time.time())
        self._msg(now - 200, "aiocqhttp:GroupMessage:123", "群里说的事")
        block, cross_meta = service.cross_window_for("u1", window_tag="aiocqhttp:FriendMessage:456")
        self.assertIn("群里说的事", block)
        self.assertTrue(cross_meta["enabled"])

        config["profile_inject_enabled"] = False
        config["cross_window_enabled"] = False
        service.apply_config()
        card, meta = service.profile_card_for("u1")
        self.assertEqual(card, "")
        self.assertFalse(meta["enabled"])
        block, cross_meta = service.cross_window_for("u1", window_tag="aiocqhttp:FriendMessage:456")
        self.assertEqual(block, "")
        self.assertFalse(cross_meta["enabled"])


class WindowFlowTest(unittest.TestCase):
    """C 层窗口全流上下文：其他窗口的完整消息流注入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "flow.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _msg(self, ts, window, text, speaker="u1", role="user", name="阿U", persona=""):
        self.store.add_timeline(
            {
                "ts": ts,
                "speaker_id": speaker,
                "speaker_name": name,
                "bot_id": "b",
                "window_tag": window,
                "role": role,
                "content": text,
                "persona_id": persona,
                "fingerprint": f"{window}-{ts}-{speaker}-{text[:8]}",
            }
        )

    def _bot(self, ts, window, text, persona=""):
        self._msg(ts, window, text, speaker=ROLE_BOT_ID, role=ROLE_ASSISTANT, name="bot", persona=persona)

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        )
        service.apply_config()
        return service

    def test_flow_carries_group_members_and_bot(self):
        from savagetype.windowflow import build_window_flow

        now = now_ts()
        self._msg(now - 300, "aiocqhttp:GroupMessage:123", "今晚八点开黑", speaker="u2", name="阿强")
        self._bot(now - 280, "aiocqhttp:GroupMessage:123", "好，我定个闹钟")
        self._msg(now - 100, "aiocqhttp:FriendMessage:456", "在吗", speaker="u1")
        block, meta = build_window_flow(self.store, "aiocqhttp:FriendMessage:456")
        self.assertIn("▸ 群 123", block)
        self.assertIn("阿强", block)
        self.assertIn("我(Bot)", block)
        self.assertNotIn("在吗", block)
        self.assertEqual(meta["windows"], 1)
        self.assertGreaterEqual(meta["items"], 2)

    def test_flow_direction_and_exclude(self):
        from savagetype.windowflow import build_window_flow

        now = now_ts()
        self._msg(now - 100, "aiocqhttp:FriendMessage:456", "私聊内容", speaker="u1")
        block, _meta = build_window_flow(self.store, "aiocqhttp:GroupMessage:123", private_to_group=False)
        self.assertEqual(block, "")
        block2, _meta2 = build_window_flow(self.store, "aiocqhttp:GroupMessage:123", private_to_group=True)
        self.assertIn("私聊内容", block2)
        block3, _meta3 = build_window_flow(
            self.store,
            "aiocqhttp:GroupMessage:123",
            private_to_group=True,
            exclude_private_users=["u1"],
        )
        self.assertEqual(block3, "")

    def test_flow_budget_keeps_newest(self):
        from savagetype.windowflow import build_window_flow

        now = now_ts()
        for index in range(30):
            self._msg(now - 900 + index, "aiocqhttp:GroupMessage:1", f"第{index}条消息，内容写长一点", speaker="u2")
        block, _meta = build_window_flow(self.store, "aiocqhttp:FriendMessage:9", max_items=6)
        self.assertIn("第29条", block)
        self.assertNotIn("第0条", block)

    def test_flow_persona_isolation(self):
        from savagetype.windowflow import build_window_flow

        now = now_ts()
        self._msg(now - 100, "aiocqhttp:GroupMessage:1", "别的性格说的话", speaker="u2", persona="p-other")
        block, meta = build_window_flow(self.store, "aiocqhttp:FriendMessage:9", persona_id="p1")
        self.assertEqual(block, "")
        self.assertEqual(meta["items"], 0)

    def test_flow_for_keyword_gate(self):
        service = self._service(window_flow_enabled=True, window_flow_keywords="群里,群友")
        now = now_ts()
        self._msg(now - 60, "aiocqhttp:GroupMessage:1", "群里在聊新插件", speaker="u2", name="阿强")
        block, meta = service.window_flow_for("你好", window_tag="aiocqhttp:FriendMessage:9")
        self.assertEqual(block, "")
        self.assertEqual(meta.get("skipped"), "no_keyword")
        block2, meta2 = service.window_flow_for("群里什么情况", window_tag="aiocqhttp:FriendMessage:9")
        self.assertIn("新插件", block2)
        self.assertTrue(meta2["enabled"])
        self.assertGreaterEqual(meta2["items"], 1)

    def test_flow_for_always_and_disabled(self):
        now = now_ts()
        self._msg(now - 60, "aiocqhttp:GroupMessage:1", "随手一句", speaker="u2")
        always = self._service(window_flow_enabled=True, window_flow_always=True)
        block, _meta = always.window_flow_for("你好", window_tag="aiocqhttp:FriendMessage:9")
        self.assertIn("随手一句", block)
        off = self._service(window_flow_enabled=False, window_flow_always=True)
        block2, meta2 = off.window_flow_for("你好", window_tag="aiocqhttp:FriendMessage:9")
        self.assertEqual(block2, "")
        self.assertFalse(meta2["enabled"])

    def test_flow_chars_budget(self):
        from savagetype.windowflow import build_window_flow

        now = now_ts()
        for index in range(40):
            self._msg(now - 400 + index, "aiocqhttp:GroupMessage:1", f"消息{index}：" + "内容" * 30, speaker="u2")
        block, _meta = build_window_flow(self.store, "aiocqhttp:FriendMessage:9", max_chars=600)
        self.assertLessEqual(len(block), 700)
        self.assertIn("消息39", block)


class ReplyGateTest(unittest.TestCase):
    """免@主动接话（reply gate）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "gate.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        )
        service.apply_config()
        return service

    def _event(self, text: str = "在吗各位", window: str = "aiocqhttp:GroupMessage:100", sender: str = "u2") -> FakeEvent:
        return FakeEvent(sender=sender, window=window, message_text=text)

    def test_evaluate_gates(self):
        from savagetype.replygate import evaluate

        base = dict(
            enabled=True,
            is_group=True,
            already_handled=False,
            is_self=False,
            window_tag="aiocqhttp:GroupMessage:100",
            targets=set(),
            text="在吗各位",
            min_chars=2,
            skip_commands=True,
            cooldown_ok=True,
            daily_ok=True,
            mode_hit=True,
            mode_reason="probability",
        )
        fire, _reason = evaluate(**base)
        self.assertTrue(fire)
        for field, value, expected in (
            ("enabled", False, "disabled"),
            ("is_group", False, "not_group"),
            ("already_handled", True, "already_handled"),
            ("is_self", True, "bot_self"),
            ("mode_hit", False, "probability_miss"),
            ("cooldown_ok", False, "cooldown"),
            ("daily_ok", False, "daily_limit"),
        ):
            payload = dict(base)
            payload[field] = value
            if field == "mode_hit":
                payload["mode_reason"] = "probability_miss"
            fire, reason = evaluate(**payload)
            self.assertFalse(fire)
            self.assertEqual(reason, expected)
        payload = dict(base)
        payload["text"] = "哈"
        self.assertEqual(evaluate(**payload)[1], "too_short")
        payload = dict(base)
        payload["text"] = "/stype status"
        self.assertEqual(evaluate(**payload)[1], "command")
        payload = dict(base)
        payload["targets"] = {"999"}
        self.assertEqual(evaluate(**payload)[1], "group_not_allowed")

    def test_probability_mode_and_cooldown(self):
        service = self._service(
            reply_gate_enabled=True,
            reply_gate_mode="probability",
            reply_gate_probability=1.0,
            reply_gate_cooldown_seconds=3600,
        )
        fire, meta = asyncio.run(service.reply_gate_for(self._event()))
        self.assertTrue(fire)
        self.assertEqual(meta["mode"], "probability")
        fire2, meta2 = asyncio.run(service.reply_gate_for(self._event("再来一句")))
        self.assertFalse(fire2)
        self.assertEqual(meta2["reason"], "cooldown")

    def test_daily_limit_and_group_whitelist(self):
        service = self._service(
            reply_gate_enabled=True,
            reply_gate_mode="probability",
            reply_gate_probability=1.0,
            reply_gate_cooldown_seconds=0,
            reply_gate_daily_limit=1,
        )
        first, _meta = asyncio.run(service.reply_gate_for(self._event("第一句")))
        self.assertTrue(first)
        fire, meta = asyncio.run(service.reply_gate_for(self._event("第二句")))
        self.assertFalse(fire)
        self.assertEqual(meta["reason"], "daily_limit")

        limited = self._service(
            reply_gate_enabled=True,
            reply_gate_mode="probability",
            reply_gate_probability=1.0,
            reply_gate_groups="12345",
        )
        fire2, meta2 = asyncio.run(limited.reply_gate_for(self._event()))
        self.assertFalse(fire2)
        self.assertEqual(meta2["reason"], "group_not_allowed")

    def test_keyword_mode(self):
        service = self._service(
            reply_gate_enabled=True,
            reply_gate_mode="keyword",
            reply_gate_keywords="在吗,问个事",
        )
        fire, meta = asyncio.run(service.reply_gate_for(self._event("在吗，问个事")))
        self.assertTrue(fire)
        self.assertIn("keyword", meta["reason"])
        fire2, _meta2 = asyncio.run(service.reply_gate_for(self._event("今天天气不错")))
        self.assertFalse(fire2)

    def test_memory_mode(self):
        self.store.add_fact(
            {
                "subject": "self",
                "attribute": "likes",
                "value": "美式咖啡",
                "content": "我喜欢美式咖啡",
                "speaker_id": "u2",
                "speaker_name": "阿U",
                "status": "live",
                "confidence": 0.9,
                "first_person": 1,
            }
        )
        service = self._service(reply_gate_enabled=True, reply_gate_mode="memory")
        fire, meta = asyncio.run(service.reply_gate_for(self._event("美式咖啡还有吗")))
        self.assertTrue(fire)
        self.assertIn("memory", meta["reason"])
        fire2, meta2 = asyncio.run(service.reply_gate_for(self._event("？？？")))
        self.assertFalse(fire2)

    def test_disabled_by_default(self):
        service = self._service()
        fire, meta = asyncio.run(service.reply_gate_for(self._event()))
        self.assertFalse(fire)
        self.assertFalse(meta["enabled"])


class SpeakTest(unittest.TestCase):
    """指派发言：私聊让 Bot 去群里说话。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "speak.db")
        self.sends: list[tuple[str, str]] = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, **config):
        from savagetype.service import SavageTypeService

        async def fake_send(umo: str, text: str) -> None:
            self.sends.append((umo, text))

        service = SavageTypeService(
            store=self.store,
            config=config,
            llm_generate=lambda *_a, **_k: "",
            get_provider=lambda *_a, **_k: None,
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
            send_message=fake_send,
        )
        service.apply_config()
        return service

    def _msg(self, window: str, text: str = "群里说句话", speaker: str = "u2", ts: int | None = None):
        self.store.add_timeline(
            {
                "ts": ts if ts is not None else now_ts(),
                "speaker_id": speaker,
                "speaker_name": "阿U",
                "bot_id": "b",
                "window_tag": window,
                "role": "user",
                "content": text,
                "persona_id": "",
                "fingerprint": f"{window}-{text}-{speaker}",
            }
        )

    def _owner_event(self, text: str = "去群里说：晚上八点开黑") -> FakeEvent:
        return FakeEvent(sender="owner1", window="aiocqhttp:FriendMessage:owner1", message_text=text)

    # -- 纯函数 -----------------------------------------------------------

    def test_parse_intent(self):
        from savagetype.speak import parse_intent

        cases = {
            "去群里说：晚上八点开黑": ("default", "", "晚上八点开黑"),
            "在群里说 明天休息": ("default", "", "明天休息"),
            "跟群友说，我下课了": ("default", "", "我下课了"),
            "去 2 群说：我到了": ("index", "2", "我到了"),
            "去第3个群说 帮忙看下": ("index", "3", "帮忙看下"),
            "去 987654321 群说：到家了": ("number", "987654321", "到家了"),
            "帮我跟群友说 晚安": ("default", "", "晚安"),
        }
        for text, (kind, value, content) in cases.items():
            intent = parse_intent(text)
            self.assertIsNotNone(intent, text)
            self.assertEqual(intent["target"], kind, text)
            self.assertEqual(intent["value"], value, text)
            self.assertEqual(intent["content"], content, text)
        for text in ("今天天气不错", "去群里说", "群友说"):
            self.assertIsNone(parse_intent(text), text)

    def test_resolve_and_allow(self):
        from savagetype.speak import allowed_target, resolve_index, resolve_number, resolve_target

        groups = ["aiocqhttp:GroupMessage:111", "aiocqhttp:GroupMessage:222"]
        self.assertEqual(resolve_index("1", groups), groups[0])
        self.assertEqual(resolve_index("5", groups), "")
        self.assertEqual(resolve_number("222", groups), groups[1])
        self.assertEqual(
            resolve_target({"target": "default", "value": ""}, default_umo=groups[0], groups=groups),
            (groups[0], ""),
        )
        self.assertEqual(
            resolve_target({"target": "default", "value": ""}, default_umo="", groups=[groups[0]]),
            (groups[0], ""),
        )
        self.assertEqual(resolve_target({"target": "default", "value": ""}, default_umo="", groups=[])[1], "no_default_group")
        self.assertEqual(resolve_target({"target": "number", "value": "999"}, default_umo="", groups=groups)[1], "group_not_found")
        self.assertTrue(allowed_target(groups[1], default_umo=groups[0], allow=set()))
        self.assertFalse(allowed_target(groups[1], default_umo=groups[0], allow={groups[0]}))
        self.assertTrue(allowed_target(groups[1], default_umo=groups[0], allow={groups[1]}))

    # -- 服务层 -----------------------------------------------------------

    def test_handle_speak_sends_and_records(self):
        self._msg("aiocqhttp:GroupMessage:111")
        service = self._service(owner_qq="owner1", speak_enabled=True)
        event = self._owner_event()
        receipt = asyncio.run(service.handle_speak_request(event, "去群里说：晚上八点开黑"))
        self.assertIn("已发到群 111", receipt or "")
        self.assertEqual(self.sends, [("aiocqhttp:GroupMessage:111", "晚上八点开黑")])
        rows = self.store.timeline_recent(limit=5, speaker_id=ROLE_BOT_ID)
        self.assertTrue(any("晚上八点开黑" in row.content for row in rows))

    def test_handle_speak_requires_owner_and_private(self):
        self._msg("aiocqhttp:GroupMessage:111")
        service = self._service(owner_qq="owner1", speak_enabled=True)
        other = FakeEvent(sender="u9", window="aiocqhttp:FriendMessage:u9", message_text="去群里说：测试")
        receipt = asyncio.run(service.handle_speak_request(other, "去群里说：测试"))
        self.assertIn("只有主人", receipt or "")
        self.assertEqual(self.sends, [])
        group_event = FakeEvent(sender="owner1", window="aiocqhttp:GroupMessage:111", message_text="去群里说：测试")
        self.assertIsNone(asyncio.run(service.handle_speak_request(group_event, "去群里说：测试")))

    def test_handle_speak_rate_limit_and_whitelist(self):
        self._msg("aiocqhttp:GroupMessage:111")
        self._msg("aiocqhttp:GroupMessage:222", ts=now_ts() + 10)
        service = self._service(
            owner_qq="owner1",
            speak_enabled=True,
            speak_rate_limit_per_min=1,
            speak_default_group="111",
        )
        event = self._owner_event()
        first = asyncio.run(service.handle_speak_request(event, "去群里说：第一条"))
        self.assertIn("已发到群", first or "")
        second = asyncio.run(service.handle_speak_request(event, "去群里说：第二条"))
        self.assertIn("太快了", second or "")
        self.assertEqual(len(self.sends), 1)

        limited = self._service(
            owner_qq="owner1",
            speak_enabled=True,
            speak_default_group="111",
            speak_groups="999999999",
        )
        blocked = asyncio.run(limited.handle_speak_request(event, "去 1 群说：越界"))
        self.assertIn("不在允许名单", blocked or "")
        self.assertEqual(len(self.sends), 1)

    def test_set_speak_default(self):
        self._msg("aiocqhttp:GroupMessage:111")
        service = self._service(owner_qq="owner1", speak_enabled=True)
        message = service.set_speak_default("1")
        self.assertIn("默认群已设为 111", message)
        self.assertEqual(service.speak_default_umo(), "aiocqhttp:GroupMessage:111")
        self.assertIn("已清除", service.set_speak_default("clear"))
        self.assertEqual(service.speak_default_umo(), "")

    def test_speak_disabled_by_default(self):
        self._msg("aiocqhttp:GroupMessage:111")
        service = self._service(owner_qq="owner1")
        self.assertIsNone(asyncio.run(service.handle_speak_request(self._owner_event(), "去群里说：测试")))
        self.assertEqual(self.sends, [])


if __name__ == "__main__":
    unittest.main()
