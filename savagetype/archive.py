"""Chat transcript parse, JSONL preview/import, sleep compaction.

Historical import is file-only in this version: no QQ API scraping.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store
from .util import clip, detect_domain, fingerprint, loads, now_ts

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
    dest = Path(dest_dir) / f"savagetype-{store.revision()}-{now_ts()}.db"
    store.backup_to(dest)
    return dest


def _as_payload(row: dict[str, Any], drop: set[str]) -> dict[str, Any]:
    payload = {k: row[k] for k in row if k not in drop}
    if "evidence" in payload and isinstance(payload["evidence"], str):
        payload["evidence"] = loads(payload["evidence"], [])
    if "embedding" in payload and isinstance(payload["embedding"], str):
        payload["embedding"] = loads(payload["embedding"], None)
    if "payload" in payload and isinstance(payload["payload"], str):
        payload["payload"] = loads(payload["payload"], {})
    if "keywords" in payload and isinstance(payload["keywords"], str):
        payload["keywords"] = loads(payload["keywords"], [])
    return payload


def import_jsonl(store: Store, path: Path) -> dict[str, Any]:
    inserted = {
        "facts": 0,
        "timeline": 0,
        "reviews": 0,
        "pending": 0,
        "profiles": 0,
        "memory_reviews": 0,
        "aliases": 0,
        "skipped": 0,
        "errors": 0,
    }
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
            elif wrapper == "profiles" or (
                "speaker_id" in row and "seen_count" in row and "first_seen" in row
            ):
                if store.import_profile(row):
                    inserted["profiles"] += 1
                else:
                    inserted["skipped"] += 1
            elif wrapper == "memory_reviews" or (
                "raw_text" in row and "source_event_id" in row and "trace" in row
            ):
                if store.import_memory_review(row):
                    inserted["memory_reviews"] += 1
                else:
                    inserted["skipped"] += 1
            elif wrapper == "aliases" or (
                row.get("alias") and row.get("canonical_id") and "payload" not in row
            ):
                alias = str(row.get("alias") or "").strip()
                canonical = str(row.get("canonical_id") or "").strip()
                if alias and canonical and alias != canonical:
                    store.set_alias(alias, canonical, str(row.get("label") or ""))
                    inserted["aliases"] += 1
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
    keep = store.referenced_timeline_ids()
    rows = store.query(
        "SELECT id FROM timeline WHERE summarized=1 AND ts<? ORDER BY id ASC LIMIT ?",
        (cutoff, max(limit, len(keep) + limit)),
    )
    ids = [int(r["id"]) for r in rows if int(r["id"]) not in keep][:limit]
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    store.execute(f"DELETE FROM timeline WHERE id IN ({q})", ids)
    return len(ids)


def archive_low_value(store: Store, min_age_days: int = 30, max_confidence: float = 0.45, limit: int = 200) -> int:
    cutoff = now_ts() - max(1, min_age_days) * 86400
    rows = store.query(
        """SELECT id FROM facts WHERE status='live' AND pinned=0 AND confidence<=? AND updated_at<?
           AND access_count<=1 AND explicit_correction=0
           ORDER BY confidence ASC, updated_at ASC LIMIT ?""",
        (max_confidence, cutoff, limit),
    )
    n = 0
    for row in rows:
        store.update_fact(int(row["id"]), status="archived", reason="sleep_low_value")
        n += 1
    return n


def archive_decayed(
    store: Store,
    min_age_days: int = 30,
    threshold: float = 0.12,
    half_life_days: float = 30.0,
    reinforce_factor: float = 0.5,
    max_multiplier: float = 3.0,
    limit: int = 200,
) -> int:
    """Archive low-weight live facts (importance decayed past the threshold)."""
    from .util import fact_weight

    if threshold <= 0:
        return 0
    now = now_ts()
    cutoff = now - max(1, min_age_days) * 86400
    live = store.live_oldest(limit=max(limit * 3, 300))
    n = 0
    for fact in live:
        if int(getattr(fact, "pinned", 0)):
            continue
        if int(fact.updated_at or 0) >= cutoff:
            continue
        if fact_weight(fact, now, half_life_days, reinforce_factor, max_multiplier) < threshold:
            store.update_fact(fact.id, status="archived", reason="importance_decayed")
            n += 1
            if n >= limit:
                break
    return n


def prune_jargon_stats(store: Store, min_age_days: int = 30, limit: int = 500) -> int:
    """Drop one-off jargon terms that have not been seen again for a while."""
    cutoff = now_ts() - max(1, min_age_days) * 86400
    rows = store.query(
        "SELECT term FROM jargon_stats WHERE count<=1 AND last_seen<? LIMIT ?",
        (cutoff, limit),
    )
    for row in rows:
        store.drop_jargon_term(str(row["term"]))
    return len(rows)


def expire_pending_overrides(store: Store, max_age_days: int = 30, limit: int = 200) -> int:
    """Close stale open overrides (mostly joke pendings) so the queue cannot grow forever."""
    cutoff = now_ts() - max(1, max_age_days) * 86400
    rows = store.query(
        "SELECT id FROM pending_overrides WHERE status='open' AND created_at<? LIMIT ?",
        (cutoff, limit),
    )
    for row in rows:
        store.set_pending_status(int(row["id"]), "expired")
    return len(rows)


def expire_status_facts(store: Store, limit: int = 200) -> int:
    now = now_ts()
    rows = store.query(
        "SELECT id FROM facts WHERE status='live' AND pinned=0 AND expires_at>0 AND expires_at<? ORDER BY expires_at ASC LIMIT ?",
        (now, limit),
    )
    n = 0
    for row in rows:
        store.update_fact(int(row["id"]), status="archived", reason="status_ttl_expired")
        n += 1
    return n


def _negated_topic(text: str) -> bool:
    from .util import normalize_slot

    t = normalize_slot(text)
    return t.startswith(("不", "没", "别", "非"))


def _extra_negative(fact) -> bool:
    if fact.attribute == "dislikes":
        return True
    from .util import normalize_slot

    blob = normalize_slot(f"{fact.value or ''} {fact.content or ''}")
    return bool(re.search(r"(不喜欢|没喜欢|不再喜欢|不爱|讨厌|受不了)", blob))


def _mentioned_in(fact, topic: str) -> bool:
    """「extra 的正文里明确提到了 like 的主题」——主题词子串匹配。"""
    from .util import normalize_slot

    topic = normalize_slot(topic)
    if not topic:
        return False
    for raw in (fact.value, fact.content):
        text = normalize_slot(raw or "")
        if not text or topic not in text:
            continue
        if len(topic) >= 2 or re.search(r"(喜欢|讨厌|爱喝|爱吃|受不了|习惯|怕)", text):
            return True
    return False


def fold_preference_slots(store: Store) -> int:
    """Merge leftover dislike/note copies of the same topic into likes.

    - 按说话人分组（人格空值=全局，可被任意人格的 likes 吸收）；
    - 主题匹配用子串（「美式」命中「主人喜欢喝美式咖啡（Americano）」）；
    - 正/反偏好并存时不删，交给人工。
    """
    from .util import topic_key

    live = store.facts_by_status("live", limit=400)
    groups: dict[str, list] = {}
    for fact in live:
        groups.setdefault(fact.speaker_id, []).append(fact)
    folded = 0
    for _key, items in groups.items():
        like_items = [f for f in items if f.attribute == "likes"]
        extras = [f for f in items if f.attribute in {"dislikes", "note"}]
        for extra in extras:
            if int(getattr(extra, "pinned", 0)):
                continue
            keeper = None
            for like in like_items:
                if like.persona_id and extra.persona_id and like.persona_id != extra.persona_id:
                    # 不同人格的档案互不合并；全局条目可以被任意人格吸收。
                    continue
                if not _mentioned_in(extra, topic_key(like.value)):
                    continue
                extra_domain = getattr(extra, "topic", "") or detect_domain(
                    extra.content or "", like.value
                )
                if like.topic and extra_domain and like.topic != extra_domain:
                    # 同词不同义（美式饮品 vs 美式穿搭）：不折叠。
                    continue
                keeper = like
                break
            if keeper is None:
                continue
            if _negated_topic(keeper.value) != _extra_negative(extra):
                # 正/反偏好并存是真实矛盾：宁可留着两条，也不在维护里静默删掉可能更新的那条。
                continue
            store.delete_fact(extra.id)
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
