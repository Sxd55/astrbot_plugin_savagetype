"""Shared constants and small helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
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

KIND_PREFERENCE = "preference"
KIND_IDENTITY = "identity"
KIND_HABIT = "habit"
KIND_PROMISE = "promise"
KIND_STATUS = "status"
KIND_NOTE = "note"

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
    r"(还记得|你记得|记不记得|你不是说|你说过|我跟你说过|改口)"
)
HISTORY_RE = re.compile(
    r"(以前|之前|原来|过去|当时|那时候|那时|从前|曾经|当年|那年|改口前|原来叫|以前叫)"
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
MASTER_RE = re.compile(r"主人")
REMEMBER_RE = re.compile(r"(记住|记一下|记下来|别忘了|帮我记)")
DIRECTIVE_RE = re.compile(
    r"(记住|记一下|记下来|别忘了|帮我记|我喜欢|我不喜欢|我讨厌|叫我|称呼我|我是|我住|改口|以后请|以后叫|"
    r"主人喜欢|主人不喜欢|主人讨厌)"
)

_PREF_SUBJ = r"(?:我|俺|咱|主人)?"
_PREF_ADV = r"(?:其实|平时|一般|通常|最近|现在|一直|以前|原来|真的|也|就|更|又|还)?"
_PREF_DEG = r"(?:很|最|超|挺|特别|尤其|真的)?"
PREF_PATTERNS = [
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}{_PREF_DEG}(?:不|没|不再)(?:太|怎么|是很)?喜欢(?:听|喝|吃|看|玩)?(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    # 主语可选：AI 整理出的 plain 常写「喜欢 X」不带主语；无主语时靠「句首/标点后」守卫防转述。
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}{_PREF_DEG}喜欢(?:听|喝|吃|看|玩)?(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}(?:迷上|入坑|上瘾)(?:了)?(?:听|喝|吃|玩|看)?(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}{_PREF_DEG}爱(?:听|喝|吃|看|玩)(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}{_PREF_DEG}(?:不(?:太|怎么)?|没)(?:吃|喝|看|玩|碰)(.+?)(?:[，。！!？?\s]|$)"), "likes"),
    (re.compile(rf"{_PREF_SUBJ}{_PREF_ADV}{_PREF_DEG}(?:讨厌|受不了)(.+?)(?:[，。！!？?\s]|$)"), "dislikes"),
    (re.compile(r"(?:我|俺|咱|主人)叫(.+?)(?:[，。！!？?\s]|$)"), "name"),
    (re.compile(r"(?:请)?(?:叫我|称呼我)(.+?)(?:[，。！!？?\s]|$)"), "name"),
    (re.compile(r"(?:我|俺|咱|主人)(?:是|住在|在)(.+?)(?:人|[，。！!？?\s]|$)"), "identity"),
    (re.compile(r"(?:我|俺|咱|主人)(?:以后|从今以后)(?:不|不再)(.+?)(?:了)?(?:[，。！!？?\s]|$)"), "habit"),
    (re.compile(r"(?:我|俺|咱|主人)(?:这周|最近|今晚|今天|这几天)(.{2,24}?)(?:[，。！!？?\s]|$)"), "status"),
]
CLOSE_RE = re.compile(r"(做完了|完成了|已经寄了|已经办了|不用记了|算了当我没说|取消约定)")
STATUS_NOW_RE = re.compile(r"(加班|熬夜|感冒|发烧|失眠|出差|请假)")

# 第一人称「自述」候选门。旧写法要求「我」和谓语字面相连（我喜欢 / 我不喜欢），
# 「我平时喜欢喝…」「我其实不喜欢…」「我最近迷上了…」这类最自然的说法全部进不了候选，
# LLM 根本看不到。这里允许主语和谓语之间夹副词，并补齐 爱/迷上/入坑/上瘾 等谓词。
SELF_SUBJECT_PAT = r"(?:我|俺|咱|本人|主人)"
SELF_ADVERB_PAT = (
    r"(?:其实|平时|一般|通常|最近|现在|一直|以前|原来|真的|特别|尤其|最|很|超|挺|也|就|更|又|还)*"
)
PREF_VERB_PAT = (
    r"(?:不(?:太|怎么|是很)?喜欢|不爱|受不了|喜欢|爱喝|爱吃|爱看|爱玩|爱听|爱|讨厌|迷上|入坑|上瘾|习惯|偏好|常吃|常喝|常去|"
    r"不(?:太|怎么)?(?:吃|喝|看|玩|碰))"
)
SELF_PREF_RE = re.compile(rf"{SELF_SUBJECT_PAT}{SELF_ADVERB_PAT}{PREF_VERB_PAT}")
# 「我(家)的 X 叫 Y」这类自家称呼（宠物名、物件名）；排除「我叫什么」这类疑问。
SELF_POSSESSIVE_NAME_RE = re.compile(
    rf"{SELF_SUBJECT_PAT}(?:家|的)[^，。！!？?\s]{{0,6}}叫(?!什么|啥|名字)"
)
# 身份/居住/养宠：我是…、我叫…、我住在…、我养了…（同样允许夹副词）。
SELF_IDENTITY_RE = re.compile(
    rf"{SELF_SUBJECT_PAT}{SELF_ADVERB_PAT}(?:是|住(?:在)?|养了|养着|吃素|叫(?!什么|啥|名字))"
)
SELF_STATEMENT_RE = re.compile(
    f"(?:{SELF_PREF_RE.pattern}|{SELF_POSSESSIVE_NAME_RE.pattern}|{SELF_IDENTITY_RE.pattern})"
)

OWNER_DIRECTIVE_RE = re.compile(
    r"(记住|记一下|记下来|别忘了|帮我记|以后|从现在起|从今以后|不要|别再|别忘|必须|禁止|叫你|称呼我|改口|"
    r"主人喜欢|主人不喜欢|主人讨厌)"
)
RELATION_GUARD_RE = re.compile(
    r"(主人|owner|老公|老婆|男朋友|女朋友|男友|女友|未婚夫|未婚妻|"
    r"爸爸|妈妈|父亲|母亲|儿子|女儿|哥哥|弟弟|姐姐|妹妹|"
    r"老板|上司|领导|管理员|群主|admin)"
)
COMMAND_SPLIT_RE = re.compile(r"^[/／!！]")
BOT_DEFINE_RE = re.compile(
    r"(你叫|你名叫|你是|你的名字|你的身份|你以后|从现在起你|记住你是|记住你叫|"
    r"你要|你不要|你不许|称呼你|给你起名)"
)


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts or 0)))
    except (OverflowError, OSError, ValueError):
        return ""


def today_str(now: int | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(int(now or time.time())))


def today_start_ts(now: int | None = None) -> int:
    current = datetime.fromtimestamp(int(now or time.time()))
    return int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fmt_date(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts or 0)))
    except (OverflowError, OSError, ValueError):
        return ""


TIME_WINDOW_CUTOFFS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(刚才|刚刚|今天早|今晚|今天)"), 1),
    (re.compile(r"昨天"), 2),
    (re.compile(r"前天"), 3),
    (re.compile(r"(这周|本周|最近)"), 7),
    (re.compile(r"(上周|上星期)"), 14),
    (re.compile(r"上个月"), 40),
)


def time_window_days(query: str) -> int:
    """Days back implied by a time phrase; 0 = no restriction."""
    text = query or ""
    for pattern, days in TIME_WINDOW_CUTOFFS:
        if pattern.search(text):
            return days
    return 0


def session_isolation(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"off", "owner", "strict"} else "strict"


_RE_YM = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_RE_YEAR = re.compile(r"(?<![\d\-])(\d{4})\s*年(?!\s*\d)")
_RE_LAST_YM = re.compile(r"去年\s*(\d{1,2})\s*月")
_RE_THIS_YM = re.compile(r"(?:今年|本年)\s*(\d{1,2})\s*月")
_RE_MONTH = re.compile(r"(?<![\d年\-])(\d{1,2})\s*月")
_RE_RECENT = re.compile(r"最近\s*(\d+)\s*(天|周|个?月|年)")
_RECENT_UNIT_DAYS = {"天": 1, "周": 7, "月": 30, "个月": 30, "年": 365}


def _month_range(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return int(start.timestamp()), int(end.timestamp())


def _year_range(year: int) -> tuple[int, int]:
    return _month_range(year, 1)[0], _month_range(year, 12)[1]


def parse_time_range(query: str, now: int | None = None) -> tuple[int, int, str]:
    """Resolve a time phrase to (start_ts, end_ts, label); (0, 0, "") when none.

    Deterministic on purpose: the router and history retrieval share it.
    """
    text = query or ""
    current = int(now or time.time())
    dt = datetime.fromtimestamp(current)

    match = _RE_YM.search(text)
    if match and 1 <= int(match.group(2)) <= 12:
        year, month = int(match.group(1)), int(match.group(2))
        return (*_month_range(year, month), f"{year}-{month:02d}")
    match = _RE_YEAR.search(text)
    if match:
        return (*_year_range(int(match.group(1))), match.group(1))
    match = _RE_LAST_YM.search(text)
    if match and 1 <= int(match.group(1)) <= 12:
        year, month = dt.year - 1, int(match.group(1))
        return (*_month_range(year, month), f"{year}-{month:02d}")
    match = _RE_THIS_YM.search(text)
    if match and 1 <= int(match.group(1)) <= 12:
        year, month = dt.year, int(match.group(1))
        return (*_month_range(year, month), f"{year}-{month:02d}")
    if "前年" in text:
        return (*_year_range(dt.year - 2), str(dt.year - 2))
    if "去年" in text:
        return (*_year_range(dt.year - 1), str(dt.year - 1))
    if "今年" in text or "本年" in text:
        return (*_year_range(dt.year), str(dt.year))
    if "上个月" in text or "上月" in text:
        year, month = (dt.year - 1, 12) if dt.month == 1 else (dt.year, dt.month - 1)
        return (*_month_range(year, month), f"{year}-{month:02d}")
    if "这个月" in text or "本月" in text:
        return (*_month_range(dt.year, dt.month), f"{dt.year}-{dt.month:02d}")
    match = _RE_RECENT.search(text)
    if match:
        count = max(1, int(match.group(1)))
        unit = match.group(2)
        days = _RECENT_UNIT_DAYS.get(unit, 1)
        return current - count * days * 86400, current, f"最近{count}{unit}"
    match = _RE_MONTH.search(text)
    if match and 1 <= int(match.group(1)) <= 12:
        month = int(match.group(1))
        year = dt.year if month <= dt.month else dt.year - 1
        return (*_month_range(year, month), f"{year}-{month:02d}")
    if HISTORY_RE.search(text):
        return 0, current, "当时"
    return 0, 0, ""


def default_importance(payload: dict[str, Any]) -> float:
    origin = str(payload.get("origin") or "")
    review = str(payload.get("review_status") or "")
    if origin == ORIGIN_MANUAL or review == REVIEW_MANUAL:
        base = 1.0
    elif review == REVIEW_AI_PASSED:
        base = 0.8
    else:
        base = 0.5
    if int(payload.get("explicit_correction") or 0):
        base += 0.1
    if int(payload.get("first_person") or 0):
        base += 0.05
    return max(0.0, min(1.0, base))


def fact_weight(
    fact: Any,
    now: int | None = None,
    half_life_days: float = 30.0,
    reinforce_factor: float = 0.5,
    max_multiplier: float = 3.0,
) -> float:
    """Base importance decayed by age, with access-stretched half-life."""
    base = float(getattr(fact, "importance", 0) or 0)
    if base <= 0:
        base = float(getattr(fact, "confidence", 0.5) or 0.5)
    if int(getattr(fact, "pinned", 0) or 0):
        return max(base, 1.0)
    now = now or now_ts()
    accesses = max(0, int(getattr(fact, "access_count", 0) or 0))
    last = max(
        int(getattr(fact, "last_accessed", 0) or 0),
        int(getattr(fact, "updated_at", 0) or 0),
        int(getattr(fact, "created_at", 0) or 0),
    )
    age_days = max(0.0, (now - last) / 86400.0) if last else 0.0
    half_life = max(0.1, float(half_life_days)) * min(
        1.0 + max(0.0, float(reinforce_factor)) * accesses,
        max(1.0, float(max_multiplier)),
    )
    return base * (0.5 ** (age_days / half_life))


PLATFORM_ALIASES = {
    "qq_official_webhook": "qq_official",
    "qq_official_websocket": "qq_official",
}


def norm_platform(value: str) -> str:
    key = (value or "").strip().lower()
    return PLATFORM_ALIASES.get(key, key)


def platform_of(window_tag: str) -> str:
    return norm_platform((window_tag or "").split(":", 1)[0])


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


TOPIC_PARTICLES = ("了", "啦", "呢", "吧", "啊", "呀", "嘛", "哦", "喔", "噢", "咯", "诶", "欸")
TOPIC_LEADING = (
    "从今以后",
    "不再",
    "其实",
    "现在",
    "以后",
    "本人",
    "喜欢",
    "讨厌",
    "受不了",
    "我",
    "俺",
    "咱",
    "不",
    "没",
    "别",
    "非",
)


def topic_key(value: str) -> str:
    """Topic part of a preference value: strip leading subject/negation/verbs, trailing particles."""
    text = normalize_slot(value)
    changed = True
    while changed and text:
        changed = False
        for token in TOPIC_LEADING:
            if text.startswith(token):
                text = text[len(token) :]
                changed = True
    for verb in ("听", "喝", "吃", "看", "玩"):
        text = text.replace(verb, "")
    while text and text[-1] in TOPIC_PARTICLES:
        text = text[:-1]
    return text[:24]


DOMAIN_CUES: dict[str, tuple[str, ...]] = {
    "饮品": ("喝", "咖啡", "奶茶", "拿铁", "饮料", "可乐", "果汁", "酒", "茶"),
    "食物": ("吃", "火锅", "披萨", "零食", "外卖", "甜食", "辣", "菜"),
    "穿搭": ("穿", "穿搭", "衣服", "外套", "裤", "裙", "鞋", "帽", "风格", "版型", "搭配", "配色", "日系", "复古"),
    "娱乐": ("玩", "游戏", "电影", "剧", "歌", "音乐", "小说", "动漫", "综艺"),
    "运动": ("运动", "跑步", "健身", "游泳", "骑行", "篮球", "足球", "网球", "羽毛球", "乒乓球", "排球", "滑板", "滑雪"),
}

DOMAIN_ALIASES = {
    "饮料": "饮品",
    "喝的": "饮品",
    "饮品": "饮品",
    "咖啡": "饮品",
    "茶": "饮品",
    "吃的": "食物",
    "食物": "食物",
    "美食": "食物",
    "服饰": "穿搭",
    "衣着": "穿搭",
    "服装": "穿搭",
    "穿衣": "穿搭",
    "穿搭": "穿搭",
    "风格": "穿搭",
    "玩的": "娱乐",
    "游戏": "娱乐",
    "娱乐": "娱乐",
    "体育": "运动",
    "运动": "运动",
    "健身": "运动",
}


def detect_domain(content: str, value: str = "") -> str:
    """从句子+值里猜偏好的领域（饮品/食物/穿搭/…）；判不出返回空串。

    线索词只收「喝/咖啡/穿/风格」这类领域特征词；「美式」这种两个领域都用的
    双关词**不**作为线索，避免自己证明自己。
    """
    text = normalize_slot(content or "")
    val = normalize_slot(value or "")
    blob = f"{text} {val}".strip()
    if not blob:
        return ""
    scores: dict[str, int] = {}
    for domain, cues in DOMAIN_CUES.items():
        hits = sum(1 for cue in cues if cue in blob)
        if hits:
            scores[domain] = hits
    if not scores:
        return ""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return ""
    return ranked[0][0]


def normalize_domain(raw: str) -> str:
    """把模型给的领域词映射到内置规范名；对不上或含糊返回空串。"""
    text = normalize_slot(raw or "")
    if not text:
        return ""
    hits = {canon for alias, canon in DOMAIN_ALIASES.items() if alias in text}
    return hits.pop() if len(hits) == 1 else ""


def make_slot_key(
    persona_id: str,
    speaker_id: str,
    subject: str,
    attribute: str,
    value: str = "",
) -> str:
    from .slots import canonical_attribute, canonical_subject

    attr = canonical_attribute(attribute)
    subj = canonical_subject(subject, speaker_id=speaker_id)
    key = f"{persona_id or ''}|{speaker_id or ''}|{normalize_slot(subj)}|{attr}"
    if attr in {"likes", "dislikes", "promise", "habit"}:
        # 约定和习惯也按主题分槽：答应带饭 / 答应交作业、早起 / 戒烟 都要能并存。
        topic = topic_key(value)
        if topic:
            key = f"{key}|{topic}"
    return key


def fingerprint(*parts: Any) -> str:
    raw = "||".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_slot(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[\s　,，.。!！?？、~～'\"“”‘’]+", "", text)
    return text


def estimate_tokens(text: str) -> int:
    """Rough token estimate for budgeting UI: CJK ≈ 1/char, others ≈ 4 chars/token."""
    cjk = 0
    other = 0
    for ch in text or "":
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF or 0x4E00 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF:
            cjk += 1
        else:
            other += 1
    return int(cjk + (other + 3) // 4)


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
