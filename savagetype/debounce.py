"""防抖（启发式）：判断用户是否还没说完 + 合并碎片消息。

不依赖任何模型：用标点/长度/连接词做启发式判断；零额外依赖。

- 只有「短消息」才可能被挂起（超过 `short_chars` 直接放行）；
- 以句末标点结尾视为说完；以逗号/连接词结尾视为没说完；
- 挂起窗口内同一人的后续消息并入同一条，窗口结束（或达到上限）后重新提交。
"""

from __future__ import annotations

SENTENCE_END = "。！？!?…~～.\u3002\uff01\uff1f"
CONT_PUNCT = "，,、；;：:、" + "\uff0c\uff1b\uff1a"
CONT_WORDS = (
    "然后", "而且", "但是", "因为", "所以", "就是", "那个", "这个", "还有", "以及",
    "如果", "虽然", "不过", "并且", "要是", "感觉", "觉得", "帮我", "你看看",
)

# 完整短回复：本身就是一句话，不用等后续（精确匹配才算）。
ACK_WORDS = frozenset({
    "好", "好的", "好嘞", "好的好的", "收到", "收到收到", "明白", "知道了",
    "了解", "可以", "行", "嗯", "嗯嗯", "哦", "哦哦", "哈哈", "嘿嘿",
    "没问题", "OK", "ok", "Ok", "okok",
})


def is_probably_incomplete(text: str, *, short_chars: int = 12) -> bool:
    """启发式判断：这条消息像不像「话还没说完」。"""
    value = (text or "").strip()
    if not value:
        return False
    if value in ACK_WORDS:
        return False
    if len(value) > max(1, int(short_chars)):
        return False
    tail = value[-1]
    if tail in SENTENCE_END:
        return False
    if tail in CONT_PUNCT:
        return True
    if value.endswith(CONT_WORDS):
        return True
    # 短句且没有句末标点（例如「在吗」「今天那个」「我想说」）→ 等一等再说
    return len(value) <= 6


def merge_fragments(fragments: list[str], *, max_chars: int = 600) -> str:
    """把碎片合并成一条消息（中文直接拼接，英文之间加空格）。"""
    parts: list[str] = []
    for raw in fragments:
        piece = (raw or "").strip()
        if not piece:
            continue
        if parts and _needs_space(parts[-1], piece):
            parts.append(" " + piece)
        else:
            parts.append(piece)
    text = "".join(parts).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left[-1] in "，,、；;：:。！？!?…~～" or right[0] in "，,、；;：:。！？!?…~～":
        return False
    return right[0].isascii() and right[0].isalnum() and left[-1].isascii() and left[-1].isalnum()
