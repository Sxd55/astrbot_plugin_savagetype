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

import random
from typing import Any, Iterable

MODES = ("probability", "keyword", "memory")
MIN_TEXT_CHARS = 2
COMMAND_PREFIXES = ("/", "／", "!", "！")


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
