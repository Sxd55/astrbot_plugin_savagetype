"""跨会话画像卡（A 层）：同一个人在任何会话都拿到一致的称呼 / 身份 / 偏好 / 语气锚点。

只读已有事实（按 speaker_ids 归并同一人），不做检索打分；每轮注入，属于稳定锚点。
不搬运第三方内容；`mention_policy=tone` 的条目只转成语气提示，不复述细节。
"""

from __future__ import annotations

from typing import Any

from .models import Fact
from .util import SCOPE_OWNER, clip, is_private_window, now_ts

SECTION_ORDER = ("name", "identity", "likes", "dislikes", "habit", "promise", "status", "note")
SECTION_TITLES = {
    "name": "称呼",
    "identity": "身份",
    "likes": "偏好",
    "dislikes": "不喜欢",
    "habit": "习惯",
    "promise": "约定",
    "status": "近况",
    "note": "备注",
}
PER_ATTR_LIMIT = 3
VALUE_CHARS = 24
TONE_CHARS = 40


def _pick(text: str) -> str:
    return clip((text or "").strip(), VALUE_CHARS)


def _section_value(fact: Fact) -> str:
    """卡片正文优先用规范值（短），没有值才退回句子。"""
    for raw in (
        getattr(fact, "value", ""),
        getattr(fact, "plain", ""),
        getattr(fact, "content", ""),
    ):
        text = str(raw or "").strip()
        if text:
            return _pick(text)
    return ""


def _tone_hint(fact: Fact) -> str:
    """语气行更想读到原话，所以 plain 优先。"""
    for raw in (
        getattr(fact, "plain", ""),
        getattr(fact, "value", ""),
        getattr(fact, "content", ""),
    ):
        text = str(raw or "").strip()
        if text:
            return _pick(text)
    return ""


def _collect(facts: list[Fact]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {key: [] for key in SECTION_ORDER}
    for fact in facts:
        if getattr(fact, "mention_policy", "") == "tone":
            continue  # 语气类只在语气行体现，不复述内容
        attr = str(getattr(fact, "attribute", "") or "").strip()
        if attr not in buckets:
            continue
        if attr == "status":
            expires = int(getattr(fact, "expires_at", 0) or 0)
            if expires and expires < now_ts():
                continue  # 已过期的近况不进画像
        value = _section_value(fact)
        if not value or value in buckets[attr]:
            continue
        if len(buckets[attr]) >= PER_ATTR_LIMIT:
            continue
        buckets[attr].append(value)
    return buckets


def _window_visible(fact: Fact, window_tag: str, isolation: str) -> bool:
    """strict 隔离时，私聊来源的事实在非私聊窗口不可见（群来源进私聊保持可见）。

    window_tag 为空表示「不针对具体窗口」（面板 / 命令查看跨会话画像），不过滤。
    """
    mode = (isolation or "").strip().lower()
    current = (window_tag or "").strip()
    if mode in ("", "off") or not current:
        return True
    origin = str(getattr(fact, "window_tag", "") or "").strip()
    if not origin:
        return True  # 老数据没有来源窗口，保守放行
    if mode == "strict" and is_private_window(origin) and not is_private_window(current):
        return False
    return True


def build_profile_card(
    store,
    speaker_id: str,
    persona_id: str = "",
    max_chars: int = 300,
    window_tag: str = "",
    isolation: str = "",
) -> tuple[str, dict[str, Any]]:
    """组装当前说话人的跨会话画像卡。

    Returns:
        (card_text, meta)。无内容或关闭时 card_text 为空字符串。
    """
    canonical = store.resolve_speaker(speaker_id)
    ids = store.speaker_ids_for(canonical)
    facts = store.live_by_speaker(
        canonical,
        persona_id=persona_id,
        speaker_ids=ids,
        limit=60,
    )
    if window_tag and isolation:
        facts = [fact for fact in facts if _window_visible(fact, window_tag, isolation)]
    is_owner = any(getattr(fact, "scope", "") == SCOPE_OWNER for fact in facts)
    name = ""
    for fact in facts:
        if getattr(fact, "attribute", "") == "name" and getattr(fact, "value", ""):
            name = str(fact.value).strip()
            break
    if not name:
        profile = store.get_profile(canonical)
        name = str(getattr(profile, "speaker_name", "") or "").strip() or str(canonical)

    buckets = _collect(facts)
    tone_facts = [
        fact
        for fact in facts
        if getattr(fact, "mention_policy", "") == "tone"
        and (getattr(fact, "plain", "") or getattr(fact, "value", ""))
    ]

    title = f"{name}（主人）" if is_owner else name
    lines = [f"【画像】{title}"]
    for attr in SECTION_ORDER:
        values = buckets.get(attr) or []
        if not values:
            continue
        lines.append(f"{SECTION_TITLES.get(attr, attr)}：" + "、".join(values))
    if tone_facts:
        hints = "、".join(hint for hint in (_tone_hint(fact) for fact in tone_facts[:2]) if hint)
        if hints:
            lines.append(f"语气：{clip(hints, TONE_CHARS)}（只影响语气，别复述）")

    if len(lines) <= 1:
        card = ""
    else:
        card = "\n".join(lines)
        if max_chars > 0 and len(card) > max_chars:
            card = clip(card, max_chars)
    meta = {
        "speaker_id": canonical,
        "name": name,
        "is_owner": is_owner,
        "chars": len(card),
        "sections": [attr for attr in SECTION_ORDER if buckets.get(attr)],
        "tone": len(tone_facts),
        "facts": len(facts),
    }
    return card, meta
