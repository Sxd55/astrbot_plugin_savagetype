"""上下文历史优化：清理历史中的 base64 图片和已完成的工具调用大结果，大幅削减 Token 开销。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

IMAGE_HISTORY_PLACEHOLDER = "[历史图片：已省略原始图像，仅保留占位符]"
TOOL_HISTORY_PLACEHOLDER = "[历史工具结果：已省略原始内容，最终结论见后续助手回复]"


def is_base64_image_part(part: Any) -> bool:
    """检查是否为包含 base64 data 的图片 part。"""
    if isinstance(part, dict):
        part_type = str(part.get("type", ""))
        image_url = part.get("image_url")
    else:
        part_type = str(getattr(part, "type", ""))
        image_url = getattr(part, "image_url", None)

    if part_type != "image_url":
        return False

    url: str | None = None
    if isinstance(image_url, dict):
        u = image_url.get("url")
        url = u if isinstance(u, str) else None
    elif image_url is not None:
        u = getattr(image_url, "url", None)
        url = u if isinstance(u, str) else None

    if not isinstance(url, str):
        return False
    normalized = url.lstrip().lower()
    meta = normalized.split(",", 1)[0]
    return meta.startswith("data:image/") and ";base64" in meta


def sanitize_part(part: Any) -> tuple[Any, bool]:
    if not is_base64_image_part(part):
        return part, False
    if isinstance(part, dict):
        res = {"type": "text", "text": IMAGE_HISTORY_PLACEHOLDER}
        if part.get("_no_save"):
            res["_no_save"] = True
        return res, True

    try:
        from astrbot.core.agent.message import TextPart
        p = TextPart(text=IMAGE_HISTORY_PLACEHOLDER)
        if getattr(part, "_no_save", False):
            setattr(p, "_no_save", True)
        return p, True
    except Exception:
        p = type("TextPartPlaceholder", (), {})()
        p.type = "text"
        p.text = IMAGE_HISTORY_PLACEHOLDER
        if getattr(part, "_no_save", False):
            setattr(p, "_no_save", True)
        return p, True


def sanitize_content(content: Any) -> tuple[Any, bool]:
    if not isinstance(content, list):
        return content, False
    changed = False
    new_parts = list(content)
    for idx, part in enumerate(new_parts):
        sanitized, part_changed = sanitize_part(part)
        if part_changed:
            new_parts[idx] = sanitized
            changed = True
    return (new_parts if changed else content), changed


def clone_message(message: Any, *, content: Any) -> Any:
    try:
        return replace(message, content=content)
    except Exception:
        pass
    if hasattr(message, "model_copy"):
        try:
            return message.model_copy(update={"content": content})
        except Exception:
            pass
    try:
        copied = message.__class__.__new__(message.__class__)
        copied.__dict__.update(getattr(message, "__dict__", {}))
        setattr(copied, "content", content)
        return copied
    except Exception:
        try:
            setattr(message, "content", content)
        except Exception:
            pass
        return message


def sanitize_image_messages(messages: list[Any]) -> tuple[list[Any], bool]:
    changed = False
    new_msgs = list(messages)
    for idx, msg in enumerate(new_msgs):
        if isinstance(msg, dict):
            c = msg.get("content")
            s_c, c_changed = sanitize_content(c)
            if c_changed:
                m = dict(msg)
                m["content"] = s_c
                new_msgs[idx] = m
                changed = True
        else:
            c = getattr(msg, "content", None)
            s_c, c_changed = sanitize_content(c)
            if c_changed:
                new_msgs[idx] = clone_message(msg, content=s_c)
                changed = True
    return (new_msgs if changed else messages), changed


def _message_val(msg: Any, key: str) -> Any:
    if isinstance(msg, dict):
        return msg.get(key)
    return getattr(msg, key, None)


def _extract_tool_call_ids(msg: Any) -> list[str]:
    raw = _message_val(msg, "tool_calls")
    if not raw or not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            tid = item.get("id")
        else:
            tid = getattr(item, "id", None)
        if tid and isinstance(tid, str) and tid.strip():
            ids.append(tid.strip())
    return ids


def find_completed_tool_result_groups(messages: list[Any]) -> list[list[int]]:
    """寻找已经闭环（已被后续 assistant 回答消费）的工具调用结果索引组。"""
    groups: list[list[int]] = []
    expected_ids: set[str] | None = None
    result_indexes: dict[str, int] = {}
    pending_invalid = False

    def reset() -> None:
        nonlocal expected_ids, result_indexes, pending_invalid
        expected_ids = None
        result_indexes = {}
        pending_invalid = False

    for idx, msg in enumerate(messages):
        role = _message_val(msg, "role")
        if role == "assistant":
            # 如果前面有期待的工具组且全部收齐，且当前 assistant 输出了正常非空回复，则闭环完成
            content = _message_val(msg, "content")
            has_content = bool(content and str(content).strip())
            if expected_ids and not pending_invalid and set(result_indexes) == expected_ids and has_content:
                groups.append(sorted(result_indexes.values()))

            reset()
            # 检查当前 assistant 是否发起了新的 tool_calls
            t_ids = _extract_tool_call_ids(msg)
            if t_ids:
                expected_ids = set(t_ids)
            continue

        if role == "_checkpoint":
            reset()
            continue

        if expected_ids is not None:
            if role == "tool":
                tid = _message_val(msg, "tool_call_id")
                if not isinstance(tid, str) or not tid.strip() or tid not in expected_ids or tid in result_indexes:
                    pending_invalid = True
                else:
                    result_indexes[tid] = idx
            elif role == "user":
                # 中间出现无关用户消息，打断工具闭环
                reset()

    return groups


def sanitize_tool_messages(messages: list[Any]) -> tuple[list[Any], bool]:
    """压缩已闭环的历史工具结果为占位符。"""
    groups = find_completed_tool_result_groups(messages)
    if not groups:
        return messages, False

    replacements: dict[int, Any] = {}
    for indexes in groups:
        for idx in indexes:
            msg = messages[idx]
            cur_content = _message_val(msg, "content")
            if cur_content == TOOL_HISTORY_PLACEHOLDER:
                continue
            if isinstance(msg, dict):
                m = dict(msg)
                m["content"] = TOOL_HISTORY_PLACEHOLDER
                replacements[idx] = m
            else:
                m = clone_message(msg, content=TOOL_HISTORY_PLACEHOLDER)
                replacements[idx] = m

    if not replacements:
        return messages, False

    new_msgs = list(messages)
    for idx, val in replacements.items():
        new_msgs[idx] = val
    return new_msgs, True


def sanitize_history_contexts(
    contexts: list[Any],
    clean_images: bool = True,
    clean_tools: bool = True,
) -> tuple[list[Any], bool]:
    changed = False
    current = contexts
    if clean_images:
        current, img_changed = sanitize_image_messages(current)
        changed = changed or img_changed
    if clean_tools:
        current, tool_changed = sanitize_tool_messages(current)
        changed = changed or tool_changed
    return current, changed


def sanitize_request_history(
    req: Any,
    clean_images: bool = True,
    clean_tools: bool = True,
) -> bool:
    """在请求发送前，对 req.contexts 与 conversation.history 瘦身。"""
    if req is None:
        return False
    changed_any = False

    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        sanitized, changed = sanitize_history_contexts(contexts, clean_images, clean_tools)
        if changed:
            try:
                req.contexts = sanitized
                changed_any = True
            except Exception:
                pass

    conversation = getattr(req, "conversation", None)
    raw_history = getattr(conversation, "history", None)
    parsed_history: list[Any] | None = None
    if isinstance(raw_history, list):
        parsed_history = raw_history
    elif isinstance(raw_history, str) and raw_history.strip():
        try:
            p = json.loads(raw_history)
            if isinstance(p, list):
                parsed_history = p
        except Exception:
            pass

    if parsed_history is not None:
        sanitized_h, h_changed = sanitize_history_contexts(parsed_history, clean_images, clean_tools)
        if h_changed:
            try:
                if isinstance(raw_history, str):
                    conversation.history = json.dumps(sanitized_h, ensure_ascii=False)
                else:
                    conversation.history = sanitized_h
                changed_any = True
            except Exception:
                pass

    return changed_any
