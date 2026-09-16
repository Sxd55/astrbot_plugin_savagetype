"""跨窗口衔接（B 层）：同一个人在别的会话里刚说的话，作为「话题连续性」提示注入。

方向规则默认保守（对齐 MemoryCompanion / A_Memorix 的默认值）：
- 私聊 → 私聊：允许（同一个人自己的会话之间）
- 群聊 → 私聊：允许（同一个人对自己说的话，不涉及第三人）
- 私聊 → 群聊：默认禁止（需显式打开 cross_window_private_to_group）
- 群聊 → 群聊：默认禁止（需显式打开 cross_window_group_to_group）

只带当前说话人自己发的消息；跳过命令、图片占位、低信息内容与 Bot 回复。
"""

from __future__ import annotations

from typing import Any

from .models import TimelineEvent
from .util import COMMAND_SPLIT_RE, ROLE_USER, clip, fmt_ts, now_ts

PLACEHOLDER_PREFIXES = ("[图片]", "[image]", "<attachment>", "[语音]", "[视频]", "[表情]")
MIN_TEXT_CHARS = 2


def window_kind(window_tag: str) -> str:
    """把 window_tag 归类成 group / private / unknown。"""
    tag = (window_tag or "").lower()
    if not tag:
        return "unknown"
    if "groupmessage" in tag or "group_message" in tag or ":group" in tag:
        return "group"
    if "friendmessage" in tag or "friend_message" in tag or "private" in tag:
        return "private"
    return "unknown"


def direction_allowed(
    source_kind: str,
    target_kind: str,
    *,
    private_to_group: bool = False,
    group_to_group: bool = False,
) -> bool:
    if "unknown" in {source_kind, target_kind}:
        return False
    if source_kind == "private" and target_kind == "private":
        return True
    if source_kind == "group" and target_kind == "private":
        return True
    if source_kind == "private" and target_kind == "group":
        return bool(private_to_group)
    if source_kind == "group" and target_kind == "group":
        return bool(group_to_group)
    return False


def _usable(event: TimelineEvent) -> bool:
    if getattr(event, "role", "") != ROLE_USER:
        return False
    text = (getattr(event, "content", "") or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        return False
    if COMMAND_SPLIT_RE.match(text):
        return False
    if any(text.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES):
        return False
    return True


def build_cross_window(
    store,
    speaker_ids: list[str],
    current_window: str,
    *,
    minutes: int = 30,
    max_items: int = 6,
    max_chars: int = 320,
    persona_id: str = "",
    private_to_group: bool = False,
    group_to_group: bool = False,
) -> tuple[str, dict[str, Any]]:
    """组装跨窗口衔接块。

    Returns:
        (block_text, meta)。没有可用内容时 block_text 为空字符串。
    """
    target_kind = window_kind(current_window)
    if target_kind == "unknown" or not speaker_ids:
        return "", {"items": 0, "chars": 0, "reason": "target_unknown"}
    since = now_ts() - max(1, int(minutes)) * 60
    rows = store.timeline_in_windows(
        speaker_ids=speaker_ids,
        exclude_window=current_window,
        since_ts=since,
        limit=max(1, int(max_items)) * 4,
    )
    picked: list[tuple[int, str, str]] = []
    skipped_direction = 0
    for event in rows:
        if persona_id and getattr(event, "persona_id", "") not in ("", persona_id):
            continue  # 人格隔离：别的人格下说的话不带过来
        source_kind = window_kind(getattr(event, "window_tag", ""))
        if not direction_allowed(
            source_kind,
            target_kind,
            private_to_group=private_to_group,
            group_to_group=group_to_group,
        ):
            skipped_direction += 1
            continue
        if not _usable(event):
            continue
        picked.append((int(getattr(event, "ts", 0) or 0), source_kind, clip((event.content or "").strip(), 60)))
        if len(picked) >= max(1, int(max_items)):
            break
    if not picked:
        return "", {"items": 0, "chars": 0, "skipped_direction": skipped_direction}

    picked.sort(key=lambda item: item[0])
    lines = [
        "【衔接·同一个人在别处刚说的】只在当前话题确实在延续时自然接上；"
        "不要主动提起、不要透露来源或“我看到了你的群聊”。"
    ]
    used = len(lines[0])
    kept = 0
    for ts, source_kind, text in picked:
        where = "群里" if source_kind == "group" else "私聊里"
        line = f"- {fmt_ts(ts)} 在{where}说过：{text}"
        if max_chars > 0 and used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        kept += 1
    if kept == 0:
        return "", {"items": 0, "chars": 0, "skipped_direction": skipped_direction}
    block = "\n".join(lines)
    return block, {
        "items": kept,
        "chars": len(block),
        "skipped_direction": skipped_direction,
        "target": target_kind,
        "sources": sorted({kind for _ts, kind, _t in picked[:kept]}),
    }
