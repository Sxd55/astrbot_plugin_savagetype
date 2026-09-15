"""AI memory pipeline: batch normalize -> verify -> revise (<= N) -> write or pending.

Runs in the background next to capture; never blocks message handling.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .extract import Extractor
from .contradiction import ContradictionEngine
from .llm import LLMBudgetExceeded
from .models import TimelineEvent
from .store import Store
from .util import (
    COMMAND_SPLIT_RE,
    CORRECTION_RE,
    DIRECTIVE_RE,
    FIRST_PERSON_RE,
    LOW_INFO_RE,
    ORIGIN_IMPORT,
    ORIGIN_QQ,
    OWNER_DIRECTIVE_RE,
    REMEMBER_RE,
    REVIEW_AI_PASSED,
    REVIEW_UNVERIFIED,
    ROLE_BOT_ID,
    ROLE_USER,
    SCOPE_OWNER,
    SCOPE_PERSON,
    SELF_STATEMENT_RE,
    STATUS_NOW_RE,
    clip,
    platform_of,
    safe_json_extract,
)

VERIFY_PROMPT = """你是记忆审核员。下面每条包含原始聊天消息和整理结果。
判断整理结果是否忠实于原文：有没有添加原文没有的信息、改变原意、把别人的话算到本人头上、加入主观判断或推理。
只输出 JSON 数组，每项字段：index(number), pass(bool), reason(简短中文), fix_hint(不通过时给出怎么改)。
原文与整理结果：
{items}
"""

REVISE_PROMPT = """你是记忆整理器。下面每条包含原始消息、上一次整理结果和不通过原因。
按不通过原因修改整理结果，不能添加原文没有的信息。
只输出 JSON 数组，字段与整理结果一致：
index, source_event_id, plain, keywords, subject, attribute, value, confidence,
first_person, explicit_correction, mention_policy, write_op, ttl_seconds, topic
index 必须与输入的 index 一致；不要遗漏任何一条。
除按原因需要修改的字段外，其余字段必须与 prev 保持一致（尤其 write_op、mention_policy、ttl_seconds）。
{items}
"""


def candidate_reason(ev: TimelineEvent, is_owner: bool) -> str:
    if ev.role != ROLE_USER or ev.speaker_id == ROLE_BOT_ID:
        return ""
    text = (ev.content or "").strip()
    if not text or COMMAND_SPLIT_RE.match(text) or LOW_INFO_RE.match(text):
        return ""
    low = text.lower()
    if low.startswith("stype") or low.startswith("savagetype_"):
        return ""
    self_directive = bool(
        FIRST_PERSON_RE.search(text)
        and (DIRECTIVE_RE.search(text) or REMEMBER_RE.search(text) or CORRECTION_RE.search(text))
    )
    if is_owner:
        if self_directive or SELF_STATEMENT_RE.search(text):
            return "owner_self"
        if OWNER_DIRECTIVE_RE.search(text):
            return "owner_directive"
        return ""
    if self_directive or SELF_STATEMENT_RE.search(text):
        return "self"
    if STATUS_NOW_RE.search(text) and FIRST_PERSON_RE.search(text):
        return "status"
    return ""


class MemoryPipeline:
    def __init__(
        self,
        store: Store,
        contradiction: ContradictionEngine,
        extractor: Extractor,
        config: dict[str, Any],
        logger: Any,
        llm: Callable[[str], Awaitable[str]] | None,
        is_owner_speaker: Callable[[str], bool],
        verify_llm: Callable[[str], Awaitable[str]] | None = None,
    ):
        self.store = store
        self.contradiction = contradiction
        self.extractor = extractor
        self.config = config
        self.logger = logger
        self.llm = llm
        self.verify_llm = verify_llm or llm
        self.is_owner_speaker = is_owner_speaker
        self._lock = asyncio.Lock()

    def _cfg_int(self, key: str, default: int) -> int:
        raw = self.config.get(key)
        return default if raw is None else int(raw)

    def max_revisions(self) -> int:
        return max(0, self._cfg_int("pipeline_max_revisions", 2))

    def batch_size(self) -> int:
        return max(1, self._cfg_int("pipeline_batch_size", 8))

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    async def run(self, force: bool = False) -> dict[str, Any]:
        min_messages = max(1, int(self.config.get("extract_min_messages") or 8))
        limit = 80 if force else max(self.batch_size(), min_messages)
        events = self.store.unsummarized(limit=limit)
        if not events:
            return {"ok": True, "skipped": True, "unsummarized": 0}
        if not force and len(events) < min_messages:
            return {"ok": True, "skipped": True, "unsummarized": len(events)}

        async with self._lock:
            return await self._process(events)

    async def _process(self, events: list[TimelineEvent]) -> dict[str, Any]:
        candidates = [e for e in events if candidate_reason(e, self.is_owner_speaker(e.speaker_id))]
        if not candidates:
            self.store.mark_summarized([e.id for e in events])
            return {"ok": True, "skipped": False, "events": len(events), "candidates": 0, "written": 0, "pending": 0}

        import_candidates = [e for e in candidates if (e.window_tag or "") == "import"]
        import_written = 0
        if import_candidates:
            import_ids = {e.id for e in import_candidates}
            import_written = self._write_heuristic(
                import_candidates,
                origin=ORIGIN_IMPORT,
                review_status=REVIEW_UNVERIFIED,
            )
            self.store.mark_summarized(list(import_ids))
            candidates = [e for e in candidates if e.id not in import_ids]
            if not candidates:
                self.store.mark_summarized([e.id for e in events])
                return {
                    "ok": True,
                    "skipped": False,
                    "events": len(events),
                    "candidates": len(import_candidates),
                    "written": import_written,
                    "pending": 0,
                }

        if not bool(self.config.get("pipeline_enabled", True)) or self.llm is None:
            return await self._fallback(events, candidates, reason="pipeline_disabled")

        try:
            entries = await self.extractor.normalize_llm(candidates)
            entries = self.extractor.expand_split(entries)
        except LLMBudgetExceeded:
            # 预算闸：不标记已总结，等额度恢复后重试同一批消息。
            return {
                "ok": True,
                "skipped": True,
                "reason": "budget",
                "events": len(events),
                "unsummarized": len(events),
            }
        except Exception as exc:  # noqa: BLE001
            return await self._fallback(events, candidates, reason=f"normalize_error: {exc}", error=True)

        by_id = {e.id: e for e in candidates}
        traces: dict[int, list[dict[str, Any]]] = {e.id: [] for e in candidates}
        pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        written = 0
        rounds = 0
        max_revisions = self.max_revisions()

        while True:
            rounds += 1
            if not entries:
                break
            try:
                verdicts = await self._verify(entries, by_id)
            except LLMBudgetExceeded:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "budget",
                    "events": len(events),
                    "unsummarized": len(events),
                }
            failed: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for index, entry in enumerate(entries):
                key = int(entry.get("source_event_id") or 0)
                verdict = verdicts.get(index, {"pass": False, "reason": "no_verdict", "fix_hint": ""})
                traces.setdefault(key, []).append(
                    {
                        "round": rounds,
                        "plain": entry.get("plain") or entry.get("content") or "",
                        "pass": bool(verdict.get("pass")),
                        "reason": str(verdict.get("reason") or ""),
                        "fix_hint": str(verdict.get("fix_hint") or ""),
                    }
                )
                if verdict.get("pass"):
                    result = self._write(entry, raw=by_id.get(key))
                    action = str(result.get("action") or "")
                    if action == "rejected_relation":
                        self.store.add_diag("memory_rejected", result)
                    elif action != "pending":
                        # 高证据冲突会进「待确认覆盖」，不算已写入。
                        written += 1
                else:
                    failed.append((entry, verdict))
            if not failed:
                break
            if rounds > max_revisions:
                pending.extend(failed)
                break
            try:
                revised = await self._revise(failed, by_id)
            except LLMBudgetExceeded:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "budget",
                    "events": len(events),
                    "unsummarized": len(events),
                }
            except Exception as exc:  # noqa: BLE001
                self.store.add_diag("pipeline_revise_fail", {"error": str(exc)})
                pending.extend(failed)
                break
            # revised 与 failed 按索引对应，空列表表示模型漏回了该条：转待审而不是丢弃。
            next_entries: list[dict[str, Any]] = []
            for (entry, verdict), new_entries in zip(failed, revised):
                if not new_entries:
                    pending.append((entry, verdict))
                else:
                    next_entries.extend(new_entries)
            if not next_entries:
                break
            entries = next_entries

        for entry, verdict in pending:
            self._pending(entry, verdict, by_id, traces)

        self.store.mark_summarized([e.id for e in events])
        result = {
            "ok": True,
            "skipped": False,
            "events": len(events),
            "candidates": len(candidates),
            "written": written,
            "pending": len(pending),
            "rounds": rounds,
        }
        self.store.add_diag("memory_pipeline", result)
        return result

    async def _verify(
        self,
        entries: list[dict[str, Any]],
        by_id: dict[int, TimelineEvent],
    ) -> dict[int, dict[str, Any]]:
        items = []
        for idx, entry in enumerate(entries):
            eid = int(entry.get("source_event_id") or 0)
            raw = by_id.get(eid)
            items.append(
                {
                    "index": idx,
                    "source_event_id": eid,
                    "raw": clip(raw.content if raw else "", 300),
                    "plain": entry.get("plain") or "",
                    "attribute": entry.get("attribute"),
                    "value": entry.get("value"),
                }
            )
        prompt = VERIFY_PROMPT.format(items=json.dumps(items, ensure_ascii=False))
        raw_text = await self._llm_call(prompt, kind="verify")
        parsed = safe_json_extract(raw_text)
        verdicts: dict[int, dict[str, Any]] = {}
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(entries):
                    verdicts[idx] = {
                        "pass": bool(item.get("pass")),
                        "reason": str(item.get("reason") or ""),
                        "fix_hint": str(item.get("fix_hint") or ""),
                    }
        return verdicts

    async def _revise(
        self,
        failed: list[tuple[dict[str, Any], dict[str, Any]]],
        by_id: dict[int, TimelineEvent],
    ) -> list[list[dict[str, Any]]]:
        items = []
        for index, (entry, verdict) in enumerate(failed):
            eid = int(entry.get("source_event_id") or 0)
            raw = by_id.get(eid)
            items.append(
                {
                    "index": index,
                    "source_event_id": eid,
                    "raw": clip(raw.content if raw else "", 300),
                    "prev": {
                        "plain": entry.get("plain"),
                        "keywords": entry.get("keywords"),
                        "subject": entry.get("subject"),
                        "attribute": entry.get("attribute"),
                        "value": entry.get("value"),
                        "write_op": entry.get("write_op"),
                        "mention_policy": entry.get("mention_policy"),
                        "ttl_seconds": entry.get("ttl_seconds"),
                        "topic": entry.get("topic"),
                    },
                    "reason": verdict.get("reason"),
                    "fix_hint": verdict.get("fix_hint"),
                }
            )
        prompt = REVISE_PROMPT.format(items=json.dumps(items, ensure_ascii=False))
        raw_text = await self._llm_call(prompt, kind="normalize")
        parsed = safe_json_extract(raw_text)
        if not isinstance(parsed, list):
            return [[] for _ in failed]
        mapping: dict[int, list[dict[str, Any]]] = {}
        next_slot = 0
        for item in parsed:
            if not isinstance(item, dict):
                continue
            normalized = self.extractor._normalize_item(item, by_id)  # noqa: SLF001
            if normalized is None:
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                idx = next_slot
            # 修订轮同样要走拆句兜底，否则模型合成的一条会带着整句 plain 直接入库。
            mapping.setdefault(idx, []).extend(self.extractor.expand_split([normalized]))
            next_slot = max(next_slot, idx + 1)
        return [mapping.get(i) or [] for i in range(len(failed))]

    def _write(self, entry: dict[str, Any], raw: TimelineEvent | None) -> dict[str, Any]:
        payload = dict(entry)
        speaker_id = str(payload.get("speaker_id") or "")
        payload["scope"] = SCOPE_OWNER if self.is_owner_speaker(speaker_id) else SCOPE_PERSON
        payload["origin"] = ORIGIN_QQ
        payload["review_status"] = REVIEW_AI_PASSED
        if raw is not None:
            payload["content"] = clip(raw.content or "", 2000)
            payload["window_tag"] = raw.window_tag or payload.get("window_tag") or ""
            payload["persona_id"] = payload.get("persona_id") or getattr(raw, "persona_id", "") or ""
        return self.contradiction.ingest(payload, source_text=payload.get("content", ""))

    def _pending(
        self,
        entry: dict[str, Any],
        verdict: dict[str, Any],
        by_id: dict[int, TimelineEvent],
        traces: dict[int, list[dict[str, Any]]],
    ) -> int:
        eid = int(entry.get("source_event_id") or 0)
        raw = by_id.get(eid)
        speaker_id = str(entry.get("speaker_id") or "")
        trace = list(traces.get(eid) or [])
        last = trace[-1] if trace else {}
        if not (
            last
            and not last.get("pass")
            and str(last.get("reason") or "") == str(verdict.get("reason") or "")
            and str(last.get("plain") or "") == str(entry.get("plain") or "")
        ):
            trace.append(
                {
                    "round": len(trace) + 1,
                    "plain": entry.get("plain") or "",
                    "pass": False,
                    "reason": str(verdict.get("reason") or ""),
                    "fix_hint": str(verdict.get("fix_hint") or ""),
                }
            )
        payload = dict(entry)
        speaker_name = str(entry.get("speaker_name") or "")
        platform = platform_of(str(entry.get("window_tag") or ""))
        return self.store.add_memory_review(
            scope=SCOPE_OWNER if self.is_owner_speaker(speaker_id) else SCOPE_PERSON,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            platform=platform,
            window_tag=str(entry.get("window_tag") or ""),
            source_event_id=eid,
            raw_text=clip(raw.content if raw else "", 2000),
            plain=str(entry.get("plain") or ""),
            keywords=list(entry.get("keywords") or []),
            payload=payload,
            attempts=len(trace),
            trace=trace,
        )

    def _write_heuristic(
        self,
        candidates: list[TimelineEvent],
        origin: str = ORIGIN_QQ,
        review_status: str = REVIEW_UNVERIFIED,
    ) -> int:
        payloads = self.extractor.extract_heuristic(candidates)
        written = 0
        for payload in payloads:
            payload["scope"] = SCOPE_OWNER if self.is_owner_speaker(payload.get("speaker_id", "")) else SCOPE_PERSON
            payload["origin"] = origin
            payload["review_status"] = review_status
            result = self.contradiction.ingest(payload, source_text=payload.get("content", ""))
            if str(result.get("action") or "") not in {"rejected_relation", "pending"}:
                written += 1
        return written

    async def _fallback(
        self,
        events: list[TimelineEvent],
        candidates: list[TimelineEvent],
        reason: str = "",
        error: bool = False,
    ) -> dict[str, Any]:
        written = self._write_heuristic(candidates, origin=ORIGIN_QQ, review_status=REVIEW_UNVERIFIED)
        self.store.mark_summarized([e.id for e in events])
        diag = {"reason": reason, "events": len(events), "candidates": len(candidates), "written": written}
        self.store.add_diag("memory_fallback", diag)
        return {
            "ok": True,
            "skipped": False,
            "fallback": True,
            "error": error,
            "events": len(events),
            "candidates": len(candidates),
            "written": written,
            "pending": 0,
            "reason": reason,
        }

    async def _llm_call(self, prompt: str, kind: str = "normalize") -> str:
        fn = self.verify_llm if kind == "verify" else self.llm
        if fn is None:
            raise RuntimeError("no llm for memory pipeline")
        return await fn(prompt)
