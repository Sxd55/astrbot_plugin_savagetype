"""Fact extraction from timeline. Evidence-bound; heuristic is the no-model fallback."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .contradiction import ContradictionEngine, looks_correction, looks_first_person
from .models import TimelineEvent
from .slots import apply_slot, canonical_subject
from .store import Store
from .util import (
    BOT_DEFINE_RE,
    CLOSE_RE,
    DIRECTIVE_RE,
    FIRST_PERSON_RE,
    MASTER_RE,
    ORIGIN_QQ,
    PREF_PATTERNS,
    REMEMBER_RE,
    REVIEW_UNVERIFIED,
    ROLE_ASSISTANT,
    ROLE_BOT_ID,
    ROLE_USER,
    SELF_STATEMENT_RE,
    STATUS_NOW_RE,
    clip,
    fingerprint,
    now_ts,
    safe_json_extract,
)

NORMALIZE_PROMPT = """你是记忆整理器。下面是一批候选聊天消息，每条都带事件 id 和说话人。
对每条明确由说话人自述、且有长期价值的信息：
1) 用直白、简短的中文复述稳定事实，不能添加原文没有的信息，不能推理；
2) 提取 1-3 个关键词；
3) 给出规范化槽位。
只输出 JSON 数组，每项字段：
source_event_id, plain, keywords, subject, attribute, value, confidence(0-1),
first_person(bool), explicit_correction(bool), mention_policy(mention|tone|uncertain),
write_op(create|update|close|ignore), ttl_seconds, topic
规则：
- source_event_id 必须是候选消息里真实存在的 id，且原文确实支持这条事实。
- 一条消息包含多个独立事实时必须拆成多条，每条只写一个事实。例如「我喜欢美式，不喜欢拿铁」拆成两条（喜欢美式 / 不喜欢拿铁），不要合并成一条。
- plain 只复述原文意思，不判断真假、不补充背景。
- attribute 只能是：likes, dislikes, name, identity, habit, promise, note, status
- 「不喜欢/不再喜欢 X」必须写成 attribute=likes、value 以「不」开头。不要用 dislikes，也不要另写 note。
- dislikes 只用于讨厌、受不了、生理反感。
- topic：这条偏好的领域词（饮品/食物/穿搭/娱乐/运动），用来区分同一个词的不同含义（「美式」咖啡 vs 「美式」穿搭）。拿不准就留空。
- status 只用于短暂当前状态（加班、感冒、这周很忙），必须带 ttl_seconds（默认 259200=3天）。
- write_op=close：用户说约定/未完成事项已完成或取消，用来归档已有 promise/habit，不要新建。
- write_op=ignore：玩笑、一次性情绪、不够格记住。
- subject：当前说话人自己的事实用 self；Bot 自己用 bot。不要记录第三个人的私事。
- 只记当前说话人用第一人称明确说出的偏好、称呼、约定、身份、习惯、纠正，或主人的明确指令。
- 闲聊、玩笑、反话、转述、别人的事不要输出。
候选消息：
{events}
"""


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Extractor:
    def __init__(
        self,
        store: Store,
        contradiction: ContradictionEngine,
        llm: Callable[..., Awaitable[str]] | None = None,
        is_owner: Callable[[str], bool] | None = None,
    ):
        self.store = store
        self.contradiction = contradiction
        self.llm = llm
        self.is_owner = is_owner

    # 「和/以及」只在两侧都有内容时才当分隔符，避免拆坏「和平精英」这种词。
    LIST_SPLIT_RE = re.compile(r"[、,，]|(?<=.)(?:和|以及)(?=.)")
    LIST_BAD_RE = re.compile(
        r"(喜欢|讨厌|不爱|受不了|不|没|别|我|你|您|他|她|它|俺|咱|"
        r"因为|所以|但是|不过|而且|然后|如果|虽然|就是|已经|正在|每天|"
        r"今天|明天|昨天|现在|最近|这周|今晚|很|太|挺|超|最)"
    )

    def _clean_list_item(self, item: str) -> str:
        text = (item or "").strip()
        text = re.sub(r"^(听|喝|吃|看|玩)", "", text)
        text = text.rstrip("了啦呢吧啊呀嘛哦喔诶欸").strip()
        return text

    def _list_like(self, item: str) -> bool:
        if not item or len(item) > 12:
            return False
        return self.LIST_BAD_RE.search(item) is None

    def _pref_hits(self, text: str) -> list[tuple[str, str, str]]:
        """Preference matches as (attribute, value, clause), with transcript guards.

        「喜欢 A，B」「喜欢 A 和 B」这类列举会拆成多条，避免只记第一个。
        """
        hits: list[tuple[str, str, str]] = []
        for regex, attr in PREF_PATTERNS:
            for match in regex.finditer(text):
                start = match.start()
                # 带主语的匹配不受限；无主语（如「不喜欢X」）必须是句首或标点后，
                # 避免从「朋友说不喜欢X」这类转述里偷事实。
                if start > 0 and text[start] not in "我俺咱主":
                    if text[start - 1] not in "，。！!？?；;：:、 \n\t":
                        continue
                raw_value = clip(match.group(1), 40)
                if not raw_value:
                    continue
                if attr == "status" and re.search(
                    r"(迷上|入坑|上瘾|喜欢|爱喝|爱吃|爱看|爱玩|爱听|讨厌)", match.group(0)
                ):
                    # 「我最近迷上了爬山」是长期偏好，交给 likes，不写成 3 天有效的状态。
                    continue
                negated = attr == "likes" and bool(
                    re.search(r"(不(?:太|怎么|是很)?喜欢|没喜欢|现在不喜欢|不再喜欢)", match.group(0))
                )
                raw_clause = match.group(0).strip().rstrip("，。！!？?、；;：: \t")
                values: list[str] = []
                if attr in {"likes", "dislikes"}:
                    parts = [p for p in self.LIST_SPLIT_RE.split(raw_value) if p.strip()]
                    if len(parts) > 1:
                        cleaned = [self._clean_list_item(p) for p in parts]
                        if all(self._list_like(p) for p in cleaned) and len(set(cleaned)) == len(cleaned):
                            values = cleaned
                if not values:
                    values = [raw_value]
                if attr in {"likes", "dislikes"}:
                    # 列举句的后续项（「喜欢咖啡，打篮球」）
                    delim = text[match.end() - 1] if match.end() > 0 else ""
                    if delim in "，,、":
                        rest = re.split(r"[。！!？?；;\n]", text[match.end():], maxsplit=1)[0]
                        piece = self._clean_list_item(rest)
                        if self._list_like(piece) and piece not in values:
                            values.append(piece)
                for value in values:
                    final = value
                    if negated:
                        final = "不" + value if not value.startswith("不") else value
                    clause = raw_clause if len(values) == 1 else final
                    hits.append((attr, final, clip(clause, 40)))
        return hits

    def infer_preferences(self, text: str) -> list[dict[str, str]]:
        """手动补记用：从一句话里认出偏好（可能多条），返回 attribute/value/plain 列表。"""
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for attr, value, clause in self._pref_hits(text or ""):
            key = (attr, value)
            if key in seen:
                continue
            seen.add(key)
            out.append({"attribute": attr, "value": value, "plain": clause or value})
        return out

    def expand_split(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """LLM 偶尔把「喜欢 A，不喜欢 B」合成一条：按模式确定性拆开，避免丢半边。

        整批按 (来源事件, 属性, 值) 去重，模型已经拆好的条目不会被重复展开。
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str]] = set()
        for entry in entries:
            attr = str(entry.get("attribute") or "")
            text = str(entry.get("plain") or "") or str(entry.get("value") or "")
            hits: list[tuple[str, str, str]] = []
            if attr in {"likes", "dislikes", "note"}:
                local: set[tuple[str, str]] = set()
                for hit in self._pref_hits(text):
                    key = (hit[0], hit[1])
                    if key in local:
                        continue
                    local.add(key)
                    hits.append(hit)
            group = hits if len(hits) >= 2 else [(attr, str(entry.get("value") or ""), "")]
            for hit_attr, value, clause in group:
                clone = dict(entry)
                if len(hits) >= 2:
                    clone["attribute"] = hit_attr
                    clone["value"] = value
                    clone["plain"] = clause or value
                    # 拆分后领域要按各自分句重判，不能继承整句的领域。
                    clone["topic"] = ""
                    if hit_attr == "status" and not int(clone.get("ttl_seconds") or 0):
                        clone["ttl_seconds"] = 3 * 86400
                key = (
                    int(clone.get("source_event_id") or 0),
                    str(clone.get("attribute") or ""),
                    str(clone.get("value") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(clone)
        return out

    def heuristic(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        return self.extract_heuristic(events)

    def extract_heuristic(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.role != ROLE_USER:
                continue
            if ev.speaker_id == ROLE_BOT_ID:
                continue
            text = (ev.content or "").strip()
            if len(text) < 2:
                continue
            if not (FIRST_PERSON_RE.search(text) or MASTER_RE.search(text)):
                continue
            if not (
                DIRECTIVE_RE.search(text)
                or REMEMBER_RE.search(text)
                or SELF_STATEMENT_RE.search(text)
                or looks_correction(text)
            ):
                continue
            seen_values: set[tuple[str, str]] = set()
            for attr, value, clause in self._pref_hits(text):
                if (attr, value) in seen_values:
                    continue
                seen_values.add((attr, value))
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute=attr,
                    value=value,
                    content=clip(text, 120),
                    confidence=0.72 if looks_first_person(text) else 0.45,
                    # 用这条事实自己的分句当 plain，领域判定才不会被整句里的其它领域污染。
                    extra={"plain": clip(clause or value, 160)},
                )
                if looks_correction(text):
                    payload["explicit_correction"] = 1
                if attr == "status":
                    payload["ttl_seconds"] = 3 * 86400
                    payload["write_op"] = "create"
                out.append(payload)
            if CLOSE_RE.search(text):
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute="promise",
                    value=clip(text, 40),
                    content=clip(text, 120),
                    confidence=0.8,
                )
                payload["write_op"] = "close"
                payload["explicit_correction"] = 1
                out.append(payload)
            elif STATUS_NOW_RE.search(text) and FIRST_PERSON_RE.search(text):
                payload = self._payload(
                    ev,
                    subject="self",
                    attribute="status",
                    value=clip(STATUS_NOW_RE.search(text).group(0), 40),
                    content=clip(text, 120),
                    confidence=0.7,
                )
                payload["ttl_seconds"] = 3 * 86400
                payload["write_op"] = "create"
                out.append(payload)
        return out

    async def normalize_llm(self, events: list[TimelineEvent]) -> list[dict[str, Any]]:
        """One batch call: raw candidates -> evidence-bound fact payloads."""
        if self.llm is None or not events:
            return []
        by_id = {e.id: e for e in events}
        lines = []
        for ev in events:
            who = ev.speaker_name or ev.speaker_id or ev.role
            role = "bot" if ev.role == ROLE_ASSISTANT else "user"
            lines.append(f"[{ev.id}] role={role} from={who}({ev.speaker_id}): {clip(ev.content, 200)}")
        raw = await self.llm(NORMALIZE_PROMPT.format(events="\n".join(lines)))
        parsed = safe_json_extract(raw)
        if parsed is None:
            # 不是「模型判断没有值得记的」（那是合法 []），而是输出根本无法解析：
            # 抛错让管线退回启发式并标记未审核，避免整批消息被静默丢弃。
            raise ValueError("normalize output unparseable")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ValueError("normalize output not a list")
        out: list[dict[str, Any]] = []
        for item in parsed:
            try:
                payload = self._normalize_item(item, by_id)
            except Exception:  # noqa: BLE001
                # 单条脏数据不拖死整批。
                continue
            if payload is not None:
                out.append(payload)
        return out

    def _normalize_item(self, item: Any, by_id: dict[int, TimelineEvent]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        try:
            event_id = int(item.get("source_event_id") or 0)
        except (TypeError, ValueError):
            return None
        ev = by_id.get(event_id)
        if ev is None:
            return None
        op = str(item.get("write_op") or "create").strip().lower()
        if op == "ignore" or op not in {"create", "update", "close", "ignore"}:
            return None
        plain = clip(str(item.get("plain") or "").strip(), 160)
        if not plain:
            return None
        attribute = str(item.get("attribute") or "").strip()
        value = str(item.get("value") or "").strip()
        subject_raw = str(item.get("subject") or "").strip()
        if not attribute or (not value and op != "close"):
            return None
        subject = canonical_subject(subject_raw or "self", ev.speaker_id, ev.speaker_name)
        if subject == "bot":
            if ev.role == ROLE_ASSISTANT:
                speaker_id, speaker_name = ROLE_BOT_ID, "bot"
            elif (
                ev.role == ROLE_USER
                and BOT_DEFINE_RE.search(ev.content or "")
                and self.is_owner is not None
                and self.is_owner(ev.speaker_id)
            ):
                # 只有主人能给 Bot 下定义（「以后你叫…」「你要…」），否则任何人都能改写 Bot 设定。
                speaker_id, speaker_name = ROLE_BOT_ID, "bot"
            else:
                return None
        else:
            if ev.role != ROLE_USER or ev.speaker_id == ROLE_BOT_ID:
                return None
            speaker_id, speaker_name = ev.speaker_id, ev.speaker_name
        keywords: list[str] = []
        for kw in item.get("keywords") or []:
            kw = clip(str(kw).strip(), 12)
            if kw and kw not in keywords:
                keywords.append(kw)
        payload = self._payload(
            ev,
            subject=subject,
            attribute=clip(attribute, 40),
            value=clip(value, 80),
            content=clip(ev.content or "", 160),
            confidence=_safe_float(item.get("confidence"), 0.6),
            extra={
                "first_person": _safe_int(item.get("first_person"), 0),
                "explicit_correction": _safe_int(item.get("explicit_correction"), 0),
                "mention_policy": item.get("mention_policy") or "mention",
                "source": "llm",
                "write_op": op,
                "ttl_seconds": _safe_int(item.get("ttl_seconds"), 0),
                "plain": plain,
                "keywords": keywords,
                "topic": clip(str(item.get("topic") or ""), 20),
            },
        )
        payload["speaker_id"] = speaker_id
        payload["speaker_name"] = speaker_name or speaker_id
        return payload

    def _payload(
        self,
        ev: TimelineEvent,
        subject: str,
        attribute: str,
        value: str,
        content: str,
        confidence: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "subject": subject,
            "attribute": attribute,
            "value": value,
            "content": content,
            "speaker_id": ev.speaker_id,
            "speaker_name": ev.speaker_name,
            "bot_id": ev.bot_id,
            "window_tag": ev.window_tag,
            "persona_id": getattr(ev, "persona_id", "") or "",
            "confidence": confidence,
            "evidence": [ev.id],
            "source_event_id": ev.id,
            "first_person": int(looks_first_person(ev.content)),
            "explicit_correction": int(looks_correction(ev.content)),
            "source": "heuristic",
            "origin": ORIGIN_QQ,
            "review_status": REVIEW_UNVERIFIED,
            "plain": clip(content, 160),
            "keywords": [],
            "created_at": now_ts(),
            "fingerprint": fingerprint(ev.speaker_id, subject, attribute, value),
        }
        if extra:
            payload.update(extra)
        return apply_slot(payload)
