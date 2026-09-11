"""Local + Embedding + Rerank retrieval. Speaker-tagged global store."""

from __future__ import annotations

import math
import time
from typing import Awaitable, Callable

from .models import Fact, RetrievalHit, RetrievalResult
from .store import Store
from .util import (
    LOW_INFO_RE,
    RECALL_RE,
    STATUS_RE,
    TIME_WINDOW_RE,
    now_ts,
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
    q = (query or "").lower()
    blob = f"{fact.subject} {fact.attribute} {fact.value} {fact.content}".lower()
    if not q or not blob:
        return 0.0
    hits = 0
    tokens = [t for t in _split_tokens(q) if len(t) >= 1]
    if not tokens:
        return 0.0
    for tok in tokens:
        if tok in blob:
            hits += 1
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
    ):
        self.store = store
        self.embed = embed
        self.rerank = rerank
        self.mode = mode
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, RetrievalResult]] = {}

    def cache_key(self, query: str, speaker_id: str, persona_id: str = "") -> str:
        return f"{persona_id}|{speaker_id}|{self.store.revision()}|{self.mode}|{query.strip()}"

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
    ) -> RetrievalResult:
        route = classify_route(query)
        key = self.cache_key(query, speaker_id, persona_id)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_ttl:
            result = cached[1]
            result.cache = "hit"
            return result

        if route == "low_info":
            result = RetrievalResult(
                query=query,
                route=route,
                path="skip",
                cache="miss",
                hits=[],
                blocked=[],
                core=[],
                related=[],
                uncertain=[],
                superseded=[],
            )
            self._cache[key] = (time.time(), result)
            return result

        ids = list(speaker_ids or [speaker_id])
        candidates = self.store.live_facts(
            speaker_id=speaker_id,
            limit=240,
            persona_id=persona_id,
            speaker_ids=ids,
        )
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
            reason = self._visibility(fact, speaker_id, query, ask_other_id, route, ids, persona_id)
            if reason:
                blocked.append(RetrievalHit(fact=fact, score=0, source="filter", filter_reason=reason))
            else:
                visible.append(fact)

        local_ranked = sorted(
            visible,
            key=lambda f: self._local_score(query, f, speaker_id, route, ids),
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
            score = rrf + 0.15 * keyword_score(query, fact) + 0.1 * embed_map.get(fid, 0)
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
        core, related, uncertain = self._slot(hits, ids, core_limit, related_limit, route)
        superseded = []
        if route in {"recall", "long_term"}:
            superseded = self.store.recent_superseded(ids, persona_id=persona_id, limit=6)[:3]

        for fact in core + related:
            self.store.update_fact(
                fact.id,
                access_count=fact.access_count + 1,
                last_accessed=now_ts(),
            )

        result = RetrievalResult(
            query=query,
            route=route,
            path=path,
            cache="miss",
            hits=hits,
            blocked=blocked[:12],
            core=core,
            related=related,
            uncertain=uncertain,
            superseded=superseded,
        )
        self._cache[key] = (time.time(), result)
        return result

    def _local_score(
        self,
        query: str,
        fact: Fact,
        speaker_id: str,
        route: str,
        speaker_ids: list[str] | None = None,
    ) -> float:
        score = keyword_score(query, fact)
        age_days = max(0, (now_ts() - (fact.updated_at or now_ts())) / 86400)
        recency = 1.0 / (1.0 + age_days / 14)
        score += 0.2 * recency
        score += 0.25 * min(1.0, fact.confidence)
        ids = set(speaker_ids or [speaker_id])
        if fact.speaker_id in ids:
            score += 0.15
        if route == "current_status" and age_days > 2:
            score -= 0.5
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
    ) -> str:
        if fact.status != "live":
            return "not_live"
        if persona_id and fact.persona_id and fact.persona_id != persona_id:
            return "other_persona"
        ids = set(speaker_ids or [speaker_id]) | {"", "bot_self"}
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
                fact.speaker_id in ids
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
