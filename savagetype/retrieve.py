"""Local + Embedding + Rerank retrieval. Speaker-tagged global store."""

from __future__ import annotations

import math
import time
from typing import Any, Awaitable, Callable

from . import tokenize as tokenizer_mod
from .bm25 import BM25Index, event_text
from .models import Event, Fact, RetrievalHit, RetrievalResult
from .store import Store
from .util import (
    HISTORY_RE,
    LOW_INFO_RE,
    RECALL_RE,
    ROLE_BOT_ID,
    SCOPE_OWNER,
    STATUS_RE,
    TIME_WINDOW_RE,
    fact_weight,
    is_private_window,
    normalize_slot,
    now_ts,
    parse_time_range,
    session_isolation,
    time_window_days,
)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
RerankFn = Callable[[str, list[str], int], Awaitable[list[tuple[int, float]]]]


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def keyword_score(query: str, fact: Fact) -> float:
    keywords = " ".join(str(k) for k in (getattr(fact, "keywords", None) or []))
    blob = f"{fact.subject} {fact.attribute} {fact.value} {fact.content} {keywords}"
    return keyword_text_score(query, blob)


def keyword_text_score(query: str, blob: str) -> float:
    q = (query or "").lower()
    text = (blob or "").lower()
    if not q or not text:
        return 0.0
    tokens = [t for t in _split_tokens(q) if len(t) >= 1]
    if not tokens:
        return 0.0
    hits = sum(1 for tok in tokens if tok in text)
    return hits / max(1, len(tokens))


def _split_tokens(text: str) -> list[str]:
    buf = []
    current = ""
    for ch in text:
        if "一" <= ch <= "鿿":
            if current:
                buf.append(current)
                current = ""
            buf.append(ch)
        elif ch.isalnum():
            current += ch
        else:
            if current:
                buf.append(current)
                current = ""
    if current:
        buf.append(current)
    return buf


def rrf_merge(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for i, fid in enumerate(ranks):
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + i + 1)
    return scores


def mmr(items: list[RetrievalHit], k: int, lambda_mult: float = 0.7) -> list[RetrievalHit]:
    if len(items) <= k:
        return items
    selected: list[RetrievalHit] = []
    remaining = list(items)
    while remaining and len(selected) < k:
        if not selected:
            selected.append(remaining.pop(0))
            continue
        best_i = 0
        best_s = -1e9
        for i, cand in enumerate(remaining):
            sim = 0.0
            for s in selected:
                sim = max(sim, _text_sim(cand.fact, s.fact))
            score = lambda_mult * cand.score - (1 - lambda_mult) * sim
            if score > best_s:
                best_s = score
                best_i = i
        selected.append(remaining.pop(best_i))
    return selected


def _text_sim(a: Fact, b: Fact) -> float:
    if a.slot_key() == b.slot_key():
        return 1.0
    sa = set(_split_tokens(a.content))
    sb = set(_split_tokens(b.content))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def classify_route(query: str) -> str:
    q = (query or "").strip()
    if not q or LOW_INFO_RE.match(q):
        return "low_info"
    if HISTORY_RE.search(q) or parse_time_range(q)[1] > 0:
        return "history"
    if STATUS_RE.search(q):
        return "current_status"
    if TIME_WINDOW_RE.search(q):
        return "time_window"
    if RECALL_RE.search(q):
        return "recall"
    return "long_term"


class Retriever:
    def __init__(
        self,
        store: Store,
        embed: EmbedFn | None = None,
        rerank: RerankFn | None = None,
        mode: str = "auto",
        cache_ttl: int = 20,
        bm25: bool = True,
    ):
        self.store = store
        self.embed = embed
        self.rerank = rerank
        self.mode = mode
        self.cache_ttl = cache_ttl
        self.bm25 = bm25
        self._terms_revision = -1
        self._cache: dict[str, tuple[float, RetrievalResult]] = {}

    def cache_key(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        window_tag: str = "",
        isolation: str = "off",
        entity_weight: float = 0.0,
        history_limit: int = 0,
        event_limit: int = 0,
    ) -> str:
        flag = "b" if self.bm25 else "k"
        window = "private" if is_private_window(window_tag) else "public"
        return (
            f"{persona_id}|{speaker_id}|{window}|{isolation}|{self.store.revision()}|"
            f"{self.mode}|{flag}|e{float(entity_weight):.2f}|h{int(history_limit)}|"
            f"t{int(event_limit)}|{query.strip()}"
        )

    def _register_custom_terms(self) -> None:
        """Feed approved jargon into the jieba dict so niche terms tokenize whole."""
        revision = self.store.revision()
        if revision == self._terms_revision:
            return
        self._terms_revision = revision
        try:
            terms = [
                str(item.payload.get("term") or "")
                for item in self.store.approved_reviews("jargon", limit=60)
            ]
            tokenizer_mod.add_terms(terms)
        except Exception:  # noqa: BLE001
            pass

    async def retrieve(
        self,
        query: str,
        speaker_id: str,
        top_k: int = 16,
        related_limit: int = 6,
        core_limit: int = 4,
        ask_other_id: str | None = None,
        persona_id: str = "",
        speaker_ids: list[str] | None = None,
        skip_ids: set[int] | None = None,
        importance_cfg: dict[str, Any] | None = None,
        skip_query_mentions: bool = False,
        window_tag: str = "",
        isolation: str = "off",
        owner_ids: set[str] | None = None,
        event_skip_ids: set[int] | None = None,
        event_limit: int = 4,
        entity_weight: float = 0.0,
        history_limit: int = 0,
    ) -> RetrievalResult:
        """检索 + 出包前过滤。

        昂贵的部分（DB 扫描、打分、Embedding、Rerank）按 query 缓存；
        去重与新颖度过滤在缓存结果上做，避免每轮都重跑昂贵路径。
        """
        bundle = await self._ranked_bundle(
            query,
            speaker_id,
            top_k,
            related_limit,
            core_limit,
            ask_other_id,
            persona_id,
            speaker_ids,
            importance_cfg,
            window_tag=window_tag,
            isolation=isolation,
            owner_ids=owner_ids,
            event_limit=event_limit,
            entity_weight=entity_weight,
            history_limit=history_limit,
        )
        route = str(bundle["route"])
        hits: list[RetrievalHit] = bundle["hits"]
        blocked: list[RetrievalHit] = list(bundle["blocked"])
        skip_ids = skip_ids or set()
        dedup_route = route in {"long_term", "current_status", "history"}

        query_norm = normalize_slot(query)
        mentioned_ids: set[int] = set()
        if skip_query_mentions and query_norm:
            for hit in hits:
                value_norm = normalize_slot(hit.fact.value or "")
                if len(value_norm) >= 2 and value_norm in query_norm:
                    mentioned_ids.add(hit.fact.id)

        if skip_ids or mentioned_ids:
            filtered: list[RetrievalHit] = []
            for hit in hits:
                fact = hit.fact
                if dedup_route and fact.id in skip_ids and not int(getattr(fact, "pinned", 0)):
                    blocked.append(
                        RetrievalHit(fact=fact, score=0, source="filter", filter_reason="recently_injected")
                    )
                    continue
                if fact.id in mentioned_ids:
                    blocked.append(
                        RetrievalHit(fact=fact, score=0, source="filter", filter_reason="query_mentioned")
                    )
                    continue
                filtered.append(hit)
            hits = mmr(filtered, k=top_k) if len(filtered) != len(hits) else filtered

        core, related, uncertain = self._slot(
            hits, bundle["ids"], core_limit, related_limit, route
        )
        for fact in core + related:
            # 原子自增，避免用缓存里的旧 access_count 回写。
            self.store.bump_access(fact.id)

        events = self._filter_events(
            list(bundle.get("events") or []),
            query_norm,
            skip_query_mentions=skip_query_mentions,
            event_skip_ids=event_skip_ids or set(),
            limit=max(1, int(event_limit or 1) * 2),
        )
        history = self._filter_history(bundle.get("history") or [], skip_ids)
        if skip_query_mentions and query_norm:
            history = [
                fact
                for fact in history
                if int(getattr(fact, "pinned", 0))
                or not self._history_mentioned(fact, query_norm)
            ]

        return RetrievalResult(
            query=query,
            route=route,
            path=str(bundle["path"]),
            cache=str(bundle["cache"]),
            hits=hits,
            blocked=blocked[:12],
            core=core,
            related=related,
            uncertain=uncertain,
            superseded=bundle["superseded"],
            events=events,
            event_blocked=list(bundle.get("event_blocked") or [])[:12],
            history=history,
            history_current=dict(bundle.get("history_current") or {}),
            history_label=str(bundle.get("history_label") or ""),
        )

    @staticmethod
    def _filter_history(history: list[Fact], skip_ids: set[int]) -> list[Fact]:
        if not skip_ids:
            return list(history)
        return [fact for fact in history if fact.id not in skip_ids or int(fact.pinned or 0)]

    @staticmethod
    def _history_mentioned(fact: Fact, query_norm: str) -> bool:
        if not query_norm:
            return False
        texts = [fact.value or ""]
        texts.extend(str(k) for k in (getattr(fact, "keywords", None) or []))
        for raw in texts:
            token = normalize_slot(raw)
            if len(token) >= 2 and token in query_norm:
                return True
        return False

    @staticmethod
    def _filter_events(
        events: list[Event],
        query_norm: str,
        *,
        skip_query_mentions: bool,
        event_skip_ids: set[int],
        limit: int,
    ) -> list[Event]:
        out: list[Event] = []
        for event in events:
            if int(event.pinned or 0):
                out.append(event)
                continue
            if event.id in event_skip_ids:
                continue
            if skip_query_mentions and query_norm:
                title = normalize_slot(event.title or "")
                if len(title) >= 2 and title in query_norm:
                    continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    async def warm(
        self,
        query: str,
        speaker_id: str,
        top_k: int = 16,
        related_limit: int = 6,
        core_limit: int = 4,
        ask_other_id: str | None = None,
        persona_id: str = "",
        speaker_ids: list[str] | None = None,
        importance_cfg: dict[str, Any] | None = None,
        window_tag: str = "",
        isolation: str = "off",
        owner_ids: set[str] | None = None,
        entity_weight: float = 0.0,
        history_limit: int = 0,
        event_limit: int = 4,
    ) -> None:
        """预热缓存（供 on_waiting_llm_request 使用）；不写访问计数、不出包。"""
        if self.cache_ttl <= 0:
            return
        await self._ranked_bundle(
            query, speaker_id, top_k, related_limit, core_limit,
            ask_other_id, persona_id, speaker_ids, importance_cfg,
            window_tag=window_tag, isolation=isolation, owner_ids=owner_ids,
            entity_weight=entity_weight, history_limit=history_limit,
            event_limit=event_limit,
        )

    async def _ranked_bundle(
        self,
        query: str,
        speaker_id: str,
        top_k: int,
        related_limit: int,
        core_limit: int,
        ask_other_id: str | None,
        persona_id: str,
        speaker_ids: list[str] | None,
        importance_cfg: dict[str, Any] | None,
        window_tag: str = "",
        isolation: str = "off",
        owner_ids: set[str] | None = None,
        event_limit: int = 4,
        entity_weight: float = 0.0,
        history_limit: int = 0,
    ) -> dict[str, Any]:
        route = classify_route(query)
        isolation = session_isolation(isolation)
        key = self.cache_key(
            query, speaker_id, persona_id, window_tag, isolation,
            entity_weight, history_limit, event_limit,
        )
        if self.cache_ttl > 0:
            cached = self._cache.get(key)
            if cached and time.time() - cached[0] < self.cache_ttl:
                bundle = dict(cached[1])
                bundle["cache"] = "hit"
                return bundle

        if route == "low_info":
            bundle: dict[str, Any] = {
                "route": route,
                "path": "skip",
                "cache": "miss",
                "hits": [],
                "blocked": [],
                "ids": [],
                "superseded": [],
                "events": [],
                "event_blocked": [],
                "history": [],
                "history_current": {},
                "history_label": "",
            }
            if self.cache_ttl > 0:
                self._cache[key] = (time.time(), dict(bundle))
            return bundle

        entity_fact_ids: set[int] = set()
        entity_event_ids: set[int] = set()
        entity_names: list[str] = []
        if entity_weight > 0:
            entity_names = self.store.entities_in_text(query)
            if entity_names:
                entity_fact_ids, entity_event_ids = self.store.entity_refs(
                    entity_names, persona_id=persona_id
                )

        ids = list(speaker_ids or [speaker_id])
        candidates = self.store.live_facts(
            speaker_id=speaker_id,
            limit=240,
            persona_id=persona_id,
            speaker_ids=ids,
        )
        # 主人条目单独并入，避免大库时被候选截断挤掉。
        owner_facts = self.store.owner_facts(limit=200)
        seen_ids = {f.id for f in candidates}
        candidates.extend(f for f in owner_facts if f.id not in seen_ids)
        if ask_other_id:
            extra = self.store.live_facts(
                speaker_id=ask_other_id,
                limit=80,
                persona_id=persona_id,
                speaker_ids=self.store.speaker_ids_for(ask_other_id),
            )
            seen = {f.id for f in candidates}
            candidates.extend(f for f in extra if f.id not in seen)

        blocked: list[RetrievalHit] = []
        visible: list[Fact] = []
        for fact in candidates:
            if fact.speaker_id == ROLE_BOT_ID:
                # Bot 设定走独立块注入，不占检索槽位（避免被包构建剔除后空占名额）。
                continue
            reason = self._visibility(
                fact,
                speaker_id,
                query,
                ask_other_id,
                route,
                ids,
                persona_id,
                window_tag=window_tag,
                isolation=isolation,
                owner_ids=owner_ids,
            )
            if reason:
                blocked.append(RetrievalHit(fact=fact, score=0, source="filter", filter_reason=reason))
            else:
                visible.append(fact)

        bm25_scores: dict[int, float] | None = None
        if self.bm25:
            try:
                self._register_custom_terms()
                index = BM25Index(visible)
                index.prepare(query)
                raw = {fact.id: index.score(fact) for fact in visible}
                top = max(raw.values(), default=0.0)
                # 归一化到 0-1，保持与既有先验权重（置信度/新旧/重要性）的平衡。
                bm25_scores = {fid: value / top for fid, value in raw.items()} if top > 0 else {}
            except Exception:  # noqa: BLE001
                bm25_scores = None

        try:
            events, event_blocked = self._rank_events(
                query,
                speaker_id,
                ids,
                persona_id,
                route,
                window_tag,
                isolation,
                owner_ids,
                max(2, event_limit * 2),
                entity_event_ids,
                entity_weight,
            )
        except Exception:  # noqa: BLE001
            events, event_blocked = [], []

        history: list[Fact] = []
        history_current: dict[int, str] = {}
        history_label = ""
        if route == "history" and history_limit > 0:
            hist_ids = list(ids)
            if ask_other_id:
                hist_ids = list({*hist_ids, *self.store.speaker_ids_for(ask_other_id)})
            history, history_current, history_label = self._collect_history(
                query,
                hist_ids,
                persona_id,
                isolation,
                owner_ids,
                window_tag,
                entity_fact_ids,
                entity_weight,
                max(2, history_limit * 2),
            )

        local_ranked = sorted(
            visible,
            key=lambda f: self._local_score(
                query, f, speaker_id, route, ids, importance_cfg, bm25_scores,
                entity_fact_ids, entity_weight,
            ),
            reverse=True,
        )
        local_ids = [f.id for f in local_ranked[: max(top_k * 2, 12)]]
        rank_lists = [local_ids]
        path = "basic"
        embed_map: dict[int, float] = {}

        if self.embed is not None:
            try:
                q_vec = (await self.embed([query]))[0]
                scored = []
                for f in visible:
                    if f.embedding:
                        scored.append((f.id, cosine(q_vec, f.embedding)))
                scored.sort(key=lambda x: x[1], reverse=True)
                rank_lists.append([fid for fid, _ in scored[: max(top_k * 2, 12)]])
                embed_map = {fid: s for fid, s in scored}
                path = "basic+emb"
            except Exception:  # noqa: BLE001
                path = "fallback_basic"

        fused = rrf_merge(rank_lists)
        fused_facts = {f.id: f for f in visible}
        hits = []
        for fid, rrf in sorted(fused.items(), key=lambda x: x[1], reverse=True)[: max(top_k * 2, 12)]:
            fact = fused_facts.get(fid)
            if not fact:
                continue
            keyword_part = (
                float(bm25_scores.get(fid, 0.0))
                if bm25_scores is not None
                else keyword_score(query, fact)
            )
            score = rrf + 0.15 * keyword_part + 0.1 * embed_map.get(fid, 0)
            if fact.speaker_id in ids:
                score += 0.08
            hits.append(RetrievalHit(fact=fact, score=score, source="rrf"))

        use_rerank = (self.mode == "rerank" or (self.mode == "auto" and self.rerank)) and self.rerank
        if use_rerank and hits:
            try:
                docs = [h.fact.content for h in hits]
                reranked = await self.rerank(query, docs, top_k)
                new_hits = []
                for idx, rel in reranked:
                    if 0 <= idx < len(hits):
                        h = hits[idx]
                        new_hits.append(RetrievalHit(fact=h.fact, score=rel, source="rerank"))
                if new_hits:
                    hits = new_hits
                    path = "rerank"
            except Exception:  # noqa: BLE001
                path = "fallback_basic"

        hits = mmr(hits, k=top_k)
        superseded = []
        if route in {"recall", "long_term"}:
            superseded = self.store.recent_superseded(ids, persona_id=persona_id, limit=6)[:3]

        bundle = {
            "route": route,
            "path": path,
            "cache": "miss",
            "hits": hits,
            "blocked": blocked[:12],
            "ids": ids,
            "superseded": superseded,
            "events": events,
            "event_blocked": event_blocked[:12],
            "history": history,
            "history_current": history_current,
            "history_label": history_label,
        }
        if self.cache_ttl > 0:
            self._cache[key] = (time.time(), dict(bundle))
        return bundle

    def _rank_events(
        self,
        query: str,
        speaker_id: str,
        speaker_ids: list[str],
        persona_id: str,
        route: str,
        window_tag: str,
        isolation: str,
        owner_ids: set[str] | None,
        limit: int,
        entity_ids: set[int] | None = None,
        entity_boost: float = 0.0,
    ) -> tuple[list[Event], list[Event]]:
        since = 0
        until = 0
        if route == "current_status":
            since = now_ts() - 7 * 86400
        elif route == "time_window":
            days = time_window_days(query)
            since = now_ts() - days * 86400 if days else 0
        elif route == "history":
            start, end, _label = parse_time_range(query)
            if end > 0:
                since = start or 0
                until = end
        same_window = isolation in {"owner", "strict"} and bool(window_tag)
        if same_window:
            candidates = self.store.live_events(
                window_tag=window_tag,
                persona_id=persona_id,
                limit=240,
                since_ts=since,
                until_ts=until,
                include_owner=True,
                statuses=("live", "archived") if route == "history" else ("live",),
            )
        else:
            candidates = self.store.live_events(
                speaker_ids=speaker_ids,
                persona_id=persona_id,
                limit=240,
                since_ts=since,
                until_ts=until,
                include_owner=True,
                statuses=("live", "archived") if route == "history" else ("live",),
            )
        blocked: list[Event] = []
        visible: list[Event] = []
        for event in candidates:
            reason = self._event_visibility(
                event, speaker_ids, window_tag, isolation, owner_ids, persona_id, same_window, route
            )
            if reason:
                blocked.append(event)
            else:
                visible.append(event)

        bm25_scores: dict[int, float] = {}
        if self.bm25 and visible:
            try:
                index = BM25Index(visible, text_fn=event_text)
                index.prepare(query)
                raw = {event.id: index.score(event) for event in visible}
                top = max(raw.values(), default=0.0)
                if top > 0:
                    bm25_scores = {eid: value / top for eid, value in raw.items()}
            except Exception:  # noqa: BLE001
                bm25_scores = {}
        ranked = sorted(
            visible,
            key=lambda e: self._event_score(
                query, e, route, bm25_scores, entity_ids or set(), until, entity_boost
            ),
            reverse=True,
        )
        return ranked[: max(1, int(limit or 1))], blocked

    def _event_score(
        self,
        query: str,
        event: Event,
        route: str,
        bm25_scores: dict[int, float],
        entity_ids: set[int] | None = None,
        until_ts: int = 0,
        entity_boost: float = 0.0,
    ) -> float:
        if bm25_scores:
            score = float(bm25_scores.get(event.id, 0.0))
        else:
            score = keyword_text_score(query, event_text(event))
        age_days = max(0, (now_ts() - (event.end_ts or now_ts())) / 86400)
        score += 0.25 / (1.0 + age_days / 14)
        score += 0.2 * min(1.0, float(event.confidence or 0))
        score += 0.25 * fact_weight(event, now_ts())
        if int(event.pinned or 0):
            score += 0.25
        if route == "current_status" and age_days > 7:
            score -= 0.4
        if route == "time_window":
            score += 0.15
        if route == "history":
            score += 0.15
            if until_ts and int(event.end_ts or 0) <= until_ts:
                score += 0.1
        if entity_boost > 0 and entity_ids and event.id in entity_ids:
            score += entity_boost
        return score

    def _collect_history(
        self,
        query: str,
        speaker_ids: list[str],
        persona_id: str,
        isolation: str,
        owner_ids: set[str] | None,
        window_tag: str,
        entity_ids: set[int],
        entity_boost: float,
        limit: int,
    ) -> tuple[list[Fact], dict[int, str], str]:
        start, end, label = parse_time_range(query)
        if end <= 0:
            start, end, label = 0, now_ts(), "当时"
        hist_ids = list(speaker_ids)
        # 点名查别人的历史：被叫到的人（含只有旧事实、已不在 live 里的人）也要进候选。
        for pname in self.store.person_names_in_text(query):
            for sid in self.store.speaker_ids_by_name(pname):
                if sid and sid not in hist_ids:
                    hist_ids.append(sid)
        facts = self.store.facts_in_window(
            start,
            end,
            speaker_ids=hist_ids,
            persona_id=persona_id,
            include_owner=True,
            limit=240,
        )
        visible: list[Fact] = []
        for fact in facts:
            if fact.status == "live":
                continue
            if fact.speaker_id == ROLE_BOT_ID:
                continue
            reason = self._history_visibility(
                fact, hist_ids, window_tag, isolation, owner_ids, persona_id
            )
            if reason:
                # 被点名只放行「别人」的历史，不放行主人私事/私聊来源（与 _visibility 同序）。
                if reason != "other_speaker":
                    continue
                name = (fact.speaker_name or "").strip()
                sid = (fact.speaker_id or "").strip()
                named = (name and len(name) >= 2 and name in query) or (
                    sid and len(sid) >= 4 and sid in query
                )
                if not named:
                    continue
            visible.append(fact)
        bm25_scores: dict[int, float] = {}
        if self.bm25 and visible:
            try:
                index = BM25Index(visible)
                index.prepare(query)
                raw = {fact.id: index.score(fact) for fact in visible}
                top = max(raw.values(), default=0.0)
                if top > 0:
                    bm25_scores = {fid: value / top for fid, value in raw.items()}
            except Exception:  # noqa: BLE001
                bm25_scores = {}
        now = now_ts()
        span = max(1, end - start)

        def score(fact: Fact) -> float:
            value = float(bm25_scores.get(fact.id, 0.0))
            value += 0.2 / (1.0 + max(0, (now - (fact.updated_at or now)) / 86400) / 30)
            value += 0.1 * min(1.0, float(fact.confidence or 0))
            if entity_boost > 0 and entity_ids and fact.id in entity_ids:
                value += entity_boost
            if int(fact.pinned or 0):
                value += 0.2
            inside = max(0, min(span, end - max(start, int(fact.created_at or 0))))
            if span and inside:
                value += 0.05
            return value

        visible.sort(key=score, reverse=True)
        picked = visible[: max(1, int(limit or 1))]
        current: dict[int, str] = {}
        for fact in picked:
            seen = {fact.id}
            target = int(getattr(fact, "superseded_by", 0) or 0)
            for _hop in range(3):
                if not target or target in seen:
                    break
                seen.add(target)
                latest = self.store.get_fact(target)
                if latest is None:
                    break
                if latest.status == "live":
                    current[fact.id] = latest.plain or latest.value or latest.content
                    break
                target = int(getattr(latest, "superseded_by", 0) or 0)
        return picked, current, label

    def _history_visibility(
        self,
        fact: Fact,
        speaker_ids: list[str],
        window_tag: str,
        isolation: str,
        owner_ids: set[str] | None,
        persona_id: str,
    ) -> str:
        if persona_id and fact.persona_id and fact.persona_id != persona_id:
            return "other_persona"
        ids = set(speaker_ids or []) | {"", "bot_self"}
        if isolation != "off":
            owners = set(owner_ids or set())
            if getattr(fact, "scope", "") == SCOPE_OWNER and owners:
                if not (ids & owners):
                    return "owner_private"
            elif isolation == "strict":
                origin = str(getattr(fact, "window_tag", "") or "")
                if is_private_window(origin):
                    if not is_private_window(window_tag or ""):
                        return "private_origin"
                    if fact.speaker_id not in ids or fact.speaker_id in {"", "bot_self"}:
                        return "private_origin"
        if getattr(fact, "scope", "") == SCOPE_OWNER:
            return ""
        if fact.speaker_id in ids:
            return ""
        return "other_speaker"

    def _event_visibility(
        self,
        event: Event,
        speaker_ids: list[str],
        window_tag: str,
        isolation: str,
        owner_ids: set[str] | None,
        persona_id: str,
        same_window: bool,
        route: str = "",
    ) -> str:
        if event.status != "live":
            # 历史路由允许查看已归档事件（时序查询的本意）；待审的一律不给。
            if not (route == "history" and event.status == "archived"):
                return "not_live"
        if event.review_status == "needs_review":
            return "needs_review"
        if persona_id and event.persona_id and event.persona_id != persona_id:
            return "other_persona"
        ids = set(speaker_ids or [])
        if isolation == "off":
            if set(event.speaker_ids or []) & ids:
                return ""
            if (window_tag and event.window_tag and event.window_tag == window_tag):
                return ""
            if event.scope == SCOPE_OWNER and (not owner_ids or ids & set(owner_ids)):
                return ""
            return "other_speaker"
        if same_window:
            return ""
        if not window_tag:
            # 没有会话上下文（命令/工具）：只给当事人自己看。
            if set(event.speaker_ids or []) & ids:
                return ""
            if event.scope == SCOPE_OWNER and owner_ids and ids & set(owner_ids):
                return ""
            return "other_session"
        return "other_session"

    def _local_score(
        self,
        query: str,
        fact: Fact,
        speaker_id: str,
        route: str,
        speaker_ids: list[str] | None = None,
        importance_cfg: dict[str, Any] | None = None,
        bm25_scores: dict[int, float] | None = None,
        entity_ids: set[int] | None = None,
        entity_boost: float = 0.0,
    ) -> float:
        if bm25_scores is None:
            score = keyword_score(query, fact)
        else:
            score = float(bm25_scores.get(fact.id, 0.0))
        age_days = max(0, (now_ts() - (fact.updated_at or now_ts())) / 86400)
        recency = 1.0 / (1.0 + age_days / 14)
        score += 0.2 * recency
        score += 0.25 * min(1.0, fact.confidence)
        ids = set(speaker_ids or [speaker_id])
        if fact.speaker_id in ids:
            score += 0.15
        if getattr(fact, "scope", "") == SCOPE_OWNER:
            score += 0.12
        if importance_cfg:
            weight = float(importance_cfg.get("weight") or 0)
            if weight:
                score += weight * fact_weight(
                    fact,
                    now_ts(),
                    half_life_days=float(importance_cfg.get("half_life_days") or 30),
                    reinforce_factor=float(importance_cfg.get("reinforce_factor") or 0.5),
                    max_multiplier=float(importance_cfg.get("max_multiplier") or 3),
                )
        if int(getattr(fact, "pinned", 0)):
            score += 0.2
        if route == "current_status" and age_days > 2:
            score -= 0.5
        if entity_boost > 0 and entity_ids and fact.id in entity_ids:
            score += entity_boost
        return score

    def _visibility(
        self,
        fact: Fact,
        speaker_id: str,
        query: str,
        ask_other_id: str | None,
        route: str,
        speaker_ids: list[str] | None = None,
        persona_id: str = "",
        window_tag: str = "",
        isolation: str = "off",
        owner_ids: set[str] | None = None,
    ) -> str:
        if fact.status != "live":
            return "not_live"
        if getattr(fact, "expires_at", 0) and fact.expires_at > 0 and fact.expires_at < now_ts():
            return "expired"
        if persona_id and fact.persona_id and fact.persona_id != persona_id:
            return "other_persona"
        ids = set(speaker_ids or [speaker_id]) | {"", "bot_self"}
        if isolation != "off":
            owners = set(owner_ids or set())
            if getattr(fact, "scope", "") == SCOPE_OWNER and owners:
                # 主人记忆只在主人自己的会话里注入，不再广播给别人。
                if not (ids & owners):
                    return "owner_private"
            elif isolation == "strict":
                origin = str(getattr(fact, "window_tag", "") or "")
                if is_private_window(origin):
                    if not is_private_window(window_tag or ""):
                        return "private_origin"
                    if fact.speaker_id not in ids or fact.speaker_id in {"", "bot_self"}:
                        return "private_origin"
        if getattr(fact, "scope", "") == SCOPE_OWNER:
            return ""
        if fact.speaker_id in ids:
            if route == "current_status":
                age_days = (now_ts() - fact.updated_at) / 86400
                if age_days > 2 and fact.attribute not in {"likes", "name", "identity"}:
                    return "stale_for_status"
            return ""
        if ask_other_id and fact.speaker_id in set(self.store.speaker_ids_for(ask_other_id)):
            return ""
        name = (fact.speaker_name or "").strip()
        sid = (fact.speaker_id or "").strip()
        if name and len(name) >= 2 and name in query:
            return ""
        if sid and len(sid) >= 4 and sid in query:
            return ""
        return "other_speaker"

    def _slot(
        self,
        hits: list[RetrievalHit],
        speaker_ids: list[str],
        core_limit: int,
        related_limit: int,
        route: str,
    ) -> tuple[list[Fact], list[Fact], list[Fact]]:
        core: list[Fact] = []
        related: list[Fact] = []
        uncertain: list[Fact] = []
        ids = set(speaker_ids)
        for hit in hits:
            fact = hit.fact
            if fact.mention_policy == "uncertain" or fact.confidence < 0.45:
                if len(uncertain) < 3:
                    uncertain.append(fact)
                continue
            if (
                (
                    fact.speaker_id in ids
                    or getattr(fact, "scope", "") == SCOPE_OWNER
                    or int(getattr(fact, "pinned", 0))
                )
                and fact.confidence >= 0.7
                and fact.attribute in {"likes", "dislikes", "name", "identity", "habit", "promise"}
                and len(core) < core_limit
            ):
                core.append(fact)
                continue
            if len(related) < related_limit:
                related.append(fact)
        if route == "current_status":
            related = related[: max(2, related_limit // 2)]
        return core, related, uncertain


def detect_other_speaker(query: str, facts: list[Fact], current_id: str) -> str | None:
    for fact in facts:
        if fact.speaker_id in {current_id, "", "bot_self"}:
            continue
        name = (fact.speaker_name or "").strip()
        sid = (fact.speaker_id or "").strip()
        if name and len(name) >= 2 and name in query:
            return fact.speaker_id
        if sid and len(sid) >= 4 and sid in query:
            return fact.speaker_id
    return None
