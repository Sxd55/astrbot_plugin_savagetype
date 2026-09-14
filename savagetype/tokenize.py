"""Pluggable tokenizer for BM25: jieba when importable, builtin fallback otherwise.

The builtin path keeps the original behavior (CJK chars + alnum runs), so the
plugin still works with zero third-party dependencies; jieba only improves
term precision and lets approved jargon words be registered into the dict.
"""

from __future__ import annotations

import re
import threading
from typing import Iterable

_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_MAX_TERM_LEN = 32

_lock = threading.Lock()
_jieba = None
_probed = False
_custom_terms: set[str] = set()


def _load_jieba():
    global _jieba, _probed
    if _probed:
        return _jieba
    with _lock:
        if _probed:
            return _jieba
        _probed = True
        try:
            import jieba  # type: ignore

            jieba.setLogLevel(20)
            for term in _custom_terms:
                jieba.add_word(term)
            _jieba = jieba
        except Exception:  # noqa: BLE001
            _jieba = None
    return _jieba


def available() -> bool:
    return _load_jieba() is not None


def name() -> str:
    return "jieba" if _load_jieba() is not None else "builtin"


def _keep(token: str) -> bool:
    token = token.strip()
    if not token or len(token) > _MAX_TERM_LEN:
        return False
    return not _PUNCT_RE.match(token)


def builtin_tokens(text: str) -> list[str]:
    """CJK chars as single tokens, alnum runs kept whole. No dependency."""
    buf: list[str] = []
    current = ""
    for ch in (text or "").lower():
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
    return [tok for tok in buf if _keep(tok)]


def tokens(text: str) -> list[str]:
    jieba = _load_jieba()
    if jieba is not None:
        try:
            return [tok.lower() for tok in jieba.lcut_for_search(text or "") if _keep(tok)]
        except Exception:  # noqa: BLE001
            pass
    return builtin_tokens(text)


def add_terms(terms: Iterable[str]) -> int:
    """Register custom words (jargon, names, project terms) for jieba."""
    added = 0
    jieba = _load_jieba()
    with _lock:
        for raw in terms:
            term = (raw or "").strip()
            if not term or len(term) > _MAX_TERM_LEN or term in _custom_terms:
                continue
            _custom_terms.add(term)
            added += 1
            if jieba is not None:
                try:
                    jieba.add_word(term)
                except Exception:  # noqa: BLE001
                    pass
    return added
