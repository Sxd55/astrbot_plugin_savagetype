"""Fact extraction from timeline. Heuristic first; optional LLM JSON extract."""

from __future__ import annotations

import re
from typing import Any, Callable, Awaitable

from .contradiction import ContradictionEngine, looks_correction, looks_first_person
from .models import TimelineEvent
from .slots import apply_slot
from .store import Store
from .util import PREF_PATTERNS, clip, fingerprint, now_ts, safe_json_extract

EXTRACT_PROMPT = """你是记忆整理器。只从对话里抽取稳定事实，不要文风、不要黑话、不要新人格。
输出 JSON 数组，每项字段：
subject, attribute, value, content, confidence(0-1), first_person(bool), explicit_correction(bool), mention_policy(mention|tone|uncertain)
规则：
- attribute 只能是：likes, dislikes, name, identity, habit, promise, note
- subject：当前说话人自己的事实用 self；Bot 自己用 bot；其他人用稳定名字。
- 只记偏好、称呼、约定、身份、习惯、明确纠正。
- 玩笑、反话、转述、一次性情绪不要写成稳定事实。
- 没有稳定事实就输出 []。
对话：
{dialog}
"""


class Extractor:
    def __init__(
        self,
        store: Store,
        contradiction: ContradictionEngine,
        llm: Callable[..., Awaitable[str]] | None = None,
    ):
        self.store = store
        self.contradiction = contradiction
        self.llm = llm

    async def maybe_extract(self, min_messages: int = 8, force: bool = False) -> dict[str, Any]:
        events = self.store.unsummarized(limit=max(min_messages, 12) if not force else 80)
        if not events:
            return {"ok": True, "skipped": True, "unsummarized": 0}
        if not force and len(events) < min_messages:
            return {"ok": True, "skipped": True, "unsummarized": len(events)}
        heuristic = self.extract_heuristic(events)
        llm_facts: list[dict[str, Any]] = []
        llm_failed = False
        if self.llm is not None:
            try:
                llm_facts = await self.extract_llm(events)
            except Exception as exc:  # noqa: BLE001
                llm_failed = True
                llm_facts = []
                self.store.add_diag("extract_llm_fail", {"error": str(exc)})
        written = []
        for payload in heuristic + llm_facts:
            result = self.contradiction.ingest(payload, source_text=payload.get("content", ""))
            written.append(result)
        if not llm_failed:
            self.store.mark_summarized([e.id for e in events])
        return {
            "ok": True,
            "skipped": False,
            "events": len(events),
            "heuristic": len(heuristic),
            "llm": len(llm_facts),
            "writes": written,
        }

    def extract_heuristic(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.role != "user":
                continue
            text = (ev.content or "").strip()
            if len(text) < 2:
                continue
            for regex, attr in PREF_PATTERNS:
                match = regex.search(text)
                if not match:
                    continue
                value = clip(match.group(1), 40)
                if not value:
                    continue
                if attr == "likes" and re.search(r"(不喜欢|没喜欢|现在不喜欢|不再喜欢)", match.group(0)):
                    value = "不" + value if not value.startswith("不") else value
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute=attr,
                    value=value,
                    content=clip(text, 120),
                    confidence=0.72 if looks_first_person(text) else 0.45,
                )
                if looks_correction(text):
                    payload["explicit_correction"] = 1
                out.append(payload)
                break
        return out

    async def extract_llm(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        if self.llm is None:
            return []
        lines = []
        for ev in events:
            who = ev.speaker_name or ev.speaker_id or ev.role
            lines.append(f"[{ev.id}] {ev.role}/{who}: {clip(ev.content, 200)}")
        raw = await self.llm(EXTRACT_PROMPT.format(dialog="\n".join(lines)))
        parsed = safe_json_extract(raw) or []
        if not isinstance(parsed, list):
            return []
        by_id = {e.id: e for e in events}
        out: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            attribute = str(item.get("attribute") or "").strip()
            value = str(item.get("value") or "").strip()
            if not subject or not attribute or not value:
                continue
            evidence_ids = []
            for ev in events:
                if value in ev.content or attribute in ev.content:
                    evidence_ids.append(ev.id)
            speaker = events[-1]
            if evidence_ids and evidence_ids[0] in by_id:
                speaker = by_id[evidence_ids[0]]
            out.append(
                self._payload(
                    speaker,
                    subject=clip(subject, 40),
                    attribute=clip(attribute, 40),
                    value=clip(value, 80),
                    content=clip(str(item.get("content") or f"{subject} {attribute} {value}"), 160),
                    confidence=float(item.get("confidence") or 0.6),
                    extra={
                        "first_person": int(bool(item.get("first_person"))),
                        "explicit_correction": int(bool(item.get("explicit_correction"))),
                        "mention_policy": item.get("mention_policy") or "mention",
                        "evidence": evidence_ids[:6],
                        "source": "llm",
                        "persona_id": getattr(speaker, "persona_id", "") or "",
                    },
                )
            )
        return out

    def _payload(
        self,
        ev: TimelineEvent,
        subject: str,
        attribute: str,
        value: str,
        content: str,
        confidence: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "subject": subject,
            "attribute": attribute,
            "value": value,
            "content": content,
            "speaker_id": ev.speaker_id,
            "speaker_name": ev.speaker_name,
            "bot_id": ev.bot_id,
            "window_tag": ev.window_tag,
            "persona_id": getattr(ev, "persona_id", "") or "",
            "confidence": confidence,
            "evidence": [ev.id],
            "first_person": int(looks_first_person(ev.content)),
            "explicit_correction": int(looks_correction(ev.content)),
            "source": "heuristic",
            "created_at": now_ts(),
            "fingerprint": fingerprint(ev.speaker_id, subject, attribute, value),
        }
        if extra:
            payload.update(extra)
        return apply_slot(payload)
