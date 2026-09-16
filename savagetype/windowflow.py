"""窗口全流上下文（C 层）：把其他窗口最近的**完整消息流**注入当前会话。

与 B 层「跨窗口衔接」（crosswin）的区别：

- B 层：只带当前说话人自己在别处说过的话，最多 6 条 / 320 字 / 每条截断 60 字，
  用来看作「碎片提醒」。
- C 层（本模块）：带目标窗口的完整对话流（其他成员 + Bot 本人 + 本人），
  按窗口分组、带说话人与时间，用来看作「整段上下文」。

方向默认双向全通（私聊 <-> 群聊）；可用 exclude_private_users 屏蔽指定用户的私聊窗口。
"""

from __future__ import annotations

from typing import Any, Iterable

from .crosswin import window_kind
from .addressee import render_addressee
from .util import ROLE_ASSISTANT, ROLE_USER, clip, fmt_ts, now_ts

HEADER = (
    "【窗口上下文】以下是其他会话（群聊/私聊）最近的完整记录，"
    "可直接据此回答；不要说「我看到了记录」，也不要在群里透露私聊内容的来源。"
)

MIN_TEXT_CHARS = 2
PLACEHOLDER_PREFIXES = ("[图片]", "[image]", "[表情]", "[语音]", "[视频]", "[文件]", "<attachment>")


def window_label(window_tag: str) -> str:
    """把 umo / window_tag 变成可读标签，如「群 123456789」「私聊 10086」。"""
    tag = (window_tag or "").strip()
    if not tag:
        return "未知会话"
    kind = window_kind(tag)
    parts = [part for part in tag.split(":") if part]
    number = next((part for part in reversed(parts) if part.isdigit()), "")
    if not number:
        number = parts[-1] if parts else ""
    if kind == "group":
        prefix = "群"
    elif kind == "private":
        prefix = "私聊"
    else:
        prefix = "会话"
    return f"{prefix} {number}".strip()


def _usable_text(text: str) -> bool:
    value = (text or "").strip()
    if len(value) < MIN_TEXT_CHARS:
        return False
    if any(value.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES):
        return False
    return True


def _who(event: Any) -> str:
    role = str(getattr(event, "role", "") or "")
    if role == ROLE_ASSISTANT:
        return "我(Bot)"
    name = str(getattr(event, "speaker_name", "") or "").strip()
    if not name:
        name = str(getattr(event, "speaker_id", "") or "").strip() or "某人"
    target = render_addressee(
        str(getattr(event, "addressee", "") or ""),
        self_id=str(getattr(event, "bot_id", "") or ""),
        bot_label="你",
    )
    return f"{name} → {target}" if target else name


def build_window_flow(
    store: Any,
    current_window: str,
    *,
    hours: int = 24,
    max_items: int = 150,
    max_chars: int = 6000,
    max_windows: int = 3,
    msg_chars: int = 200,
    include_bot: bool = True,
    group_to_private: bool = True,
    private_to_group: bool = True,
    exclude_private_users: Iterable[str] = (),
    persona_id: str = "",
) -> tuple[str, dict[str, Any]]:
    """组装窗口全流块。

    Returns:
        (block_text, meta)；没有可用内容时 block_text 为空字符串。
    """
    target_kind = window_kind(current_window)
    if target_kind == "unknown":
        return "", {"enabled": True, "items": 0, "chars": 0, "reason": "target_unknown"}
    since = now_ts() - max(1, int(hours)) * 3600
    rows = store.window_flow_events(
        exclude_window=current_window,
        since_ts=since,
        limit=max(1, int(max_items)) * 4,
    )
    exclude = {str(item).strip() for item in (exclude_private_users or []) if str(item).strip()}
    by_window: dict[str, list[Any]] = {}
    for event in rows:
        window = str(getattr(event, "window_tag", "") or "")
        if not window or window == current_window:
            continue
        source_kind = window_kind(window)
        if source_kind == "unknown":
            continue
        if source_kind == "private" and target_kind == "group" and not private_to_group:
            continue
        if source_kind == "group" and target_kind == "private" and not group_to_private:
            continue
        if persona_id and str(getattr(event, "persona_id", "") or "") not in ("", persona_id):
            continue
        role = str(getattr(event, "role", "") or "")
        if role == ROLE_ASSISTANT and not include_bot:
            continue
        if role not in (ROLE_USER, ROLE_ASSISTANT):
            continue
        if source_kind == "private" and exclude and str(getattr(event, "speaker_id", "") or "") in exclude:
            continue
        if not _usable_text(str(getattr(event, "content", "") or "")):
            continue
        by_window.setdefault(window, []).append(event)

    if not by_window:
        return "", {"enabled": True, "items": 0, "chars": 0, "windows": 0}

    ordered_windows = sorted(
        by_window,
        key=lambda window: max(int(getattr(event, "ts", 0) or 0) for event in by_window[window]),
        reverse=True,
    )[: max(1, int(max_windows))]

    per_window = max(4, int(max_items) // max(1, len(ordered_windows)))
    picked: list[Any] = []
    for window in ordered_windows:
        events = by_window[window][:per_window]
        picked.extend(events)
    picked.sort(key=lambda event: int(getattr(event, "ts", 0) or 0))
    if len(picked) > max(1, int(max_items)):
        picked = picked[-max(1, int(max_items)) :]

    lines: list[str] = [HEADER]
    used = len(HEADER)
    keep: list[tuple[str, str]] = []
    for event in picked:
        window = str(getattr(event, "window_tag", "") or "")
        text = clip(str(getattr(event, "content", "") or "").strip().replace("\n", " "), max(20, int(msg_chars)))
        line = f"[{fmt_ts(int(getattr(event, 'ts', 0) or 0))}] {_who(event)}: {text}"
        keep.append((window, line))
    # 超预算时优先保留最近的消息：从最旧的开始丢。
    total = used + sum(len(line) + 1 for _window, line in keep)
    while keep and max_chars > 0 and total > max_chars:
        _window, dropped = keep.pop(0)
        total -= len(dropped) + 1
    if not keep:
        return "", {"enabled": True, "items": 0, "chars": 0, "windows": 0}

    grouped: list[str] = []
    current = ""
    windows_used = 0
    items = 0
    for window, line in keep:
        if window != current:
            current = window
            windows_used += 1
            grouped.append(f"▸ {window_label(window)}")
        grouped.append(line)
        items += 1
    block = "\n".join([*lines, *grouped])
    return block, {
        "enabled": True,
        "items": items,
        "chars": len(block),
        "windows": windows_used,
        "window_labels": [window_label(window) for window in ordered_windows[:windows_used]],
    }
