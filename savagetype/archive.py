"""Chat transcript parse, JSONL preview/import, sleep compaction.

Historical import is file-only in this version: no QQ API scraping.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store
from .util import clip, fingerprint, loads, now_ts

HEAD_RE = re.compile(
    r"^(?P<name>.+?)[:：]\s*(?P<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|\d{1,2}-\d{1,2}[ T]\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2})$"
)
FIELD_SENDER_RE = re.compile(r"^(?:发送者|说话人|昵称)[:：]\s*(.+)$")
FIELD_TIME_RE = re.compile(r"^(?:时间|日期)[:：]\s*(.+)$")
FIELD_CONTENT_RE = re.compile(r"^(?:内容|消息)[:：]\s*(.*)$")
SKIP_NAMES = {"时间", "内容", "消息", "消息id", "消息 ID", "msgid"}


def parse_time(text: str, year_hint: int | None = None) -> int:
    raw = (text or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except ValueError:
            continue
    if year_hint:
        for fmt in ("%m-%d %H:%M:%S", "%m-%d %H:%M", "%m-%d"):
            try:
                return int(datetime.strptime(f"{year_hint}-{raw}", f"%Y-{fmt}").timestamp())
            except ValueError:
                continue
    return now_ts()


def parse_transcript(text: str, *, user_names: list[str] | None = None, bot_names: list[str] | None = None, year_hint: int | None = None) -> dict[str, Any]:
    """Parse QQ-style or field-style chat export into timeline payloads."""
    user_names = [n.strip() for n in (user_names or []) if n.strip()]
    bot_names = [n.strip() for n in (bot_names or []) if n.strip()]
    lines = (text or "").replace("\r\n", "\n").split("\n")
    events: list[dict[str, Any]] = []
    speakers: dict[str, int] = {}
    year = year_hint or datetime.now().year

    def role_for(name: str) -> str:
        if name in bot_names:
            return "assistant"
        if name in user_names:
            return "user"
        if bot_names and not user_names:
            return "user" if name not in bot_names else "assistant"
        if user_names and not bot_names:
            return "assistant" if name not in user_names else "user"
        return "user"

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        sender_m = FIELD_SENDER_RE.match(line)
        if sender_m:
            name = sender_m.group(1).strip()
            ts_text = ""
            body_lines: list[str] = []
            i += 1
            while i < len(lines):
                cur = lines[i]
                if FIELD_SENDER_RE.match(cur.strip()) and body_lines:
                    break
                time_m = FIELD_TIME_RE.match(cur.strip())
                content_m = FIELD_CONTENT_RE.match(cur.strip())
                if time_m:
                    ts_text = time_m.group(1).strip()
                elif content_m:
                    body_lines.append(content_m.group(1))
                elif body_lines:
                    if not cur.strip():
                        break
                    body_lines.append(cur)
                i += 1
            body = "\n".join(body_lines).strip()
            if name and name not in SKIP_NAMES and body:
                speakers[name] = speakers.get(name, 0) + 1
                ts = parse_time(ts_text, year)
                events.append(_event(name, body, ts, role_for(name)))
            continue
        head = HEAD_RE.match(line)
        if head:
            name = head.group("name").strip()
            if name in SKIP_NAMES:
                i += 1
                continue
            ts = parse_time(head.group("time"), year)
            body_lines = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if HEAD_RE.match(nxt.strip()) or FIELD_SENDER_RE.match(nxt.strip()):
                    break
                if nxt.strip() or body_lines:
                    body_lines.append(nxt)
                if not nxt.strip() and body_lines:
                    i += 1
                    break
                i += 1
            body = "\n".join(body_lines).strip()
            if name and body:
                speakers[name] = speakers.get(name, 0) + 1
                events.append(_event(name, body, ts, role_for(name)))
            continue
        i += 1

    return {
        "ok": True,
        "count": len(events),
        "speakers": speakers,
        "events": events,
        "truncated": False,
    }


def _event(name: str, body: str, ts: int, role: str) -> dict[str, Any]:
    content = clip(body, 2000)
    speaker_id = "bot_self" if role == "assistant" else name
    return {
        "ts": ts,
        "speaker_id": speaker_id,
        "speaker_name": name,
        "bot_id": "",
        "window_tag": "import",
        "role": role,
        "content": content,
        "persona_id": "",
        "fingerprint": fingerprint("import", speaker_id, ts, content),
    }


def preview_jsonl(path: Path, limit: int = 8) -> dict[str, Any]:
    counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    errors = 0
    total = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            kind = str(row.get("table") or row.get("review_kind") or row.get("kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1
            if len(samples) < limit:
                samples.append({"kind": kind, "id": row.get("id"), "content": clip(str(row.get("content") or row.get("title") or ""), 80)})
    return {"ok": True, "lines": total, "errors": errors, "counts": counts, "samples": samples}


def backup_db(store: Store, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"savagetype-{store.revision()}-{now_ts()}.db"
    shutil.copy2(store.db_path, dest)
    return dest


def _as_payload(row: dict[str, Any], drop: set[str]) -> dict[str, Any]:
    payload = {k: row[k] for k in row if k not in drop}
    if "evidence" in payload and isinstance(payload["evidence"], str):
        payload["evidence"] = loads(payload["evidence"], [])
    if "embedding" in payload and isinstance(payload["embedding"], str):
        payload["embedding"] = loads(payload["embedding"], None)
    if "payload" in payload and isinstance(payload["payload"], str):
        payload["payload"] = loads(payload["payload"], {})
    return payload


def import_jsonl(store: Store, path: Path) -> dict[str, Any]:
    inserted = {"facts": 0, "timeline": 0, "reviews": 0, "pending": 0, "skipped": 0, "errors": 0}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                inserted["errors"] += 1
                continue
            wrapper = str(row.get("table") or row.get("kind") or "")
            review_kind = str(row.get("review_kind") or "")
            if wrapper == "facts" or (row.get("fingerprint") and row.get("subject") and row.get("attribute") and row.get("value") is not None):
                fp = str(row.get("fingerprint") or "")
                if fp and store.fact_by_fingerprint(fp):
                    inserted["skipped"] += 1
                    continue
                payload = _as_payload(row, {"kind", "id", "review_kind", "table"})
                store.add_fact(payload)
                inserted["facts"] += 1
            elif wrapper == "timeline" or (row.get("role") and row.get("content") and "ts" in row and "payload" not in row):
                event = _as_payload(row, {"kind", "id", "summarized", "review_kind", "table"})
                event.setdefault(
                    "fingerprint",
                    fingerprint(event.get("speaker_id"), event.get("ts"), event.get("content")),
                )
                if store.add_timeline(event):
                    inserted["timeline"] += 1
                else:
                    inserted["skipped"] += 1
            elif wrapper == "reviews" or review_kind or (row.get("fingerprint") and row.get("payload") is not None and row.get("status")):
                actual = review_kind or (wrapper if wrapper in {"jargon", "fewshot", "persona"} else "")
                if not actual:
                    inserted["skipped"] += 1
                    continue
                payload = row.get("payload")
                if isinstance(payload, str):
                    payload = loads(payload, {})
                store.upsert_review(
                    actual,
                    str(row.get("fingerprint") or ""),
                    str(row.get("title") or ""),
                    payload or {},
                    str(row.get("reason") or "import"),
                    str(row.get("speaker_id") or ""),
                    str(row.get("persona_id") or ""),
                )
                inserted["reviews"] += 1
            else:
                inserted["skipped"] += 1
    return {"ok": True, **inserted}


def import_transcript_events(store: Store, events: list[dict[str, Any]]) -> dict[str, Any]:
    added = 0
    skipped = 0
    for ev in events:
        if store.add_timeline(ev):
            added += 1
        else:
            skipped += 1
    return {"ok": True, "added": added, "skipped": skipped, "count": len(events)}


def compact_summarized_timeline(store: Store, retain_days: int = 30, limit: int = 2000) -> int:
    cutoff = now_ts() - max(1, retain_days) * 86400
    rows = store.query(
        "SELECT id FROM timeline WHERE summarized=1 AND ts<? ORDER BY id ASC LIMIT ?",
        (cutoff, limit),
    )
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    store.execute(f"DELETE FROM timeline WHERE id IN ({q})", ids)
    return len(ids)


def archive_low_value(store: Store, min_age_days: int = 30, max_confidence: float = 0.45, limit: int = 200) -> int:
    cutoff = now_ts() - max(1, min_age_days) * 86400
    rows = store.query(
        """SELECT id FROM facts WHERE status='live' AND confidence<=? AND updated_at<?
           AND access_count<=1 AND explicit_correction=0
           ORDER BY confidence ASC, updated_at ASC LIMIT ?""",
        (max_confidence, cutoff, limit),
    )
    n = 0
    for row in rows:
        store.update_fact(int(row["id"]), status="archived", reason="sleep_low_value")
        n += 1
    return n


def _topic_key(text: str) -> str:
    from .util import normalize_slot

    t = normalize_slot(text)
    for prefix in ("不", "没", "别", "非"):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    t = t.replace("听", "").replace("喝", "").replace("吃", "")
    return t[:24]


def _topics(fact) -> set[str]:
    keys = set()
    for raw in (fact.value, fact.content):
        key = _topic_key(raw or "")
        if key:
            keys.add(key)
            if len(key) >= 4:
                keys.add(key[:6])
                keys.add(key[-6:])
    return {k for k in keys if len(k) >= 3}


def fold_preference_slots(store: Store) -> int:
    """Merge leftover dislike/note copies of the same topic into likes."""
    live = store.facts_by_status("live", limit=400)
    groups: dict[tuple[str, str], list] = {}
    for fact in live:
        groups.setdefault((fact.speaker_id, fact.persona_id or ""), []).append(fact)
    folded = 0
    for _key, items in groups.items():
        like_items = [f for f in items if f.attribute == "likes"]
        extras = [f for f in items if f.attribute in {"dislikes", "note"}]
        for extra in extras:
            extra_topics = _topics(extra)
            keeper = None
            for like in like_items:
                if extra_topics & _topics(like):
                    keeper = like
                    break
            if keeper is None:
                continue
            store.update_fact(
                extra.id,
                status="superseded",
                superseded_by=keeper.id,
                reason="sleep_fold_preference",
            )
            folded += 1
    return folded


def expire_persona_drafts(store: Store, ttl_seconds: int = 14 * 86400) -> int:
    cutoff = now_ts() - max(1, ttl_seconds)
    rows = store.query(
        "SELECT id, payload, created_at FROM reviews WHERE kind='persona' AND status='approved'"
    )
    n = 0
    for row in rows:
        payload = loads(row["payload"], {})
        created = int(payload.get("created_at") or row["created_at"] or 0)
        if created and created < cutoff:
            store.set_review_status(int(row["id"]), "archived")
            n += 1
    return n
