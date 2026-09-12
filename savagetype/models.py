"""Dataclasses used across the plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TimelineEvent:
    id: int
    ts: int
    speaker_id: str
    speaker_name: str
    bot_id: str
    window_tag: str
    role: str
    content: str
    summarized: int = 0
    persona_id: str = ""


@dataclass
class Fact:
    id: int
    subject: str
    attribute: str
    value: str
    content: str
    speaker_id: str
    speaker_name: str
    bot_id: str
    window_tag: str
    status: str
    confidence: float
    evidence: list[int] = field(default_factory=list)
    mention_policy: str = "mention"
    first_person: int = 0
    explicit_correction: int = 0
    source: str = "extract"
    created_at: int = 0
    updated_at: int = 0
    superseded_by: int | None = None
    supersedes: int | None = None
    fingerprint: str = ""
    embedding: list[float] | None = None
    access_count: int = 0
    last_accessed: int = 0
    reason: str = ""
    persona_id: str = ""
    slot_key_value: str = ""
    expires_at: int = 0
    write_op: str = ""
    scope: str = ""
    plain: str = ""
    keywords: list[str] = field(default_factory=list)
    source_event_id: int = 0
    review_status: str = ""
    origin: str = ""
    edited_at: int = 0
    edited_by: str = ""
    importance: float = 0.0
    kind: str = ""
    pinned: int = 0

    def slot_key(self) -> str:
        from .slots import canonical_attribute, canonical_subject
        from .util import normalize_slot

        if self.slot_key_value:
            return self.slot_key_value
        attr = canonical_attribute(self.attribute)
        subject = canonical_subject(self.subject, self.speaker_id, self.speaker_name)
        persona = self.persona_id or ""
        return f"{persona}|{self.speaker_id}|{normalize_slot(subject)}|{attr}"


@dataclass
class PendingOverride:
    id: int
    old_fact_id: int
    new_payload: dict[str, Any]
    reason: str
    created_at: int
    status: str = "open"


@dataclass
class RetrievalHit:
    fact: Fact
    score: float
    source: str
    filter_reason: str = ""


@dataclass
class RetrievalResult:
    query: str
    route: str
    path: str
    cache: str
    hits: list[RetrievalHit]
    blocked: list[RetrievalHit]
    core: list[Fact]
    related: list[Fact]
    uncertain: list[Fact]
    superseded: list[Fact]


@dataclass
class ReviewItem:
    id: int
    kind: str
    status: str
    fingerprint: str
    speaker_id: str
    persona_id: str
    title: str
    payload: dict[str, Any]
    reason: str
    created_at: int
    updated_at: int


@dataclass
class LearningPack:
    jargon: list[dict[str, Any]] = field(default_factory=list)
    fewshots: list[dict[str, Any]] = field(default_factory=list)
    persona_draft: str = ""


@dataclass
class Profile:
    speaker_id: str
    speaker_name: str = ""
    platform: str = ""
    is_owner: int = 0
    note: str = ""
    first_seen: int = 0
    last_seen: int = 0
    seen_count: int = 0
    fact_count: int = 0


@dataclass
class MemoryReview:
    id: int
    scope: str
    speaker_id: str
    speaker_name: str
    platform: str
    window_tag: str
    source_event_id: int
    raw_text: str
    plain: str
    keywords: list[str]
    payload: dict[str, Any]
    status: str
    attempts: int
    trace: list[dict[str, Any]]
    notified_at: int
    created_at: int
    updated_at: int
