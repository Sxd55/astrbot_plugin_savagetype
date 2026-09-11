"""Canonical fact slots so heuristic and LLM writes collide on the same key."""

from __future__ import annotations

import re

from .util import normalize_slot

ATTR_ALIASES = {
    "likes": "likes",
    "like": "likes",
    "love": "likes",
    "喜欢": "likes",
    "爱好": "likes",
    "口味": "likes",
    "偏好": "likes",
    "喜好": "likes",
    "爱喝": "likes",
    "爱吃": "likes",
    "dislikes": "dislikes",
    "dislike": "dislikes",
    "讨厌": "dislikes",
    "受不了": "dislikes",
    "name": "name",
    "称呼": "name",
    "名字": "name",
    "昵称": "name",
    "小名": "name",
    "identity": "identity",
    "身份": "identity",
    "职业": "identity",
    "habit": "habit",
    "习惯": "habit",
    "promise": "promise",
    "约定": "promise",
    "承诺": "promise",
    "note": "note",
    "correction": "note",
    "纠正": "note",
    "status": "status",
    "状态": "status",
    "schedule": "schedule",
    "日程": "schedule",
}

SELF_SUBJECTS = {
    "用户",
    "user",
    "self",
    "我",
    "俺",
    "咱",
    "本人",
    "说话人",
    "当前用户",
}

BOT_SUBJECTS = {"bot", "机器人", "助手", "你"}

CANON_ATTRS = (
    "likes",
    "dislikes",
    "name",
    "identity",
    "habit",
    "promise",
    "note",
)


def canonical_attribute(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "note"
    key = normalize_slot(text)
    if key in ATTR_ALIASES:
        return ATTR_ALIASES[key]
    compact = normalize_slot(text.replace("_", "").replace("-", ""))
    if compact in ATTR_ALIASES:
        return ATTR_ALIASES[compact]
    for alias, canon in ATTR_ALIASES.items():
        if alias and (alias in text or text in alias):
            return canon
    return key or "note"


def canonical_subject(raw: str, speaker_id: str = "", speaker_name: str = "") -> str:
    text = (raw or "").strip()
    sid = (speaker_id or "").strip()
    name = (speaker_name or "").strip()
    key = normalize_slot(text)
    if key in BOT_SUBJECTS:
        return "bot"
    if not text or key in SELF_SUBJECTS:
        return "self"
    if sid and (text == sid or key == normalize_slot(sid)):
        return "self"
    if name and (text == name or key == normalize_slot(name)):
        return "self"
    return text[:40]


def apply_slot(payload: dict) -> dict:
    payload = dict(payload)
    speaker_id = str(payload.get("speaker_id") or "")
    speaker_name = str(payload.get("speaker_name") or "")
    attribute = str(payload.get("attribute") or "note")
    value = str(payload.get("value") or "")
    content = str(payload.get("content") or "")
    blob = f"{attribute} {value} {content}"
    if re.search(r"(不喜欢|没喜欢|不再喜欢)", blob) and canonical_attribute(attribute) in {"likes", "dislikes", "note"}:
        payload["attribute"] = "likes"
        if value and not value.startswith("不"):
            payload["value"] = "不" + value
    else:
        payload["attribute"] = canonical_attribute(attribute)
    payload["subject"] = canonical_subject(
        str(payload.get("subject") or ""),
        speaker_id=speaker_id,
        speaker_name=speaker_name,
    )
    return payload
