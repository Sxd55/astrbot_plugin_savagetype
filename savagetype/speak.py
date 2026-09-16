"""指派发言：在私聊里让 Bot 去指定群聊说一句话。

自然语言触发（仅主人、仅私聊）：

- 「去群里说：晚上八点开黑」        → 发到默认群
- 「跟群友说 明天休息」             → 发到默认群
- 「去 2 群说：我下课了」           → 按已知群列表序号发
- 「去 987654321 群说：到家了」     → 按群号发

解析与目标解析都在本模块（纯函数，便于离线测试）；真正的发送与记录在 service 层。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MAX_CONTENT_CHARS = 300

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^(?:帮我|麻烦)?\s*去\s*第?\s*(?P<value>\d{1,2})\s*个?群(?:里)?说\s*[:：,，]?\s*(?P<content>.+)$"),
        "index",
    ),
    (
        re.compile(r"^(?:帮我|麻烦)?\s*去\s*(?P<value>\d{5,})\s*群(?:里)?说\s*[:：,，]?\s*(?P<content>.+)$"),
        "number",
    ),
    (
        re.compile(
            r"^(?:帮我|麻烦)?\s*(?:去群里说|去群说|在群里说|群里说|跟群友说|和群友说|给大家说)\s*[:：,，]?\s*(?P<content>.+)$"
        ),
        "default",
    ),
]


def parse_intent(text: str) -> dict[str, str] | None:
    """把一句话解析成指派发言意图：{"target": default|index|number, "value": str, "content": str}。"""
    value = (text or "").strip()
    if not value:
        return None
    for pattern, kind in _PATTERNS:
        match = pattern.match(value)
        if not match:
            continue
        content = (match.group("content") or "").strip()
        if not content:
            continue
        return {
            "target": kind,
            "value": (match.groupdict().get("value") or "").strip(),
            "content": content,
        }
    return None


def clip_content(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    value = (text or "").strip()
    if limit > 0 and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def resolve_number(value: str, groups: Iterable[str]) -> str:
    """group 编号（群号）匹配：umo 里包含该群号即可。"""
    needle = str(value or "").strip()
    if not needle:
        return ""
    for window in groups:
        if needle in str(window):
            return str(window)
    return ""


def resolve_index(value: str, groups: Iterable[str]) -> str:
    """按序号（1 起）取已知群。"""
    try:
        index = int(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    items = [str(item) for item in groups]
    if 1 <= index <= len(items):
        return items[index - 1]
    return ""


def group_label(window_tag: str) -> str:
    """群标签：群号优先，取不到就退化为原 window。"""
    parts = [part for part in str(window_tag or "").split(":") if part]
    number = next((part for part in reversed(parts) if part.isdigit()), "")
    return number or (parts[-1] if parts else str(window_tag or ""))


def resolve_target(intent: dict[str, str], *, default_umo: str, groups: list[str]) -> tuple[str, str]:
    """返回 (目标 umo, 失败原因)。目标为空时第二个值为原因。"""
    kind = str(intent.get("target") or "")
    value = str(intent.get("value") or "")
    if kind == "default":
        if default_umo:
            return default_umo, ""
        if len(groups) == 1:
            return groups[0], ""
        return "", "no_default_group"
    if kind == "index":
        found = resolve_index(value, groups)
        return (found, "") if found else ("", "index_out_of_range")
    if kind == "number":
        found = resolve_number(value, groups)
        return (found, "") if found else ("", "group_not_found")
    return "", "unknown_target"


def allowed_target(window_tag: str, *, default_umo: str, allow: set[str]) -> bool:
    """白名单：名单为空=不额外限制（目标必须来自已知群，解析阶段已保证）；
    填写后只能发到名单内的群或默认群。"""
    tag = str(window_tag or "")
    if not tag:
        return False
    if not allow:
        return True
    if default_umo and tag == default_umo:
        return True
    return tag in allow
