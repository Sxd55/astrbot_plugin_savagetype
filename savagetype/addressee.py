"""收件人解析：从消息组件里提取「这条消息在对谁说」（@ 谁 / 回复谁）。

存储格式（timeline.addressee）：逗号分隔的 token，便于解析与展示：

    at:10001|小明,reply:10002|阿May

- `at:`  @ 的接收者（`at:all|` 表示 @全体）
- `reply:` 被引用消息的发送者

渲染时用 `render_addressee()` 转成「小明」或「小明、阿May」。
"""

from __future__ import annotations

from typing import Any, Iterable

MAX_ITEMS = 3


def _kind(component: Any) -> str:
    return type(component).__name__.lower()


def parse_components(components: Iterable[Any] | None) -> list[dict[str, str]]:
    """从消息组件列表提取收件人（保持出现顺序，去重）。"""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for component in components or []:
        name = _kind(component)
        if name in ("atat", "atall", "allat"):
            entry = {"kind": "at", "id": "all", "name": ""}
        elif name == "at":
            entry = {
                "kind": "at",
                "id": str(getattr(component, "qq", "") or ""),
                "name": str(getattr(component, "name", "") or ""),
            }
        elif name == "reply":
            entry = {
                "kind": "reply",
                "id": str(getattr(component, "sender_id", "") or ""),
                "name": str(getattr(component, "sender_nickname", "") or ""),
            }
        else:
            continue
        key = f"{entry['kind']}:{entry['id']}"
        if entry["id"] and key not in seen:
            seen.add(key)
            items.append(entry)
        if len(items) >= MAX_ITEMS:
            break
    return items


def encode(items: list[dict[str, str]]) -> str:
    """序列化成 timeline.addressee 的存储字符串。"""
    tokens: list[str] = []
    for item in items:
        name = str(item.get("name") or "").replace("|", " ").replace(",", " ").strip()
        tokens.append(f"{item.get('kind', 'at')}:{item.get('id', '')}|{name}")
    return ",".join(tokens)


def decode(raw: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        kind, rest = token.split(":", 1)
        target, _, name = rest.partition("|")
        if not target.strip():
            continue
        items.append({"kind": kind.strip(), "id": target.strip(), "name": name.strip()})
    return items


def from_components(components: Iterable[Any] | None) -> str:
    """便捷入口：组件 -> 存储字符串。"""
    return encode(parse_components(components))


def render_addressee(raw: str, *, self_id: str = "", bot_label: str = "你") -> str:
    """把存储字符串渲染成给人看的目标标签（去掉 @bot 自己）。"""
    labels: list[str] = []
    own = str(self_id or "")
    for item in decode(raw):
        target = item.get("id", "")
        if own and target == own:
            label = bot_label
        elif target == "all":
            label = "全体"
        else:
            label = item.get("name") or target
        if label and label not in labels:
            labels.append(label)
    return "、".join(labels)


def has_bot_mention(raw: str, self_id: str) -> bool:
    own = str(self_id or "")
    if not own:
        return False
    return any(item.get("id") == own for item in decode(raw))
