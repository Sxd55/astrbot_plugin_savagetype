"""Local + Embedding + Rerank retrieval. Speaker-tagged global store."""

from __future__ import annotations

import math
import time
from typing import Any, Awaitable, Callable

from .models import Fact, RetrievalHit, RetrievalResult
from .store import Store
from .util import (
    LOW_INFO_RE,
    RECALL_RE,
    ROLE_BOT_ID,
    SCOPE_OWNER,
    STATUS_RE,
    TIME_WINDOW_RE,
    fact_weight,
    normalize_slot,
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
    keywords = " ".join(str(k) for k in (getattr(fact, "keywords", None) or []))
    blob = f"{fact.subject} {fact.attribute} {fact.value} {fact.content} {keywords}".lower()
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
        skip_ids: set[int] | None = None,
        importance_cfg: dict[str, Any] | None = None,
        skip_query_mentions: bool = False,
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
        )
        route = str(bundle["route"])
        hits: list[RetrievalHit] = bundle["hits"]
        blocked: list[RetrievalHit] = list(bundle["blocked"])
        skip_ids = skip_ids or set()
        dedup_route = route in {"long_term", "current_status"}

        mentioned_ids: set[int] = set()
        if skip_query_mentions:
            query_norm = normalize_slot(query)
            if query_norm:
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
        )

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
    ) -> None:
        """预热缓存（供 on_waiting_llm_request 使用）；不写访问计数、不出包。"""
        if self.cache_ttl <= 0:
            return
        await self._ranked_bundle(
            query, speaker_id, top_k, related_limit, core_limit,
            ask_other_id, persona_id, speaker_ids, importance_cfg,
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
    ) -> dict[str, Any]:
        route = classify_route(query)
        key = self.cache_key(query, speaker_id, persona_id)
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
            }
            if self.cache_ttl > 0:
                self._cache[key] = (time.time(), dict(bundle))
            return bundle

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
            reason = self._visibility(fact, speaker_id, query, ask_other_id, route, ids, persona_id)
            if reason:
                blocked.append(RetrievalHit(fact=fact, score=0, source="filter", filter_reason=reason))
            else:
                visible.append(fact)

        local_ranked = sorted(
            visible,
            key=lambda f: self._local_score(query, f, speaker_id, route, ids, importance_cfg),
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
        }
        if self.cache_ttl > 0:
            self._cache[key] = (time.time(), dict(bundle))
        return bundle

    def _local_score(
        self,
        query: str,
        fact: Fact,
        speaker_id: str,
        route: str,
        speaker_ids: list[str] | None = None,
        importance_cfg: dict[str, Any] | None = None,
    ) -> float:
        score = keyword_score(query, fact)
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
        if getattr(fact, "expires_at", 0) and fact.expires_at > 0 and fact.expires_at < now_ts():
            return "expired"
        if persona_id and fact.persona_id and fact.persona_id != persona_id:
            return "other_persona"
        if getattr(fact, "scope", "") == SCOPE_OWNER:
            return ""
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
