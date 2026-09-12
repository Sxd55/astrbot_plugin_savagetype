"""Shared constants and small helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

PLUGIN_NAME = "astrbot_plugin_savagetype"

STATUS_LIVE = "live"
STATUS_SUPERSEDED = "superseded"
STATUS_PENDING = "pending_confirm"
STATUS_ARCHIVED = "archived"

MENTION = "mention"
TONE = "tone"
UNCERTAIN = "uncertain"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_BOT_ID = "bot_self"

SCOPE_OWNER = "owner"
SCOPE_PERSON = "person"

ORIGIN_QQ = "qq"
ORIGIN_MANUAL = "manual"
ORIGIN_IMPORT = "import"

REVIEW_AI_PASSED = "ai_passed"
REVIEW_UNVERIFIED = "unverified"
REVIEW_MANUAL = "manual"
REVIEW_NEEDS = "needs_review"

MEMORY_STATUS_PENDING = "pending"
MEMORY_STATUS_APPROVED = "approved"
MEMORY_STATUS_REJECTED = "rejected"

LOW_INFO_RE = re.compile(
    r"^(哈+|啊+|嗯+|哦+|额+|好+|ok+|okay+|你好|在吗|早|晚安|谢谢|谢谢你)[\s!！。.~～]*$",
    re.IGNORECASE,
)
STATUS_RE = re.compile(
    r"(在干嘛|在做什么|吃了没|吃晚饭|吃午饭|累不累|睡了吗|起床了|今天穿)",
)
TIME_WINDOW_RE = re.compile(
    r"(昨天|前天|上周|上个月|最近一周|这周|那天|上次|刚才|刚刚|今天早|今晚)",
)
RECALL_RE = re.compile(
    r"(还记得|你记得|记不记得|你不是说|你说过|我跟你说过|改口)",
)
CORRECTION_RE = re.compile(
    r"(不是|改口|纠正|以后叫|以后请|其实是|记错|说错|不要再说|别再记)",
)
JOKE_RE = re.compile(
    r"(开玩笑|逗你|反话|随口|假装|骗你的|笑死|哈哈哈)",
)
HEARSAY_RE = re.compile(
    r"(听说|别人说|他好像|她好像|可能是|大概是|不确定)",
)
FIRST_PERSON_RE = re.compile(r"(我|俺|咱|本人)")
REMEMBER_RE = re.compile(r"(记住|记一下|记下来|别忘了|帮我记)")
DIRECTIVE_RE = re.compile(
    r"(记住|记一下|记下来|别忘了|帮我记|我喜欢|我不喜欢|我讨厌|叫我|称呼我|我是|我住|改口|以后请|以后叫)"
)

PREF_PATTERNS = [
    (re.compile(r"(?:我|俺|咱)(?:其实)?(?:现在)?(?:不|没|不再)喜欢(?:听|喝|吃)?(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(r"(?:我|俺|咱)(?:其实)?(?:很|最|超)?喜欢(?:听|喝|吃)?(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(r"(?:我|俺|咱)(?:讨厌|受不了)(.+?)(?:[，。！!？?\s]|$)"), "dislikes"),
    (re.compile(r"(?:我|俺|咱)叫(.+?)(?:[，。！!？?\s]|$)"), "name"),
    (re.compile(r"(?:请)?(?:叫我|称呼我)(.+?)(?:[，。！!？?\s]|$)"), "name"),
    (re.compile(r"(?:我|俺|咱)(?:是|住在|在)(.+?)(?:人|[，。！!？?\s]|$)"), "identity"),
    (re.compile(r"(?:我|俺|咱)(?:以后|从今以后)(?:不|不再)(.+?)(?:了)?(?:[，。！!？?\s]|$)"), "habit"),
    (re.compile(r"(?:我|俺|咱)(?:这周|最近|今晚|今天|这几天)(.{2,24}?)(?:[，。！!？?\s]|$)"), "status"),
]
CLOSE_RE = re.compile(r"(做完了|完成了|已经寄了|已经办了|不用记了|算了当我没说|取消约定)")
STATUS_NOW_RE = re.compile(r"(加班|熬夜|感冒|发烧|失眠|出差|请假)")

OWNER_DIRECTIVE_RE = re.compile(
    r"(记住|记一下|记下来|别忘了|帮我记|以后|从现在起|从今以后|不要|别再|别忘|必须|禁止|叫你|称呼我|改口)"
)
RELATION_GUARD_RE = re.compile(
    r"(主人|owner|老公|老婆|男朋友|女朋友|男友|女友|未婚夫|未婚妻|"
    r"爸爸|妈妈|父亲|母亲|儿子|女儿|哥哥|弟弟|姐姐|妹妹|"
    r"老板|上司|领导|管理员|群主|admin)"
)
COMMAND_SPLIT_RE = re.compile(r"^[/／]")


def now_ts() -> int:
    return int(time.time())


def platform_of(window_tag: str) -> str:
    return (window_tag or "").split(":", 1)[0].strip().lower()


def is_private_window(window_tag: str) -> bool:
    tag = (window_tag or "").lower()
    if not tag:
        return False
    if "friend" in tag or "private" in tag:
        return True
    return False


def parse_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").replace("\n", ",").split(",") if p.strip()]


def has_relation_claim(*texts: str) -> bool:
    return any(RELATION_GUARD_RE.search(t or "") for t in texts)


def make_slot_key(persona_id: str, speaker_id: str, subject: str, attribute: str) -> str:
    from .slots import canonical_attribute, canonical_subject

    attr = canonical_attribute(attribute)
    subj = canonical_subject(subject, speaker_id=speaker_id)
    return f"{persona_id or ''}|{speaker_id or ''}|{normalize_slot(subj)}|{attr}"


def fingerprint(*parts: Any) -> str:
    raw = "||".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_slot(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[\s　,，.。!！?？、~～'\"“”‘’]+", "", text)
    return text


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def safe_json_extract(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
