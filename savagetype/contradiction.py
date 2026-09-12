"""Guarded contradiction: new live fact, old archived as superseded."""

from __future__ import annotations

from typing import Any

from .models import Fact
from .slots import apply_slot
from .store import Store
from .util import (
    CORRECTION_RE,
    FIRST_PERSON_RE,
    HEARSAY_RE,
    JOKE_RE,
    RELATION_GUARD_RE,
    STATUS_PENDING,
    fingerprint,
    make_slot_key,
    normalize_slot,
    now_ts,
)


def looks_like_joke(text: str) -> bool:
    return bool(JOKE_RE.search(text or ""))


def looks_like_hearsay(text: str) -> bool:
    return bool(HEARSAY_RE.search(text or ""))


def looks_first_person(text: str) -> bool:
    return bool(FIRST_PERSON_RE.search(text or ""))


def looks_correction(text: str) -> bool:
    return bool(CORRECTION_RE.search(text or ""))


def values_conflict(old: str, new: str) -> bool:
    a, b = normalize_slot(old), normalize_slot(new)
    if not a or not b or a == b:
        return False
    neg = ("不", "没", "别", "非")
    a_neg = any(a.startswith(n) for n in neg)
    b_neg = any(b.startswith(n) for n in neg)
    if a_neg != b_neg:
        return True
    if a in b or b in a:
        return False
    return True


class ContradictionEngine:
    def __init__(self, store: Store, high_evidence: float = 0.8, owner_ids: set[str] | None = None):
        self.store = store
        self.high_evidence = high_evidence
        self.owner_ids = {str(x).strip() for x in (owner_ids or set()) if str(x).strip()}

    def is_owner_speaker(self, speaker_id: str) -> bool:
        return bool(speaker_id) and speaker_id in self.owner_ids

    def _relation_guard(self, payload: dict[str, Any]) -> bool:
        """Return True when the fact must be rejected as an unverifiable relation claim."""
        attribute = str(payload.get("attribute") or "")
        if attribute not in {"identity", "name"}:
            return False
        blob = " ".join(
            str(payload.get(key) or "")
            for key in ("value", "content", "plain", "subject")
        )
        if not RELATION_GUARD_RE.search(blob):
            return False
        speaker_id = str(payload.get("speaker_id") or "")
        if self.is_owner_speaker(speaker_id):
            payload["attribute"] = "note"
            payload["mention_policy"] = "mention"
            return False
        return True

    def ingest(self, payload: dict[str, Any], source_text: str = "") -> dict[str, Any]:
        """Write a fact with guarded override. Conflicting live facts are deleted."""
        payload = apply_slot(dict(payload))
        payload.setdefault("status", "live")
        persona_id = str(payload.get("persona_id") or "")
        payload["slot_key"] = make_slot_key(
            persona_id,
            str(payload.get("speaker_id") or ""),
            str(payload.get("subject") or ""),
            str(payload.get("attribute") or ""),
        )
        payload.setdefault(
            "fingerprint",
            fingerprint(
                persona_id,
                payload.get("speaker_id"),
                payload.get("subject"),
                payload.get("attribute"),
                payload.get("value"),
            ),
        )
        source_text = source_text or payload.get("content", "")
        first_person = bool(payload.get("first_person")) or looks_first_person(source_text)
        explicit = bool(payload.get("explicit_correction")) or looks_correction(source_text)
        payload["first_person"] = int(first_person)
        payload["explicit_correction"] = int(explicit)
        op = str(payload.get("write_op") or "create").strip().lower()
        if op not in {"create", "update", "close", "ignore"}:
            op = "create"
        payload["write_op"] = op
        if payload.get("attribute") == "status" and not int(payload.get("expires_at") or 0):
            payload["expires_at"] = now_ts() + int(payload.get("ttl_seconds") or 3 * 86400)

        if op == "ignore":
            return {"action": "ignored", "reason": payload.get("reason") or "extractor_ignore"}

        if self._relation_guard(payload):
            return {
                "action": "rejected_relation",
                "reason": "relation_claim_not_owner",
                "speaker_id": payload.get("speaker_id"),
            }

        if looks_like_joke(source_text) and not explicit:
            payload["status"] = STATUS_PENDING
            payload["reason"] = "joke_or_banter"
            pending_id = self.store.add_pending(0, payload, "joke_or_banter")
            return {"action": "ignored_joke", "pending_id": pending_id}

        if looks_like_hearsay(source_text) and not first_person and not explicit:
            payload["mention_policy"] = "uncertain"
            payload["confidence"] = min(float(payload.get("confidence", 0.4)), 0.4)
            payload["reason"] = "hearsay"
            fact_id = self.store.add_fact(payload)
            return {"action": "wrote_uncertain", "fact_id": fact_id}

        existing = self.store.live_by_slot(
            payload["speaker_id"],
            payload["subject"],
            payload["attribute"],
            persona_id=persona_id,
            speaker_ids=self.store.speaker_ids_for(str(payload.get("speaker_id") or "")),
        )
        if existing is None:
            if op == "close":
                return {"action": "ignored", "reason": "close_without_existing"}
            fact_id = self.store.add_fact(payload)
            return {"action": "insert", "fact_id": fact_id}

        if op == "close":
            self.store.update_fact(
                existing.id,
                status="archived",
                reason=payload.get("reason") or "closed",
                write_op="close",
            )
            return {"action": "closed", "fact_id": existing.id}

        if not values_conflict(existing.value, payload["value"]):
            merged = self._merge_same(existing, payload)
            return {"action": "refresh", "fact_id": existing.id, **merged}

        allowed = first_person or explicit
        if not allowed:
            pending_id = self.store.add_pending(
                existing.id,
                payload,
                "not_first_person_or_correction",
            )
            return {
                "action": "pending",
                "pending_id": pending_id,
                "old_fact_id": existing.id,
                "reason": "not_first_person_or_correction",
            }

        high_old = existing.confidence >= self.high_evidence and existing.access_count >= 1
        if high_old and not explicit:
            pending_id = self.store.add_pending(
                existing.id,
                payload,
                "high_evidence_needs_confirm",
            )
            return {
                "action": "pending",
                "pending_id": pending_id,
                "old_fact_id": existing.id,
                "reason": "high_evidence_needs_confirm",
            }

        return self.supersede(existing, payload)

    def supersede(self, old: Fact, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload["status"] = "live"
        new_id = self.store.add_fact(payload, bump=False)
        self.store.delete_fact(old.id)
        return {
            "action": "supersede",
            "fact_id": new_id,
            "old_fact_id": old.id,
            "deleted_old": True,
        }

    def confirm_pending(self, pending_id: int) -> dict[str, Any]:
        items = [p for p in self.store.pending_open(200) if p.id == pending_id]
        if not items:
            return {"ok": False, "error": "pending not found"}
        item = items[0]
        old = self.store.get_fact(item.old_fact_id) if item.old_fact_id else None
        payload = dict(item.new_payload)
        if old and old.status == "live":
            result = self.supersede(old, payload)
        else:
            result = {"action": "insert", "fact_id": self.store.add_fact(payload)}
        self.store.set_pending_status(pending_id, "applied")
        result["ok"] = True
        return result

    def reject_pending(self, pending_id: int) -> dict[str, Any]:
        self.store.set_pending_status(pending_id, "rejected")
        return {"ok": True, "action": "rejected", "pending_id": pending_id}

    def rollback(self, fact_id: int) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "冲突覆盖会删除旧条，无法回滚。请重新手动记住。",
        }

    def _merge_same(self, existing: Fact, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = list(existing.evidence)
        for eid in payload.get("evidence") or []:
            if eid not in evidence:
                evidence.append(eid)
        confidence = max(existing.confidence, float(payload.get("confidence", existing.confidence)))
        self.store.update_fact(
            existing.id,
            value=payload.get("value", existing.value),
            content=payload.get("content", existing.content),
            evidence=evidence,
            confidence=confidence,
            last_accessed=now_ts(),
            access_count=existing.access_count + 1,
        )
        return {"confidence": confidence}
