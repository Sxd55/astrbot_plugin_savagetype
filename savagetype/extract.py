"""Fact extraction from timeline. Evidence-bound; heuristic is the no-model fallback."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .contradiction import ContradictionEngine, looks_correction, looks_first_person
from .models import TimelineEvent
from .slots import apply_slot, canonical_subject
from .store import Store
from .util import (
    CLOSE_RE,
    DIRECTIVE_RE,
    FIRST_PERSON_RE,
    ORIGIN_QQ,
    PREF_PATTERNS,
    REMEMBER_RE,
    REVIEW_UNVERIFIED,
    ROLE_ASSISTANT,
    ROLE_BOT_ID,
    ROLE_USER,
    STATUS_NOW_RE,
    clip,
    fingerprint,
    now_ts,
    safe_json_extract,
)

NORMALIZE_PROMPT = """你是记忆整理器。下面是一批候选聊天消息，每条都带事件 id 和说话人。
对每条明确由说话人自述、且有长期价值的信息：
1) 用直白、简短的中文复述稳定事实，不能添加原文没有的信息，不能推理；
2) 提取 1-3 个关键词；
3) 给出规范化槽位。
只输出 JSON 数组，每项字段：
source_event_id, plain, keywords, subject, attribute, value, confidence(0-1),
first_person(bool), explicit_correction(bool), mention_policy(mention|tone|uncertain),
write_op(create|update|close|ignore), ttl_seconds
规则：
- source_event_id 必须是候选消息里真实存在的 id，且原文确实支持这条事实。
- plain 只复述原文意思，不判断真假、不补充背景。
- attribute 只能是：likes, dislikes, name, identity, habit, promise, note, status
- 「不喜欢/不再喜欢 X」必须写成 attribute=likes、value 以「不」开头。不要用 dislikes，也不要另写 note。
- dislikes 只用于讨厌、受不了、生理反感。
- status 只用于短暂当前状态（加班、感冒、这周很忙），必须带 ttl_seconds（默认 259200=3天）。
- write_op=close：用户说约定/未完成事项已完成或取消，用来归档已有 promise/habit，不要新建。
- write_op=ignore：玩笑、一次性情绪、不够格记住。
- subject：当前说话人自己的事实用 self；Bot 自己用 bot。不要记录第三个人的私事。
- 只记当前说话人用第一人称明确说出的偏好、称呼、约定、身份、习惯、纠正，或主人的明确指令。
- 闲聊、玩笑、反话、转述、别人的事不要输出。
候选消息：
{events}
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

    def heuristic(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        return self.extract_heuristic(events)

    def extract_heuristic(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.role != ROLE_USER:
                continue
            if ev.speaker_id == ROLE_BOT_ID:
                continue
            text = (ev.content or "").strip()
            if len(text) < 2:
                continue
            if not FIRST_PERSON_RE.search(text):
                continue
            if not (DIRECTIVE_RE.search(text) or REMEMBER_RE.search(text) or looks_correction(text)):
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
                if attr == "status":
                    payload["ttl_seconds"] = 3 * 86400
                    payload["write_op"] = "create"
                out.append(payload)
                break
            if CLOSE_RE.search(text):
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute="promise",
                    value=clip(text, 40),
                    content=clip(text, 120),
                    confidence=0.8,
                )
                payload["write_op"] = "close"
                payload["explicit_correction"] = 1
                out.append(payload)
            elif STATUS_NOW_RE.search(text) and FIRST_PERSON_RE.search(text):
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute="status",
                    value=clip(STATUS_NOW_RE.search(text).group(0), 40),
                    content=clip(text, 120),
                    confidence=0.7,
                )
                payload["ttl_seconds"] = 3 * 86400
                payload["write_op"] = "create"
                out.append(payload)
        return out

    async def normalize_llm(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        """One batch call: raw candidates -> evidence-bound fact payloads."""
        if self.llm is None or not events:
            return []
        by_id = {e.id: e for e in events}
        lines = []
        for ev in events:
            who = ev.speaker_name or ev.speaker_id or ev.role
            role = "bot" if ev.role == ROLE_ASSISTANT else "user"
            lines.append(f"[{ev.id}] role={role} from={who}({ev.speaker_id}): {clip(ev.content, 200)}")
        raw = await self.llm(NORMALIZE_PROMPT.format(events="\n".join(lines)))
        parsed = safe_json_extract(raw) or []
        if not isinstance(parsed, list):
            return []
        out: list[dict[str, Any]] = []
        for item in parsed:
            payload = self._normalize_item(item, by_id)
            if payload is not None:
                out.append(payload)
        return out

    def _normalize_item(self, item: Any, by_id: dict[int, TimelineEvent]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        try:
            event_id = int(item.get("source_event_id") or 0)
        except (TypeError, ValueError):
            return None
        ev = by_id.get(event_id)
        if ev is None:
            return None
        op = str(item.get("write_op") or "create").strip().lower()
        if op == "ignore" or op not in {"create", "update", "close", "ignore"}:
            return None
        plain = clip(str(item.get("plain") or "").strip(), 160)
        if not plain:
            return None
        attribute = str(item.get("attribute") or "").strip()
        value = str(item.get("value") or "").strip()
        subject_raw = str(item.get("subject") or "").strip()
        if not attribute or (not value and op != "close"):
            return None
        subject = canonical_subject(subject_raw or "self", ev.speaker_id, ev.speaker_name)
        if subject == "bot":
            if ev.role != ROLE_ASSISTANT:
                return None
            speaker_id, speaker_name = ROLE_BOT_ID, "bot"
        else:
            if ev.role != ROLE_USER or ev.speaker_id == ROLE_BOT_ID:
                return None
            speaker_id, speaker_name = ev.speaker_id, ev.speaker_name
        keywords: list[str] = []
        for kw in item.get("keywords") or []:
            kw = clip(str(kw).strip(), 12)
            if kw and kw not in keywords:
                keywords.append(kw)
        payload = self._payload(
            ev,
            subject=subject,
            attribute=clip(attribute, 40),
            value=clip(value, 80),
            content=clip(ev.content or "", 160),
            confidence=float(item.get("confidence") or 0.6),
            extra={
                "first_person": int(bool(item.get("first_person"))),
                "explicit_correction": int(bool(item.get("explicit_correction"))),
                "mention_policy": item.get("mention_policy") or "mention",
                "source": "llm",
                "write_op": op,
                "ttl_seconds": int(item.get("ttl_seconds") or 0),
                "plain": plain,
                "keywords": keywords,
            },
        )
        payload["speaker_id"] = speaker_id
        payload["speaker_name"] = speaker_name or speaker_id
        return payload

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
            "source_event_id": ev.id,
            "first_person": int(looks_first_person(ev.content)),
            "explicit_correction": int(looks_correction(ev.content)),
            "source": "heuristic",
            "origin": ORIGIN_QQ,
            "review_status": REVIEW_UNVERIFIED,
            "plain": clip(content, 160),
            "keywords": [],
            "created_at": now_ts(),
            "fingerprint": fingerprint(ev.speaker_id, subject, attribute, value),
        }
        if extra:
            payload.update(extra)
        return apply_slot(payload)
