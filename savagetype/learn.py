"""v2.1 learning: quality-gated jargon, few-shots, persona drafts.

Inspired by self_learning's review-first loop. Original implementation, not a port.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .models import LearningPack, TimelineEvent
from .store import Store
from .util import LOW_INFO_RE, clip, fingerprint, normalize_slot, now_ts, safe_json_extract

STOP_TERMS = {
    "的", "了", "吗", "呢", "啊", "吧", "哦", "哈", "嗯", "这个", "那个", "什么",
    "不是", "就是", "可以", "没有", "还是", "一个", "我们", "你们", "他们",
    "真的", "一下", "然后", "所以", "因为", "如果", "怎么", "为什么",
    "确实", "离谱", "那种", "这样", "那样", "感觉", "知道", "觉得", "可能",
    "应该", "已经", "还是", "或者", "以及", "自己", "今天", "明天", "昨天",
    "哈哈", "嘿嘿", "呵呵", "www", "lol", "ok", "okay", "emmm",
}

GENERIC_MEANING_RE = re.compile(
    r"(表示认同|常用语气|口头禅|语气词|没有特殊含义|普通词|感叹词)"
)
IDENTITY_RE = re.compile(r"(你是|你叫|你住|你的名字|你今年|你的身份|设定你)")
COMMAND_RE = re.compile(r"^[/／]|stype |savagetype_|ok=true|ok=false|tool_call|function_call")
URL_RE = re.compile(r"https?://|www\.")
JARGON_TOKEN_RE = re.compile(r"[A-Za-z]{2,16}|[一-鿿]{2,8}")
PAIR_GAP_SECONDS = 90

JARGON_PROMPT = """你是黑话注释器。根据对话判断候选词是不是小圈子用语。
只输出 JSON 数组，每项：term, meaning, in_group_only(bool)
规则：
- 只解释含义，不要教人复读或扩散。
- 普通词、人名、表情、无把握、只有语气作用的项不要输出。
- 没有就 []。
候选：{terms}
对话：
{dialog}
"""

PERSONA_PROMPT = """你是人格增量草稿员。根据已批准的表达样本和当前人格摘要，写一段不超过 80 字的补丁。
这是草稿，不会写回人格文件。只描述「怎么说」，不要新身份、不要秘密、不要命令覆盖人设。
不要出现「你是/你叫/你住」这类身份句。输出纯文本一段，不要 JSON。
当前人格摘要：
{persona}
样本：
{samples}
"""


def _tokens(text: str) -> list[str]:
    out = []
    for tok in JARGON_TOKEN_RE.findall(text or ""):
        if is_junk_term(tok):
            continue
        out.append(tok)
    return out


def is_junk_term(term: str) -> bool:
    t = (term or "").strip()
    if len(t) < 2:
        return True
    if t.isdigit():
        return True
    if t.lower() in STOP_TERMS or t in STOP_TERMS:
        return True
    if URL_RE.search(t):
        return True
    if re.fullmatch(r"[A-Za-z]{1,2}", t):
        return True
    return False


def is_generic_meaning(meaning: str) -> bool:
    return bool(GENERIC_MEANING_RE.search(meaning or ""))


def is_command_or_tool(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if COMMAND_RE.search(t):
        return True
    if t.startswith("ok=") or "Traceback" in t or "Error:" in t:
        return True
    return False


def term_in_query(term: str, query: str) -> bool:
    term = (term or "").strip()
    query = query or ""
    if not term or not query:
        return False
    if re.fullmatch(r"[A-Za-z0-9_]+", term):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", query, re.I) is not None
    return term in query


def _same_turn(user: TimelineEvent, bot: TimelineEvent) -> bool:
    if user.window_tag and bot.window_tag:
        if user.window_tag != bot.window_tag:
            return False
    elif user.speaker_id and bot.speaker_id not in {"", "bot_self", user.speaker_id}:
        return False
    user_p, bot_p = user.persona_id or "", bot.persona_id or ""
    if user_p and bot_p and user_p != bot_p:
        return False
    return True


def pair_user_bot(events: list[TimelineEvent], gap: int = PAIR_GAP_SECONDS) -> list[tuple[TimelineEvent, TimelineEvent]]:
    pairs: list[tuple[TimelineEvent, TimelineEvent]] = []
    pending: TimelineEvent | None = None
    for ev in events:
        if ev.role == "user" and not LOW_INFO_RE.match((ev.content or "").strip()):
            pending = ev
            continue
        if ev.role != "assistant":
            continue
        if pending and 0 <= ev.ts - pending.ts <= gap and _same_turn(pending, ev) and (ev.content or "").strip():
            if not is_command_or_tool(pending.content) and not is_command_or_tool(ev.content):
                pairs.append((pending, ev))
        pending = None
    return pairs


class LearningEngine:
    def __init__(self, store: Store, llm: Callable[..., Awaitable[str]] | None, config: dict[str, Any]):
        self.store = store
        self.llm = llm
        self.config = config

    def learning_ok(self) -> bool:
        return bool(self.config.get("learning_enabled", True))

    def skip_style(self) -> bool:
        return bool(self.config.get("_skip_style_learning"))

    def observe_message(self, text: str, persona_id: str = "") -> None:
        if not self.learning_ok() or self.skip_style():
            return
        if not bool(self.config.get("jargon_enabled", True)):
            return
        if LOW_INFO_RE.match((text or "").strip()) or is_command_or_tool(text):
            return
        for term in _tokens(text):
            self.store.bump_jargon(term, persona_id=persona_id)

    async def maybe_learn(self, force: bool = False, persona_text: str = "") -> dict[str, Any]:
        if not self.learning_ok() or self.skip_style():
            return {"ok": True, "skipped": True, "reason": "learning disabled"}
        events = self.store.timeline_recent(limit=int(self.config.get("learn_window") or 40))
        events = list(reversed(events))
        out: dict[str, Any] = {"ok": True, "jargon": 0, "fewshot": 0, "persona": 0}
        if bool(self.config.get("fewshot_enabled", True)):
            out["fewshot"] = self._queue_fewshots(events)
        if bool(self.config.get("jargon_enabled", True)):
            out["jargon"] = await self._queue_jargon(events, force=force)
        if bool(self.config.get("persona_draft_enabled", True)):
            out["persona"] = await self._queue_persona_draft(force=force, persona_text=persona_text)
        return out

    def _rejected_fewshot_keys(self) -> set[str]:
        keys: set[str] = set()
        for item in self.store.list_reviews("rejected", kind="fewshot", limit=80):
            user = normalize_slot(str(item.payload.get("user") or ""))[:24]
            if user:
                keys.add(user)
        return keys

    def score_review(self, kind: str, payload: dict[str, Any]) -> int:
        if kind == "jargon":
            term = str(payload.get("term") or "")
            meaning = str(payload.get("meaning") or "")
            score = 50
            if 2 <= len(term) <= 8:
                score += 15
            if meaning and not is_generic_meaning(meaning):
                score += 20
            if is_junk_term(term):
                score -= 40
            return max(0, min(100, score))
        if kind == "fewshot":
            u = str(payload.get("user") or "")
            b = str(payload.get("bot") or "")
            score = 40 + min(20, len(u) // 4) + min(20, len(b) // 6)
            if is_command_or_tool(u) or is_command_or_tool(b):
                score -= 40
            return max(0, min(100, score))
        if kind == "persona":
            draft = str(payload.get("draft") or "")
            score = 55 + min(20, len(draft) // 4)
            if IDENTITY_RE.search(draft):
                score -= 50
            return max(0, min(100, score))
        return 50

    def _queue_fewshots(self, events: list[TimelineEvent]) -> int:
        n = 0
        min_user = int(self.config.get("fewshot_min_user_chars") or 4)
        min_bot = int(self.config.get("fewshot_min_bot_chars") or 4)
        rejected = self._rejected_fewshot_keys()
        for user, bot in pair_user_bot(events):
            u = clip(user.content, 120)
            b = clip(bot.content, 160)
            if len(u) < min_user or len(b) < min_bot:
                continue
            if LOW_INFO_RE.match(u.strip()) or is_command_or_tool(u) or is_command_or_tool(b):
                continue
            user_key = normalize_slot(u)[:24]
            if user_key in rejected:
                continue
            payload = {
                "user": u,
                "bot": b,
                "user_event_id": user.id,
                "bot_event_id": bot.id,
                "mention_policy": "tone",
            }
            payload["quality"] = self.score_review("fewshot", payload)
            fp = fingerprint("fewshot", user.persona_id, normalize_slot(u), normalize_slot(b))
            self.store.upsert_review(
                kind="fewshot",
                fingerprint=fp,
                title=clip(u, 24),
                payload=payload,
                reason="real_user_bot_pair",
                speaker_id=user.speaker_id,
                persona_id=user.persona_id or bot.persona_id,
            )
            n += 1
        return n

    def _blocked_jargon(self) -> set[str]:
        blocked: set[str] = set()
        for item in self.store.list_reviews("rejected", kind="jargon", limit=80):
            term = str(item.payload.get("term") or item.title or "")
            if term:
                blocked.add(term)
        for item in self.store.approved_reviews("jargon", limit=80):
            term = str(item.payload.get("term") or "")
            if term:
                blocked.add(term)
        for item in self.store.list_reviews("pending", kind="jargon", limit=80):
            term = str(item.payload.get("term") or "")
            if term:
                blocked.add(term)
        return blocked

    async def _queue_jargon(self, events: list[TimelineEvent], force: bool = False) -> int:
        now = now_ts()
        cooldown = int(self.config.get("jargon_cooldown_seconds") or 120)
        last = int(self.store.get_meta("jargon_last_at") or "0")
        if not force and last and now - last < cooldown:
            return 0
        min_count = int(self.config.get("jargon_min_count") or 4)
        hot = self.store.hot_jargon(min_count=min_count, limit=12)
        if not hot:
            return 0
        blocked = self._blocked_jargon()
        corpus = self.store.jargon_corpus_size()
        terms = []
        for h in hot:
            term = h["term"]
            if term in blocked or is_junk_term(term):
                continue
            if corpus >= 80 and h["count"] / corpus > 0.25:
                continue
            if self.store.jargon_persona_spread(term) >= 4:
                continue
            terms.append(term)
        if not terms:
            return 0
        meanings: dict[str, str] = {}
        if self.llm is not None:
            dialog = "\n".join(f"{ev.role}: {clip(ev.content, 80)}" for ev in events[-16:])
            try:
                raw = await self.llm(JARGON_PROMPT.format(terms="、".join(terms), dialog=dialog))
                parsed = safe_json_extract(raw) or []
                if isinstance(parsed, list):
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        term = str(item.get("term") or "").strip()
                        meaning = clip(str(item.get("meaning") or ""), 40)
                        if term and meaning and not is_generic_meaning(meaning) and not is_junk_term(term):
                            meanings[term] = meaning
            except Exception:  # noqa: BLE001
                meanings = {}
        n = 0
        for term in terms:
            meaning = meanings.get(term, "")
            if not meaning:
                continue
            payload = {"term": term, "meaning": meaning}
            payload["quality"] = self.score_review("jargon", payload)
            self.store.upsert_review(
                kind="jargon",
                fingerprint=fingerprint("jargon", term),
                title=term,
                payload=payload,
                reason="statistical_prefilter",
            )
            n += 1
        self.store.set_meta("jargon_last_at", str(now))
        return n

    def _draft_too_close(self, draft: str, persona_text: str) -> bool:
        if not persona_text or not draft:
            return False
        a = set(normalize_slot(draft))
        b = set(normalize_slot(persona_text[:400]))
        if not a or not b:
            return False
        return len(a & b) / max(1, len(a)) > 0.72

    async def _queue_persona_draft(self, force: bool = False, persona_text: str = "") -> int:
        now = now_ts()
        cooldown = int(self.config.get("persona_draft_cooldown_seconds") or 86400)
        last = int(self.store.get_meta("persona_draft_last_at") or "0")
        if not force and last and now - last < cooldown:
            return 0
        shots = self.store.approved_reviews("fewshot", limit=8)
        if len(shots) < int(self.config.get("persona_draft_min_fewshots") or 4):
            return 0
        if self.llm is None:
            return 0
        samples = "\n".join(
            f"用户: {item.payload.get('user')}\nBot: {item.payload.get('bot')}" for item in shots[:6]
        )
        try:
            draft = clip(
                (await self.llm(PERSONA_PROMPT.format(persona=clip(persona_text, 240) or "（未提供）", samples=samples))).strip(),
                80,
            )
        except Exception:  # noqa: BLE001
            return 0
        if len(draft) < 8 or IDENTITY_RE.search(draft) or self._draft_too_close(draft, persona_text):
            return 0
        payload = {"draft": draft, "source_fewshots": [s.id for s in shots[:6]], "created_at": now}
        payload["quality"] = self.score_review("persona", payload)
        self.store.upsert_review(
            kind="persona",
            fingerprint=fingerprint("persona", normalize_slot(draft)),
            title=clip(draft, 24),
            payload=payload,
            reason="approved_fewshot_summary",
            persona_id=shots[0].persona_id,
        )
        self.store.set_meta("persona_draft_last_at", str(now))
        return 1

    def set_status(self, review_id: int, status: str) -> dict[str, Any]:
        item = self.store.get_review(review_id)
        if not item:
            return {"ok": False, "error": "review not found"}
        if status not in {"approved", "rejected", "pending"}:
            return {"ok": False, "error": "bad status"}
        self.store.set_review_status(review_id, status)
        if status == "rejected" and item.kind == "jargon":
            term = str(item.payload.get("term") or item.title or "").strip()
            if term:
                self.store.drop_jargon_term(term)
        return {"ok": True, "id": review_id, "status": status}

    def pack_for(self, query: str, persona_id: str = "", route: str = "long_term") -> LearningPack:
        pack = LearningPack()
        if self.skip_style() or not self.learning_ok():
            return pack
        q = query or ""
        jargon_limit = int(self.config.get("inject_jargon_limit") or 3)
        fewshot_limit = int(self.config.get("inject_fewshot_limit") or 2)
        jargon_items = []
        for item in self.store.approved_reviews("jargon", persona_id=persona_id, limit=20):
            term = str(item.payload.get("term") or "")
            meaning = str(item.payload.get("meaning") or "")
            if not term or not meaning:
                continue
            if term_in_query(term, q):
                jargon_items.append({"term": term, "meaning": meaning})
            if len(jargon_items) >= jargon_limit:
                break
        pack.jargon = jargon_items
        if route != "low_info":
            seen: list[str] = []
            shots = []
            for i in self.store.approved_reviews("fewshot", persona_id=persona_id, limit=12):
                key = normalize_slot(str(i.payload.get("user") or ""))[:24]
                if key in seen:
                    continue
                seen.append(key)
                shots.append(
                    {
                        "user": clip(str(i.payload.get("user") or ""), 60),
                        "bot": clip(str(i.payload.get("bot") or ""), 80),
                    }
                )
                if len(shots) >= fewshot_limit:
                    break
            pack.fewshots = shots
            ttl = int(self.config.get("persona_draft_ttl_seconds") or 14 * 86400)
            drafts = self.store.approved_reviews("persona", persona_id=persona_id, limit=1)
            if drafts:
                created = int(drafts[0].payload.get("created_at") or drafts[0].created_at or 0)
                if created and now_ts() - created > ttl:
                    pack.persona_draft = ""
                else:
                    pack.persona_draft = clip(str(drafts[0].payload.get("draft") or ""), 80)
        return pack
