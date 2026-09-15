"""BM25 keyword ranking over fact texts. Tokenizer comes from .tokenize."""

from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Iterable

from . import tokenize as tokenizer_mod
from .models import Fact


def fact_text(fact: Fact) -> str:
    keywords = " ".join(str(k) for k in (getattr(fact, "keywords", None) or []))
    return f"{fact.subject} {fact.attribute} {fact.value} {fact.content} {keywords}"


def event_text(event: Any) -> str:
    highlights = " ".join(str(h) for h in (getattr(event, "highlights", None) or []))
    keywords = " ".join(str(k) for k in (getattr(event, "keywords", None) or []))
    summary = getattr(event, "summary", "") or ""
    return f"{getattr(event, 'title', '')} {summary} {highlights} {keywords}"


class BM25Index:
    """Small in-memory index rebuilt per retrieval query (corpus is per-speaker)."""

    K1 = 1.5
    B = 0.75

    def __init__(
        self,
        facts: Iterable[Any],
        tokenize_fn: Callable[[str], list[str]] | None = None,
        text_fn: Callable[[Any], str] | None = None,
    ):
        self._tokenize = tokenize_fn or tokenizer_mod.tokens
        self._text = text_fn or fact_text
        self._docs: dict[int, list[str]] = {}
        df: Counter[str] = Counter()
        total = 0
        for fact in facts:
            toks = self._tokenize(self._text(fact))
            self._docs[fact.id] = toks
            total += len(toks)
            for term in set(toks):
                df[term] += 1
        self.n = max(1, len(self._docs))
        self.avgdl = (total / self.n) if self._docs else 0.0
        self._df = df
        self._idf: dict[str, float] = {}
        self._query_tf: Counter[str] = Counter()

    def prepare(self, query: str) -> None:
        self._query_tf = Counter(self._tokenize(query or ""))
        self._idf = {}
        for term in self._query_tf:
            n_qt = self._df.get(term, 0)
            # +1 form keeps IDF positive even for very common terms.
            self._idf[term] = math.log(1.0 + (self.n - n_qt + 0.5) / (n_qt + 0.5))

    def score(self, fact: Fact) -> float:
        tokens = self._docs.get(fact.id)
        if not tokens or not self._query_tf:
            return 0.0
        doc_tf = Counter(tokens)
        dl = len(tokens)
        avgdl = self.avgdl or 1.0
        total = 0.0
        for term, _query_count in self._query_tf.items():
            f = doc_tf.get(term, 0)
            if not f:
                continue
            idf = self._idf.get(term, 0.0)
            if idf <= 0:
                continue
            denom = f + self.K1 * (1.0 - self.B + self.B * dl / avgdl)
            total += idf * f * (self.K1 + 1.0) / denom
        return total
