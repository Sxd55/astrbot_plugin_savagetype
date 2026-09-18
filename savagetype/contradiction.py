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
    MASTER_RE,
    ORIGIN_MANUAL,
    RELATION_GUARD_RE,
    REVIEW_MANUAL,
    ROLE_BOT_ID,
    STATUS_PENDING,
    detect_domain,
    fingerprint,
    make_slot_key,
    normalize_slot,
    now_ts,
    topic_key,
)


def looks_like_joke(text: str) -> bool:
    return bool(JOKE_RE.search(text or ""))


def looks_like_hearsay(text: str) -> bool:
    return bool(HEARSAY_RE.search(text or ""))


def looks_first_person(text: str) -> bool:
    return bool(FIRST_PERSON_RE.search(text or "") or MASTER_RE.search(text or ""))


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
        if str(payload.get("speaker_id") or "") == ROLE_BOT_ID:
            # Bot 自己的身份/称呼由主人定义，按定义记录，不走「外人攀关系」守卫。
            return False
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
        if payload.get("speaker_id"):
            # 身份合并后，旧 id 的写入统一归到规范 id，避免绕过合并又建一份。
            payload["speaker_id"] = self.store.resolve_speaker(str(payload.get("speaker_id")))
        persona_id = str(payload.get("persona_id") or "")
        payload["slot_key"] = make_slot_key(
            persona_id,
            str(payload.get("speaker_id") or ""),
            str(payload.get("subject") or ""),
            str(payload.get("attribute") or ""),
            str(payload.get("value") or ""),
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

        manual = (
            str(payload.get("origin") or "") == ORIGIN_MANUAL
            or str(payload.get("review_status") or "") == REVIEW_MANUAL
        )
        if not manual and looks_like_joke(source_text) and not explicit:
            # 玩笑守卫只拦自动链路；人工补记/审核通过表示人已确认，不应再进待审。
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
            value=str(payload.get("value") or ""),
        )
        domain_new = str(payload.get("topic") or "")
        prefer = payload["attribute"] in {"likes", "dislikes"}
        if existing is None and prefer and domain_new:
            # 基础槽没有，但可能已存在「同词不同义」的分槽事实。
            sep_key = f"{payload['slot_key']}|{domain_new}"
            separated = self.store.live_fact_by_slot_key(sep_key)
            if separated is not None:
                existing = separated
                payload["slot_key"] = sep_key

        if existing is None and op == "close" and payload.get("attribute") in {"promise", "habit"}:
            # 约定/习惯按主题分槽后，close 可能带的是整句而不是原值：回退到该属性最近一条。
            existing = self.store.live_latest_by_attr(
                str(payload.get("speaker_id") or ""),
                str(payload.get("subject") or ""),
                str(payload.get("attribute") or ""),
                persona_id=persona_id,
                speaker_ids=self.store.speaker_ids_for(str(payload.get("speaker_id") or "")),
                topic=topic_key(str(payload.get("value") or "")),
            )

        if existing is None:
            if op == "close":
                return {"action": "ignored", "reason": "close_without_existing"}
            fact_id = self.store.add_fact(payload)
            return {"action": "insert", "fact_id": fact_id}

        domain_old = str(getattr(existing, "topic", "") or "") or detect_domain(
            existing.content or "", existing.value or ""
        )
        if prefer and domain_new and domain_old and domain_new != domain_old:
            # 同一个词、不同领域（美式咖啡 vs 美式穿搭）：不合并，存成独立分槽。
            sep_key = f"{existing.slot_key()}|{domain_new}"
            same = self.store.live_fact_by_slot_key(sep_key)
            if same is None:
                payload["slot_key"] = sep_key
                fact_id = self.store.add_fact(payload)
                return {"action": "insert", "fact_id": fact_id, "reason": "domain_split"}
            existing = same

        if op == "close":
            self.store.update_fact(
                existing.id,
                status="archived",
                reason=payload.get("reason") or "closed",
                write_op="close",
            )
            return {"action": "closed", "fact_id": existing.id}

        if not values_conflict(existing.value, payload["value"]):
            if prefer and domain_new and not domain_old:
                # 旧条目判不出领域、新条目判出了：拿不准是否同义，交人工确认（安全阀）。
                pending_id = self.store.add_pending(
                    existing.id,
                    payload,
                    "domain_needs_confirm",
                )
                return {
                    "action": "pending",
                    "pending_id": pending_id,
                    "old_fact_id": existing.id,
                    "reason": "domain_needs_confirm",
                }
            merged = self._merge_same(existing, payload)
            return {"action": "refresh", "fact_id": existing.id, **merged}

        if int(getattr(existing, "pinned", 0) or 0):
            # 置顶是明确的长期记忆，任何冲突都先人工确认，不自动删除。
            pending_id = self.store.add_pending(existing.id, payload, "pinned_needs_confirm")
            return {
                "action": "pending",
                "pending_id": pending_id,
                "old_fact_id": existing.id,
                "reason": "pinned_needs_confirm",
            }

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
        payload["supersedes"] = old.id
        if float(payload.get("importance") or 0) <= 0 and float(old.importance or 0) > 0:
            # 覆盖不降级：新事实至少继承旧事实的重要性。
            payload["importance"] = float(old.importance)
        if not payload.get("topic") and getattr(old, "topic", ""):
            # 领域信息随覆盖继承，避免新条丢失语境。
            payload["topic"] = old.topic
        new_id = self.store.add_fact(payload, bump=False)
        # 旧条不删除：标记作废保留，回滚和「改口摘要」都靠它。
        self.store.update_fact(
            old.id,
            status="superseded",
            superseded_by=new_id,
            reason="superseded",
        )
        return {
            "action": "supersede",
            "fact_id": new_id,
            "old_fact_id": old.id,
            "invalidated_old": True,
        }

    def confirm_pending(self, pending_id: int) -> dict[str, Any]:
        items = [p for p in self.store.pending_open(200) if p.id == pending_id]
        if not items:
            return {"ok": False, "error": "pending not found"}
        item = items[0]
        old = self.store.get_fact(item.old_fact_id) if item.old_fact_id else None
        payload = dict(item.new_payload)
        payload["status"] = "live"
        if item.reason == "domain_needs_confirm":
            # 通过 = 确认这是另一种含义：存成独立分槽，旧条保留。
            domain = str(payload.get("topic") or "")
            base = str(payload.get("slot_key") or "")
            if base and domain:
                payload["slot_key"] = f"{base}|{domain}"
            result = {"action": "insert", "fact_id": self.store.add_fact(payload)}
        elif old and old.status == "live":
            result = self.supersede(old, payload)
        else:
            # 待审期间旧条目可能已消失或已被改写：按当前槽位重新找冲突，避免插入重复活条。
            current = self.store.live_by_slot(
                str(payload.get("speaker_id") or ""),
                str(payload.get("subject") or ""),
                str(payload.get("attribute") or ""),
                persona_id=str(payload.get("persona_id") or ""),
                speaker_ids=self.store.speaker_ids_for(str(payload.get("speaker_id") or "")),
                value=str(payload.get("value") or ""),
            )
            if current is not None:
                result = self.supersede(current, payload)
            else:
                result = {"action": "insert", "fact_id": self.store.add_fact(payload)}
        self.store.set_pending_status(pending_id, "applied")
        result["ok"] = True
        return result

    def reject_pending(self, pending_id: int) -> dict[str, Any]:
        if not any(p.id == pending_id for p in self.store.pending_open(200)):
            return {"ok": False, "error": "pending not found"}
        self.store.set_pending_status(pending_id, "rejected")
        return {"ok": True, "action": "rejected", "pending_id": pending_id}

    def rollback(self, fact_id: int) -> dict[str, Any]:
        """Undo one supersede: archive the new fact, restore the old one to live."""
        fact = self.store.get_fact(fact_id)
        if fact is None:
            return {"ok": False, "error": "fact not found"}
        if fact.status != "live":
            return {"ok": False, "error": f"只能回滚 live 事实（当前 {fact.status}）"}
        old_id = int(getattr(fact, "supersedes", 0) or 0)
        if not old_id:
            return {"ok": False, "error": "这条事实没有可回滚的覆盖记录"}
        old = self.store.get_fact(old_id)
        if old is None:
            return {"ok": False, "error": f"旧事实 #{old_id} 已被维护清理，无法回滚"}
        if old.status != "superseded":
            return {"ok": False, "error": f"旧事实 #{old_id} 当前是 {old.status}，不是被覆盖状态"}
        conflict = self.store.live_by_slot(
            old.speaker_id,
            old.subject,
            old.attribute,
            persona_id=old.persona_id,
            speaker_ids=self.store.speaker_ids_for(old.speaker_id),
            value=old.value,
        )
        if conflict is not None and conflict.id != fact.id:
            return {"ok": False, "error": f"槽位已被 #{conflict.id} 占用，先处理它再回滚"}
        self.store.update_fact(fact.id, status="archived", reason="rolled_back")
        self.store.update_fact(old.id, status="live", reason="rollback", superseded_by=None)
        return {"ok": True, "action": "rollback", "restored_id": old.id, "archived_id": fact.id}

    def _merge_same(self, existing: Fact, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = list(existing.evidence)
        for eid in payload.get("evidence") or []:
            if eid not in evidence:
                evidence.append(eid)
        confidence = max(existing.confidence, float(payload.get("confidence", existing.confidence)))
        importance = max(
            float(existing.importance or 0),
            float(payload.get("importance") or 0),
        )
        subject = str(payload.get("subject", existing.subject) or "")
        attribute = str(payload.get("attribute", existing.attribute) or "")
        value = str(payload.get("value", existing.value) or "")
        persona_id = existing.persona_id or ""
        fields: dict[str, Any] = {
            "value": value,
            "content": payload.get("content", existing.content),
            "evidence": evidence,
            "confidence": confidence,
            "importance": importance,
            "last_accessed": now_ts(),
            "access_count": existing.access_count + 1,
        }
        if payload.get("plain"):
            # 用最新一次归一的直白写法刷新展示文本；拆出的分句不会再显示整句。
            fields["plain"] = str(payload.get("plain"))
        if payload.get("keywords"):
            fields["keywords"] = list(payload.get("keywords") or [])
        if payload.get("topic") and not getattr(existing, "topic", ""):
            fields["topic"] = str(payload.get("topic"))
        if value != existing.value:
            # value 变了必须同步 slot_key / fingerprint，否则会出现同一槽位的重复活条。
            old_base = make_slot_key(
                persona_id, existing.speaker_id, subject, attribute, existing.value
            )
            new_key = make_slot_key(persona_id, existing.speaker_id, subject, attribute, value)
            if existing.slot_key_value and existing.slot_key_value != old_base:
                # 这条事实本来就在「同词不同义」的分槽里，保持分槽键。
                topic = getattr(existing, "topic", "") or str(payload.get("topic") or "")
                if topic:
                    new_key = f"{new_key}|{topic}"
            fields["slot_key"] = new_key
            fields["fingerprint"] = fingerprint(
                persona_id, existing.speaker_id, subject, attribute, value
            )
        self.store.update_fact(existing.id, **fields)
        return {"confidence": confidence}
