"""Plugin orchestration: capture, extract, retrieve, inject, providers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .archive import (
    archive_low_value,
    backup_db,
    compact_summarized_timeline,
    expire_persona_drafts,
    expire_status_facts,
    fold_preference_slots,
    import_jsonl,
    import_transcript_events,
    parse_transcript,
    preview_jsonl,
)
from .coexistence import Coexistence
from .contradiction import ContradictionEngine
from .extract import Extractor
from .inject import build_pack
from .learn import LearningEngine
from .profiles import build_profile
from .retrieve import Retriever, detect_other_speaker
from .store import Store
from .slots import apply_slot
from .util import (
    ROLE_ASSISTANT,
    ROLE_USER,
    clip,
    fingerprint,
    now_ts,
)


def _unpack_llm_result(result: Any) -> tuple[str, int, int]:
    if isinstance(result, tuple) and len(result) >= 1:
        text = str(result[0] or "")
        tokens_in = int(result[1]) if len(result) > 1 and result[1] is not None else 0
        tokens_out = int(result[2]) if len(result) > 2 and result[2] is not None else 0
        return text, tokens_in, tokens_out
    return str(result or ""), 0, 0


class SavageTypeService:
    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        llm_generate,
        get_provider,
        logger,
        get_persona_text=None,
    ):
        self.store = store
        self.config = config
        self.llm_generate = llm_generate
        self.get_provider = get_provider
        self.logger = logger
        self.get_persona_text = get_persona_text
        self.coexistence = Coexistence(enabled=bool(config.get("coexistence_degrade", True)))
        self.contradiction = ContradictionEngine(
            store,
            high_evidence=float(config.get("high_evidence_confidence", 0.8)),
        )
        self.extractor = Extractor(store, self.contradiction, llm=self._llm)
        self.learning = LearningEngine(store, llm=self._llm, config=self.config)
        self.retriever = Retriever(
            store,
            embed=None,
            rerank=self._rerank,
            mode=str(config.get("retrieval_mode") or "auto"),
            cache_ttl=int(config.get("cache_ttl_seconds") or 20),
        )
        self._extract_lock = asyncio.Lock()
        self._embed_lock = asyncio.Lock()
        self._last_extract_at = 0
        self._extract_fail_until = 0
        self._learn_task: asyncio.Task | None = None
        self._sync_embed_fn()

    def embedding_auto_threshold(self) -> int:
        return int(self.config.get("embedding_auto_threshold") or 2500)

    def embedding_wanted(self) -> bool:
        if bool(self.config.get("embedding_enabled")):
            return True
        live = int(self.store.counts().get("facts_live") or 0)
        return live >= self.embedding_auto_threshold()

    def embedding_status(self) -> dict[str, Any]:
        live = int(self.store.counts().get("facts_live") or 0)
        provider = None
        try:
            provider = self.get_provider("embedding", str(self.config.get("embedding_provider_id") or ""))
        except Exception:
            provider = None
        wanted = self.embedding_wanted()
        reason = "off"
        if bool(self.config.get("embedding_enabled")):
            reason = "manual"
        elif live >= self.embedding_auto_threshold():
            reason = "auto_threshold"
        active = wanted and provider is not None
        if wanted and provider is None:
            reason = "need_provider"
        return {
            "config_enabled": bool(self.config.get("embedding_enabled")),
            "wanted": wanted,
            "active": active,
            "reason": reason,
            "live": live,
            "threshold": self.embedding_auto_threshold(),
            "has_provider": provider is not None,
        }

    def _sync_embed_fn(self) -> None:
        status = self.embedding_status()
        self.retriever.embed = self._embed if status["active"] else None

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def whitelist_ids(self) -> list[str]:
        raw = str(self.config.get("memory_whitelist") or "")
        return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

    def window_allowed(self, event: Any = None, ident: dict[str, str] | None = None) -> bool:
        allow = self.whitelist_ids()
        if not allow:
            return True
        ident = ident or (self._ident_from_event(event) if event is not None else {})
        window = str((ident or {}).get("window_tag") or "")
        speaker = str((ident or {}).get("speaker_id") or "")
        group = ""
        if event is not None:
            try:
                group = str(event.get_group_id() or "")
            except Exception:
                group = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
        hay = " ".join([window, speaker, group])
        return any(item and item in hay for item in allow)

    def capture_ok(self, event: Any = None) -> bool:
        return (
            self.enabled()
            and bool(self.config.get("capture_enabled", True))
            and not self.coexistence.skip_capture
            and self.window_allowed(event)
        )

    def inject_ok(self, event: Any = None) -> bool:
        return (
            self.enabled()
            and bool(self.config.get("inject_enabled", True))
            and not self.coexistence.skip_inject
            and self.window_allowed(event)
        )

    def refresh_coexistence(self, stars: list[Any]) -> None:
        self.coexistence.refresh(stars)
        self.config["_skip_style_learning"] = bool(self.coexistence.skip_style)
        self.learning.config = self.config

    def _ident_from_event(self, event: Any) -> dict[str, str]:
        try:
            cached = event.get_extra("_stype_ident")
            if cached:
                return cached
        except Exception:
            pass
        return self.identity_from_event(event)

    def identity_from_event(self, event: Any, persona_id: str = "") -> dict[str, str]:
        speaker_id = ""
        speaker_name = ""
        bot_id = ""
        window_tag = ""
        try:
            speaker_id = str(event.get_sender_id() or "")
        except Exception:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            speaker_id = str(getattr(sender, "user_id", "") or "")
        try:
            speaker_name = str(event.get_sender_name() or "")
        except Exception:
            speaker_name = speaker_id
        try:
            bot_id = str(event.message_obj.self_id or "")
        except Exception:
            bot_id = ""
        try:
            window_tag = str(event.unified_msg_origin or "")
        except Exception:
            window_tag = ""
        raw_id = speaker_id or "unknown"
        canonical = self.store.resolve_speaker(raw_id)
        if raw_id != canonical:
            self.store.set_alias(raw_id, canonical, speaker_name or "")
        return {
            "speaker_id": canonical,
            "speaker_name": speaker_name or canonical,
            "bot_id": bot_id,
            "window_tag": window_tag,
            "persona_id": persona_id or "",
        }

    def is_admin_event(self, event: Any) -> bool:
        if event is None:
            return False
        try:
            if str(getattr(event, "role", "") or "").lower() == "admin":
                return True
        except Exception:
            pass
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:
            pass
        try:
            if bool(event.get_extra("_stype_admin")):
                return True
        except Exception:
            pass
        return False

    def is_self_directive(self, text: str) -> bool:
        from .util import DIRECTIVE_RE, FIRST_PERSON_RE, REMEMBER_RE

        t = (text or "").strip()
        if not t:
            return False
        if REMEMBER_RE.search(t) and FIRST_PERSON_RE.search(t):
            return True
        return bool(DIRECTIVE_RE.search(t) and FIRST_PERSON_RE.search(t))

    def capture_user(self, event: Any, text: str) -> int | None:
        if not self.capture_ok(event):
            return None
        text = (text or "").strip()
        if not text:
            return None
        if not self.is_admin_event(event):
            return None
        if not self.is_self_directive(text):
            return None
        ident = self._ident_from_event(event)
        ts = now_ts()
        event_id = self.store.add_timeline(
            {
                "ts": ts,
                "role": ROLE_USER,
                "content": clip(text, 2000),
                "fingerprint": fingerprint(ident.get("persona_id"), ident["speaker_id"], ROLE_USER, ts, text),
                **ident,
            }
        )
        if event_id:
            self.learning.observe_message(text, persona_id=ident.get("persona_id") or "")
        return event_id

    def capture_bot(self, event: Any, text: str) -> int | None:
        if not self.capture_ok(event):
            return None
        text = (text or "").strip()
        if not text:
            return None
        ident = self._ident_from_event(event)
        ts = now_ts()
        return self.store.add_timeline(
            {
                "ts": ts,
                "role": ROLE_ASSISTANT,
                "content": clip(text, 2000),
                "fingerprint": fingerprint(ident.get("persona_id"), ident.get("bot_id"), ROLE_ASSISTANT, text),
                **ident,
            }
        )

    async def maybe_extract(self, force: bool = False) -> dict[str, Any]:
        if not self.enabled() or not self.config.get("extract_enabled", True):
            return {"ok": True, "skipped": True, "reason": "extract disabled"}
        now = now_ts()
        cooldown = int(self.config.get("extract_cooldown_seconds") or 45)
        if not force and now < self._extract_fail_until:
            return {"ok": True, "skipped": True, "reason": "cooldown_after_fail"}
        if not force and self._last_extract_at and now - self._last_extract_at < cooldown:
            return {"ok": True, "skipped": True, "reason": "debounce"}
        if self._extract_lock.locked():
            return {"ok": True, "skipped": True, "reason": "busy"}
        async with self._extract_lock:
            min_messages = int(self.config.get("extract_min_messages") or 8)
            try:
                result = await self.extractor.maybe_extract(min_messages=min_messages, force=force)
                self._last_extract_at = now_ts()
                if not result.get("skipped"):
                    self.store.add_usage("extract", ok=True, detail=str(result.get("events") or 0))
                return result
            except Exception as exc:  # noqa: BLE001
                fail_cd = int(self.config.get("extract_fail_cooldown_seconds") or 180)
                self._extract_fail_until = now_ts() + fail_cd
                self.store.add_usage("extract", ok=False, detail=str(exc)[:200])
                self.store.add_diag("extract_fail", {"error": str(exc)})
                return {"ok": False, "skipped": True, "reason": "extract_fail", "error": str(exc)}

    def schedule_learn(self) -> None:
        if self._learn_task and not self._learn_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._learn_task = loop.create_task(self._background_learn())

    async def _background_learn(self) -> None:
        try:
            await self.maybe_extract()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type extract failed: %s", exc)
        try:
            await self.run_learning()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type style learn failed: %s", exc)
        try:
            await self.fill_embeddings()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type embed failed: %s", exc)

    async def run_learning(self, force: bool = False, persona_id: str = "") -> dict[str, Any]:
        text = ""
        if self.get_persona_text is not None:
            try:
                maybe = self.get_persona_text(persona_id)
                text = await maybe if asyncio.iscoroutine(maybe) else (maybe or "")
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("Savage Type persona text failed: %s", exc)
                text = ""
        return await self.learning.maybe_learn(force=force, persona_text=text or "")

    async def retrieve_for(self, query: str, speaker_id: str, persona_id: str = "") -> Any:
        self._sync_embed_fn()
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        ask_other = detect_other_speaker(query, self.store.all_live(80, persona_id=persona_id), canonical)
        return await self.retriever.retrieve(
            query=query,
            speaker_id=canonical,
            top_k=int(self.config.get("top_k") or 16),
            related_limit=int(self.config.get("related_fact_limit") or 6),
            core_limit=int(self.config.get("core_fact_limit") or 4),
            ask_other_id=ask_other,
            persona_id=persona_id,
            speaker_ids=ids,
        )

    def dossier_for(self, speaker_id: str, persona_id: str = "") -> dict[str, Any]:
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        facts = self.store.live_by_speaker(canonical, persona_id=persona_id, speaker_ids=ids, limit=40)
        name = ""
        if facts:
            name = facts[0].speaker_name or ""
        return build_profile(canonical, facts, speaker_name=name)

    def list_dossiers(self, persona_id: str = "") -> list[dict[str, Any]]:
        out = []
        for row in self.store.distinct_live_speakers(persona_id=persona_id, limit=80):
            card = self.dossier_for(row["speaker_id"], persona_id=persona_id)
            if card.get("lines"):
                out.append(card)
        return out

    async def build_injection(self, query: str, speaker_id: str, persona_id: str = "") -> tuple[str, Any, dict[str, Any]]:
        result = await self.retrieve_for(query, speaker_id, persona_id=persona_id)
        learning = self.learning.pack_for(query, persona_id=persona_id, route=result.route)
        dossier = self.dossier_for(speaker_id, persona_id=persona_id)
        pack = build_pack(
            result,
            budget=int(self.config.get("inject_budget_chars") or 800),
            companion_present=any("companion" in d for d in self.coexistence.detected),
            learning=learning,
            dossier=dossier.get("card") or "",
        )
        snapshot = {
            "query": clip(query, 80),
            "speaker_id": speaker_id,
            "persona_id": persona_id,
            "route": result.route,
            "path": result.path,
            "cache": result.cache,
            "pack_chars": len(pack),
            "core": [f.id for f in result.core],
            "related": [f.id for f in result.related],
            "uncertain": [f.id for f in result.uncertain],
            "superseded": [f.id for f in result.superseded],
            "blocked": [
                {"id": h.fact.id, "reason": h.filter_reason}
                for h in result.blocked[:8]
            ],
            "jargon": [j.get("term") for j in (learning.jargon or [])],
            "fewshots": len(learning.fewshots or []),
            "persona_draft": bool(learning.persona_draft),
            "dossier": bool(dossier.get("card")),
            "injected": bool(pack),
        }
        self.store.add_diag("inject", snapshot)
        return pack, result, snapshot

    def remember(self, speaker: dict[str, str], content: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        extra = extra or {}
        payload = apply_slot(
            {
                "subject": extra.get("subject") or "self",
                "attribute": extra.get("attribute") or "note",
                "value": extra.get("value") or clip(content, 80),
                "content": clip(content, 240),
                "speaker_id": "admin",
                "speaker_name": extra.get("speaker_name") or "admin",
                "bot_id": speaker.get("bot_id") or "",
                "window_tag": speaker.get("window_tag") or "",
                "persona_id": speaker.get("persona_id") or extra.get("persona_id") or "",
                "confidence": float(extra.get("confidence") or 0.9),
                "first_person": 1,
                "explicit_correction": int(bool(extra.get("explicit_correction"))),
                "source": extra.get("source") or "tool",
                "mention_policy": extra.get("mention_policy") or "mention",
            }
        )
        return self.contradiction.ingest(payload, source_text=content)

    async def fill_embeddings(self, limit: int = 16) -> int:
        self._sync_embed_fn()
        if not self.embedding_wanted():
            return 0
        if self.retriever.embed is None:
            status = self.embedding_status()
            if status["reason"] == "need_provider":
                self.store.add_diag("embed_skip", status)
            return 0
        facts = self.store.missing_embeddings(limit=limit)
        if not facts:
            return 0
        async with self._embed_lock:
            vectors = await self._embed([f.content for f in facts])
            n = 0
            for fact, vec in zip(facts, vectors):
                if vec:
                    self.store.update_fact(fact.id, embedding=vec)
                    n += 1
            return n

    def sleep_maintenance(self) -> dict[str, Any]:
        merged = 0
        for keeper, dup in self.store.live_near_duplicates():
            evidence = list(keeper.evidence)
            for eid in dup.evidence:
                if eid not in evidence:
                    evidence.append(eid)
            self.store.update_fact(
                keeper.id,
                evidence=evidence,
                confidence=max(keeper.confidence, dup.confidence),
            )
            self.store.update_fact(
                dup.id,
                status="superseded",
                superseded_by=keeper.id,
                reason="sleep_near_duplicate",
            )
            merged += 1
        folded = fold_preference_slots(self.store)
        expired_status = expire_status_facts(self.store)
        retain_days = int(self.config.get("sleep_timeline_retain_days") or 30)
        compacted = compact_summarized_timeline(self.store, retain_days=retain_days)
        archived = archive_low_value(
            self.store,
            min_age_days=int(self.config.get("sleep_low_value_days") or 30),
            max_confidence=float(self.config.get("sleep_low_value_confidence") or 0.45),
        )
        expired = expire_persona_drafts(
            self.store,
            ttl_seconds=int(self.config.get("persona_draft_ttl_seconds") or 14 * 86400),
        )
        counts = self.store.counts()
        result = {
            "merged_duplicates": merged,
            "folded_preferences": folded,
            "expired_status": expired_status,
            "compacted_timeline": compacted,
            "archived_low_value": archived,
            "expired_persona_drafts": expired,
            **counts,
        }
        self.store.add_diag("sleep", result)
        return result

    def backup_now(self, dest_dir: Path) -> Path:
        return backup_db(self.store, dest_dir)

    def preview_archive(self, path: Path) -> dict[str, Any]:
        return preview_jsonl(path)

    def import_archive(self, path: Path, dest_dir: Path) -> dict[str, Any]:
        backup = backup_db(self.store, dest_dir)
        result = import_jsonl(self.store, path)
        result["backup"] = str(backup)
        self.store.add_diag("import_jsonl", {k: v for k, v in result.items() if k != "backup"})
        return result

    def preview_chat(self, text: str, user_names: list[str] | None = None, bot_names: list[str] | None = None) -> dict[str, Any]:
        parsed = parse_transcript(text, user_names=user_names, bot_names=bot_names)
        preview = dict(parsed)
        preview["events"] = parsed["events"][:8]
        preview["preview_only"] = True
        return preview

    def import_chat(self, text: str, user_names: list[str] | None = None, bot_names: list[str] | None = None) -> dict[str, Any]:
        parsed = parse_transcript(text, user_names=user_names, bot_names=bot_names)
        result = import_transcript_events(self.store, parsed["events"])
        result["speakers"] = parsed["speakers"]
        self.store.add_diag("import_chat", {"added": result.get("added"), "skipped": result.get("skipped")})
        return result

    def alias_suggestions(self) -> list[dict[str, Any]]:
        rows = self.store.speaker_name_map()
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            name = (row.get("speaker_name") or "").strip()
            if len(name) < 2:
                continue
            by_name.setdefault(name, []).append(row)
        mapped = {(a["alias"], a["canonical_id"]) for a in self.store.list_aliases()}
        suggestions = []
        for name, items in by_name.items():
            ids = {i["speaker_id"] for i in items}
            if len(ids) < 2:
                continue
            ranked = sorted(items, key=lambda x: x["count"], reverse=True)
            canonical = ranked[0]["speaker_id"]
            for item in ranked[1:]:
                alias = item["speaker_id"]
                if alias == canonical or (alias, canonical) in mapped:
                    continue
                suggestions.append(
                    {
                        "name": name,
                        "alias": alias,
                        "canonical_id": canonical,
                        "alias_count": item["count"],
                        "canonical_count": ranked[0]["count"],
                    }
                )
        return suggestions[:20]

    def speaker_options(self) -> list[dict[str, str]]:
        seen: dict[str, str] = {"admin": "admin"}
        for row in self.store.speaker_name_map():
            sid = str(row.get("speaker_id") or "")
            if sid and sid not in seen:
                seen[sid] = str(row.get("speaker_name") or sid)
        for fact in self.store.facts_by_status("live", limit=200):
            if fact.speaker_id and fact.speaker_id not in seen:
                seen[fact.speaker_id] = fact.speaker_name or fact.speaker_id
        return [{"id": k, "name": v} for k, v in seen.items()]

    def overview(self) -> dict[str, Any]:
        counts = self.store.counts()
        return {
            "counts": counts,
            "revision": self.store.revision(),
            "coexistence": self.coexistence.snapshot(),
            "usage": self.store.usage_summary(),
            "aliases": self.store.list_aliases(),
            "alias_suggestions": self.alias_suggestions(),
            "speakers": self.speaker_options(),
            "data_dir": str(self.store.db_path.parent),
            "embedding": self.embedding_status(),
            "config": {
                "enabled": self.enabled(),
                "capture": self.capture_ok(),
                "inject": self.inject_ok(),
                "retrieval_mode": self.config.get("retrieval_mode"),
                "embedding_enabled": bool(self.config.get("embedding_enabled")),
            },
        }

    def export_jsonl(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = self.store.export_rows()
        with dest.open("w", encoding="utf-8") as f:
            for kind, rows in data.items():
                for row in rows:
                    payload = {k: row[k] for k in row.keys()}
                    payload["table"] = kind
                    if kind == "reviews":
                        payload["review_kind"] = payload.get("review_kind") or payload.get("kind")
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return dest

    async def _llm(self, prompt: str) -> str:
        provider_id = str(self.config.get("summary_provider_id") or "").strip()
        try:
            result = await self.llm_generate(prompt, provider_id)
            text, tokens_in, tokens_out = _unpack_llm_result(result)
            self.store.add_usage(
                "llm",
                provider_id,
                True,
                len(prompt),
                len(text or ""),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            return text
        except Exception:
            self.store.add_usage("llm", provider_id, False, len(prompt), 0)
            raise

    async def navigate(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        fact_id: int = 0,
        max_steps: int = 3,
    ) -> dict[str, Any]:
        steps = []
        seen: set[int] = set()
        current = query
        hops = max(1, min(int(max_steps or 3), 3))
        if fact_id:
            seed = self.store.get_fact(fact_id)
            if seed:
                seen.add(seed.id)
                current = seed.content or query
        for i in range(hops):
            result = await self.retrieve_for(current, speaker_id, persona_id=persona_id)
            batch = []
            for fact in result.core + result.related + result.uncertain:
                if fact.id in seen:
                    continue
                seen.add(fact.id)
                batch.append({"id": fact.id, "content": clip(fact.content, 80), "attribute": fact.attribute})
                if len(batch) >= 6:
                    break
            steps.append({"step": i + 1, "query": current, "hits": batch})
            if not batch:
                break
            current = batch[0]["content"]
        return {"ok": True, "steps": steps, "unique": len(seen)}

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        provider = self.get_provider("embedding", str(self.config.get("embedding_provider_id") or ""))
        if provider is None:
            raise RuntimeError("no embedding provider")
        pid = ""
        try:
            pid = provider.meta().id
        except Exception:
            pid = ""
        try:
            if hasattr(provider, "get_embeddings"):
                vectors = await provider.get_embeddings(texts)
            else:
                vectors = [await provider.get_embedding(text) for text in texts]
            self.store.add_usage("embed", pid, True, sum(len(t) for t in texts), 0)
            return vectors
        except Exception:
            self.store.add_usage("embed", pid, False, sum(len(t) for t in texts), 0)
            raise

    async def _rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        provider = self.get_provider("rerank", str(self.config.get("rerank_provider_id") or ""))
        if provider is None:
            raise RuntimeError("no rerank provider")
        pid = ""
        try:
            pid = provider.meta().id
        except Exception:
            pid = ""
        try:
            results = await provider.rerank(query, documents, top_n=top_n)
            out: list[tuple[int, float]] = []
            for item in results or []:
                idx = getattr(item, "index", None)
                score = getattr(item, "relevance_score", None)
                if idx is None and isinstance(item, dict):
                    idx = item.get("index")
                    score = item.get("relevance_score")
                if idx is None:
                    continue
                out.append((int(idx), float(score or 0)))
            self.store.add_usage("rerank", pid, True, len(query) + sum(len(d) for d in documents), 0)
            return out
        except Exception:
            self.store.add_usage("rerank", pid, False, len(query), 0)
            raise
