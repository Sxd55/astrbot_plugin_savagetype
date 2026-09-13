"""Canonical fact slots so heuristic and LLM writes collide on the same key."""

from __future__ import annotations

import re

from .util import (
    KIND_HABIT,
    KIND_IDENTITY,
    KIND_NOTE,
    KIND_PREFERENCE,
    KIND_PROMISE,
    KIND_STATUS,
    TOPIC_PARTICLES,
    detect_domain,
    normalize_domain,
    normalize_slot,
)

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
    "主人",
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


KIND_BY_ATTRIBUTE = {
    "likes": KIND_PREFERENCE,
    "dislikes": KIND_PREFERENCE,
    "name": KIND_IDENTITY,
    "identity": KIND_IDENTITY,
    "habit": KIND_HABIT,
    "promise": KIND_PROMISE,
    "status": KIND_STATUS,
    "note": KIND_NOTE,
}


def fact_kind(attribute: str) -> str:
    return KIND_BY_ATTRIBUTE.get(canonical_attribute(attribute), KIND_NOTE)


def apply_slot(payload: dict) -> dict:
    payload = dict(payload)
    speaker_id = str(payload.get("speaker_id") or "")
    speaker_name = str(payload.get("speaker_name") or "")
    attribute = str(payload.get("attribute") or "note")
    value = str(payload.get("value") or "")
    content = str(payload.get("content") or "")
    attr_canon = canonical_attribute(attribute)
    neg = bool(re.search(r"(不喜欢|没喜欢|不再喜欢)", value))
    if not neg and attr_canon in {"dislikes", "note"}:
        # LLM 可能给出 dislikes/note，但内容其实是「不喜欢 X」，只在非偏好属性时看正文，
        # 避免同一条消息里另一句「不喜欢」把「喜欢 X」也否定掉。
        neg = bool(re.search(r"(不喜欢|没喜欢|不再喜欢)", content))
    if (
        not neg
        and attr_canon == "likes"
        and value
        and not value.startswith("不")
        and re.search(rf"不喜欢[^，。！!？?]{{0,6}}{re.escape(value)}", content)
    ):
        # 兜底：模型漏写否定，但正文里确实是「不喜欢 <这个值>」。
        neg = True
    if neg and attr_canon in {"likes", "dislikes", "note"}:
        payload["attribute"] = "likes"
        base = value or content
        match = re.search(
            r"(?:不再喜欢|不喜欢|没喜欢|喜欢|讨厌|受不了)(?:听|喝|吃|看|玩)?(.+?)(?:[，。！!？?\s]|$)",
            base,
        )
        if match:
            base = match.group(1).strip()
        while base and base[-1] in TOPIC_PARTICLES:
            base = base[:-1]
        if base and not base.startswith("不"):
            base = "不" + base
        payload["value"] = base or value
    else:
        payload["attribute"] = attr_canon
    payload["subject"] = canonical_subject(
        str(payload.get("subject") or ""),
        speaker_id=speaker_id,
        speaker_name=speaker_name,
    )
    payload["kind"] = str(payload.get("kind") or fact_kind(payload["attribute"]))
    if payload["attribute"] in {"likes", "dislikes"}:
        # 领域用于区分同一个词的不同含义（「美式」咖啡 / 「美式」穿搭）。
        domain = normalize_domain(str(payload.get("topic") or ""))
        if not domain:
            # 优先用这条事实自己的短句判领域：整句里可能混着多个领域
            # （「喜欢喝咖啡，打篮球」不能把打篮球也判成饮品）。
            clue = str(payload.get("plain") or "").strip() or content
            domain = detect_domain(clue, value)
        payload["topic"] = domain
    else:
        payload["topic"] = ""
    return payload
