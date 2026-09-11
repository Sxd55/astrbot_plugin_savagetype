"""SQLite persistence. Data lives under AstrBot plugin_data, never the plugin dir."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import Fact, PendingOverride, ReviewItem, TimelineEvent
from .slots import apply_slot
from .util import dumps, loads, make_slot_key, now_ts

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    speaker_id TEXT NOT NULL,
    speaker_name TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT '',
    window_tag TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    summarized INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    persona_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_timeline_unsum ON timeline(summarized, id);
CREATE INDEX IF NOT EXISTS idx_timeline_speaker ON timeline(speaker_id, ts);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    content TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    speaker_name TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT '',
    window_tag TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence TEXT NOT NULL DEFAULT '[]',
    mention_policy TEXT NOT NULL DEFAULT 'mention',
    first_person INTEGER NOT NULL DEFAULT 0,
    explicit_correction INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'extract',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    superseded_by INTEGER,
    supersedes INTEGER,
    fingerprint TEXT NOT NULL,
    embedding TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    slot_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_facts_slot ON facts(speaker_id, subject, attribute, status);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_facts_fp ON facts(fingerprint);

CREATE TABLE IF NOT EXISTS pending_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_fact_id INTEGER NOT NULL,
    new_payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        if self.get_meta("revision") is None:
            self.set_meta("revision", "1")

    def _table_cols(self, table: str) -> set[str]:
        rows = self.query(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}

    def _migrate(self) -> None:
        fact_cols = self._table_cols("facts")
        if "persona_id" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''")
        if "slot_key" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN slot_key TEXT NOT NULL DEFAULT ''")
        tl_cols = self._table_cols("timeline")
        if "persona_id" not in tl_cols:
            self.execute("ALTER TABLE timeline ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''")
        self.execute(
            "CREATE TABLE IF NOT EXISTS speaker_aliases ("
            "alias TEXT NOT NULL, "
            "canonical_id TEXT NOT NULL, "
            "label TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY(alias))"
        )
        self.execute(
            "CREATE TABLE IF NOT EXISTS usage_ledger ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, "
            "kind TEXT NOT NULL, "
            "provider_id TEXT NOT NULL DEFAULT '', "
            "ok INTEGER NOT NULL DEFAULT 1, "
            "chars_in INTEGER NOT NULL DEFAULT 0, "
            "chars_out INTEGER NOT NULL DEFAULT 0, "
            "tokens_in INTEGER NOT NULL DEFAULT 0, "
            "tokens_out INTEGER NOT NULL DEFAULT 0, "
            "detail TEXT NOT NULL DEFAULT '')"
        )
        usage_cols = self._table_cols("usage_ledger")
        if "tokens_in" not in usage_cols:
            self.execute("ALTER TABLE usage_ledger ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0")
        if "tokens_out" not in usage_cols:
            self.execute("ALTER TABLE usage_ledger ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0")
        self.execute("CREATE INDEX IF NOT EXISTS idx_facts_slotkey ON facts(slot_key, status)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_facts_persona ON facts(persona_id, speaker_id, status)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_timeline_persona ON timeline(persona_id, speaker_id, ts)")
        self.execute(
            "CREATE TABLE IF NOT EXISTS reviews ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "fingerprint TEXT NOT NULL, "
            "speaker_id TEXT NOT NULL DEFAULT '', "
            "persona_id TEXT NOT NULL DEFAULT '', "
            "title TEXT NOT NULL DEFAULT '', "
            "payload TEXT NOT NULL, "
            "reason TEXT NOT NULL DEFAULT '', "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_fp ON reviews(kind, fingerprint)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status, kind, updated_at)")
        self.execute(
            "CREATE TABLE IF NOT EXISTS jargon_stats ("
            "term TEXT NOT NULL, "
            "persona_id TEXT NOT NULL DEFAULT '', "
            "count INTEGER NOT NULL DEFAULT 0, "
            "last_seen INTEGER NOT NULL, "
            "PRIMARY KEY(term, persona_id))"
        )
        rows = self.query("SELECT id, subject, attribute, speaker_id, speaker_name, persona_id FROM facts WHERE slot_key='' OR slot_key IS NULL")
        for row in rows:
            payload = apply_slot(
                {
                    "subject": row["subject"],
                    "attribute": row["attribute"],
                    "speaker_id": row["speaker_id"],
                    "speaker_name": row["speaker_name"] if "speaker_name" in row.keys() else "",
                }
            )
            persona = row["persona_id"] if "persona_id" in row.keys() else ""
            key = make_slot_key(persona or "", row["speaker_id"], payload["subject"], payload["attribute"])
            self.execute(
                "UPDATE facts SET subject=?, attribute=?, slot_key=?, persona_id=? WHERE id=?",
                (payload["subject"], payload["attribute"], key, persona or "", row["id"]),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def bump_revision(self) -> int:
        current = int(self.get_meta("revision") or "1")
        nxt = current + 1
        self.set_meta("revision", str(nxt))
        return nxt

    def revision(self) -> int:
        return int(self.get_meta("revision") or "1")

    def add_timeline(self, event: dict[str, Any]) -> int | None:
        fp = event.get("fingerprint") or ""
        exists = self.query(
            "SELECT id FROM timeline WHERE fingerprint=? AND speaker_id=? AND ts=?",
            (fp, event["speaker_id"], event["ts"]),
        )
        if exists:
            return None
        cur = self.execute(
            """INSERT INTO timeline(ts, speaker_id, speaker_name, bot_id, window_tag, role, content, summarized, fingerprint, persona_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                event["ts"],
                event["speaker_id"],
                event.get("speaker_name", ""),
                event.get("bot_id", ""),
                event.get("window_tag", ""),
                event["role"],
                event["content"],
                0,
                fp,
                event.get("persona_id", ""),
            ),
        )
        return int(cur.lastrowid)

    def unsummarized(self, limit: int = 40) -> list[TimelineEvent]:
        rows = self.query(
            "SELECT * FROM timeline WHERE summarized=0 ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        return [self._timeline(r) for r in rows]

    def mark_summarized(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join("?" * len(ids))
        self.execute(f"UPDATE timeline SET summarized=1 WHERE id IN ({q})", ids)

    def timeline_recent(self, limit: int = 20, speaker_id: str | None = None) -> list[TimelineEvent]:
        if speaker_id:
            rows = self.query(
                "SELECT * FROM timeline WHERE speaker_id=? ORDER BY id DESC LIMIT ?",
                (speaker_id, limit),
            )
        else:
            rows = self.query("SELECT * FROM timeline ORDER BY id DESC LIMIT ?", (limit,))
        return [self._timeline(r) for r in rows]

    def counts(self) -> dict[str, int]:
        def n(sql: str, params: Iterable[Any] = ()) -> int:
            try:
                return int(self.query(sql, params)[0][0])
            except Exception:
                return 0

        return {
            "timeline": n("SELECT COUNT(*) FROM timeline"),
            "unsummarized": n("SELECT COUNT(*) FROM timeline WHERE summarized=0"),
            "facts_live": n("SELECT COUNT(*) FROM facts WHERE status='live'"),
            "facts_superseded": n("SELECT COUNT(*) FROM facts WHERE status='superseded'"),
            "facts_archived": n("SELECT COUNT(*) FROM facts WHERE status='archived'"),
            "pending": n("SELECT COUNT(*) FROM pending_overrides WHERE status='open'"),
            "reviews_pending": n("SELECT COUNT(*) FROM reviews WHERE status='pending'"),
            "jargon_approved": n("SELECT COUNT(*) FROM reviews WHERE kind='jargon' AND status='approved'"),
            "fewshot_approved": n("SELECT COUNT(*) FROM reviews WHERE kind='fewshot' AND status='approved'"),
            "persona_drafts": n("SELECT COUNT(*) FROM reviews WHERE kind='persona' AND status='approved'"),
        }

    def add_fact(self, payload: dict[str, Any], bump: bool = True) -> int:
        now = now_ts()
        payload = apply_slot(payload)
        persona_id = str(payload.get("persona_id") or "")
        slot_key = payload.get("slot_key") or make_slot_key(
            persona_id,
            str(payload.get("speaker_id") or ""),
            str(payload.get("subject") or ""),
            str(payload.get("attribute") or ""),
        )
        cur = self.execute(
            """INSERT INTO facts(
                subject, attribute, value, content, speaker_id, speaker_name, bot_id, window_tag,
                status, confidence, evidence, mention_policy, first_person, explicit_correction,
                source, created_at, updated_at, superseded_by, supersedes, fingerprint, embedding,
                access_count, last_accessed, reason, persona_id, slot_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["subject"],
                payload["attribute"],
                payload["value"],
                payload["content"],
                payload["speaker_id"],
                payload.get("speaker_name", ""),
                payload.get("bot_id", ""),
                payload.get("window_tag", ""),
                payload.get("status", "live"),
                float(payload.get("confidence", 0.6)),
                dumps(payload.get("evidence", [])),
                payload.get("mention_policy", "mention"),
                int(payload.get("first_person", 0)),
                int(payload.get("explicit_correction", 0)),
                payload.get("source", "extract"),
                payload.get("created_at", now),
                payload.get("updated_at", now),
                payload.get("superseded_by"),
                payload.get("supersedes"),
                payload.get("fingerprint", ""),
                dumps(payload["embedding"]) if payload.get("embedding") else None,
                int(payload.get("access_count", 0)),
                int(payload.get("last_accessed", 0)),
                payload.get("reason", ""),
                persona_id,
                slot_key,
            ),
        )
        if bump:
            self.bump_revision()
        return int(cur.lastrowid)

    def update_fact(self, fact_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "updated_at" not in fields:
            fields["updated_at"] = now_ts()
        if "evidence" in fields and not isinstance(fields["evidence"], str):
            fields["evidence"] = dumps(fields["evidence"])
        if "embedding" in fields and not isinstance(fields["embedding"], (str, type(None))):
            fields["embedding"] = dumps(fields["embedding"])
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE facts SET {assignments} WHERE id=?", (*fields.values(), fact_id))
        self.bump_revision()

    def get_fact(self, fact_id: int) -> Fact | None:
        rows = self.query("SELECT * FROM facts WHERE id=?", (fact_id,))
        return self._fact(rows[0]) if rows else None

    def live_facts(
        self,
        speaker_id: str | None = None,
        limit: int = 200,
        persona_id: str | None = None,
        speaker_ids: list[str] | None = None,
    ) -> list[Fact]:
        ids = list(speaker_ids or [])
        if speaker_id and speaker_id not in ids:
            ids.append(speaker_id)
        clauses = ["status='live'"]
        params: list[Any] = []
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        if ids:
            placeholders = ",".join("?" * (len(ids) + 2))
            clauses.append(f"(speaker_id IN ({placeholders}))")
            params.extend([*ids, "", "bot_self"])
        params.append(limit)
        sql = f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        return [self._fact(r) for r in self.query(sql, params)]

    def all_live(self, limit: int = 400, persona_id: str | None = None) -> list[Fact]:
        if not persona_id:
            rows = self.query(
                "SELECT * FROM facts WHERE status='live' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self.query(
                "SELECT * FROM facts WHERE status='live' AND (persona_id=? OR persona_id='') ORDER BY updated_at DESC LIMIT ?",
                (persona_id, limit),
            )
        return [self._fact(r) for r in rows]

    def facts_by_status(self, status: str, limit: int = 50) -> list[Fact]:
        rows = self.query(
            "SELECT * FROM facts WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        )
        return [self._fact(r) for r in rows]

    def archive_facts(self, ids: list[int], reason: str = "ui_delete") -> dict[str, Any]:
        archived: list[int] = []
        missing: list[int] = []
        for raw in ids:
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                continue
            fact = self.get_fact(fid)
            if not fact:
                missing.append(fid)
                continue
            if fact.status in {"archived", "superseded"}:
                archived.append(fid)
                continue
            self.update_fact(fid, status="archived", reason=reason)
            archived.append(fid)
        return {"ok": True, "archived": archived, "missing": missing, "count": len(archived)}

    def search_facts(
        self,
        keyword: str,
        speaker_id: str | None = None,
        limit: int = 20,
        persona_id: str | None = None,
        speaker_ids: list[str] | None = None,
    ) -> list[Fact]:
        like = f"%{keyword}%"
        ids = list(speaker_ids or [])
        if speaker_id and speaker_id not in ids:
            ids.append(speaker_id)
        clauses = [
            "status='live'",
            "(content LIKE ? OR subject LIKE ? OR attribute LIKE ? OR value LIKE ?)",
        ]
        params: list[Any] = [like, like, like, like]
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        if ids:
            placeholders = ",".join("?" * (len(ids) + 2))
            clauses.append(f"(speaker_id IN ({placeholders}))")
            params.extend([*ids, "", "bot_self"])
        params.append(limit)
        sql = f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY confidence DESC LIMIT ?"
        return [self._fact(r) for r in self.query(sql, params)]

    def live_by_slot(
        self,
        speaker_id: str,
        subject: str,
        attribute: str,
        persona_id: str = "",
        speaker_ids: list[str] | None = None,
    ) -> Fact | None:
        payload = apply_slot(
            {
                "subject": subject,
                "attribute": attribute,
                "speaker_id": speaker_id,
            }
        )
        key = make_slot_key(persona_id, speaker_id, payload["subject"], payload["attribute"])
        rows = self.query(
            """SELECT * FROM facts WHERE status='live' AND slot_key=?
               ORDER BY confidence DESC, updated_at DESC LIMIT 1""",
            (key,),
        )
        if rows:
            return self._fact(rows[0])
        ids = list(speaker_ids or [speaker_id])
        placeholders = ",".join("?" * len(ids))
        rows = self.query(
            f"""SELECT * FROM facts WHERE status='live' AND speaker_id IN ({placeholders})
                AND subject=? AND attribute=? AND (persona_id=? OR persona_id='')
                ORDER BY confidence DESC, updated_at DESC LIMIT 1""",
            (*ids, payload["subject"], payload["attribute"], persona_id),
        )
        return self._fact(rows[0]) if rows else None

    def fact_by_fingerprint(self, fingerprint: str) -> Fact | None:
        if not fingerprint:
            return None
        rows = self.query("SELECT * FROM facts WHERE fingerprint=? LIMIT 1", (fingerprint,))
        return self._fact(rows[0]) if rows else None

    def missing_embeddings(self, limit: int = 32) -> list[Fact]:
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' AND (embedding IS NULL OR embedding='') ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [self._fact(r) for r in rows]

    def add_pending(self, old_fact_id: int, new_payload: dict[str, Any], reason: str) -> int:
        cur = self.execute(
            "INSERT INTO pending_overrides(old_fact_id, new_payload, reason, created_at, status) VALUES(?,?,?,?,?)",
            (old_fact_id, dumps(new_payload), reason, now_ts(), "open"),
        )
        self.bump_revision()
        return int(cur.lastrowid)

    def pending_open(self, limit: int = 50) -> list[PendingOverride]:
        rows = self.query(
            "SELECT * FROM pending_overrides WHERE status='open' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            PendingOverride(
                id=r["id"],
                old_fact_id=r["old_fact_id"],
                new_payload=loads(r["new_payload"], {}),
                reason=r["reason"],
                created_at=r["created_at"],
                status=r["status"],
            )
            for r in rows
        ]

    def set_pending_status(self, pending_id: int, status: str) -> None:
        self.execute(
            "UPDATE pending_overrides SET status=? WHERE id=?",
            (status, pending_id),
        )
        self.bump_revision()

    def add_diag(self, kind: str, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO diagnostics(ts, kind, payload) VALUES(?,?,?)",
            (now_ts(), kind, dumps(payload)),
        )
        self.execute(
            "DELETE FROM diagnostics WHERE id NOT IN (SELECT id FROM diagnostics ORDER BY id DESC LIMIT 200)"
        )

    def recent_diag(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM diagnostics ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": loads(r["payload"], {})}
            for r in rows
        ]

    def export_rows(self) -> dict[str, list[dict[str, Any]]]:
        facts = [dict(r) for r in self.query("SELECT * FROM facts")]
        timeline = [dict(r) for r in self.query("SELECT * FROM timeline")]
        pending = [dict(r) for r in self.query("SELECT * FROM pending_overrides")]
        reviews = []
        for r in self.query("SELECT * FROM reviews"):
            item = dict(r)
            item["review_kind"] = item.get("kind")
            reviews.append(item)
        return {"facts": facts, "timeline": timeline, "pending": pending, "reviews": reviews}

    def upsert_review(self, kind: str, fingerprint: str, title: str, payload: dict[str, Any], reason: str = "", speaker_id: str = "", persona_id: str = "") -> int:
        existing = self.query(
            "SELECT id, status FROM reviews WHERE kind=? AND fingerprint=?",
            (kind, fingerprint),
        )
        now = now_ts()
        if existing:
            row = existing[0]
            if row["status"] in {"approved", "rejected"}:
                return int(row["id"])
            self.execute(
                "UPDATE reviews SET title=?, payload=?, reason=?, speaker_id=?, persona_id=?, updated_at=? WHERE id=?",
                (title, dumps(payload), reason, speaker_id, persona_id, now, row["id"]),
            )
            return int(row["id"])
        cur = self.execute(
            """INSERT INTO reviews(kind, status, fingerprint, speaker_id, persona_id, title, payload, reason, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (kind, "pending", fingerprint, speaker_id, persona_id, title, dumps(payload), reason, now, now),
        )
        self.bump_revision()
        return int(cur.lastrowid)

    def list_reviews(self, status: str = "pending", kind: str | None = None, limit: int = 80) -> list[ReviewItem]:
        if kind:
            rows = self.query(
                "SELECT * FROM reviews WHERE status=? AND kind=? ORDER BY id DESC LIMIT ?",
                (status, kind, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM reviews WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
        items = [self._review(r) for r in rows]
        items.sort(key=lambda r: int((r.payload or {}).get("quality") or 0), reverse=True)
        return items

    def get_review(self, review_id: int) -> ReviewItem | None:
        rows = self.query("SELECT * FROM reviews WHERE id=?", (review_id,))
        return self._review(rows[0]) if rows else None

    def set_review_status(self, review_id: int, status: str) -> None:
        self.execute(
            "UPDATE reviews SET status=?, updated_at=? WHERE id=?",
            (status, now_ts(), review_id),
        )
        self.bump_revision()

    def approved_reviews(self, kind: str, persona_id: str = "", limit: int = 12) -> list[ReviewItem]:
        if persona_id:
            rows = self.query(
                "SELECT * FROM reviews WHERE kind=? AND status='approved' AND (persona_id=? OR persona_id='') ORDER BY updated_at DESC LIMIT ?",
                (kind, persona_id, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM reviews WHERE kind=? AND status='approved' ORDER BY updated_at DESC LIMIT ?",
                (kind, limit),
            )
        return [self._review(r) for r in rows]

    def bump_jargon(self, term: str, persona_id: str = "") -> int:
        now = now_ts()
        self.execute(
            """INSERT INTO jargon_stats(term, persona_id, count, last_seen) VALUES(?,?,1,?)
               ON CONFLICT(term, persona_id) DO UPDATE SET count=count+1, last_seen=excluded.last_seen""",
            (term, persona_id, now),
        )
        rows = self.query(
            "SELECT count FROM jargon_stats WHERE term=? AND persona_id=?",
            (term, persona_id),
        )
        return int(rows[0]["count"]) if rows else 1

    def hot_jargon(self, min_count: int = 3, persona_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if persona_id:
            rows = self.query(
                "SELECT term, persona_id, count, last_seen FROM jargon_stats WHERE count>=? AND (persona_id=? OR persona_id='') ORDER BY count DESC LIMIT ?",
                (min_count, persona_id, limit),
            )
        else:
            rows = self.query(
                "SELECT term, persona_id, count, last_seen FROM jargon_stats WHERE count>=? ORDER BY count DESC LIMIT ?",
                (min_count, limit),
            )
        return [{"term": r["term"], "persona_id": r["persona_id"], "count": r["count"], "last_seen": r["last_seen"]} for r in rows]

    def jargon_corpus_size(self) -> int:
        rows = self.query("SELECT COALESCE(SUM(count), 0) FROM jargon_stats")
        return int(rows[0][0] or 0)

    def jargon_persona_spread(self, term: str) -> int:
        rows = self.query("SELECT COUNT(*) FROM jargon_stats WHERE term=?", (term,))
        return int(rows[0][0] or 0)

    def drop_jargon_term(self, term: str) -> None:
        self.execute("DELETE FROM jargon_stats WHERE term=?", (term,))

    def _review(self, row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            fingerprint=row["fingerprint"],
            speaker_id=row["speaker_id"],
            persona_id=row["persona_id"],
            title=row["title"],
            payload=loads(row["payload"], {}),
            reason=row["reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def resolve_speaker(self, speaker_id: str) -> str:
        if not speaker_id:
            return speaker_id
        rows = self.query("SELECT canonical_id FROM speaker_aliases WHERE alias=?", (speaker_id,))
        return rows[0]["canonical_id"] if rows else speaker_id

    def speaker_ids_for(self, canonical_id: str) -> list[str]:
        ids = [canonical_id]
        rows = self.query("SELECT alias FROM speaker_aliases WHERE canonical_id=?", (canonical_id,))
        for row in rows:
            alias = row["alias"]
            if alias not in ids:
                ids.append(alias)
        return ids

    def set_alias(self, alias: str, canonical_id: str, label: str = "") -> None:
        self.execute(
            "INSERT INTO speaker_aliases(alias, canonical_id, label) VALUES(?,?,?) "
            "ON CONFLICT(alias) DO UPDATE SET canonical_id=excluded.canonical_id, label=excluded.label",
            (alias, canonical_id, label),
        )
        self.bump_revision()

    def list_aliases(self) -> list[dict[str, str]]:
        rows = self.query("SELECT alias, canonical_id, label FROM speaker_aliases ORDER BY canonical_id, alias")
        return [{"alias": r["alias"], "canonical_id": r["canonical_id"], "label": r["label"]} for r in rows]

    def add_usage(
        self,
        kind: str,
        provider_id: str = "",
        ok: bool = True,
        chars_in: int = 0,
        chars_out: int = 0,
        detail: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.execute(
            "INSERT INTO usage_ledger(ts, kind, provider_id, ok, chars_in, chars_out, tokens_in, tokens_out, detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (now_ts(), kind, provider_id, int(ok), chars_in, chars_out, int(tokens_in or 0), int(tokens_out or 0), detail[:240]),
        )
        self.execute(
            "DELETE FROM usage_ledger WHERE id NOT IN (SELECT id FROM usage_ledger ORDER BY id DESC LIMIT 400)"
        )

    def usage_summary(self) -> dict[str, Any]:
        rows = self.query(
            "SELECT kind, COUNT(*) AS n, SUM(ok) AS ok_n, SUM(chars_in) AS cin, SUM(chars_out) AS cout, "
            "SUM(tokens_in) AS tin, SUM(tokens_out) AS tout FROM usage_ledger GROUP BY kind"
        )
        out = {}
        for r in rows:
            keys = r.keys()
            out[r["kind"]] = {
                "count": int(r["n"] or 0),
                "ok": int(r["ok_n"] or 0),
                "chars_in": int(r["cin"] or 0),
                "chars_out": int(r["cout"] or 0),
                "tokens_in": int(r["tin"] or 0) if "tin" in keys else 0,
                "tokens_out": int(r["tout"] or 0) if "tout" in keys else 0,
            }
        return out

    def speaker_name_map(self) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT speaker_id, speaker_name, COUNT(*) AS n
               FROM timeline WHERE speaker_name != '' AND role='user'
               GROUP BY speaker_id, speaker_name ORDER BY n DESC LIMIT 200"""
        )
        return [{"speaker_id": r["speaker_id"], "speaker_name": r["speaker_name"], "count": int(r["n"] or 0)} for r in rows]

    def recent_superseded(self, speaker_ids: list[str], persona_id: str = "", limit: int = 6) -> list[Fact]:
        ids = speaker_ids or [""]
        placeholders = ",".join("?" * (len(ids) + 2))
        rows = self.query(
            f"""SELECT * FROM facts WHERE status='superseded'
                AND speaker_id IN ({placeholders})
                AND (persona_id=? OR persona_id='')
                ORDER BY updated_at DESC LIMIT ?""",
            (*ids, "", "bot_self", persona_id, limit),
        )
        return [self._fact(r) for r in rows]

    def live_near_duplicates(self) -> list[tuple[Fact, Fact]]:
        rows = self.query("SELECT * FROM facts WHERE status='live' ORDER BY slot_key, confidence DESC")
        facts = [self._fact(r) for r in rows]
        pairs: list[tuple[Fact, Fact]] = []
        by_slot: dict[str, Fact] = {}
        for fact in facts:
            key = fact.slot_key()
            if key in by_slot:
                pairs.append((by_slot[key], fact))
            else:
                by_slot[key] = fact
        return pairs

    def _timeline(self, row: sqlite3.Row) -> TimelineEvent:
        keys = row.keys()
        return TimelineEvent(
            id=row["id"],
            ts=row["ts"],
            speaker_id=row["speaker_id"],
            speaker_name=row["speaker_name"],
            bot_id=row["bot_id"],
            window_tag=row["window_tag"],
            role=row["role"],
            content=row["content"],
            summarized=row["summarized"],
            persona_id=row["persona_id"] if "persona_id" in keys else "",
        )

    def _fact(self, row: sqlite3.Row) -> Fact:
        keys = row.keys()
        return Fact(
            id=row["id"],
            subject=row["subject"],
            attribute=row["attribute"],
            value=row["value"],
            content=row["content"],
            speaker_id=row["speaker_id"],
            speaker_name=row["speaker_name"],
            bot_id=row["bot_id"],
            window_tag=row["window_tag"],
            status=row["status"],
            confidence=float(row["confidence"] or 0),
            evidence=loads(row["evidence"], []),
            mention_policy=row["mention_policy"],
            first_person=int(row["first_person"] or 0),
            explicit_correction=int(row["explicit_correction"] or 0),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            superseded_by=row["superseded_by"],
            supersedes=row["supersedes"],
            fingerprint=row["fingerprint"],
            embedding=loads(row["embedding"], None),
            access_count=int(row["access_count"] or 0),
            last_accessed=int(row["last_accessed"] or 0),
            reason=row["reason"] or "",
            persona_id=row["persona_id"] if "persona_id" in keys else "",
            slot_key_value=row["slot_key"] if "slot_key" in keys else "",
        )
