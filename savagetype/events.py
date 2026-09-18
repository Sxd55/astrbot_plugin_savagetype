"""Episodic memory: segment the timeline into episodes and summarize whole events.

A fact answers "who is this person"; an event answers "what happened".
Episodes close after a quiet gap (or a hard max span); each closed episode that
carries something worth recalling becomes one append-only event card. Events
never overwrite each other — that is the whole point of this layer.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from .models import Event, TimelineEvent
from .llm import LLMBudgetExceeded
from .store import Store
from .util import (
    LOW_INFO_RE,
    ORIGIN_QQ,
    REVIEW_AI_PASSED,
    REVIEW_NEEDS,
    ROLE_ASSISTANT,
    ROLE_BOT_ID,
    ROLE_USER,
    SCOPE_OWNER,
    SCOPE_PERSON,
    clip,
    fingerprint,
    fmt_ts,
    now_ts,
    safe_json_extract,
)

# 叙事线索：出现这些词说明这一段在讲「发生了什么事」，值得成段。
EPISODE_CUE_RE = re.compile(
    r"(去了|到了|见了|见到|碰到|参加|出差|旅游|旅行|搬家|买房|买车|买了|"
    r"辞职|入职|面试|考试|挂科|毕业|住院|看病|手术|结婚|订婚|分手|离婚|"
    r"生了|怀孕|领养|养了|报名|预约|约好|答应|决定|打算|计划|完成|办完|"
    r"搞定|签了|开了|开始学|放弃了|聚会|聚餐|出去玩|过生日|过年|回家|回老家|加班|"
    r"改口|纠正|记错|说错)"
)

EPISODE_MAX_MESSAGES = 40
EPISODE_MAX_CHARS = 6000

SUMMARIZE_PROMPT = """你是记忆整理器。下面是一段连续对话（带事件 id、说话人和时间）。
请把它整理成「一件事」的摘要，供以后回忆这段经历或这次聊天使用。
要求：
1) kind：用户讲了自己生活里真实发生的事（经历、计划、决定、见闻）填 life；只是聊了某个话题、没有具体发生的事填 talk。
2) title：不超过 12 个字的标题（如「成都三日游」「聊考研」）。
3) summary：80-160 字，只复述原文出现的信息，按时间顺序写清发生了什么、结果如何。不推理、不评价、不补充背景。
4) highlights：2-4 条短要点，每条不超过 30 字；没有就留空数组。
5) keywords：2-6 个关键词（人名、地点、事物、主题）。
6) importance：0-1，越可能是以后想回忆起的事越高（普通闲聊 0.3，重要经历或决定 0.8）。
7) confidence：0-1，对复述忠实度的自评；有拿不准的地方就降低，并在 summary 里保留原文的不确定词。
只输出 JSON 对象，不要解释。字段：kind, title, summary, highlights, keywords, importance, confidence。
对话开始 {start}，结束 {end}，参与者：{participants}
消息：
{lines}
"""

SUMMARIZE_RETRY_PROMPT = """上一版整理结果没通过审核，原因：{reason}
请按原因修改后重新输出 JSON 对象（字段不变，只复述原文有的信息）。
原始对话：
{lines}
上一版：
{previous}
"""

VERIFY_PROMPT = """你是记忆审核员。下面是一段原始对话和根据它写出的事件摘要。
判断摘要是否忠实：有没有添加原文没有的信息（时间、地点、人名、结果）、把推测写成事实、把别人的事算到说话人头上。
只输出 JSON 对象：{{"pass": true 或 false, "reason": "简短中文原因", "fix_hint": "不通过时怎么改"}}
原始对话：
{lines}
事件摘要：
{summary}
"""


def episode_lines(episode: list[TimelineEvent], limit: int = 300) -> str:
    lines = []
    for ev in episode:
        who = ev.speaker_name or ev.speaker_id or ("bot" if ev.role == ROLE_ASSISTANT else "?")
        role = "bot" if ev.role == ROLE_ASSISTANT else "user"
        lines.append(f"[{ev.id}] {fmt_ts(ev.ts)} {who}({role}): {clip(ev.content or '', limit)}")
    return "\n".join(lines)


def split_episodes(
    rows: list[TimelineEvent],
    gap_seconds: int,
    max_span_seconds: int,
) -> list[list[TimelineEvent]]:
    """Group messages into episodes: same window, quiet gap, bounded span."""
    groups: dict[tuple[str, str], list[TimelineEvent]] = {}
    for ev in rows:
        groups.setdefault((ev.window_tag or "", ev.persona_id or ""), []).append(ev)
    episodes: list[list[TimelineEvent]] = []
    for items in groups.values():
        items.sort(key=lambda e: (e.ts, e.id))
        current: list[TimelineEvent] = []
        for ev in items:
            if not current:
                current = [ev]
                continue
            prev = current[-1]
            too_long = int(ev.ts) - int(current[0].ts) > max_span_seconds
            if int(ev.ts) - int(prev.ts) > gap_seconds or too_long:
                episodes.append(current)
                current = [ev]
            else:
                current.append(ev)
        if current:
            episodes.append(current)
    episodes.sort(key=lambda ep: (ep[0].ts, ep[0].id))
    return episodes


def chunk_episode(
    episode: list[TimelineEvent],
    max_messages: int = EPISODE_MAX_MESSAGES,
    max_chars: int = EPISODE_MAX_CHARS,
) -> list[list[TimelineEvent]]:
    chunks: list[list[TimelineEvent]] = []
    current: list[TimelineEvent] = []
    chars = 0
    for ev in episode:
        size = len(ev.content or "")
        if current and (len(current) >= max_messages or chars + size > max_chars):
            chunks.append(current)
            current, chars = [], 0
        current.append(ev)
        chars += size
    if current:
        chunks.append(current)
    return chunks or [episode]


def episode_worthy(episode: list[TimelineEvent], min_messages: int) -> bool:
    """Skip pure small talk: enough user messages, or at least one narrative cue."""
    users = [e for e in episode if e.role == ROLE_USER and e.speaker_id != ROLE_BOT_ID]
    if not users:
        return False
    if all(LOW_INFO_RE.match((e.content or "").strip()) for e in users):
        return False
    if len(users) >= max(1, min_messages):
        return True
    return any(EPISODE_CUE_RE.search(e.content or "") for e in users)


def episode_participants(episode: list[TimelineEvent]) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for ev in episode:
        sid = str(ev.speaker_id or "")
        if not sid or sid == ROLE_BOT_ID or ev.role == ROLE_ASSISTANT:
            sid = ROLE_BOT_ID
            name = ev.speaker_name or "bot"
            role = "assistant"
        else:
            name = ev.speaker_name or sid
            role = "user"
        if sid in seen:
            continue
        seen[sid] = {"id": sid, "name": name, "role": role}
    return list(seen.values())


def episode_speaker_ids(episode: list[TimelineEvent]) -> list[str]:
    out: list[str] = []
    for ev in episode:
        if ev.role == ROLE_ASSISTANT or ev.speaker_id == ROLE_BOT_ID:
            continue
        sid = str(ev.speaker_id or "")
        if sid and sid not in out:
            out.append(sid)
    return out


def narrative_kind(episode: list[TimelineEvent]) -> str:
    for ev in episode:
        if ev.role == ROLE_USER and EPISODE_CUE_RE.search(ev.content or ""):
            return "life"
    return "talk"


class EventPipeline:
    """Deterministic segmentation + LLM summary with verify, never silently drops."""

    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        logger: Any,
        llm: Callable[[str], Awaitable[str]] | None,
        verify_llm: Callable[[str], Awaitable[str]] | None = None,
        is_owner_speaker: Callable[[str], bool] | None = None,
    ):
        self.store = store
        self.config = config
        self.logger = logger
        self.llm = llm
        self.verify_llm = verify_llm or llm
        self.is_owner_speaker = is_owner_speaker or (lambda _sid: False)
        self._lock = asyncio.Lock()

    def _cfg_int(self, key: str, default: int) -> int:
        raw = self.config.get(key)
        try:
            return default if raw is None else int(raw)
        except (TypeError, ValueError):
            return default

    def enabled(self) -> bool:
        return bool(self.config.get("event_enabled", True))

    def gap_seconds(self) -> int:
        return max(60, self._cfg_int("event_gap_minutes", 45) * 60)

    def max_span_seconds(self) -> int:
        return max(self.gap_seconds(), self._cfg_int("event_max_hours", 6) * 3600)

    def merge_seconds(self) -> int:
        return max(0, self._cfg_int("event_merge_minutes", 120) * 60)

    def min_messages(self) -> int:
        return max(1, self._cfg_int("event_min_messages", 4))

    def max_per_run(self) -> int:
        return max(1, self._cfg_int("event_max_per_run", 2))

    async def run(self, force: bool = False) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": True, "skipped": True, "reason": "event_disabled"}
        now = now_ts()
        cutoff = now if force else now - self.gap_seconds()
        cursor = int(self.store.get_meta("event_cursor") or "0")
        rows = self.store.timeline_after(cursor, cutoff, limit=400)
        if not rows:
            return {"ok": True, "skipped": True, "events": 0, "cursor": cursor}
        if self._lock.locked():
            return {"ok": True, "skipped": True, "reason": "busy"}
        async with self._lock:
            return await self._process(rows, cursor)

    async def _process(self, rows: list[TimelineEvent], cursor: int) -> dict[str, Any]:
        episodes = split_episodes(rows, self.gap_seconds(), self.max_span_seconds())
        min_messages = self.min_messages()
        budget = self.max_per_run()
        processed = 0
        created = 0
        extended = 0
        skipped = 0
        failed = 0
        new_cursor = cursor
        for episode in episodes:
            last_id = max(int(e.id) for e in episode)
            if not episode_worthy(episode, min_messages):
                skipped += 1
                new_cursor = last_id
                continue
            if processed >= budget:
                break
            try:
                chunks = chunk_episode(episode)
                previous = self._merge_target(episode)
                for index, chunk in enumerate(chunks):
                    target = previous if index == 0 else None
                    if target is not None:
                        await self._extend(target, chunk)
                        extended += 1
                    else:
                        await self._write(chunk)
                        created += 1
                processed += 1
                new_cursor = last_id
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.store.add_diag("event_fail", {"error": str(exc)[:200], "ids": [e.id for e in episode][:8]})
                if self.logger:
                    self.logger.warning("Savage Type event summary failed: %s", exc)
                # 停在这一段之前，下次重试；不跳过后续消息，避免丢事件。
                break
        self.store.set_meta("event_cursor", str(new_cursor))
        result = {
            "ok": True,
            "skipped": False,
            "episodes": len(episodes),
            "created": created,
            "extended": extended,
            "dropped": skipped,
            "failed": failed,
            "cursor": new_cursor,
        }
        self.store.add_diag("event_pipeline", result)
        return result

    def _merge_target(self, episode: list[TimelineEvent]) -> Event | None:
        """Recently closed auto event in the same window: continue it instead of fragmenting."""
        merge = self.merge_seconds()
        if merge <= 0:
            return None
        window = episode[0].window_tag or ""
        if not window:
            return None
        start = int(episode[0].ts)
        target = self.store.latest_event_for_window(window, start - merge, start)
        if target is None:
            return None
        end = int(episode[-1].ts)
        if end - int(target.start_ts or 0) > self.max_span_seconds():
            return None
        return target

    def _base_payload(self, episode: list[TimelineEvent]) -> dict[str, Any]:
        speakers = episode_speaker_ids(episode)
        scope = SCOPE_OWNER if speakers and all(self.is_owner_speaker(s) for s in speakers) else SCOPE_PERSON
        first = episode[0]
        return {
            "speaker_id": speakers[0] if speakers else ROLE_BOT_ID,
            "speaker_name": first.speaker_name or first.speaker_id,
            "bot_id": first.bot_id or "",
            "window_tag": first.window_tag or "",
            "persona_id": first.persona_id or "",
            "scope": scope,
            "participants": episode_participants(episode),
            "speaker_ids": speakers,
            "evidence": [int(e.id) for e in episode],
            "start_ts": int(first.ts),
            "end_ts": int(episode[-1].ts),
            "source": "pipeline",
            "origin": ORIGIN_QQ,
        }

    async def _write(self, episode: list[TimelineEvent]) -> int:
        payload = self._base_payload(episode)
        summary = await self._finalize(episode)
        payload.update(summary)
        payload["fingerprint"] = fingerprint(
            payload.get("persona_id"),
            payload.get("window_tag"),
            payload.get("start_ts"),
            payload.get("title"),
        )
        return self.store.add_event(payload)

    async def _extend(self, target: Event, episode: list[TimelineEvent]) -> int:
        summary = await self._finalize(episode, previous=target)
        evidence = list(target.evidence or [])
        for eid in [int(e.id) for e in episode]:
            if eid not in evidence:
                evidence.append(eid)
        speakers = list(target.speaker_ids or [])
        for sid in episode_speaker_ids(episode):
            if sid not in speakers:
                speakers.append(sid)
        participants = list(target.participants or [])
        known = {str(p.get("id") or "") for p in participants}
        for item in episode_participants(episode):
            if str(item.get("id") or "") not in known:
                participants.append(item)
        self.store.update_event(
            target.id,
            title=summary["title"],
            summary=summary["summary"],
            highlights=summary["highlights"],
            keywords=summary["keywords"],
            importance=max(float(target.importance or 0), float(summary["importance"])),
            confidence=summary["confidence"],
            review_status=summary["review_status"],
            evidence=evidence,
            speaker_ids=speakers,
            participants=participants,
            end_ts=int(episode[-1].ts),
        )
        return target.id

    async def _finalize(self, episode: list[TimelineEvent], previous: Event | None = None) -> dict[str, Any]:
        """LLM summary + verify + one retry; deterministic fallback when unavailable."""
        fallback = self._fallback_summary(episode)
        if self.llm is None:
            return fallback
        previous_block = ""
        if previous is not None:
            previous_block = (
                f"同一件事已有摘要（{fmt_ts(previous.start_ts)} 起）：{previous.summary}\n"
                "请把已有摘要和下面新增的对话合并，输出更新后的完整事件摘要。\n"
            )
        lines = previous_block + episode_lines(episode)
        participants = "、".join(
            f"{p.get('name') or p.get('id')}" for p in episode_participants(episode)
        )
        try:
            prompt = SUMMARIZE_PROMPT.format(
                start=fmt_ts(episode[0].ts),
                end=fmt_ts(episode[-1].ts),
                participants=participants or "未知",
                lines=lines,
            )
            draft = self._parse(await self.llm(prompt))
        except LLMBudgetExceeded:
            # 预算闸：整段抛给上层，游标不动，等额度恢复后重试同一段。
            raise
        except Exception as exc:  # noqa: BLE001
            self.store.add_diag(
                "event_summary_fail",
                {"error": str(exc)[:200], "ids": [e.id for e in episode][:8]},
            )
            return fallback

        verdict = await self._verify(lines, draft)
        if not verdict.get("pass"):
            try:
                retry_prompt = SUMMARIZE_RETRY_PROMPT.format(
                    reason=verdict.get("fix_hint") or verdict.get("reason") or "不够忠实",
                    lines=lines,
                    previous=json_dumps_short(draft),
                )
                revised = self._parse(await self.llm(retry_prompt))
                if revised:
                    draft = revised
                    verdict = await self._verify(lines, draft)
            except Exception:  # noqa: BLE001
                pass

        merged = {
            "kind": draft.get("kind") or fallback["kind"],
            "title": draft.get("title") or fallback["title"],
            "summary": draft.get("summary") or fallback["summary"],
            "highlights": draft.get("highlights") or [],
            "keywords": draft.get("keywords") or [],
            "importance": draft.get("importance"),
            "confidence": draft.get("confidence"),
            "review_status": REVIEW_AI_PASSED if verdict.get("pass") else REVIEW_NEEDS,
        }
        normalized = self._normalize_payload(merged, fallback)
        if not verdict.get("pass"):
            normalized["confidence"] = min(normalized["confidence"], 0.4)
            self.store.add_diag(
                "event_needs_review",
                {"reason": str(verdict.get("reason") or "")[:120], "title": normalized["title"]},
            )
        return normalized

    async def _verify(self, lines: str, draft: dict[str, Any]) -> dict[str, Any]:
        if self.verify_llm is None or not draft:
            return {"pass": True, "reason": "", "fix_hint": ""}
        summary = f"{draft.get('title') or ''}\n{draft.get('summary') or ''}\n" + "\n".join(
            str(h) for h in (draft.get("highlights") or [])
        )
        try:
            raw = await self.verify_llm(VERIFY_PROMPT.format(lines=lines, summary=summary))
        except Exception:  # noqa: BLE001
            return {"pass": True, "reason": "verify_unavailable", "fix_hint": ""}
        parsed = safe_json_extract(raw)
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if not isinstance(parsed, dict) or "pass" not in parsed:
            return {"pass": True, "reason": "verify_unparsed", "fix_hint": ""}
        return {
            "pass": bool(parsed.get("pass")),
            "reason": str(parsed.get("reason") or ""),
            "fix_hint": str(parsed.get("fix_hint") or ""),
        }

    def _parse(self, raw: Any) -> dict[str, Any]:
        parsed = safe_json_extract(raw if isinstance(raw, str) else str(raw or ""))
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            raise ValueError("event summary unparseable")
        return parsed

    def _normalize_payload(self, draft: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        kind = str(draft.get("kind") or "").strip().lower()
        if kind not in {"life", "talk"}:
            kind = fallback["kind"]
        raw_highlights = draft.get("highlights") or []
        if isinstance(raw_highlights, str):
            raw_highlights = [raw_highlights]
        highlights: list[str] = []
        for item in raw_highlights:
            text = clip(str(item or ""), 40)
            if text and text not in highlights:
                highlights.append(text)
        raw_keywords = draft.get("keywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        keywords: list[str] = []
        for item in raw_keywords:
            text = clip(str(item or ""), 16)
            if text and text not in keywords:
                keywords.append(text)
        return {
            "kind": kind,
            "title": clip(str(draft.get("title") or ""), 40) or fallback["title"],
            "summary": clip(str(draft.get("summary") or ""), 240) or fallback["summary"],
            "highlights": highlights[:4],
            "keywords": keywords[:6],
            "importance": _safe_rate(draft.get("importance"), float(fallback.get("importance") or 0.5)),
            "confidence": _safe_rate(draft.get("confidence"), float(fallback.get("confidence") or 0.5)),
            "review_status": str(draft.get("review_status") or fallback["review_status"]),
        }

    def _fallback_summary(self, episode: list[TimelineEvent]) -> dict[str, Any]:
        """No model / model output unusable: keep a deterministic recap, mark for review."""
        if not episode:
            return {
                "kind": "chat",
                "title": "一段对话",
                "summary": "一段对话",
                "highlights": [],
                "keywords": [],
                "importance": 0.5,
                "confidence": 0.3,
            }
        users = [e for e in episode if e.role == ROLE_USER and e.speaker_id != ROLE_BOT_ID]
        title = clip((users[0].content if users else episode[0].content) or "一段对话", 20)
        parts = [clip(e.content or "", 80) for e in users[-4:]]
        summary = clip(" / ".join(parts), 200) or title
        return {
            "kind": narrative_kind(episode),
            "title": title,
            "summary": summary,
            "highlights": [],
            "keywords": [],
            "importance": 0.5,
            "confidence": 0.3,
            "review_status": REVIEW_NEEDS,
        }


def json_dumps_short(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)[:1200]


def _safe_rate(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
