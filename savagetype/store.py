"""SQLite persistence. Data lives under AstrBot plugin_data, never the plugin dir."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import Event, Fact, MemoryReview, PendingOverride, Profile, ReviewItem, TimelineEvent
from .slots import apply_slot, fact_kind
from .util import (
    MEMORY_STATUS_PENDING,
    ROLE_BOT_ID,
    SCOPE_OWNER,
    clip,
    default_importance,
    detect_domain,
    dumps,
    loads,
    make_slot_key,
    now_ts,
    normalize_slot,
    today_str,
)

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
    slot_key TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS profiles (
    speaker_id TEXT PRIMARY KEY,
    speaker_name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    is_owner INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER NOT NULL DEFAULT 0,
    seen_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_profiles_seen ON profiles(last_seen);

CREATE TABLE IF NOT EXISTS memory_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'person',
    speaker_id TEXT NOT NULL DEFAULT '',
    speaker_name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    window_tag TEXT NOT NULL DEFAULT '',
    source_event_id INTEGER NOT NULL DEFAULT 0,
    raw_text TEXT NOT NULL DEFAULT '',
    plain TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    trace TEXT NOT NULL DEFAULT '[]',
    notified_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_reviews_status ON memory_reviews(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_reviews_speaker ON memory_reviews(speaker_id, status);

CREATE TABLE IF NOT EXISTS recall_log (
    window_tag TEXT NOT NULL,
    fact_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (window_tag, fact_id)
);
CREATE INDEX IF NOT EXISTS idx_recall_log_ts ON recall_log(ts);

CREATE TABLE IF NOT EXISTS event_recall_log (
    window_tag TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (window_tag, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_recall_log_ts ON event_recall_log(ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'life',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    speaker_id TEXT NOT NULL DEFAULT '',
    speaker_name TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT '',
    window_tag TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'person',
    participants TEXT NOT NULL DEFAULT '[]',
    speaker_ids TEXT NOT NULL DEFAULT '[]',
    highlights TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    start_ts INTEGER NOT NULL DEFAULT 0,
    end_ts INTEGER NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.6,
    status TEXT NOT NULL DEFAULT 'live',
    pinned INTEGER NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'pipeline',
    review_status TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    edited_at INTEGER NOT NULL DEFAULT 0,
    edited_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(status, start_ts);
CREATE INDEX IF NOT EXISTS idx_events_window ON events(status, window_tag, start_ts);
CREATE INDEX IF NOT EXISTS idx_events_speaker ON events(speaker_id, status, start_ts);
CREATE INDEX IF NOT EXISTS idx_events_fp ON events(fingerprint);

CREATE TABLE IF NOT EXISTS entities (
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'person',
    ref TEXT NOT NULL DEFAULT 'fact',
    ref_id INTEGER NOT NULL,
    persona_id TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, ref, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_ref ON entities(ref, ref_id);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connect()
        self._migrate()
        if self.get_meta("revision") is None:
            self.set_meta("revision", "1")

    def _connect(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._closed = False

    def _ensure_conn(self) -> None:
        if self._closed:
            self._connect()
            return
        try:
            self._conn.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError, AttributeError):
            self._connect()

    def _table_cols(self, table: str) -> set[str]:
        rows = self.query(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}

    def _migrate(self) -> None:
        fact_cols = self._table_cols("facts")
        if "persona_id" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''")
        if "slot_key" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN slot_key TEXT NOT NULL DEFAULT ''")
        if "expires_at" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
        if "write_op" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN write_op TEXT NOT NULL DEFAULT ''")
        if "scope" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        if "plain" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN plain TEXT NOT NULL DEFAULT ''")
        if "keywords" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
        if "source_event_id" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN source_event_id INTEGER NOT NULL DEFAULT 0")
        if "review_status" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN review_status TEXT NOT NULL DEFAULT ''")
        if "origin" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
        if "edited_at" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN edited_at INTEGER NOT NULL DEFAULT 0")
        if "edited_by" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN edited_by TEXT NOT NULL DEFAULT ''")
        if "importance" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN importance REAL NOT NULL DEFAULT 0")
        if "kind" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        if "pinned" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "topic" not in fact_cols:
            self.execute("ALTER TABLE facts ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
        tl_cols = self._table_cols("timeline")
        if "persona_id" not in tl_cols:
            self.execute("ALTER TABLE timeline ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''")
        if "addressee" not in tl_cols:
            self.execute("ALTER TABLE timeline ADD COLUMN addressee TEXT NOT NULL DEFAULT ''")
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
        self.execute(
            "CREATE TABLE IF NOT EXISTS usage_daily ("
            "day TEXT NOT NULL, "
            "task TEXT NOT NULL DEFAULT '', "
            "provider_id TEXT NOT NULL DEFAULT '', "
            "calls INTEGER NOT NULL DEFAULT 0, "
            "skipped INTEGER NOT NULL DEFAULT 0, "
            "tokens_in INTEGER NOT NULL DEFAULT 0, "
            "tokens_out INTEGER NOT NULL DEFAULT 0, "
            "source TEXT NOT NULL DEFAULT '', "
            "skip_reason TEXT NOT NULL DEFAULT '', "
            "updated_at INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(day, task, provider_id))"
        )
        self.execute("CREATE INDEX IF NOT EXISTS idx_facts_slotkey ON facts(slot_key, status)")
        if self.get_meta("importance_backfill_v343") != "1":
            # 一次性回填老数据的 importance；之后用户手动设为 0 的值不能再被覆盖。
            self.execute("UPDATE facts SET importance=confidence WHERE importance<=0 AND confidence>0")
            self.set_meta("importance_backfill_v343", "1")
        rows = self.query("SELECT id, attribute FROM facts WHERE kind='' OR kind IS NULL")
        for row in rows:
            self.execute("UPDATE facts SET kind=? WHERE id=?", (fact_kind(row["attribute"]), int(row["id"])))
        if self.get_meta("slot_topic_v332") != "1":
            rows = self.query(
                "SELECT id, persona_id, speaker_id, subject, attribute, value FROM facts"
            )
            for row in rows:
                key = make_slot_key(
                    row["persona_id"] or "",
                    row["speaker_id"],
                    row["subject"],
                    row["attribute"],
                    row["value"],
                )
                self.execute("UPDATE facts SET slot_key=? WHERE id=?", (key, int(row["id"])))
            self.resolve_all_slot_conflicts()
            self.set_meta("slot_topic_v332", "1")
        if self.get_meta("slot_domain_v3417") != "1":
            # 回填领域（不改现有 slot_key，零风险）；之后新旧事实都能正确判域。
            rows = self.query("SELECT id, value, content FROM facts")
            for row in rows:
                domain = detect_domain(row["content"] or "", row["value"] or "")
                if domain:
                    self.execute("UPDATE facts SET topic=? WHERE id=?", (domain, int(row["id"])))
            self.set_meta("slot_domain_v3417", "1")
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
        if self.get_meta("events_v420") != "1":
            self.set_meta("events_v420", "1")
        if self.get_meta("slot_topic_v420") != "1":
            # v4.2.0：约定/习惯改为按主题分槽，回填 slot_key 后消解可能出现的重复。
            rows = self.query(
                "SELECT id, persona_id, speaker_id, subject, attribute, value FROM facts "
                "WHERE attribute IN ('promise', 'habit')"
            )
            for row in rows:
                key = make_slot_key(
                    row["persona_id"] or "",
                    row["speaker_id"],
                    row["subject"],
                    row["attribute"],
                    row["value"] or "",
                )
                self.execute("UPDATE facts SET slot_key=? WHERE id=?", (key, int(row["id"])))
            self.resolve_all_slot_conflicts()
            self.set_meta("slot_topic_v420", "1")
        if self.get_meta("entity_links_v430") != "1":
            # v4.3.0：实体链接，老库一次性回填 live 事实与事件（单事务批量写）。
            bulk = self._entity_backfill_rows()
            if bulk:
                with self._lock:
                    self._ensure_conn()
                    self._conn.executemany(
                        "INSERT INTO entities(name, kind, ref, ref_id, persona_id, ts) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(name, ref, ref_id) DO UPDATE SET kind=excluded.kind, ts=excluded.ts",
                        bulk,
                    )
                    self._conn.commit()
            self.bump_revision()
            self.set_meta("entity_links_v430", "1")

    def _entity_backfill_rows(self) -> list[tuple[str, str, str, int, str, int]]:
        """One-shot entity registration for existing live facts/events."""
        bulk: list[tuple[str, str, str, int, str, int]] = []
        seen: set[tuple[str, str, int]] = set()

        def push(raw: str, kind: str, ref: str, ref_id: int, persona: str) -> None:
            name = self._clean_entity(raw)
            key = (name, ref, ref_id)
            if not name or key in seen:
                return
            seen.add(key)
            bulk.append((name, kind, ref, int(ref_id), persona or "", now_ts()))

        for row in self.query(
            "SELECT id, speaker_name, keywords, topic, persona_id FROM facts WHERE status='live'"
        ):
            persona = str(row["persona_id"] or "")
            fid = int(row["id"])
            push(str(row["speaker_name"] or ""), "person", "fact", fid, persona)
            for key in self._keyword_list(str(row["keywords"] or "[]")):
                push(key, "keyword", "fact", fid, persona)
            if row["topic"]:
                push(str(row["topic"]), "topic", "fact", fid, persona)
        for row in self.query(
            "SELECT id, participants, keywords, persona_id FROM events WHERE status='live'"
        ):
            persona = str(row["persona_id"] or "")
            eid = int(row["id"])
            for part in loads(row["participants"], []) or []:
                if str(part.get("id") or "") != ROLE_BOT_ID:
                    push(str(part.get("name") or ""), "person", "event", eid, persona)
            for key in self._keyword_list(str(row["keywords"] or "[]")):
                push(key, "keyword", "event", eid, persona)
        return bulk

    def _keyword_list(self, keywords_json: str) -> list[str]:
        items = loads(keywords_json, [])
        return [str(k) for k in (items or []) if k]
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

    def backup_to(self, dest: Path) -> None:
        """Consistent snapshot via SQLite online backup (WAL-safe, unlike file copy)."""
        import sqlite3 as _sqlite3

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_conn()
            target = _sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
                target.commit()
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            self._ensure_conn()
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_conn()
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
            """INSERT INTO timeline(ts, speaker_id, speaker_name, bot_id, window_tag, role, content, summarized, fingerprint, persona_id, addressee)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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
                event.get("addressee", ""),
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

    def timeline_after(self, after_id: int, until_ts: int, limit: int = 400) -> list[TimelineEvent]:
        """Timeline rows newer than a cursor whose episode can no longer grow."""
        rows = self.query(
            "SELECT * FROM timeline WHERE id>? AND ts<=? ORDER BY ts ASC, id ASC LIMIT ?",
            (int(after_id), int(until_ts), int(limit)),
        )
        return [self._timeline(r) for r in rows]

    def timeline_by_ids(self, ids: list[int]) -> list[TimelineEvent]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.query(f"SELECT * FROM timeline WHERE id IN ({q}) ORDER BY ts ASC, id ASC", ids)
        return [self._timeline(r) for r in rows]

    def timeline_recent(self, limit: int = 20, speaker_id: str | None = None) -> list[TimelineEvent]:
        if speaker_id:
            rows = self.query(
                "SELECT * FROM timeline WHERE speaker_id=? ORDER BY id DESC LIMIT ?",
                (speaker_id, limit),
            )
        else:
            rows = self.query("SELECT * FROM timeline ORDER BY id DESC LIMIT ?", (limit,))
        return [self._timeline(r) for r in rows]

    def timeline_in_windows(
        self,
        speaker_ids: list[str],
        exclude_window: str = "",
        since_ts: int = 0,
        limit: int = 40,
    ) -> list[TimelineEvent]:
        """取这些说话人在「其它会话」里的用户消息（跨窗口衔接用），最新在前。"""
        ids = [str(item) for item in (speaker_ids or []) if str(item).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        params: list[Any] = [*ids, since_ts]
        sql = (
            "SELECT * FROM timeline WHERE role='user'"
            f" AND speaker_id IN ({placeholders}) AND ts>=?"
        )
        if exclude_window:
            sql += " AND window_tag!=?"
            params.append(exclude_window)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [self._timeline(r) for r in self.query(sql, params)]

    def window_flow_events(
        self,
        exclude_window: str = "",
        since_ts: int = 0,
        limit: int = 200,
    ) -> list[TimelineEvent]:
        """取所有窗口（含 Bot 发言）的最近消息流（窗口全流注入用），最新在前。"""
        params: list[Any] = [since_ts]
        sql = "SELECT * FROM timeline WHERE ts>=? AND window_tag!=''"
        if exclude_window:
            sql += " AND window_tag!=?"
            params.append(exclude_window)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [self._timeline(r) for r in self.query(sql, params)]

    def recent_windows(self, limit: int = 50, since_ts: int = 0) -> list[dict[str, Any]]:
        """按最近活跃列出见过的窗口（供 /stype groups 与指派发言选目标）。"""
        params: list[Any] = [since_ts]
        sql = (
            "SELECT window_tag, COUNT(*) AS cnt, MAX(ts) AS last_ts"
            " FROM timeline WHERE window_tag!='' AND ts>=?"
            " GROUP BY window_tag ORDER BY last_ts DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        rows = self.query(sql, params)
        return [
            {"window_tag": str(row[0]), "count": int(row[1] or 0), "last_ts": int(row[2] or 0)}
            for row in rows
        ]

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
            "owner_facts": n("SELECT COUNT(*) FROM facts WHERE status='live' AND scope='owner'"),
            "person_facts": n("SELECT COUNT(*) FROM facts WHERE status='live' AND scope!='owner'"),
            "memory_pending": n("SELECT COUNT(*) FROM memory_reviews WHERE status='pending'"),
            "profiles": n("SELECT COUNT(*) FROM profiles"),
            "facts_pinned": n("SELECT COUNT(*) FROM facts WHERE status='live' AND pinned=1"),
            "events": n("SELECT COUNT(*) FROM events WHERE status='live'"),
            "events_archived": n("SELECT COUNT(*) FROM events WHERE status='archived'"),
            "events_needs_review": n("SELECT COUNT(*) FROM events WHERE review_status='needs_review'"),
            "events_pinned": n("SELECT COUNT(*) FROM events WHERE status='live' AND pinned=1"),
        }

    def add_fact(self, payload: dict[str, Any], bump: bool = True) -> int:
        now = now_ts()
        payload = apply_slot(payload)
        if float(payload.get("importance") or 0) <= 0:
            payload["importance"] = default_importance(payload)
        persona_id = str(payload.get("persona_id") or "")
        slot_key = payload.get("slot_key") or make_slot_key(
            persona_id,
            str(payload.get("speaker_id") or ""),
            str(payload.get("subject") or ""),
            str(payload.get("attribute") or ""),
            str(payload.get("value") or ""),
        )
        cur = self.execute(
            """INSERT INTO facts(
                subject, attribute, value, content, speaker_id, speaker_name, bot_id, window_tag,
                status, confidence, evidence, mention_policy, first_person, explicit_correction,
                source, created_at, updated_at, superseded_by, supersedes, fingerprint, embedding,
                access_count, last_accessed, reason, persona_id, slot_key, expires_at, write_op,
                scope, plain, keywords, source_event_id, review_status, origin, edited_at, edited_by,
                importance, kind, pinned, topic
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                int(payload.get("expires_at", 0) or 0),
                str(payload.get("write_op") or ""),
                str(payload.get("scope") or ""),
                str(payload.get("plain") or ""),
                dumps(payload.get("keywords") or []),
                int(payload.get("source_event_id", 0) or 0),
                str(payload.get("review_status") or ""),
                str(payload.get("origin") or ""),
                int(payload.get("edited_at", 0) or 0),
                str(payload.get("edited_by") or ""),
                float(payload.get("importance") or 0),
                str(payload.get("kind") or ""),
                int(payload.get("pinned", 0) or 0),
                str(payload.get("topic") or ""),
            ),
        )
        if bump:
            self.bump_revision()
        fact_id = int(cur.lastrowid)
        keywords = payload.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        names = [(str(payload.get("speaker_name") or ""), "person")]
        names.extend((str(k), "keyword") for k in keywords)
        if payload.get("topic"):
            names.append((str(payload.get("topic")), "topic"))
        self.link_entities(names, "fact", fact_id, persona_id)
        return int(cur.lastrowid)

    def update_fact(self, fact_id: int, bump: bool = True, **fields: Any) -> None:
        if not fields:
            return
        if "updated_at" not in fields:
            fields["updated_at"] = now_ts()
        if "evidence" in fields and not isinstance(fields["evidence"], str):
            fields["evidence"] = dumps(fields["evidence"])
        if "embedding" in fields and not isinstance(fields["embedding"], (str, type(None))):
            fields["embedding"] = dumps(fields["embedding"])
        if "keywords" in fields and not isinstance(fields["keywords"], str):
            fields["keywords"] = dumps(fields["keywords"])
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE facts SET {assignments} WHERE id=?", (*fields.values(), fact_id))
        if bump:
            self.bump_revision()
        if {"keywords", "speaker_name", "topic", "status"} & set(fields):
            self._relink_fact(int(fact_id))

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
            clauses.append(f"(speaker_id IN ({placeholders}) OR scope='owner')")
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

    def live_by_speaker(
        self,
        speaker_id: str,
        persona_id: str = "",
        speaker_ids: list[str] | None = None,
        limit: int = 40,
    ) -> list[Fact]:
        ids = list(speaker_ids or [])
        if speaker_id and speaker_id not in ids:
            ids.append(speaker_id)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        params: list[Any] = list(ids)
        clauses = [f"status='live'", f"speaker_id IN ({placeholders})"]
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        params.append(limit)
        sql = f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        return [self._fact(r) for r in self.query(sql, params)]

    def distinct_live_speakers(self, persona_id: str = "", limit: int = 80) -> list[dict[str, Any]]:
        if persona_id:
            rows = self.query(
                """SELECT speaker_id, MAX(speaker_name) AS speaker_name, COUNT(*) AS n
                   FROM facts WHERE status='live' AND (persona_id=? OR persona_id='')
                   GROUP BY speaker_id ORDER BY n DESC LIMIT ?""",
                (persona_id, limit),
            )
        else:
            rows = self.query(
                """SELECT speaker_id, MAX(speaker_name) AS speaker_name, COUNT(*) AS n
                   FROM facts WHERE status='live'
                   GROUP BY speaker_id ORDER BY n DESC LIMIT ?""",
                (limit,),
            )
        return [
            {"speaker_id": r["speaker_id"], "speaker_name": r["speaker_name"] or r["speaker_id"], "count": int(r["n"] or 0)}
            for r in rows
            if r["speaker_id"]
        ]

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

    def delete_fact(self, fact_id: int) -> bool:
        cur = self.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        self.execute("DELETE FROM entities WHERE ref='fact' AND ref_id=?", (int(fact_id),))
        self.bump_revision()
        return cur.rowcount > 0

    def bump_access(self, fact_id: int) -> None:
        """Atomic access reinforcement; avoids stale write-back from cached fact objects."""
        self.execute(
            "UPDATE facts SET access_count=access_count+1, last_accessed=? WHERE id=?",
            (now_ts(), int(fact_id)),
        )

    def set_pinned(self, fact_id: int, pinned: bool) -> bool:
        fact = self.get_fact(fact_id)
        if fact is None:
            return False
        self.update_fact(fact_id, pinned=int(bool(pinned)))
        return True

    def restore_facts(self, ids: list[int]) -> dict[str, Any]:
        """Bring archived/superseded facts back. Blocks when a live fact holds the slot."""
        restored: list[int] = []
        blocked: list[dict[str, Any]] = []
        missing: list[int] = []
        for raw in ids:
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                continue
            fact = self.get_fact(fid)
            if fact is None:
                missing.append(fid)
                continue
            if fact.status == "live":
                restored.append(fid)
                continue
            conflict = self.live_by_slot(
                fact.speaker_id,
                fact.subject,
                fact.attribute,
                persona_id=fact.persona_id,
                speaker_ids=self.speaker_ids_for(fact.speaker_id),
                value=fact.value,
            )
            if conflict is not None and conflict.id != fid:
                blocked.append({"id": fid, "conflict": conflict.id})
                continue
            self.update_fact(fid, status="live", reason="restored", superseded_by=None)
            restored.append(fid)
        return {"ok": True, "restored": restored, "blocked": blocked, "missing": missing}

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
            "(content LIKE ? OR subject LIKE ? OR attribute LIKE ? OR value LIKE ? OR keywords LIKE ?)",
        ]
        params: list[Any] = [like, like, like, like, like]
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        if ids:
            placeholders = ",".join("?" * (len(ids) + 2))
            clauses.append(f"(speaker_id IN ({placeholders}) OR scope='owner')")
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
        value: str = "",
    ) -> Fact | None:
        payload = apply_slot(
            {
                "subject": subject,
                "attribute": attribute,
                "speaker_id": speaker_id,
                "value": value,
            }
        )
        key = make_slot_key(persona_id, speaker_id, payload["subject"], payload["attribute"], value)
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
                ORDER BY confidence DESC, updated_at DESC LIMIT 20""",
            (*ids, payload["subject"], payload["attribute"], persona_id),
        )
        for row in rows:
            fact = self._fact(row)
            if not fact.slot_key_value and fact.slot_key() == key:
                return fact
        return None

    def live_fact_by_slot_key(self, slot_key: str) -> Fact | None:
        if not slot_key:
            return None
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' AND slot_key=? "
            "ORDER BY confidence DESC, updated_at DESC LIMIT 1",
            (slot_key,),
        )
        return self._fact(rows[0]) if rows else None

    def live_latest_by_attr(
        self,
        speaker_id: str,
        subject: str,
        attribute: str,
        persona_id: str = "",
        speaker_ids: list[str] | None = None,
        topic: str = "",
    ) -> Fact | None:
        """Most recent live fact of one attribute (used by write_op=close on promise/habit)."""
        from .util import topic_key

        payload = apply_slot(
            {
                "subject": subject,
                "attribute": attribute,
                "speaker_id": speaker_id,
            }
        )
        ids = list(speaker_ids or [speaker_id])
        placeholders = ",".join("?" * len(ids))
        rows = self.query(
            f"""SELECT * FROM facts WHERE status='live' AND speaker_id IN ({placeholders})
                AND subject=? AND attribute=? AND (persona_id=? OR persona_id='')
                ORDER BY updated_at DESC LIMIT 50""",
            (*ids, payload["subject"], payload["attribute"], persona_id),
        )
        facts = [self._fact(r) for r in rows]
        if not facts:
            return None
        if topic:
            for fact in facts:
                if topic_key(fact.value or "") == topic:
                    return fact
        return facts[0]

    def fact_by_fingerprint(self, fingerprint: str) -> Fact | None:
        if not fingerprint:
            return None
        rows = self.query("SELECT * FROM facts WHERE fingerprint=? LIMIT 1", (fingerprint,))
        return self._fact(rows[0]) if rows else None

    def live_oldest(self, limit: int = 300) -> list[Fact]:
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' AND pinned=0 ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        )
        return [self._fact(r) for r in rows]

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
        profiles = [dict(r) for r in self.query("SELECT * FROM profiles")]
        memory_reviews = [dict(r) for r in self.query("SELECT * FROM memory_reviews")]
        aliases = [
            dict(r)
            for r in self.query("SELECT alias, canonical_id, label FROM speaker_aliases")
        ]
        events = [dict(r) for r in self.query("SELECT * FROM events")]
        return {
            "facts": facts,
            "timeline": timeline,
            "pending": pending,
            "reviews": reviews,
            "profiles": profiles,
            "memory_reviews": memory_reviews,
            "aliases": aliases,
            "events": events,
        }

    def import_profile(self, row: dict[str, Any]) -> bool:
        sid = str(row.get("speaker_id") or "").strip()
        if not sid:
            return False
        now = now_ts()
        self.execute(
            """INSERT INTO profiles(speaker_id, speaker_name, platform, is_owner, note, first_seen, last_seen, seen_count, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(speaker_id) DO UPDATE SET
                 speaker_name=CASE WHEN excluded.speaker_name!='' THEN excluded.speaker_name ELSE profiles.speaker_name END,
                 note=CASE WHEN excluded.note!='' THEN excluded.note ELSE profiles.note END,
                 is_owner=MAX(profiles.is_owner, excluded.is_owner),
                 first_seen=CASE
                   WHEN profiles.first_seen=0 OR (excluded.first_seen>0 AND excluded.first_seen<profiles.first_seen)
                   THEN excluded.first_seen ELSE profiles.first_seen END,
                 last_seen=CASE WHEN excluded.last_seen>profiles.last_seen THEN excluded.last_seen ELSE profiles.last_seen END,
                 seen_count=MAX(profiles.seen_count, excluded.seen_count),
                 updated_at=excluded.updated_at""",
            (
                sid,
                str(row.get("speaker_name") or ""),
                str(row.get("platform") or ""),
                int(row.get("is_owner") or 0),
                str(row.get("note") or ""),
                int(row.get("first_seen") or 0),
                int(row.get("last_seen") or 0),
                int(row.get("seen_count") or 0),
                int(row.get("created_at") or now),
                now,
            ),
        )
        return True

    def import_memory_review(self, row: dict[str, Any]) -> bool:
        speaker_id = str(row.get("speaker_id") or "")
        source_event_id = int(row.get("source_event_id") or 0)
        plain = str(row.get("plain") or "")
        exists = self.query(
            "SELECT id FROM memory_reviews WHERE speaker_id=? AND source_event_id=? AND plain=?",
            (speaker_id, source_event_id, plain),
        )
        if exists:
            return False
        keywords = row.get("keywords")
        if isinstance(keywords, str):
            keywords = loads(keywords, [])
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = loads(payload, {})
        trace = row.get("trace")
        if isinstance(trace, str):
            trace = loads(trace, [])
        review_id = self.add_memory_review(
            scope=str(row.get("scope") or "person"),
            speaker_id=speaker_id,
            speaker_name=str(row.get("speaker_name") or ""),
            platform=str(row.get("platform") or ""),
            window_tag=str(row.get("window_tag") or ""),
            source_event_id=source_event_id,
            raw_text=str(row.get("raw_text") or ""),
            plain=plain,
            keywords=list(keywords or []),
            payload=dict(payload or {}),
            attempts=int(row.get("attempts") or 0),
            trace=list(trace or []),
        )
        status = str(row.get("status") or "pending")
        if status in {"approved", "rejected"} and review_id:
            self.update_memory_review(review_id, status=status)
        return True

    def import_event(self, row: dict[str, Any]) -> bool:
        fingerprint = str(row.get("fingerprint") or "")
        if fingerprint and self.event_by_fingerprint(fingerprint):
            return False
        payload = {k: row[k] for k in row if k not in {"table", "id"}}
        for key in ("participants", "highlights", "keywords", "evidence"):
            value = payload.get(key)
            if isinstance(value, str):
                payload[key] = loads(value, [])
        self.add_event(payload)
        return True

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
        current = speaker_id
        for _ in range(4):
            rows = self.query("SELECT canonical_id FROM speaker_aliases WHERE alias=?", (current,))
            if not rows:
                return current
            nxt = str(rows[0]["canonical_id"] or "")
            if not nxt or nxt == current:
                return current
            current = nxt
        return current

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

    def reassign_speaker(self, old_id: str, new_id: str, new_name: str = "") -> int:
        """Move facts/timeline/profile from one speaker id to another (identity merge)."""
        if not old_id or not new_id or old_id == new_id:
            return 0
        rows = self.query(
            "SELECT id, persona_id, subject, attribute, value, speaker_name FROM facts WHERE speaker_id=?",
            (old_id,),
        )
        for row in rows:
            key = make_slot_key(
                row["persona_id"] or "",
                new_id,
                row["subject"],
                row["attribute"],
                row["value"],
            )
            self.execute(
                "UPDATE facts SET speaker_id=?, speaker_name=?, slot_key=? WHERE id=?",
                (new_id, new_name or row["speaker_name"], key, int(row["id"])),
            )
        if new_name:
            self.execute(
                "UPDATE timeline SET speaker_id=?, speaker_name=? WHERE speaker_id=?",
                (new_id, new_name, old_id),
            )
        else:
            self.execute("UPDATE timeline SET speaker_id=? WHERE speaker_id=?", (new_id, old_id))
        if new_name:
            self.execute(
                "UPDATE memory_reviews SET speaker_id=?, speaker_name=? WHERE speaker_id=?",
                (new_id, new_name, old_id),
            )
        else:
            self.execute(
                "UPDATE memory_reviews SET speaker_id=? WHERE speaker_id=?", (new_id, old_id)
            )
        old_profile = self.get_profile(old_id)
        if old_profile is not None:
            self.upsert_profile(
                new_id,
                new_name or old_profile.speaker_name,
                old_profile.platform,
                is_owner=bool(old_profile.is_owner),
            )
            self.execute("DELETE FROM profiles WHERE speaker_id=?", (old_id,))
        self.set_alias(old_id, new_id, new_name)
        self.resolve_slot_conflicts(new_id)
        self.bump_revision()
        if new_name:
            self.sync_event_participant(new_id, new_name)
        self.relink_speaker(new_id)
        return len(rows)

    def _keep_and_archive_duplicate(self, keeper: Fact, dup: Fact, reason: str) -> tuple[Fact, bool]:
        """Merge same-value evidence, archive the duplicate; pinned always wins.

        Returns (keeper, archived). Both pinned -> (first, False) and a human decides.
        """
        from .contradiction import values_conflict

        if int(dup.pinned or 0) and int(keeper.pinned or 0):
            # 两条都置顶：都不归档，交给人工处理。
            return keeper, False
        if int(dup.pinned or 0) and not int(keeper.pinned or 0):
            keeper, dup = dup, keeper
        if not values_conflict(keeper.value, dup.value):
            evidence = list(keeper.evidence)
            for eid in dup.evidence:
                if eid not in evidence:
                    evidence.append(eid)
            fields: dict[str, Any] = {
                "evidence": evidence,
                "confidence": max(keeper.confidence, dup.confidence),
                "importance": max(float(keeper.importance or 0), float(dup.importance or 0)),
            }
            if not getattr(keeper, "topic", "") and getattr(dup, "topic", ""):
                fields["topic"] = dup.topic
            self.update_fact(keeper.id, **fields)
        self.update_fact(dup.id, status="archived", reason=reason)
        return keeper, True

    def resolve_all_slot_conflicts(self) -> int:
        """Keep the newest (or pinned) live fact per slot; archive older duplicates."""
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' ORDER BY slot_key, pinned DESC, updated_at DESC, confidence DESC"
        )
        keepers: dict[str, Fact] = {}
        archived = 0
        for row in rows:
            fact = self._fact(row)
            key = fact.slot_key()
            keeper = keepers.get(key)
            if keeper is None:
                keepers[key] = fact
                continue
            keepers[key], done = self._keep_and_archive_duplicate(keeper, fact, "slot_conflict")
            archived += 1 if done else 0
        return archived

    def resolve_slot_conflicts(self, speaker_id: str) -> int:
        """After an identity merge, keep the newest (or pinned) live fact per slot."""
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' AND speaker_id=? "
            "ORDER BY slot_key, pinned DESC, updated_at DESC, confidence DESC",
            (speaker_id,),
        )
        keepers: dict[str, Fact] = {}
        archived = 0
        for row in rows:
            fact = self._fact(row)
            key = fact.slot_key()
            keeper = keepers.get(key)
            if keeper is None:
                keepers[key] = fact
                continue
            keepers[key], done = self._keep_and_archive_duplicate(
                keeper, fact, "identity_merge_conflict"
            )
            archived += 1 if done else 0
        return archived

    def recent_recall_ids(self, window_tag: str, since_ts: int) -> set[int]:
        rows = self.query(
            "SELECT fact_id FROM recall_log WHERE window_tag=? AND ts>=?",
            (window_tag, int(since_ts)),
        )
        return {int(r["fact_id"]) for r in rows}

    def add_recall(self, window_tag: str, fact_ids: list[int], ts: int) -> None:
        if not window_tag or not fact_ids:
            return
        for fid in fact_ids:
            self.execute(
                "INSERT INTO recall_log(window_tag, fact_id, ts) VALUES(?,?,?) "
                "ON CONFLICT(window_tag, fact_id) DO UPDATE SET ts=excluded.ts",
                (window_tag, int(fid), int(ts)),
            )
        # 去重窗口允许设得很长，日志保留 7 天，避免窗口未到就被清掉。
        self.execute("DELETE FROM recall_log WHERE ts < ?", (int(ts) - 7 * 86400,))

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
        task: str = "",
        source: str = "",
        reason: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO usage_ledger(ts, kind, provider_id, ok, chars_in, chars_out, tokens_in, tokens_out, detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (now_ts(), kind, provider_id, int(ok), chars_in, chars_out, int(tokens_in or 0), int(tokens_out or 0), detail[:240]),
        )
        self.execute(
            "DELETE FROM usage_ledger WHERE id NOT IN (SELECT id FROM usage_ledger ORDER BY id DESC LIMIT 400)"
        )
        self._bump_usage_daily(
            task=task or kind,
            provider_id=provider_id,
            ok=bool(ok),
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            source=source,
            reason=reason,
        )

    def _bump_usage_daily(
        self,
        task: str,
        provider_id: str = "",
        ok: bool = True,
        tokens_in: int = 0,
        tokens_out: int = 0,
        source: str = "",
        reason: str = "",
    ) -> None:
        """按天聚合的用量账：Token 预算和面板按任务统计都用它，不受流水保留期影响。"""
        self.execute(
            """INSERT INTO usage_daily(day, task, provider_id, calls, skipped, tokens_in, tokens_out, source, skip_reason, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, task, provider_id) DO UPDATE SET
                 calls=usage_daily.calls+excluded.calls,
                 skipped=usage_daily.skipped+excluded.skipped,
                 tokens_in=usage_daily.tokens_in+excluded.tokens_in,
                 tokens_out=usage_daily.tokens_out+excluded.tokens_out,
                 source=CASE WHEN excluded.source!='' THEN excluded.source ELSE usage_daily.source END,
                 skip_reason=CASE WHEN excluded.skip_reason!='' THEN excluded.skip_reason ELSE usage_daily.skip_reason END,
                 updated_at=excluded.updated_at""",
            (
                today_str(),
                str(task or "")[:40],
                str(provider_id or "")[:80],
                1 if ok else 0,
                0 if ok else 1,
                max(0, int(tokens_in or 0)),
                max(0, int(tokens_out or 0)),
                str(source or "")[:60],
                str(reason or "")[:60] if not ok else "",
                now_ts(),
            ),
        )

    def tokens_today(self, day: str = "") -> int:
        rows = self.query(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM usage_daily WHERE day=?",
            (day or today_str(),),
        )
        return int(rows[0][0] or 0)

    def usage_by_task_today(self, day: str = "") -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT task, provider_id, source, SUM(calls) AS calls, SUM(skipped) AS skipped,
                      SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, MAX(skip_reason) AS skip_reason
               FROM usage_daily WHERE day=? GROUP BY task, provider_id, source
               ORDER BY (SUM(tokens_in) + SUM(tokens_out)) DESC""",
            (day or today_str(),),
        )
        return [
            {
                "task": str(r["task"]),
                "provider_id": str(r["provider_id"]),
                "source": str(r["source"]),
                "calls": int(r["calls"] or 0),
                "skipped": int(r["skipped"] or 0),
                "tokens_in": int(r["tin"] or 0),
                "tokens_out": int(r["tout"] or 0),
                "tokens": int(r["tin"] or 0) + int(r["tout"] or 0),
                "skip_reason": str(r["skip_reason"] or ""),
            }
            for r in rows
        ]

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
            addressee=row["addressee"] if "addressee" in keys else "",
        )

    def _fact(self, row: sqlite3.Row) -> Fact:
        keys = row.keys()

        def _str_list(raw: Any) -> list[str]:
            items = loads(raw, [])
            if isinstance(items, str):
                items = [items]
            return [str(x) for x in (items or []) if x]

        def _int_list(raw: Any) -> list[int]:
            items = loads(raw, [])
            if isinstance(items, (int, str)):
                items = [items]
            out: list[int] = []
            for x in items or []:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
            return out

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
            evidence=_int_list(row["evidence"]),
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
            expires_at=int(row["expires_at"] or 0) if "expires_at" in keys else 0,
            write_op=row["write_op"] if "write_op" in keys else "",
            scope=row["scope"] if "scope" in keys else "",
            plain=row["plain"] if "plain" in keys else "",
            keywords=_str_list(row["keywords"]) if "keywords" in keys else [],
            source_event_id=int(row["source_event_id"] or 0) if "source_event_id" in keys else 0,
            review_status=row["review_status"] if "review_status" in keys else "",
            origin=row["origin"] if "origin" in keys else "",
            edited_at=int(row["edited_at"] or 0) if "edited_at" in keys else 0,
            edited_by=row["edited_by"] if "edited_by" in keys else "",
            importance=float(row["importance"] or 0) if "importance" in keys else 0.0,
            kind=row["kind"] if "kind" in keys else "",
            pinned=int(row["pinned"] or 0) if "pinned" in keys else 0,
            topic=row["topic"] if "topic" in keys else "",
        )

    # ------------------------------------------------------------------
    # Profiles (auto-created per QQ sender)
    # ------------------------------------------------------------------

    def sync_speaker_name(self, speaker_id: str, speaker_name: str) -> int:
        """Propagate a learned nickname to denormalized copies (facts/timeline)."""
        sid = (speaker_id or "").strip()
        name = (speaker_name or "").strip()
        if not sid or not name or name == sid:
            return 0
        updated = 0
        for table in ("facts", "timeline", "memory_reviews"):
            cur = self.execute(
                f"UPDATE {table} SET speaker_name=? WHERE speaker_id=? AND speaker_name!=?",
                (name, sid, name),
            )
            updated += int(cur.rowcount or 0)
        if updated:
            self.bump_revision()
            self.relink_speaker(sid)
            self.sync_event_participant(sid, name)
        return updated

    def upsert_profile(
        self,
        speaker_id: str,
        speaker_name: str = "",
        platform: str = "",
        is_owner: bool = False,
    ) -> None:
        sid = (speaker_id or "").strip()
        if not sid:
            return
        now = now_ts()
        rows = self.query(
            "SELECT speaker_id, speaker_name, platform FROM profiles WHERE speaker_id=?", (sid,)
        )
        if rows:
            name = (speaker_name or "").strip() or rows[0]["speaker_name"]
            platform_value = (platform or "").strip() or str(rows[0]["platform"] or "")
            if name and name != str(rows[0]["speaker_name"] or ""):
                # 昵称更新后同步事实/时间线上的冗余副本，避免旧名残留。
                self.sync_speaker_name(sid, name)
            self.execute(
                "UPDATE profiles SET speaker_name=?, platform=?, is_owner=?, last_seen=?, seen_count=seen_count+1, updated_at=? WHERE speaker_id=?",
                (name, platform_value, int(bool(is_owner)), now, now, sid),
            )
            return
        self.execute(
            """INSERT INTO profiles(speaker_id, speaker_name, platform, is_owner, note, first_seen, last_seen, seen_count, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid, (speaker_name or "").strip(), platform or "", int(bool(is_owner)), "", now, now, 1, now, now),
        )

    def get_profile(self, speaker_id: str) -> Profile | None:
        rows = self.query(
            """SELECT p.*, (SELECT COUNT(*) FROM facts f WHERE f.speaker_id=p.speaker_id AND f.status='live') AS fact_count
               FROM profiles p WHERE p.speaker_id=?""",
            (speaker_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return Profile(
            speaker_id=r["speaker_id"],
            speaker_name=r["speaker_name"],
            platform=r["platform"],
            is_owner=int(r["is_owner"] or 0),
            note=r["note"] or "",
            first_seen=int(r["first_seen"] or 0),
            last_seen=int(r["last_seen"] or 0),
            seen_count=int(r["seen_count"] or 0),
            fact_count=int(r["fact_count"] or 0) if "fact_count" in r.keys() else 0,
        )

    def list_profiles(self, limit: int = 200, non_empty_only: bool = False) -> list[Profile]:
        rows = self.query(
            """SELECT p.*, (SELECT COUNT(*) FROM facts f WHERE f.speaker_id=p.speaker_id AND f.status='live') AS fact_count
               FROM profiles p ORDER BY p.is_owner DESC, p.last_seen DESC LIMIT ?""",
            (limit,),
        )
        out = []
        for r in rows:
            profile = Profile(
                speaker_id=r["speaker_id"],
                speaker_name=r["speaker_name"],
                platform=r["platform"],
                is_owner=int(r["is_owner"] or 0),
                note=r["note"] or "",
                first_seen=int(r["first_seen"] or 0),
                last_seen=int(r["last_seen"] or 0),
                seen_count=int(r["seen_count"] or 0),
                fact_count=int(r["fact_count"] or 0) if "fact_count" in r.keys() else 0,
            )
            if non_empty_only and profile.fact_count <= 0:
                continue
            out.append(profile)
        return out

    def update_profile(
        self,
        speaker_id: str,
        speaker_name: str | None = None,
        note: str | None = None,
        is_owner: bool | None = None,
    ) -> bool:
        sid = (speaker_id or "").strip()
        if not sid:
            return False
        profile = self.get_profile(sid)
        if profile is None:
            return False
        fields: dict[str, Any] = {"updated_at": now_ts()}
        if speaker_name is not None:
            fields["speaker_name"] = speaker_name.strip()
        if note is not None:
            fields["note"] = note.strip()
        if is_owner is not None:
            fields["is_owner"] = int(bool(is_owner))
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE profiles SET {assignments} WHERE speaker_id=?",
            (*fields.values(), sid),
        )
        return True

    def delete_empty_profiles(self, ttl_days: int = 7, limit: int = 200) -> int:
        if ttl_days <= 0:
            return 0
        cutoff = now_ts() - ttl_days * 86400
        rows = self.query(
            """SELECT p.speaker_id FROM profiles p
               WHERE p.last_seen < ? AND p.speaker_id != ''
                 AND NOT EXISTS (
                   SELECT 1 FROM facts f WHERE f.speaker_id=p.speaker_id
                 )
               ORDER BY p.last_seen ASC LIMIT ?""",
            (cutoff, limit),
        )
        ids = [r["speaker_id"] for r in rows]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        self.execute(f"DELETE FROM profiles WHERE speaker_id IN ({q})", ids)
        return len(ids)

    # ------------------------------------------------------------------
    # Memory review queue (normalize + verify pipeline)
    # ------------------------------------------------------------------

    def add_memory_review(
        self,
        *,
        scope: str,
        speaker_id: str,
        speaker_name: str = "",
        platform: str = "",
        window_tag: str = "",
        source_event_id: int = 0,
        raw_text: str = "",
        plain: str = "",
        keywords: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        attempts: int = 0,
        trace: list[dict[str, Any]] | None = None,
    ) -> int:
        now = now_ts()
        existing = self.query(
            "SELECT id FROM memory_reviews WHERE status=? AND speaker_id=? AND source_event_id=? AND plain=?",
            (MEMORY_STATUS_PENDING, speaker_id or "", int(source_event_id or 0), plain or ""),
        )
        if existing:
            # 同一来源重复入队（例如标记已总结前失败重试）直接复用。
            return int(existing[0]["id"])
        cur = self.execute(
            """INSERT INTO memory_reviews(
                scope, speaker_id, speaker_name, platform, window_tag, source_event_id,
                raw_text, plain, keywords, payload, status, attempts, trace, notified_at,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scope or "person",
                speaker_id or "",
                speaker_name or "",
                platform or "",
                window_tag or "",
                int(source_event_id or 0),
                raw_text or "",
                plain or "",
                dumps(keywords or []),
                dumps(payload or {}),
                MEMORY_STATUS_PENDING,
                int(attempts or 0),
                dumps(trace or []),
                0,
                now,
                now,
            ),
        )
        self.bump_revision()
        return int(cur.lastrowid)

    def _memory_review(self, row: sqlite3.Row) -> MemoryReview:
        return MemoryReview(
            id=int(row["id"]),
            scope=row["scope"] or "person",
            speaker_id=row["speaker_id"] or "",
            speaker_name=row["speaker_name"] or "",
            platform=row["platform"] or "",
            window_tag=row["window_tag"] or "",
            source_event_id=int(row["source_event_id"] or 0),
            raw_text=row["raw_text"] or "",
            plain=row["plain"] or "",
            keywords=loads(row["keywords"], []),
            payload=loads(row["payload"], {}),
            status=row["status"] or "pending",
            attempts=int(row["attempts"] or 0),
            trace=loads(row["trace"], []),
            notified_at=int(row["notified_at"] or 0),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
        )

    def list_memory_reviews(self, status: str = "pending", limit: int = 80) -> list[MemoryReview]:
        rows = self.query(
            "SELECT * FROM memory_reviews WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
        return [self._memory_review(r) for r in rows]

    def get_memory_review(self, review_id: int) -> MemoryReview | None:
        rows = self.query("SELECT * FROM memory_reviews WHERE id=?", (review_id,))
        return self._memory_review(rows[0]) if rows else None

    def update_memory_review(self, review_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_ts()
        if "keywords" in fields and not isinstance(fields["keywords"], str):
            fields["keywords"] = dumps(fields["keywords"])
        if "payload" in fields and not isinstance(fields["payload"], str):
            fields["payload"] = dumps(fields["payload"])
        if "trace" in fields and not isinstance(fields["trace"], str):
            fields["trace"] = dumps(fields["trace"])
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE memory_reviews SET {assignments} WHERE id=?", (*fields.values(), review_id))
        self.bump_revision()

    def delete_memory_review(self, review_id: int) -> bool:
        cur = self.execute("DELETE FROM memory_reviews WHERE id=?", (review_id,))
        self.bump_revision()
        return cur.rowcount > 0

    def pending_memory_unqueued(self, limit: int = 20) -> list[MemoryReview]:
        rows = self.query(
            "SELECT * FROM memory_reviews WHERE status='pending' AND notified_at=0 ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        return [self._memory_review(r) for r in rows]

    def owner_facts(self, limit: int = 200) -> list[Fact]:
        rows = self.query(
            "SELECT * FROM facts WHERE status='live' AND scope=? ORDER BY updated_at DESC LIMIT ?",
            (SCOPE_OWNER, limit),
        )
        return [self._fact(r) for r in rows]

    def person_facts(
        self,
        speaker_id: str,
        limit: int = 200,
        include_archived: bool = False,
        speaker_ids: list[str] | None = None,
    ) -> list[Fact]:
        ids = list(speaker_ids or [speaker_id])
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        clause = "" if include_archived else "AND status='live'"
        rows = self.query(
            f"SELECT * FROM facts WHERE speaker_id IN ({placeholders}) {clause} ORDER BY updated_at DESC LIMIT ?",
            (*ids, limit),
        )
        return [self._fact(r) for r in rows]

    # ------------------------------------------------------------------
    # Events (episodic memory: one whole thing that happened)
    # ------------------------------------------------------------------

    def add_event(self, payload: dict[str, Any], bump: bool = True) -> int:
        now = now_ts()
        cur = self.execute(
            """INSERT INTO events(
                kind, title, summary, speaker_id, speaker_name, bot_id, window_tag, persona_id,
                scope, participants, speaker_ids, highlights, keywords, evidence, start_ts, end_ts,
                importance, confidence, status, pinned, access_count, last_accessed,
                source, review_status, origin, fingerprint, reason, edited_at, edited_by,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(payload.get("kind") or "life"),
                clip(str(payload.get("title") or ""), 60),
                clip(str(payload.get("summary") or ""), 400),
                str(payload.get("speaker_id") or ""),
                str(payload.get("speaker_name") or ""),
                str(payload.get("bot_id") or ""),
                str(payload.get("window_tag") or ""),
                str(payload.get("persona_id") or ""),
                str(payload.get("scope") or "person"),
                dumps(payload.get("participants") or []),
                dumps(payload.get("speaker_ids") or []),
                dumps(payload.get("highlights") or []),
                dumps(payload.get("keywords") or []),
                dumps(payload.get("evidence") or []),
                int(payload.get("start_ts") or 0),
                int(payload.get("end_ts") or 0),
                float(payload.get("importance") or 0) or 0.5,
                float(payload.get("confidence") or 0.6),
                str(payload.get("status") or "live"),
                int(payload.get("pinned") or 0),
                int(payload.get("access_count") or 0),
                int(payload.get("last_accessed") or 0),
                str(payload.get("source") or "pipeline"),
                str(payload.get("review_status") or ""),
                str(payload.get("origin") or ""),
                str(payload.get("fingerprint") or ""),
                str(payload.get("reason") or ""),
                int(payload.get("edited_at") or 0),
                str(payload.get("edited_by") or ""),
                int(payload.get("created_at") or now),
                int(payload.get("updated_at") or now),
            ),
        )
        if bump:
            self.bump_revision()
        event_id = int(cur.lastrowid)
        participants = payload.get("participants") or []
        if not isinstance(participants, list):
            participants = []
        event_keywords = payload.get("keywords") or []
        if isinstance(event_keywords, str):
            event_keywords = [event_keywords]
        names = [
            (str(p.get("name") or ""), "person")
            for p in participants
            if isinstance(p, dict) and str(p.get("id") or "") != ROLE_BOT_ID
        ]
        names.extend((str(k), "keyword") for k in event_keywords)
        self.link_entities(names, "event", event_id, str(payload.get("persona_id") or ""))
        return int(cur.lastrowid)

    def update_event(self, event_id: int, bump: bool = True, **fields: Any) -> None:
        if not fields:
            return
        if "updated_at" not in fields:
            fields["updated_at"] = now_ts()
        for key in ("participants", "speaker_ids", "highlights", "keywords", "evidence"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = dumps(fields[key])
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE events SET {assignments} WHERE id=?", (*fields.values(), event_id))
        if bump:
            self.bump_revision()
        if {"participants", "keywords", "status"} & set(fields):
            self._relink_event(int(event_id))

    def delete_event(self, event_id: int) -> bool:
        cur = self.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.execute("DELETE FROM entities WHERE ref='event' AND ref_id=?", (int(event_id),))
        self.bump_revision()
        return cur.rowcount > 0

    def get_event(self, event_id: int) -> Event | None:
        rows = self.query("SELECT * FROM events WHERE id=?", (event_id,))
        return self._event(rows[0]) if rows else None

    def event_by_fingerprint(self, fingerprint: str) -> Event | None:
        if not fingerprint:
            return None
        rows = self.query("SELECT * FROM events WHERE fingerprint=? LIMIT 1", (fingerprint,))
        return self._event(rows[0]) if rows else None

    def live_events(
        self,
        speaker_id: str | None = None,
        speaker_ids: list[str] | None = None,
        persona_id: str | None = None,
        limit: int = 120,
        since_ts: int = 0,
        until_ts: int = 0,
        window_tag: str | None = None,
        include_owner: bool = True,
        statuses: tuple[str, ...] = ("live",),
    ) -> list[Event]:
        ids = list(speaker_ids or [])
        if speaker_id and speaker_id not in ids:
            ids.append(speaker_id)
        marks = ",".join("?" * len(statuses or ("live",)))
        clauses = [f"status IN ({marks})"]
        params: list[Any] = list(statuses or ("live",))
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        if since_ts > 0:
            clauses.append("end_ts>=?")
            params.append(int(since_ts))
        if until_ts > 0:
            clauses.append("start_ts<=?")
            params.append(int(until_ts))
        if window_tag:
            clauses.append("window_tag=?")
            params.append(window_tag)
        if ids:
            placeholders = ",".join("?" * len(ids))
            speaker_clause = f"speaker_id IN ({placeholders})"
            params.extend(ids)
            if include_owner:
                speaker_clause = f"({speaker_clause} OR scope='owner')"
            clauses.append(speaker_clause)
        params.append(limit)
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY start_ts DESC, id DESC LIMIT ?"
        return [self._event(r) for r in self.query(sql, params)]

    def events_by_status(self, status: str = "live", limit: int = 60) -> list[Event]:
        rows = self.query(
            "SELECT * FROM events WHERE status=? ORDER BY start_ts DESC, id DESC LIMIT ?",
            (status, limit),
        )
        return [self._event(r) for r in rows]

    # ------------------------------------------------------------------
    # Entity links (facts/events <-> people, keywords, topics)
    # ------------------------------------------------------------------

    ENTITY_STOPWORDS = {
        "bot", "bot_self", "未知", "某人", "主人", "admin", "用户",
        "我", "你", "他", "她", "它", "我们", "你们", "他们",
    }

    def _clean_entity(self, raw: str) -> str:
        name = normalize_slot(str(raw or ""))
        if len(name) < 2 or len(name) > 20:
            return ""
        if name.isdigit() or name in self.ENTITY_STOPWORDS:
            return ""
        return name

    def link_entities(
        self,
        names: list[tuple[str, str]],
        ref: str,
        ref_id: int,
        persona_id: str = "",
    ) -> None:
        rows: list[tuple[str, str, str, int, str, int]] = []
        seen: set[tuple[str, str]] = set()
        for raw, kind in names:
            name = self._clean_entity(raw)
            key = (name, kind)
            if not name or key in seen:
                continue
            seen.add(key)
            rows.append((name, kind, ref, int(ref_id), persona_id or "", now_ts()))
        if not rows:
            return
        with self._lock:
            self._ensure_conn()
            self._conn.executemany(
                "INSERT INTO entities(name, kind, ref, ref_id, persona_id, ts) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name, ref, ref_id) DO UPDATE SET kind=excluded.kind, ts=excluded.ts",
                rows,
            )
            self._conn.commit()

    def _relink_fact(self, fact_id: int) -> None:
        self.execute("DELETE FROM entities WHERE ref='fact' AND ref_id=?", (int(fact_id),))
        rows = self.query("SELECT * FROM facts WHERE id=?", (int(fact_id),))
        if not rows:
            return
        fact = self._fact(rows[0])
        names = [(fact.speaker_name, "person")]
        names.extend((str(k), "keyword") for k in (fact.keywords or []))
        if getattr(fact, "topic", ""):
            names.append((fact.topic, "topic"))
        self.link_entities(names, "fact", fact_id, fact.persona_id)

    def _relink_event(self, event_id: int) -> None:
        self.execute("DELETE FROM entities WHERE ref='event' AND ref_id=?", (int(event_id),))
        rows = self.query("SELECT * FROM events WHERE id=?", (int(event_id),))
        if not rows:
            return
        event = self._event(rows[0])
        names = [
            (str(p.get("name") or ""), "person")
            for p in (event.participants or [])
            if isinstance(p, dict) and str(p.get("id") or "") != ROLE_BOT_ID
        ]
        names.extend((str(k), "keyword") for k in (event.keywords or []))
        self.link_entities(names, "event", event_id, event.persona_id)

    def relink_speaker(self, speaker_id: str) -> None:
        """Refresh person links after a nickname change or identity merge."""
        if not speaker_id:
            return
        for row in self.query("SELECT id FROM facts WHERE speaker_id=?", (speaker_id,)):
            self._relink_fact(int(row["id"]))
        for row in self.query("SELECT id FROM events WHERE speaker_id=?", (speaker_id,)):
            self._relink_event(int(row["id"]))

    def sync_event_participant(self, speaker_id: str, speaker_name: str) -> int:
        """Fix stale participant names stored inside event cards after a rename."""
        sid = (speaker_id or "").strip()
        name = (speaker_name or "").strip()
        if not sid or not name or name == sid:
            return 0
        touched = 0
        for row in self.query(
            "SELECT id, participants FROM events WHERE status='live'"
        ):
            parts = loads(row["participants"], []) or []
            if not isinstance(parts, list):
                continue
            changed = False
            for part in parts:
                if str(part.get("id") or "") == sid and str(part.get("name") or "") != name:
                    part["name"] = name
                    changed = True
            if changed:
                self.execute(
                    "UPDATE events SET participants=? WHERE id=?",
                    (dumps(parts), int(row["id"])),
                )
                self._relink_event(int(row["id"]))
                touched += 1
        return touched

    def entities_in_text(self, text: str, limit: int = 8) -> list[str]:
        norm = normalize_slot(text or "")
        if len(norm) < 2:
            return []
        rows = self.query("SELECT DISTINCT name FROM entities LIMIT 5000")
        hits = [str(r["name"]) for r in rows if len(str(r["name"])) >= 2 and str(r["name"]) in norm]
        hits.sort(key=len, reverse=True)
        return hits[:limit]

    def entity_refs(
        self,
        names: list[str],
        limit: int = 400,
        persona_id: str = "",
    ) -> tuple[set[int], set[int]]:
        if not names:
            return set(), set()
        marks = ",".join("?" * len(names))
        params: list[Any] = list(names)
        clause = ""
        if persona_id:
            # 跨人格不串：只认本事人格或全局实体的链接。
            clause = " AND (persona_id=? OR persona_id='')"
            params.append(persona_id)
        params.append(int(limit))
        rows = self.query(
            f"SELECT ref, ref_id FROM entities WHERE name IN ({marks}){clause} LIMIT ?",
            params,
        )
        facts = {int(r["ref_id"]) for r in rows if r["ref"] == "fact"}
        events = {int(r["ref_id"]) for r in rows if r["ref"] == "event"}
        return facts, events

    def entities_for_ref(self, ref: str, ref_id: int) -> list[dict[str, str]]:
        rows = self.query(
            "SELECT name, kind FROM entities WHERE ref=? AND ref_id=? ORDER BY kind, name",
            (str(ref or "fact"), int(ref_id)),
        )
        return [{"name": r["name"], "kind": r["kind"]} for r in rows]

    def person_names_in_text(self, text: str, limit: int = 8) -> list[str]:
        norm = normalize_slot(text or "")
        if len(norm) < 2:
            return []
        rows = self.query("SELECT DISTINCT name FROM entities WHERE kind='person' LIMIT 2000")
        hits = [str(r["name"]) for r in rows if len(str(r["name"])) >= 2 and str(r["name"]) in norm]
        hits.sort(key=len, reverse=True)
        return hits[:limit]

    def speaker_ids_by_name(self, name: str) -> list[str]:
        text = (name or "").strip()
        if len(text) < 2:
            return []
        rows = self.query(
            "SELECT DISTINCT speaker_id FROM facts WHERE speaker_name=? "
            "UNION SELECT speaker_id FROM profiles WHERE speaker_name=? LIMIT 20",
            (text, text),
        )
        return [str(r["speaker_id"]) for r in rows if r["speaker_id"]]

    # ------------------------------------------------------------------
    # Time-travel queries (facts valid inside a window, events overlapping it)
    # ------------------------------------------------------------------

    def facts_in_window(
        self,
        start_ts: int,
        end_ts: int,
        speaker_ids: list[str] | None = None,
        persona_id: str = "",
        include_owner: bool = False,
        limit: int = 240,
    ) -> list[Fact]:
        ids = [str(x) for x in (speaker_ids or []) if x]
        clauses = [
            "created_at<=?",
            "(status='live' OR (status IN ('superseded','archived') AND updated_at>=?))",
            "(expires_at=0 OR expires_at>=?)",
        ]
        params: list[Any] = [int(end_ts), int(start_ts), int(start_ts)]
        if persona_id:
            clauses.append("(persona_id=? OR persona_id='')")
            params.append(persona_id)
        if ids:
            marks = ",".join("?" * len(ids))
            speaker_clause = f"speaker_id IN ({marks})"
            params.extend(ids)
            if include_owner:
                speaker_clause = f"({speaker_clause} OR scope='owner')"
            clauses.append(speaker_clause)
        params.append(int(limit))
        sql = f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
        return [self._fact(r) for r in self.query(sql, params)]

    def events_between(
        self,
        start_ts: int,
        end_ts: int,
        speaker_ids: list[str] | None = None,
        persona_id: str = "",
        include_owner: bool = True,
        limit: int = 60,
    ) -> list[Event]:
        return self.live_events(
            speaker_ids=speaker_ids,
            persona_id=persona_id or None,
            limit=limit,
            since_ts=int(start_ts),
            until_ts=int(end_ts),
            include_owner=include_owner,
            statuses=("live", "archived"),
        )

    def events_for_review(self, status: str = "live", limit: int = 120) -> list[Event]:
        if status == "needs_review":
            rows = self.query(
                "SELECT * FROM events WHERE review_status='needs_review' "
                "ORDER BY start_ts DESC, id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self.query(
                "SELECT * FROM events WHERE status=? AND review_status!='needs_review' "
                "ORDER BY start_ts DESC, id DESC LIMIT ?",
                (status, limit),
            )
        return [self._event(r) for r in rows]

    def latest_event_for_window(self, window_tag: str, after_ts: int, before_ts: int) -> Event | None:
        """Most recent auto event in this window closed inside the merge window."""
        if not window_tag:
            return None
        rows = self.query(
            """SELECT * FROM events
               WHERE window_tag=? AND status='live' AND source='pipeline'
                 AND review_status NOT IN ('manual', 'needs_review') AND pinned=0
                 AND end_ts>=? AND end_ts<=?
               ORDER BY end_ts DESC, id DESC LIMIT 1""",
            (window_tag, int(after_ts), int(before_ts)),
        )
        return self._event(rows[0]) if rows else None

    def live_events_oldest(self, limit: int = 300) -> list[Event]:
        rows = self.query(
            "SELECT * FROM events WHERE status='live' AND pinned=0 ORDER BY start_ts ASC LIMIT ?",
            (limit,),
        )
        return [self._event(r) for r in rows]

    def bump_event_access(self, event_id: int) -> None:
        self.execute(
            "UPDATE events SET access_count=access_count+1, last_accessed=? WHERE id=?",
            (now_ts(), int(event_id)),
        )

    def set_event_pinned(self, event_id: int, pinned: bool) -> bool:
        if self.get_event(event_id) is None:
            return False
        self.update_event(event_id, pinned=int(bool(pinned)))
        return True

    def archive_events(self, ids: list[int], reason: str = "ui_delete") -> dict[str, Any]:
        archived: list[int] = []
        missing: list[int] = []
        for raw in ids:
            try:
                eid = int(raw)
            except (TypeError, ValueError):
                continue
            if self.get_event(eid) is None:
                missing.append(eid)
                continue
            self.update_event(eid, status="archived", reason=reason)
            archived.append(eid)
        return {"ok": True, "archived": archived, "missing": missing, "count": len(archived)}

    def restore_events(self, ids: list[int]) -> dict[str, Any]:
        restored: list[int] = []
        missing: list[int] = []
        for raw in ids:
            try:
                eid = int(raw)
            except (TypeError, ValueError):
                continue
            if self.get_event(eid) is None:
                missing.append(eid)
                continue
            self.update_event(eid, status="live")
            restored.append(eid)
        return {"ok": True, "restored": restored, "missing": missing}

    def recent_event_recall_ids(self, window_tag: str, since_ts: int) -> set[int]:
        rows = self.query(
            "SELECT event_id FROM event_recall_log WHERE window_tag=? AND ts>=?",
            (window_tag, int(since_ts)),
        )
        return {int(r["event_id"]) for r in rows}

    def add_event_recall(self, window_tag: str, event_ids: list[int], ts: int) -> None:
        if not window_tag or not event_ids:
            return
        with self._lock:
            self._ensure_conn()
            self._conn.executemany(
                "INSERT INTO event_recall_log(window_tag, event_id, ts) VALUES(?,?,?) "
                "ON CONFLICT(window_tag, event_id) DO UPDATE SET ts=excluded.ts",
                [(window_tag, int(eid), int(ts)) for eid in event_ids],
            )
            self._conn.execute(
                "DELETE FROM event_recall_log WHERE ts < ?", (int(ts) - 7 * 86400,)
            )
            self._conn.commit()

    def _event(self, row: sqlite3.Row) -> Event:
        keys = row.keys()

        def _str_list(raw: Any) -> list[str]:
            items = loads(raw, [])
            if isinstance(items, str):
                items = [items]
            return [str(x) for x in (items or []) if x]

        def _dict_list(raw: Any) -> list[dict[str, Any]]:
            items = loads(raw, [])
            return [dict(x) for x in (items or []) if isinstance(x, dict)]

        def _int_list(raw: Any) -> list[int]:
            items = loads(raw, [])
            if isinstance(items, (int, str)):
                items = [items]
            out: list[int] = []
            for x in items or []:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
            return out
        return Event(
            id=int(row["id"]),
            kind=row["kind"] if "kind" in keys else "life",
            title=row["title"] if "title" in keys else "",
            summary=row["summary"] if "summary" in keys else "",
            speaker_id=row["speaker_id"] if "speaker_id" in keys else "",
            speaker_name=row["speaker_name"] if "speaker_name" in keys else "",
            bot_id=row["bot_id"] if "bot_id" in keys else "",
            window_tag=row["window_tag"] if "window_tag" in keys else "",
            persona_id=row["persona_id"] if "persona_id" in keys else "",
            scope=row["scope"] if "scope" in keys else "",
            participants=_dict_list(row["participants"]) if "participants" in keys else [],
            speaker_ids=_str_list(row["speaker_ids"]) if "speaker_ids" in keys else [],
            highlights=_str_list(row["highlights"]) if "highlights" in keys else [],
            keywords=_str_list(row["keywords"]) if "keywords" in keys else [],
            evidence=_int_list(row["evidence"]) if "evidence" in keys else [],
            start_ts=int(row["start_ts"] or 0) if "start_ts" in keys else 0,
            end_ts=int(row["end_ts"] or 0) if "end_ts" in keys else 0,
            importance=float(row["importance"] or 0) if "importance" in keys else 0.0,
            confidence=float(row["confidence"] or 0) if "confidence" in keys else 0.6,
            status=row["status"] if "status" in keys else "live",
            pinned=int(row["pinned"] or 0) if "pinned" in keys else 0,
            access_count=int(row["access_count"] or 0) if "access_count" in keys else 0,
            last_accessed=int(row["last_accessed"] or 0) if "last_accessed" in keys else 0,
            source=row["source"] if "source" in keys else "",
            review_status=row["review_status"] if "review_status" in keys else "",
            origin=row["origin"] if "origin" in keys else "",
            fingerprint=row["fingerprint"] if "fingerprint" in keys else "",
            reason=row["reason"] if "reason" in keys else "",
            edited_at=int(row["edited_at"] or 0) if "edited_at" in keys else 0,
            edited_by=row["edited_by"] if "edited_by" in keys else "",
            created_at=int(row["created_at"] or 0) if "created_at" in keys else 0,
            updated_at=int(row["updated_at"] or 0) if "updated_at" in keys else 0,
        )

    def referenced_timeline_ids(self) -> set[int]:
        ids: set[int] = set()
        for table in ("facts", "memory_reviews"):
            try:
                rows = self.query(f"SELECT source_event_id FROM {table} WHERE source_event_id>0")
            except sqlite3.OperationalError:
                continue
            for row in rows:
                ids.add(int(row["source_event_id"]))
        try:
            for row in self.query("SELECT evidence FROM events WHERE evidence!='' AND evidence!='[]'"):
                for raw in loads(row["evidence"], []) or []:
                    try:
                        ids.add(int(raw))
                    except (TypeError, ValueError):
                        continue
        except sqlite3.OperationalError:
            pass
        return ids

    def clear_dirty_v280(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in (
            "facts",
            "timeline",
            "pending_overrides",
            "diagnostics",
            "reviews",
            "jargon_stats",
            "usage_ledger",
            "memory_reviews",
            "recall_log",
            "profiles",
            "events",
            "event_recall_log",
        ):
            try:
                cur = self.execute(f"DELETE FROM {table}")
                counts[table] = int(cur.rowcount or 0)
            except sqlite3.OperationalError:
                counts[table] = 0
        for key in (
            "capture_skip",
            "capture_skip_at",
            "notify_last_at",
            "jargon_last_at",
            "persona_draft_last_at",
            "housekeeping_last_at",
        ):
            self.execute("DELETE FROM meta WHERE key=?", (key,))
        self.bump_revision()
        return counts
