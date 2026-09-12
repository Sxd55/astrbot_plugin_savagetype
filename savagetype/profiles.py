"""Per-QQ dossier cards. Not a second database — a view over live facts."""

from __future__ import annotations

from typing import Any

from .models import Fact
from .util import clip, normalize_slot, now_ts


ATTR_LABELS = {
    "name": "称呼",
    "identity": "身份",
    "likes": "偏好",
    "habit": "习惯",
    "promise": "约定",
    "note": "备注",
}

CARD_ATTRS = ("name", "identity", "likes", "habit", "promise", "note")


def build_profile(speaker_id: str, facts: list[Fact], speaker_name: str = "") -> dict[str, Any]:
    sid = (speaker_id or "").strip()
    if not sid:
        return {"speaker_id": "", "lines": [], "card": ""}
    by_attr: dict[str, list[Fact]] = {k: [] for k in CARD_ATTRS}
    name = speaker_name
    for fact in facts:
        if fact.speaker_id != sid or fact.status != "live":
            continue
        if getattr(fact, "expires_at", 0) and fact.expires_at > 0 and fact.expires_at < now_ts():
            continue
        if not name:
            name = fact.speaker_name or sid
        if fact.attribute in by_attr:
            by_attr[fact.attribute].append(fact)
    lines: list[str] = []
    evidence: list[int] = []
    for attr in CARD_ATTRS:
        items = by_attr.get(attr) or []
        if not items:
            continue
        items = sorted(items, key=lambda f: (-float(f.confidence or 0), -(f.updated_at or 0)))[:2]
        bits = []
        for fact in items:
            text = clip(getattr(fact, "plain", "") or fact.value or fact.content, 40)
            if text:
                bits.append(text)
                evidence.append(fact.id)
        if bits:
            lines.append(f"{ATTR_LABELS.get(attr, attr)}: {'；'.join(bits)}")
    card = ""
    if lines:
        who = name or sid
        card = f"【此人】{who} ({sid})\n" + "\n".join(lines)
    return {
        "speaker_id": sid,
        "speaker_name": name or sid,
        "lines": lines,
        "card": clip(card, 280),
        "fact_ids": evidence,
        "fact_count": len(evidence),
    }


def profile_key(speaker_id: str) -> str:
    return normalize_slot(speaker_id)
