"""免@主动接话（reply gate）：判定群消息是否值得让 Bot 主动回复。

与 AstrBot 内置 `provider_ltm_settings.active_reply` 的关系：

- 内置只实现概率法，且 `need_active_reply()` 会跳过「已唤醒」的消息；
- 本模块判定命中后，由主插件把 `event.is_at_or_wake_command` 置真，让消息走
  AstrBot 默认 LLM 通路（人格 / 记忆注入 / 分段 / TTS 全部照旧），
  因此同一条消息不会再被内置概率法重复处理。

判定顺序（任一不通过即不接话，并记录原因）：

1. 开关、群聊、非空文本、非命令、非 bot 自己、目标群白名单；
2. 冷却（同群两次主动回复的最小间隔）与每日上限；
3. 模式判定：probability 概率 / keyword 关键词 / memory 记忆命中。
"""

from __future__ import annotations

import datetime
import json
import random
import re
from typing import Any, Iterable

from .addressee import decode as decode_addressee

MODES = ("probability", "keyword", "memory", "judge")
MIN_TEXT_CHARS = 2
COMMAND_PREFIXES = ("/", "／", "!", "！")
QUESTION_MARKERS = ("？", "?", "吗", "呢", "怎么", "为什么", "为啥", "能不能", "可不可以", "是否", "啥", "多少", "几点", "谁", "哪里", "哪儿", "如何")
BOT_NAME_KEYS = ("name", "alias", "aka", "nickname", "昵称", "称呼")
JUDGE_WEIGHTS = {"relevance": 0.3, "willingness": 0.25, "social": 0.25, "timing": 0.2}


def normalize_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in MODES else "probability"


def parse_targets(raw: Any) -> set[str]:
    """解析群白名单：支持 umo、群号，逗号 / 换行分隔。"""
    if isinstance(raw, (list, tuple, set)):
        items: Iterable[Any] = raw
    else:
        items = str(raw or "").replace("\n", ",").split(",")
    return {str(item).strip() for item in items if str(item).strip()}


def in_targets(window_tag: str, targets: set[str]) -> bool:
    """空名单 = 不限群；否则 umo 相等或群号出现在 umo 里即算命中。"""
    if not targets:
        return True
    tag = str(window_tag or "")
    if not tag:
        return False
    if tag in targets:
        return True
    return any(target and target in tag for target in targets)


def text_usable(text: str, *, min_chars: int = MIN_TEXT_CHARS, skip_commands: bool = True) -> tuple[bool, str]:
    value = (text or "").strip()
    if len(value) < max(1, int(min_chars)):
        return False, "too_short"
    if skip_commands and value.startswith(COMMAND_PREFIXES):
        return False, "command"
    if not any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in value):
        return False, "no_content"
    return True, ""


def keyword_hit(text: str, keywords: set[str]) -> tuple[bool, str]:
    if not keywords:
        return False, "no_keywords"
    value = text or ""
    for keyword in keywords:
        if keyword and keyword in value:
            return True, f"keyword:{keyword}"
    return False, "keyword_miss"


def memory_hit(result: Any) -> tuple[bool, str]:
    """记忆模式：这条消息命中了记忆（核心 / 相关事实）才接话。"""
    core = list(getattr(result, "core", None) or [])
    related = list(getattr(result, "related", None) or [])
    events = list(getattr(result, "events", None) or [])
    if core:
        return True, f"memory_core:{len(core)}"
    if related:
        return True, f"memory_related:{len(related)}"
    if events:
        return True, f"memory_event:{len(events)}"
    return False, "memory_miss"


def probability_hit(probability: float, rng: random.Random | None = None) -> bool:
    try:
        value = float(probability)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(1.0, value))
    roller = rng or random
    return roller.random() < value


def evaluate(
    *,
    enabled: bool,
    is_group: bool,
    already_handled: bool,
    is_self: bool,
    window_tag: str,
    targets: set[str],
    text: str,
    min_chars: int,
    skip_commands: bool,
    cooldown_ok: bool,
    daily_ok: bool,
    mode_hit: bool,
    mode_reason: str,
) -> tuple[bool, str]:
    """纯判定函数：返回 (是否接话, 原因)。"""
    if not enabled:
        return False, "disabled"
    if not is_group:
        return False, "not_group"
    if already_handled:
        return False, "already_handled"
    if is_self:
        return False, "bot_self"
    if not in_targets(window_tag, targets):
        return False, "group_not_allowed"
    usable, reason = text_usable(text, min_chars=min_chars, skip_commands=skip_commands)
    if not usable:
        return False, reason
    if not cooldown_ok:
        return False, "cooldown"
    if not daily_ok:
        return False, "daily_limit"
    if not mode_hit:
        return False, mode_reason or "mode_miss"
    return True, mode_reason or "hit"


# ---- reply_gate v2 新增判定层（纯函数） ------------------------------------


def turn_is_open(addressee_raw: str, bot_id: str) -> tuple[bool, str]:
    """话轮判断（b）：消息 @/引用了别人 → 这是别人之间的对话，不插嘴。

    返回 (是否开放话轮, 原因)。
    """
    items = decode_addressee(addressee_raw)
    if not items:
        return True, "open"
    own = str(bot_id or "")
    others = [
        item
        for item in items
        if str(item.get("id") or "") not in (own, "all", "")
    ]
    if others:
        return False, f"turn_taken:{others[0].get('id')}"
    return True, "addressed_to_bot"


def name_hit(text: str, names: Iterable[str]) -> tuple[bool, str]:
    """称呼白名单（j）：不@、但话里叫到了 Bot 的名字 → 必接。"""
    value = text or ""
    if not value:
        return False, "name_miss"
    for raw in names:
        name = str(raw or "").strip()
        if len(name) >= 2 and name in value:
            return True, f"name:{name}"
    return False, "name_miss"


def question_like(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if value.endswith(("？", "?")):
        return True
    return any(marker in value for marker in QUESTION_MARKERS)


def parse_quiet_ranges(raw: str) -> list[tuple[int, int]]:
    """解析免打扰时段："1:00-7:00;13:00-14:00" → [(60, 420), (780, 840)]（分钟）。"""
    ranges: list[tuple[int, int]] = []
    for chunk in str(raw or "").replace("\n", ";").replace(",", ";").split(";"):
        piece = chunk.strip()
        if not piece or "-" not in piece:
            continue
        start_raw, _, end_raw = piece.partition("-")
        start = _to_minutes(start_raw)
        end = _to_minutes(end_raw)
        if start is None or end is None or start == end:
            continue
        ranges.append((start, end))
    return ranges


def _to_minutes(value: str) -> int | None:
    text = str(value or "").strip().replace("：", ":")
    if not text:
        return None
    if ":" in text:
        hour_raw, _, minute_raw = text.partition(":")
        try:
            hour = int(hour_raw)
            minute = int(minute_raw or 0)
        except (TypeError, ValueError):
            return None
    else:
        try:
            hour = int(text)
            minute = 0
        except (TypeError, ValueError):
            return None
    if not (0 <= hour <= 24) or not (0 <= minute <= 59):
        return None
    return hour * 60 + minute


def quiet_now(raw: str, now=None) -> tuple[bool, str]:
    """当前是否处于免打扰时段（i）。跨零点区间（如 23:00-7:00）也支持。"""
    ranges = parse_quiet_ranges(raw)
    if not ranges:
        return False, ""
    moment = now
    if moment is None:
        moment = datetime.datetime.now()
    minutes = int(moment.hour) * 60 + int(moment.minute)
    for start, end in ranges:
        if start < end:
            if start <= minutes < end:
                return True, f"quiet:{start}-{end}"
        else:  # 跨零点
            if minutes >= start or minutes < end:
                return True, f"quiet:{start}-{end}"
    return False, ""


def judge_weights() -> dict[str, float]:
    return dict(JUDGE_WEIGHTS)


def judge_prompt(bot_name: str, recent_lines: list[str], message: str) -> str:
    """读空气判定提示词（④）：只输出一个 JSON 分数对象，尽量省 token。"""
    context = "\n".join(line for line in (recent_lines or []) if line)[-800:]
    return (
        f"你是群聊里的「{bot_name or '机器人'}」，正在判断要不要主动接一句话。\n"
        f"最近群聊（可能为空）：\n{context or '（无）'}\n"
        f"现在这条消息：{message}\n"
        "请按 0-10 打分：relevance=和你的相关度（提到你/你了解的话题=高）、"
        "willingness=你现在接话的意愿、social=接话是否合群聊氛围、timing=现在开口的时机是否合适。\n"
        '只输出 JSON，不要解释：{"relevance":0,"willingness":0,"social":0,"timing":0}'
    )


def judge_parse(raw: str) -> tuple[float, str]:
    """解析判定模型输出：各维 0-10 → 加权归一化到 0-1。"""
    text = str(raw or "")
    match = None
    for candidate in re.finditer(r"\{[^{}]*\}", text):
        try:
            payload = json.loads(candidate.group(0))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and any(key in payload for key in JUDGE_WEIGHTS):
            match = payload
            break
    if match is None:
        return -1.0, "judge_unparsed"
    score = 0.0
    weight_sum = 0.0
    for key, weight in JUDGE_WEIGHTS.items():
        try:
            value = float(match.get(key))
        except (TypeError, ValueError):
            continue
        value = max(0.0, min(10.0, value)) / 10.0
        score += value * weight
        weight_sum += weight
    if weight_sum <= 0:
        return -1.0, "judge_empty"
    return score / weight_sum, "judged"


def evaluate_v2(
    *,
    enabled: bool,
    is_group: bool,
    already_handled: bool,
    is_self: bool,
    window_tag: str,
    targets: set[str],
    text: str,
    min_chars: int,
    skip_commands: bool,
    quiet: bool,
    cooldown_ok: bool,
    min_interval_ok: bool,
    daily_ok: bool,
    turn_open: bool,
    name_fired: bool,
    mode_hit: bool,
    mode_reason: str,
) -> tuple[bool, str]:
    """v2 分层判定：任何一层否决都会带原因返回；点名命中直接放行（quiet 除外）。"""
    if not enabled:
        return False, "disabled"
    if not is_group:
        return False, "not_group"
    if already_handled:
        return False, "already_handled"
    if is_self:
        return False, "bot_self"
    if quiet:
        return False, "quiet_hours"
    if not in_targets(window_tag, targets):
        return False, "group_not_allowed"
    usable, reason = text_usable(text, min_chars=min_chars, skip_commands=skip_commands)
    if not usable:
        return False, reason
    if not cooldown_ok:
        return False, "cooldown"
    if not min_interval_ok:
        return False, "min_interval"
    if not daily_ok:
        return False, "daily_limit"
    if name_fired:
        return True, "name_hit"
    if not turn_open:
        return False, "turn_not_open"
    if not mode_hit:
        return False, mode_reason or "mode_miss"
    return True, mode_reason or "hit"
