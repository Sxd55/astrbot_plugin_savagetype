"""Plugin orchestration: capture, extract, retrieve, inject, providers."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive import (
    archive_decayed,
    archive_decayed_events,
    archive_low_value,
    backup_db,
    compact_summarized_timeline,
    compact_superseded,
    expire_pending_overrides,
    expire_persona_drafts,
    expire_status_facts,
    fold_preference_slots,
    import_jsonl,
    import_transcript_events,
    parse_transcript,
    preview_jsonl,
    prune_jargon_stats,
)
from . import tokenize as tokenizer_mod
from .coexistence import Coexistence
from .contradiction import ContradictionEngine
from .crosswin import build_cross_window, window_kind
from .events import EventPipeline
from .extract import Extractor
from .inject import build_pack
from .learn import LearningEngine
from .addressee import (
    from_components as addressee_from_components,
    has_bot_mention,
    render_addressee,
)
from .profile import build_profile_card
from .speak import (
    allowed_target,
    clip_content,
    group_label,
    parse_intent,
    resolve_number,
    resolve_target,
)
from .replygate import (
    evaluate as reply_gate_evaluate,
    in_targets,
    keyword_hit,
    memory_hit,
    normalize_mode,
    parse_targets,
    probability_hit,
)
from .windowflow import build_window_flow
from .llm import (
    BudgetGuard,
    LLMBudgetExceeded,
    estimate_tokens,
    looks_refusal,
    resolve_provider,
)
from .pipeline import MemoryPipeline
from .profiles import build_profile
from .retrieve import Retriever, classify_route
from .store import Store
from .slots import apply_slot
from .util import (
    COMMAND_SPLIT_RE,
    ORIGIN_MANUAL,
    REVIEW_MANUAL,
    ROLE_ASSISTANT,
    ROLE_BOT_ID,
    ROLE_USER,
    SCOPE_OWNER,
    SCOPE_PERSON,
    clip,
    estimate_tokens,
    fingerprint,
    norm_platform,
    now_ts,
    parse_csv,
    platform_of,
)

DEFAULT_SOURCE_PLATFORMS = "aiocqhttp,qq_official,qq_official_webhook"

KNOWN_ADAPTER_TYPES = {
    "aiocqhttp",
    "qq_official",
    "telegram",
    "wecom",
    "wecom_ai_bot",
    "lark",
    "dingtalk",
    "discord",
    "slack",
    "kook",
    "vocechat",
    "weixin_official_account",
    "weixin_oc",
    "satori",
    "misskey",
    "line",
    "matrix",
    "mattermost",
    "webchat",
}


def _unpack_llm_result(result: Any) -> tuple[str, int, int]:
    if isinstance(result, tuple) and len(result) >= 1:
        text = str(result[0] or "")
        tokens_in = int(result[1]) if len(result) > 1 and result[1] is not None else 0
        tokens_out = int(result[2]) if len(result) > 2 and result[2] is not None else 0
        return text, tokens_in, tokens_out
    return str(result or ""), 0, 0


class SavageTypeService:
    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        llm_generate,
        get_provider,
        logger,
        get_persona_text=None,
        send_message=None,
    ):
        self.store = store
        self.config = config
        self.llm_generate = llm_generate
        self.get_provider = get_provider
        self.logger = logger
        self.get_persona_text = get_persona_text
        self.send_message = send_message
        self._owner_ids: set[str] = set()
        self.coexistence = Coexistence(enabled=bool(config.get("coexistence_degrade", True)))
        self.contradiction = ContradictionEngine(
            store,
            high_evidence=float(config.get("high_evidence_confidence", 0.8)),
            owner_ids=self._owner_ids,
        )
        self.extractor = Extractor(
            store,
            self.contradiction,
            llm=self._llm_for("normalize"),
            is_owner=self.is_owner_speaker,
        )
        self.pipeline = MemoryPipeline(
            store,
            self.contradiction,
            self.extractor,
            self.config,
            logger,
            llm=self._llm_for("normalize"),
            verify_llm=self._llm_for("verify"),
            is_owner_speaker=self.is_owner_speaker,
        )
        self.learning = LearningEngine(store, llm=self._llm_for("learn"), config=self.config)
        self.events = EventPipeline(
            store,
            self.config,
            logger,
            llm=self._llm_for("event"),
            verify_llm=self._llm_for("verify"),
            is_owner_speaker=self.is_owner_speaker,
        )
        self.retriever = Retriever(
            store,
            embed=None,
            rerank=self._rerank,
            mode=str(config.get("retrieval_mode") or "auto"),
            cache_ttl=max(0, int(20 if config.get("cache_ttl_seconds") is None else config.get("cache_ttl_seconds"))),
            bm25=bool(True if config.get("retrieval_bm25") is None else config.get("retrieval_bm25")),
        )
        self._extract_lock = asyncio.Lock()
        self._embed_lock = asyncio.Lock()
        self._last_extract_at = 0
        self._extract_fail_until = 0
        self._events_fail_until = 0
        self._llm_guard: BudgetGuard | None = None
        self._learn_task: asyncio.Task | None = None
        self._sync_embed_fn()

    def embedding_auto_threshold(self) -> int:
        raw = self.config.get("embedding_auto_threshold")
        return int(2500 if raw is None else raw)

    def embedding_wanted(self) -> bool:
        if bool(self.config.get("embedding_enabled")):
            return True
        threshold = self.embedding_auto_threshold()
        if threshold <= 0:
            return False
        live = int(self.store.counts().get("facts_live") or 0)
        return live >= threshold

    def embedding_status(self) -> dict[str, Any]:
        live = int(self.store.counts().get("facts_live") or 0)
        provider = None
        try:
            provider = self.get_provider("embedding", str(self.config.get("embedding_provider_id") or ""))
        except Exception:
            provider = None
        wanted = self.embedding_wanted()
        reason = "off"
        if bool(self.config.get("embedding_enabled")):
            reason = "manual"
        elif self.embedding_auto_threshold() > 0 and live >= self.embedding_auto_threshold():
            reason = "auto_threshold"
        active = wanted and provider is not None
        if wanted and provider is None:
            reason = "need_provider"
        return {
            "config_enabled": bool(self.config.get("embedding_enabled")),
            "wanted": wanted,
            "active": active,
            "reason": reason,
            "live": live,
            "threshold": self.embedding_auto_threshold(),
            "has_provider": provider is not None,
        }

    def _sync_embed_fn(self) -> None:
        status = self.embedding_status()
        self.retriever.embed = self._embed if status["active"] else None

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def owner_qq(self) -> str:
        return str(self.config.get("owner_qq") or "").strip()

    def _rebuild_owner_ids(self) -> None:
        ids = set(self._owner_ids)
        owner = self.owner_qq()
        if owner:
            ids.add(self.store.resolve_speaker(owner))
        self._owner_ids = ids
        self.contradiction.owner_ids = set(ids)

    def mark_owner_speaker(self, speaker_id: str) -> None:
        if not speaker_id:
            return
        canonical = self.store.resolve_speaker(speaker_id)
        if canonical and canonical not in self._owner_ids:
            self._owner_ids.add(canonical)
            self.contradiction.owner_ids = set(self._owner_ids)

    def is_owner_speaker(self, speaker_id: str) -> bool:
        if not speaker_id:
            return False
        return self.store.resolve_speaker(speaker_id) in self._owner_ids

    def owner_notify_umo(self) -> str:
        umo = str(self.store.get_meta("owner_umo") or "").strip()
        if umo:
            return umo
        return str(self.config.get("notify_umo") or "").strip()

    def clear_runtime_caches(self) -> None:
        self.retriever._cache.clear()

    def apply_config(self) -> None:
        self.config["_skip_style_learning"] = bool(self.coexistence.skip_style)
        self.learning.config = self.config
        self.pipeline.config = self.config
        self.events.config = self.config
        mode = str(self.config.get("retrieval_mode") or "auto")
        cache_ttl = max(0, int(self._cfg_value("cache_ttl_seconds", 20)))
        bm25 = bool(self._cfg_value("retrieval_bm25", True))
        if (
            self.retriever.mode != mode
            or self.retriever.cache_ttl != cache_ttl
            or self.retriever.bm25 != bm25
        ):
            self.retriever._cache.clear()
        self.retriever.mode = mode
        self.retriever.cache_ttl = cache_ttl
        self.retriever.bm25 = bm25
        self._rebuild_owner_ids()
        was_active = self.retriever.embed is not None
        self._sync_embed_fn()
        if was_active != (self.retriever.embed is not None):
            # Embedding 开关切换后，旧缓存里的排序结果不再适用，必须清缓存。
            self.retriever._cache.clear()

    def allowed_platforms(self) -> list[str]:
        raw = self.config.get("memory_source_platforms")
        if raw is None:
            raw = DEFAULT_SOURCE_PLATFORMS
        return sorted({norm_platform(p) for p in parse_csv(str(raw))})

    def event_platform_candidates(self, event: Any) -> set[str]:
        """Best-effort adapter type candidates (never trust a single source)."""
        out: set[str] = set()
        try:
            getter = getattr(event, "get_platform_name", None)
            if callable(getter):
                name = norm_platform(str(getter() or ""))
                if name:
                    out.add(name)
        except Exception:
            pass
        try:
            meta = getattr(event, "platform_meta", None)
            for attr in ("adapter_type", "id", "name"):
                value = norm_platform(str(getattr(meta, attr, "") or ""))
                if value:
                    out.add(value)
        except Exception:
            pass
        try:
            prefix = platform_of(str(event.unified_msg_origin or ""))
            if prefix:
                out.add(prefix)
        except Exception:
            pass
        return out

    def event_platform(self, event: Any) -> str:
        """Preferred display value for the current event's adapter type."""
        candidates = self.event_platform_candidates(event)
        for name in sorted(candidates):
            if name in self.allowed_platforms():
                return name
        for name in sorted(candidates):
            if name in KNOWN_ADAPTER_TYPES:
                return name
        return sorted(candidates)[0] if candidates else ""

    def platform_allowed(self, ident: dict[str, str] | None = None) -> bool:
        ident = ident or {}
        if not ident:
            return True
        allow = set(self.allowed_platforms())
        if not allow:
            return True
        candidates: set[str] = set()
        platform = norm_platform(str(ident.get("platform") or ""))
        if platform:
            candidates.add(platform)
        prefix = platform_of(str(ident.get("window_tag") or ""))
        if prefix:
            candidates.add(prefix)
        if not candidates:
            return True
        if candidates & allow:
            return True
        if candidates & KNOWN_ADAPTER_TYPES:
            return False
        # Custom/unknown instance id: don't silently drop memory.
        return True

    def capture_skip_reason(
        self,
        event: Any = None,
        ident: dict[str, str] | None = None,
        owner_bypass: bool = False,
    ) -> str:
        if not self.enabled():
            return "disabled"
        if not bool(self.config.get("capture_enabled", True)):
            return "capture_off"
        if self.coexistence.skip_capture:
            return "coexistence"
        ident = ident or (self._ident_from_event(event) if event is not None else {})
        is_owner = owner_bypass or bool(ident.get("is_owner"))
        if not is_owner and not self.platform_allowed(ident):
            platform = norm_platform(str(ident.get("platform") or "")) or "unknown"
            return f"platform:{platform}"
        if not self.window_allowed(event, ident):
            return "whitelist"
        return ""

    def _clear_capture_skip(self) -> None:
        if self.store.get_meta("capture_skip"):
            self.store.set_meta("capture_skip", "")

    def _note_capture_skip(self, reason: str, event: Any = None, ident: dict[str, str] | None = None) -> None:
        now = now_ts()
        last = int(self.store.get_meta("capture_skip_at") or "0")
        if now - last < 60:
            return
        self.store.set_meta("capture_skip_at", str(now))
        ident = ident or {}
        payload = {
            "reason": reason,
            "platform": norm_platform(str(ident.get("platform") or "")),
            "window": str(ident.get("window_tag") or ""),
            "allowed": self.allowed_platforms(),
        }
        self.store.set_meta("capture_skip", json.dumps(payload, ensure_ascii=False))
        self.store.add_diag("capture_skip", payload)
        if self.logger:
            self.logger.info("Savage Type capture skipped: %s", payload)

    def last_capture_skip(self) -> dict[str, Any] | None:
        raw = self.store.get_meta("capture_skip")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def whitelist_ids(self) -> list[str]:
        raw = str(self.config.get("memory_whitelist") or "")
        return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

    def window_allowed(self, event: Any = None, ident: dict[str, str] | None = None) -> bool:
        allow = self.whitelist_ids()
        if not allow:
            return True
        ident = ident or (self._ident_from_event(event) if event is not None else {})
        window = str((ident or {}).get("window_tag") or "")
        speaker = str((ident or {}).get("speaker_id") or "")
        group = ""
        if event is not None:
            try:
                group = str(event.get_group_id() or "")
            except Exception:
                group = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
        hay = " ".join([window, speaker, group])
        return any(item and item in hay for item in allow)

    def capture_ok(self, event: Any = None, ident: dict[str, str] | None = None) -> bool:
        return not self.capture_skip_reason(event, ident)

    def inject_ok(self, event: Any = None) -> bool:
        return (
            self.enabled()
            and bool(self.config.get("inject_enabled", True))
            and not self.coexistence.skip_inject
            and self.window_allowed(event)
        )

    def refresh_coexistence(self, stars: list[Any]) -> None:
        self.coexistence.refresh(stars)
        self.apply_config()

    def _ident_from_event(self, event: Any) -> dict[str, str]:
        try:
            cached = event.get_extra("_stype_ident")
            if cached:
                return cached
        except Exception:
            pass
        return self.identity_from_event(event)

    def identity_from_event(self, event: Any, persona_id: str = "") -> dict[str, str]:
        speaker_id = ""
        speaker_name = ""
        bot_id = ""
        window_tag = ""
        try:
            speaker_id = str(event.get_sender_id() or "")
        except Exception:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            speaker_id = str(getattr(sender, "user_id", "") or "")
        try:
            speaker_name = str(event.get_sender_name() or "")
        except Exception:
            speaker_name = speaker_id
        try:
            bot_id = str(event.message_obj.self_id or "")
        except Exception:
            bot_id = ""
        try:
            window_tag = str(event.unified_msg_origin or "")
        except Exception:
            window_tag = ""
        raw_id = speaker_id or "unknown"
        platform = self.event_platform(event) or platform_of(window_tag)
        canonical = self.store.resolve_speaker(raw_id)
        if platform == "webchat":
            owner = self.owner_qq()
            if owner:
                owner_canonical = self.store.resolve_speaker(owner)
                if canonical != owner_canonical:
                    # ChatUI 的主人是同一个主人：合并到主人 QQ 身份。
                    # 没有主人档案时不要用 ChatUI 的临时显示名去覆盖称呼，等 QQ 侧真实昵称。
                    owner_profile = self.store.get_profile(owner_canonical)
                    owner_name = owner_profile.speaker_name if owner_profile else ""
                    self.store.reassign_speaker(canonical or raw_id, owner_canonical, owner_name)
                    canonical = owner_canonical
                    speaker_name = owner_name
        if raw_id != canonical:
            self.store.set_alias(raw_id, canonical, speaker_name or "")
        return {
            "speaker_id": canonical,
            "speaker_name": speaker_name or canonical,
            "bot_id": bot_id,
            "window_tag": window_tag,
            "platform": platform,
            "persona_id": persona_id or "",
            "is_owner": self.is_owner_event(event),
        }

    def is_admin_event(self, event: Any) -> bool:
        if event is None:
            return False
        try:
            if str(getattr(event, "role", "") or "").lower() == "admin":
                return True
        except Exception:
            pass
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:
            pass
        try:
            if bool(event.get_extra("_stype_admin")):
                return True
        except Exception:
            pass
        return False

    def is_self_directive(self, text: str) -> bool:
        from .util import DIRECTIVE_RE, FIRST_PERSON_RE, REMEMBER_RE

        t = (text or "").strip()
        if not t:
            return False
        if REMEMBER_RE.search(t) and FIRST_PERSON_RE.search(t):
            return True
        return bool(DIRECTIVE_RE.search(t) and FIRST_PERSON_RE.search(t))

    def is_owner_event(self, event: Any) -> bool:
        if event is None:
            return False
        try:
            if self.event_platform(event) == "webchat":
                # ChatUI 只有管理员能进，视为主人本人。
                return True
        except Exception:
            pass
        owner = self.owner_qq()
        if owner:
            try:
                sender = self.store.resolve_speaker(str(event.get_sender_id() or ""))
            except Exception:
                sender = ""
            return bool(sender) and sender == self.store.resolve_speaker(owner)
        return self.is_admin_event(event)

    def remember_owner_window(self, event: Any) -> None:
        """Track the owner's private session for pending-memory notifications."""
        if not self.is_owner_event(event):
            return
        umo = ""
        try:
            umo = str(event.unified_msg_origin or "")
        except Exception:
            return
        if not umo:
            return
        try:
            if self.event_platform(event) == "webchat":
                return
        except Exception:
            pass
        try:
            if event.get_group_id():
                return
        except Exception:
            pass
        self.store.set_meta("owner_umo", umo)

    def raw_message_text(self, event: Any) -> str:
        """Original text from the message chain (keeps the leading command slash)."""
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            parts = []
            for comp in chain:
                raw = getattr(comp, "text", None)
                if raw:
                    parts.append(str(raw))
            return "".join(parts)
        except Exception:
            return ""

    def is_command_text(self, text: str, event: Any = None) -> bool:
        candidates = [(text or "").strip()]
        raw = self.raw_message_text(event).strip() if event is not None else ""
        if raw:
            candidates.append(raw)
        for item in candidates:
            if not item:
                continue
            if COMMAND_SPLIT_RE.match(item):
                return True
            low = item.lower()
            if low.startswith("stype") or low.startswith("savagetype_"):
                return True
        return False

    def capture_user(self, event: Any, text: str) -> int | None:
        ident = self._ident_from_event(event)
        skip = self.capture_skip_reason(event, ident)
        if skip:
            self._note_capture_skip(skip, event, ident)
            return None
        text = (text or "").strip()
        if not text or self.is_command_text(text, event):
            return None
        is_owner = self.is_owner_event(event)
        if is_owner:
            self.mark_owner_speaker(ident["speaker_id"])
            # 主人身份与人物档案分开：只同步称呼到事实，不建/不更档案。
            self.store.sync_speaker_name(ident["speaker_id"], ident.get("speaker_name", ""))
        elif self.platform_allowed(ident):
            # 档案只建在 QQ 侧；ChatUI（webchat）只进主人记忆，不建人物档案。
            self.store.upsert_profile(
                ident["speaker_id"],
                ident.get("speaker_name", ""),
                ident.get("platform", ""),
                is_owner=False,
            )
        if not text.startswith("[图片]") and self.learning.jargon_scope_ok(is_owner):
            self.learning.observe_message(text, persona_id=ident.get("persona_id") or "")
        ts = now_ts()
        event_id = self.store.add_timeline(
            {
                "ts": ts,
                "role": ROLE_USER,
                "content": clip(text, 2000),
                "fingerprint": fingerprint(ident.get("persona_id"), ident["speaker_id"], ROLE_USER, ts, text),
                "addressee": self.addressee_from_event(event),
                **ident,
            }
        )
        if event_id:
            self._clear_capture_skip()
        return event_id

    def addressee_from_event(self, event: Any) -> str:
        """解析当前消息的收件人（@ 谁 / 回复谁），失败返回空串。"""
        try:
            components = getattr(getattr(event, "message_obj", None), "message", None) or []
            return addressee_from_components(components)
        except Exception:  # noqa: BLE001
            return ""

    def note_reply_target(self, event: Any, ident: dict[str, str]) -> None:
        """记录「Bot 最近一次明确回复的人」，供空 @ 提示使用。"""
        window = str(ident.get("window_tag") or "")
        if not window:
            return
        try:
            payload = {
                "id": str(ident.get("speaker_id") or ""),
                "name": str(ident.get("speaker_name") or ""),
                "ts": now_ts(),
            }
            self.store.set_meta(f"reply_target:{window}", json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    def last_reply_target(self, window_tag: str) -> dict[str, Any]:
        window = str(window_tag or "")
        if not window:
            return {}
        raw = self.store.get_meta(f"reply_target:{window}")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def blank_mention_hint(self, event: Any, ident: dict[str, str]) -> str:
        """空 @（只有 @、没有正文）时给的上下文提醒。"""
        if not bool(self._cfg_value("blank_mention_hint_enabled", True)):
            return ""
        try:
            window = str(ident.get("window_tag") or "")
            if window_kind(window) != "group":
                return ""
            components = getattr(getattr(event, "message_obj", None), "message", None) or []
            raw_addressee = addressee_from_components(components)
            self_id = ""
            try:
                self_id = str(event.message_obj.self_id or "")
            except Exception:  # noqa: BLE001
                self_id = ""
            if not has_bot_mention(raw_addressee, self_id):
                return ""
            text = str(getattr(event, "message_str", "") or "").strip()
            if len(text) > 1:
                return ""
            target = self.last_reply_target(window)
            if not target:
                return ""
            speaker = str(getattr(event, "message_str", "") or "")
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                sender_id = ""
            ttl_minutes = max(1, int(self._cfg_value("blank_mention_hint_ttl_minutes", 30)))
            age = now_ts() - int(target.get("ts") or 0)
            if age > ttl_minutes * 60:
                return ""
            same_user = bool(sender_id) and str(target.get("id") or "") == sender_id
            name = str(target.get("name") or target.get("id") or "对方")
            gap = max(0, int(self._cfg_value("blank_mention_hint_gap_messages", 12)))
            recent = self.store.query(
                "SELECT COUNT(*) FROM timeline WHERE window_tag=? AND ts>?",
                (window, int(target.get("ts") or 0)),
            )
            since = int(recent[0][0] or 0) if recent else 0
            lines = ["【单独 @ 提醒】这条消息只有 @，没有正文。"]
            if same_user and age <= 600 and since <= gap:
                lines.append(
                    f"上次明确和你对话的人就是 ta（{age} 秒前、之后隔了 {since} 条消息），"
                    "很可能想接着刚才的话题：参考最近上下文自然续；"
                )
            else:
                lines.append(
                    f"你上次明确回复的人是 {name}（{age} 秒前、之后隔了 {since} 条消息），"
                    "但这次 @ 你的不一定还是 ta；"
                )
            lines.append("拿不准时别强行续话，自然回一句「怎么了」「？」之类即可。")
            block = "\n".join(lines)
            self.store.add_diag("blank_mention", {"window": window, "same_user": same_user, "age": age})
            return block
        except Exception as exc:  # noqa: BLE001
            self.store.add_diag("blank_mention_fail", {"error": str(exc)[:160]})
            return ""

    def capture_bot(self, event: Any, text: str) -> int | None:
        ident = self._ident_from_event(event)
        skip = self.capture_skip_reason(event, ident)
        if skip:
            self._note_capture_skip(skip, event, ident)
            return None
        text = (text or "").strip()
        if not text:
            return None
        ts = now_ts()
        event_id = self.store.add_timeline(
            {
                "ts": ts,
                "role": ROLE_ASSISTANT,
                "content": clip(text, 2000),
                "fingerprint": fingerprint(
                    ident.get("persona_id"),
                    ident.get("bot_id") or ROLE_BOT_ID,
                    ROLE_ASSISTANT,
                    ts,
                    text,
                ),
                "speaker_id": ROLE_BOT_ID,
                "speaker_name": "bot",
                "bot_id": ident.get("bot_id") or "",
                "window_tag": ident.get("window_tag") or "",
                "persona_id": ident.get("persona_id") or "",
            }
        )
        if event_id:
            self._clear_capture_skip()
        return event_id

    def _cfg_value(self, key: str, default: Any) -> Any:
        raw = self.config.get(key)
        return default if raw is None else raw

    def idle_pending(self) -> bool:
        """True when the newest unsummarized message has been silent long enough."""
        idle = int(self._cfg_value("extract_idle_seconds", 300))
        if idle <= 0:
            return False
        rows = self.store.query("SELECT MAX(ts) AS ts FROM timeline WHERE summarized=0")
        newest = int(rows[0]["ts"] or 0) if rows else 0
        return bool(newest) and now_ts() - newest >= idle

    async def maybe_extract(self, force: bool = False) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": True, "skipped": True, "reason": "disabled"}
        extract_enabled = bool(self.config.get("extract_enabled", True))
        event_enabled = self.events.enabled()
        if not extract_enabled and not event_enabled:
            return {"ok": True, "skipped": True, "reason": "extract disabled"}
        now = now_ts()
        cooldown = max(0, int(self._cfg_value("extract_cooldown_seconds", 45)))
        if not force and now < self._extract_fail_until:
            return {"ok": True, "skipped": True, "reason": "cooldown_after_fail"}
        if not force and self._last_extract_at and now - self._last_extract_at < cooldown:
            return {"ok": True, "skipped": True, "reason": "debounce"}
        if self._extract_lock.locked():
            return {"ok": True, "skipped": True, "reason": "busy"}
        idle = not force and self.idle_pending()
        fail_cd = max(0, int(self._cfg_value("extract_fail_cooldown_seconds", 180)))
        async with self._extract_lock:
            if extract_enabled:
                try:
                    result = await self.pipeline.run(force=force or idle)
                except LLMBudgetExceeded as exc:
                    # 预算闸：不算失败、不冷却，等额度恢复后重跑同一批。
                    self.store.add_diag("llm_budget", {"task": "normalize", "reason": exc.reason})
                    result = {"ok": True, "skipped": True, "reason": "budget"}
                except Exception as exc:  # noqa: BLE001
                    self._extract_fail_until = now_ts() + fail_cd
                    self.store.add_usage("extract", ok=False, detail=str(exc)[:200])
                    self.store.add_diag("extract_fail", {"error": str(exc)})
                    result = {"ok": False, "skipped": True, "reason": "extract_fail", "error": str(exc)}
            else:
                result = {"ok": True, "skipped": True, "reason": "extract disabled"}
            if idle:
                result["idle"] = True
            if not result.get("skipped"):
                self.store.add_usage("extract", ok=True, detail=str(result.get("events") or 0))
                if result.get("pending"):
                    await self.notify_pending()
            # 事件层独立失败冷却：事实管线挂了不拖累事件（事件有确定性兜底）。
            if event_enabled and (force or now_ts() >= self._events_fail_until):
                try:
                    events_result = await self.events.run(force=force or idle)
                    result["events_layer"] = events_result
                except LLMBudgetExceeded as exc:
                    self.store.add_diag("llm_budget", {"task": "event", "reason": exc.reason})
                    result["events_layer"] = {"ok": True, "skipped": True, "reason": "budget"}
                except Exception as exc:  # noqa: BLE001
                    self._events_fail_until = now_ts() + fail_cd
                    self.store.add_diag("events_fail", {"error": str(exc)[:200]})
                    result["events_layer"] = {
                        "ok": False, "skipped": True, "reason": "events_fail", "error": str(exc),
                    }
            self._last_extract_at = now_ts()
            return result

    async def notify_pending(self) -> bool:
        items = self.store.pending_memory_unqueued(limit=10)
        if not items:
            return False
        umo = self.owner_notify_umo()
        if not umo or self.send_message is None:
            self.store.add_diag("notify_skip", {"reason": "no_owner_window", "count": len(items)})
            return False
        now = now_ts()
        cooldown = max(0, int(self._cfg_value("pipeline_notify_cooldown_seconds", 300)))
        last = int(self.store.get_meta("notify_last_at") or "0")
        if last and cooldown and now - last < cooldown:
            return False
        lines = ["【待审记忆】整理结果没通过审核，需要你确认："]
        for item in items:
            plain = item.plain or clip(item.raw_text, 60)
            lines.append(f"#{item.id} {clip(plain, 80)}（{item.speaker_name or item.speaker_id}）")
        lines.append("回复 是 <编号> 通过，否 <编号> 删除。")
        ok = await self.send_text(umo, "\n".join(lines))
        if ok:
            for item in items:
                self.store.update_memory_review(item.id, notified_at=now)
            self.store.set_meta("notify_last_at", str(now))
        return ok

    async def send_text(self, umo: str, text: str) -> bool:
        if not umo or self.send_message is None:
            return False
        try:
            result = self.send_message(umo, text)
            if asyncio.iscoroutine(result):
                await result
            return True
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type notify failed: %s", exc)
            self.store.add_diag("notify_fail", {"error": str(exc)[:200]})
            return False

    BARE_REPLY_WORDS = {
        "是", "否", "通过", "批准", "过审", "同意", "驳回", "拒绝", "不过",
        "yes", "no", "y", "n", "ok",
    }

    async def handle_owner_reply(self, text: str) -> str | None:
        t = (text or "").strip()
        if not t or len(t) > 24:
            return None
        yes = re.match(r"^(是|通过|批准|过审|同意|yes|y|ok)[\s#:：]*(\d+)?$", t, re.I)
        no = re.match(r"^(否|驳回|拒绝|删除|不过|no|n)[\s#:：]*(\d+)?$", t, re.I)
        match = yes or no
        if match is None:
            return None
        if not match.group(2) and t.lower() not in self.BARE_REPLY_WORDS:
            # 不带编号时必须是很短的明确答复，避免正常聊天里的「删除」误触。
            return None
        approved = yes is not None
        items = self.store.list_memory_reviews("pending", limit=50)
        if not items:
            return "没有待审记忆。"
        review_id = int(match.group(2)) if match.group(2) else 0
        if review_id:
            item = next((x for x in items if x.id == review_id), None)
            if item is None:
                return f"没有找到待审 #{review_id}。"
        elif len(items) == 1:
            item = items[0]
        else:
            ids = "、".join(f"#{x.id}" for x in items[:10])
            return f"待审不止一条，请回复 是/否 + 编号：{ids}"
        return self.resolve_memory_review(item.id, approved)

    def resolve_memory_review(self, review_id: int, approved: bool) -> str:
        item = self.store.get_memory_review(review_id)
        if item is None or item.status != "pending":
            return f"#{review_id} 不在待审列表。"
        if not approved:
            self.store.delete_memory_review(review_id)
            self.store.add_diag(
                "memory_rejected",
                {"id": review_id, "plain": item.plain, "by": "owner"},
            )
            return f"已删除 #{review_id}。"
        payload = dict(item.payload or {})
        if not payload:
            payload = {
                "subject": "self",
                "attribute": "note",
                "value": item.plain or clip(item.raw_text, 80),
                "content": item.raw_text,
            }
        payload["speaker_id"] = item.speaker_id
        payload["speaker_name"] = item.speaker_name or item.speaker_id
        payload["window_tag"] = item.window_tag or ""
        payload["persona_id"] = payload.get("persona_id") or ""
        payload["plain"] = item.plain or payload.get("plain") or ""
        payload["keywords"] = item.keywords or payload.get("keywords") or []
        payload["source_event_id"] = item.source_event_id or payload.get("source_event_id") or 0
        payload["scope"] = item.scope or payload.get("scope") or SCOPE_PERSON
        payload["origin"] = payload.get("origin") or "manual"
        payload["review_status"] = REVIEW_MANUAL
        result = self.contradiction.ingest(payload, source_text=item.raw_text or payload.get("content", ""))
        action = str(result.get("action") or "")
        self.store.update_memory_review(review_id, status="approved", payload=payload)
        self.store.add_diag("memory_approved", {"id": review_id, "action": action, "by": "owner"})
        if action == "pending":
            return f"已通过 #{review_id}。和高证据旧记忆冲突，已进「待确认覆盖」。"
        if action == "rejected_relation":
            return f"已通过 #{review_id}。但被关系守卫拒收，没有写入。"
        if action in {"ignored", "ignored_joke"}:
            return f"已通过 #{review_id}。但这句按整理结果被忽略，没有写入。"
        return f"已通过 #{review_id}。"

    def schedule_learn(self) -> None:
        if self._learn_task and not self._learn_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._learn_task = loop.create_task(self._background_learn())

    def auto_housekeeping(self) -> None:
        """Cheap periodic cleanup (empty profiles), at most once per 6 hours."""
        now = now_ts()
        last = int(self.store.get_meta("housekeeping_last_at") or "0")
        if last and now - last < 6 * 3600:
            return
        deleted = self.store.delete_empty_profiles(
            ttl_days=int(self._cfg_value("empty_profile_ttl_days", 7))
        )
        folded = fold_preference_slots(self.store)
        self.store.set_meta("housekeeping_last_at", str(now))
        if deleted or folded:
            self.store.add_diag(
                "housekeeping",
                {"deleted_empty_profiles": deleted, "folded_preferences": folded},
            )

    async def _background_learn(self) -> None:
        try:
            await self.maybe_extract()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type extract failed: %s", exc)
        try:
            self.auto_housekeeping()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type housekeeping failed: %s", exc)
        try:
            await self.run_learning()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type style learn failed: %s", exc)
        try:
            await self.fill_embeddings()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("Savage Type embed failed: %s", exc)

    async def run_learning(self, force: bool = False, persona_id: str = "") -> dict[str, Any]:
        text = ""
        if self.get_persona_text is not None:
            try:
                maybe = self.get_persona_text(persona_id)
                text = await maybe if asyncio.iscoroutine(maybe) else (maybe or "")
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("Savage Type persona text failed: %s", exc)
                text = ""
        return await self.learning.maybe_learn(force=force, persona_text=text or "")

    def importance_cfg(self) -> dict[str, float]:
        return {
            "weight": float(self._cfg_value("importance_weight", 0.25)),
            "half_life_days": float(self._cfg_value("importance_half_life_days", 30)),
            "reinforce_factor": float(self._cfg_value("importance_reinforce_factor", 0.5)),
            "max_multiplier": float(self._cfg_value("importance_max_half_life_multiplier", 3)),
        }

    def _recent_ids(self, window_tag: str) -> set[int]:
        if not window_tag:
            return set()
        window = max(0, int(self._cfg_value("inject_dedup_window_seconds", 600)))
        if window <= 0:
            return set()
        return self.store.recent_recall_ids(window_tag, now_ts() - window)

    def _remember_injected(self, window_tag: str, fact_ids: list[int]) -> None:
        if not window_tag or not fact_ids:
            return
        self.store.add_recall(window_tag, [int(fid) for fid in fact_ids], now_ts())

    def _recent_event_ids(self, window_tag: str) -> set[int]:
        if not window_tag:
            return set()
        window = max(0, int(self._cfg_value("inject_dedup_window_seconds", 600)))
        if window <= 0:
            return set()
        return self.store.recent_event_recall_ids(window_tag, now_ts() - window)

    def _remember_injected_events(self, window_tag: str, event_ids: list[int]) -> None:
        if not window_tag or not event_ids:
            return
        self.store.add_event_recall(window_tag, [int(eid) for eid in event_ids], now_ts())

    def session_isolation_mode(self) -> str:
        from .util import session_isolation

        return session_isolation(str(self._cfg_value("memory_session_isolation", "strict")))

    def entity_boost_weight(self) -> float:
        if not bool(self._cfg_value("entity_linking_enabled", True)):
            return 0.0
        try:
            return max(0.0, min(1.0, float(self._cfg_value("entity_boost_weight", 0.2))))
        except (TypeError, ValueError):
            return 0.2

    def history_limit(self) -> int:
        if not bool(self._cfg_value("history_enabled", True)):
            return 0
        try:
            return max(0, int(self._cfg_value("history_max_facts", 6)))
        except (TypeError, ValueError):
            return 6

    def _retrieval_ctx(self, query: str, speaker_id: str, persona_id: str = "") -> tuple[str, list[str], str | None]:
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        ask_other = None
        id_set = set(ids)
        for row in self.store.distinct_live_speakers(persona_id=persona_id, limit=80):
            sid = str(row["speaker_id"] or "")
            if not sid or sid in id_set or sid == ROLE_BOT_ID:
                continue
            name = str(row["speaker_name"] or "").strip()
            if (name and len(name) >= 2 and name in query) or (len(sid) >= 4 and sid in query):
                ask_other = sid
                break
        return canonical, ids, ask_other

    async def retrieve_for(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        skip_ids: set[int] | None = None,
        skip_query_mentions: bool = False,
        window_tag: str = "",
        event_skip_ids: set[int] | None = None,
        event_limit: int = 0,
    ) -> Any:
        self._sync_embed_fn()
        canonical, ids, ask_other = self._retrieval_ctx(query, speaker_id, persona_id)
        return await self.retriever.retrieve(
            query=query,
            speaker_id=canonical,
            top_k=int(self.config.get("top_k") or 16),
            related_limit=int(self.config.get("related_fact_limit") or 6),
            core_limit=int(self.config.get("core_fact_limit") or 4),
            ask_other_id=ask_other,
            persona_id=persona_id,
            speaker_ids=ids,
            skip_ids=skip_ids,
            importance_cfg=self.importance_cfg(),
            skip_query_mentions=skip_query_mentions,
            window_tag=window_tag,
            isolation=self.session_isolation_mode(),
            owner_ids=set(self._owner_ids),
            event_skip_ids=event_skip_ids,
            event_limit=max(1, int(event_limit or self.config.get("event_max_inject") or 2)),
            entity_weight=self.entity_boost_weight(),
            history_limit=self.history_limit(),
        )

    async def warm_retrieval(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        window_tag: str = "",
    ) -> None:
        """会话锁等待期间预热检索缓存，让检索和排队时间重叠。"""
        if self.retriever.cache_ttl <= 0:
            return
        self._sync_embed_fn()
        canonical, ids, ask_other = self._retrieval_ctx(query, speaker_id, persona_id)
        await self.retriever.warm(
            query=query,
            speaker_id=canonical,
            top_k=int(self.config.get("top_k") or 16),
            related_limit=int(self.config.get("related_fact_limit") or 6),
            core_limit=int(self.config.get("core_fact_limit") or 4),
            ask_other_id=ask_other,
            persona_id=persona_id,
            speaker_ids=ids,
            importance_cfg=self.importance_cfg(),
            window_tag=window_tag,
            isolation=self.session_isolation_mode(),
            owner_ids=set(self._owner_ids),
            entity_weight=self.entity_boost_weight(),
            history_limit=self.history_limit(),
            event_limit=max(1, int(self.config.get("event_max_inject") or 2)),
        )

    def dossier_for(
        self,
        speaker_id: str,
        persona_id: str = "",
        window_tag: str = "",
        query: str = "",
        isolation: str = "off",
    ) -> dict[str, Any]:
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        facts = self.store.live_by_speaker(canonical, persona_id=persona_id, speaker_ids=ids, limit=40)
        if isolation != "off" and window_tag:
            # 档案卡也要过隐私：私聊来源的条目不能借档案卡漏进群聊。
            kept = []
            for fact in facts:
                reason = self.retriever._visibility(  # noqa: SLF001
                    fact, canonical, query, None, "long_term", ids, persona_id,
                    window_tag, isolation, set(self._owner_ids),
                )
                if not reason:
                    kept.append(fact)
            facts = kept
        name = ""
        if facts:
            name = facts[0].speaker_name or ""
        return build_profile(canonical, facts, speaker_name=name, speaker_ids=ids)

    def list_dossiers(self, persona_id: str = "") -> list[dict[str, Any]]:
        out = []
        for row in self.store.distinct_live_speakers(persona_id=persona_id, limit=80):
            card = self.dossier_for(row["speaker_id"], persona_id=persona_id)
            if card.get("lines"):
                out.append(card)
        return out

    def profile_card_for(
        self,
        speaker_id: str,
        persona_id: str = "",
        window_tag: str = "",
        isolation: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """跨会话画像卡（A 层）：同一个人在任何会话里的称呼/身份/偏好/语气锚点。

        window_tag + isolation 传入时按会话隔离过滤来源（strict 下私聊事实不进群聊）；
        不传则表示面板 / 命令查看跨会话全量画像。
        """
        if not bool(self._cfg_value("profile_inject_enabled", True)):
            return "", {"enabled": False, "chars": 0}
        try:
            max_chars = max(0, int(self._cfg_value("profile_max_chars", 300)))
        except (TypeError, ValueError):
            max_chars = 300
        card, meta = build_profile_card(
            self.store,
            speaker_id,
            persona_id=persona_id,
            max_chars=max_chars,
            window_tag=window_tag,
            isolation=isolation,
        )
        meta["enabled"] = True
        return card, meta

    def cross_window_for(
        self,
        speaker_id: str,
        window_tag: str = "",
        persona_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """跨窗口衔接（B 层）：默认只允许 私聊→私聊、群聊→私聊。"""
        if not bool(self._cfg_value("cross_window_enabled", True)):
            return "", {"enabled": False, "items": 0, "chars": 0}
        try:
            minutes = max(1, int(self._cfg_value("cross_window_minutes", 30)))
            max_items = max(1, int(self._cfg_value("cross_window_max_items", 6)))
            max_chars = max(0, int(self._cfg_value("cross_window_max_chars", 320)))
        except (TypeError, ValueError):
            minutes, max_items, max_chars = 30, 6, 320
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        block, meta = build_cross_window(
            self.store,
            ids,
            window_tag,
            minutes=minutes,
            max_items=max_items,
            max_chars=max_chars,
            persona_id=persona_id,
            private_to_group=bool(self._cfg_value("cross_window_private_to_group", False)),
            group_to_group=bool(self._cfg_value("cross_window_group_to_group", False)),
        )
        meta["enabled"] = True
        return block, meta

    def window_flow_for(
        self,
        query: str,
        window_tag: str = "",
        persona_id: str = "",
        owner_ids: set[str] | None = None,
        force: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """窗口全流上下文（C 层）：其他窗口最近的完整消息流。

        默认双向全通（私聊 <-> 群聊），但可用 window_flow_exclude_private_users
        屏蔽指定用户的私聊窗口，避免他人私聊内容流入群聊。
        force=True 时忽略关键词门槛（预览命令用）。
        """
        if not bool(self._cfg_value("window_flow_enabled", True)):
            return "", {"enabled": False, "items": 0, "chars": 0}
        if not force and not bool(self._cfg_value("window_flow_always", False)):
            keywords = parse_csv(str(self._cfg_value("window_flow_keywords", "") or ""))
            text = query or ""
            if keywords and not any(keyword and keyword in text for keyword in keywords):
                return "", {"enabled": True, "skipped": "no_keyword", "items": 0, "chars": 0}
        try:
            hours = max(1, int(self._cfg_value("window_flow_hours", 24)))
            max_items = max(1, int(self._cfg_value("window_flow_max_items", 150)))
            max_chars = max(0, int(self._cfg_value("window_flow_max_chars", 6000)))
            max_windows = max(1, int(self._cfg_value("window_flow_max_windows", 3)))
            msg_chars = max(20, int(self._cfg_value("window_flow_msg_chars", 200)))
        except (TypeError, ValueError):
            hours, max_items, max_chars, max_windows, msg_chars = 24, 150, 6000, 3, 200
        exclude_private_users = parse_csv(
            str(self._cfg_value("window_flow_exclude_private_users", "") or "")
        )
        exclude_ids: list[str] = []
        for item in exclude_private_users:
            try:
                exclude_ids.extend(self.store.speaker_ids_for(self.store.resolve_speaker(item)))
            except Exception:  # noqa: BLE001
                exclude_ids.append(item)
        block, meta = build_window_flow(
            self.store,
            window_tag,
            hours=hours,
            max_items=max_items,
            max_chars=max_chars,
            max_windows=max_windows,
            msg_chars=msg_chars,
            include_bot=bool(self._cfg_value("window_flow_include_bot", True)),
            group_to_private=bool(self._cfg_value("window_flow_group_to_private", True)),
            private_to_group=bool(self._cfg_value("window_flow_private_to_group", True)),
            exclude_private_users=exclude_ids,
            persona_id=persona_id,
        )
        meta["enabled"] = True
        if meta.get("items"):
            self.store.add_diag("window_flow", meta)
        return block, meta

    def reply_gate_enabled(self) -> bool:
        return bool(self._cfg_value("reply_gate_enabled", False))

    # ---- 指派发言（私聊让 Bot 去群里说话） ---------------------------------

    def speak_enabled(self) -> bool:
        return bool(self._cfg_value("speak_enabled", False))

    def speak_groups(self, limit: int = 30, days: int = 30) -> list[dict[str, Any]]:
        """按最近活跃列出已知群窗口。"""
        since = now_ts() - max(1, int(days)) * 86400
        rows = self.store.recent_windows(limit=max(1, int(limit)) * 3, since_ts=since)
        groups = [row for row in rows if window_kind(str(row.get("window_tag") or "")) == "group"]
        return groups[: max(1, int(limit))]

    def speak_default_umo(self) -> str:
        """默认群：优先 /stype default 存在 meta 的覆盖值，其次配置 speak_default_group。"""
        override = str(self.store.get_meta("speak_default_umo") or "").strip()
        if override:
            return override
        raw = str(self._cfg_value("speak_default_group", "") or "").strip()
        if not raw:
            return ""
        if ":" in raw:
            return raw
        known = [str(row["window_tag"]) for row in self.speak_groups(limit=80, days=365)]
        return resolve_number(raw, known)

    def set_speak_default(self, value: str) -> str:
        """设置 / 清除默认群，返回给人看的回执。"""
        raw = (value or "").strip()
        if not raw or raw in {"clear", "清除", "重置"}:
            self.store.set_meta("speak_default_umo", "")
            return "默认群已清除。"
        groups = [str(row["window_tag"]) for row in self.speak_groups(limit=80, days=365)]
        target = ""
        if ":" in raw:
            target = raw
        elif raw.isdigit() and len(raw) <= 2:
            from .speak import resolve_index

            target = resolve_index(raw, groups)
        if not target:
            target = resolve_number(raw, groups)
        if not target:
            return f"没找到群「{raw}」。用 /stype groups 查看群号或序号。"
        self.store.set_meta("speak_default_umo", target)
        self.store.add_diag("speak_default", {"window": target})
        return f"默认群已设为 {group_label(target)}。"

    async def handle_speak_request(self, event: Any, text: str) -> str | None:
        """私聊指派发言：命中则发送并返回回执文本；未命中返回 None。"""
        if not self.speak_enabled():
            return None
        intent = parse_intent(text or "")
        if not intent:
            return None
        window_tag = str(getattr(event, "unified_msg_origin", "") or "")
        if window_kind(window_tag) != "private":
            return None
        if bool(self._cfg_value("speak_require_owner", True)) and not self.is_owner_event(event):
            return "（只有主人能让我去群里说话）"
        groups = [str(row["window_tag"]) for row in self.speak_groups(limit=80, days=365)]
        default_umo = self.speak_default_umo()
        target, reason = resolve_target(intent, default_umo=default_umo, groups=groups)
        if not target:
            tips = {
                "no_default_group": "还没设置默认群：用 /stype groups 看群号，再 /stype default <群号> 设置。",
                "index_out_of_range": "群序号超出范围，用 /stype groups 看看有哪些群。",
                "group_not_found": "没找到这个群号，用 /stype groups 核对一下。",
                "unknown_target": "没认出目标群。",
            }
            return tips.get(reason, "没认出目标群。")
        allow = parse_targets(self._cfg_value("speak_groups", ""))
        if not allowed_target(target, default_umo=default_umo, allow=allow):
            return f"群 {group_label(target)} 不在允许名单里（speak_groups）。"
        try:
            limit = max(0, int(self._cfg_value("speak_rate_limit_per_min", 5)))
        except (TypeError, ValueError):
            limit = 5
        try:
            owner_id = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            owner_id = ""
        minute_key = f"speak_rate:{owner_id}:{datetime.now().strftime('%Y%m%d%H%M')}"
        try:
            used = int(self.store.get_meta(minute_key) or 0)
        except (TypeError, ValueError):
            used = 0
        if limit and used >= limit:
            return f"太快了，每分钟最多 {limit} 条，等一分钟再发。"
        try:
            max_chars = max(1, int(self._cfg_value("speak_max_chars", 300)))
        except (TypeError, ValueError):
            max_chars = 300
        content = clip_content(str(intent.get("content") or ""), max_chars)
        if not content:
            return "内容为空，没发。"
        ok = await self.send_text(target, content)
        if not ok:
            return "发送失败（目标群可能不支持主动消息，或平台未连接）。"
        self.store.set_meta(minute_key, str(used + 1))
        if bool(self._cfg_value("speak_record_to_target", True)):
            try:
                ts = now_ts()
                self.store.add_timeline(
                    {
                        "ts": ts,
                        "role": ROLE_ASSISTANT,
                        "content": content,
                        "fingerprint": fingerprint("", ROLE_BOT_ID, ROLE_ASSISTANT, ts, content),
                        "speaker_id": ROLE_BOT_ID,
                        "speaker_name": "bot",
                        "bot_id": "",
                        "window_tag": target,
                        "persona_id": "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.store.add_diag("speak_record_fail", {"error": str(exc)[:120]})
        self.store.add_diag("speak", {"window": target, "chars": len(content)})
        if bool(self._cfg_value("speak_reply_receipt", True)):
            return f"已发到群 {group_label(target)}：{content}"
        return "已发送。"

    def _reply_gate_counter_key(self, kind: str, window_tag: str) -> str:
        day = datetime.now().strftime("%Y%m%d")
        return f"reply_gate_{kind}:{day}:{window_tag}"

    async def reply_gate_for(
        self,
        event: Any,
        *,
        handled: bool = False,
        persona_id: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """免@主动接话判定：命中时由主插件把事件标为唤醒，走默认 LLM 通路。"""
        meta: dict[str, Any] = {"enabled": self.reply_gate_enabled()}
        if not meta["enabled"]:
            return False, meta
        try:
            window_tag = str(getattr(event, "unified_msg_origin", "") or "")
            text = str(getattr(event, "message_str", "") or "")
            try:
                is_self = str(event.get_self_id()) == str(event.get_sender_id())
            except Exception:  # noqa: BLE001
                is_self = False
            mode = normalize_mode(str(self._cfg_value("reply_gate_mode", "probability")))
            try:
                min_chars = max(1, int(self._cfg_value("reply_gate_min_chars", 2)))
                cooldown = max(0, int(self._cfg_value("reply_gate_cooldown_seconds", 90)))
                daily_limit = max(0, int(self._cfg_value("reply_gate_daily_limit", 30)))
            except (TypeError, ValueError):
                min_chars, cooldown, daily_limit = 2, 90, 30
            targets = parse_targets(self._cfg_value("reply_gate_groups", ""))
            now = now_ts()
            last_ts = 0
            today_count = 0
            if window_tag:
                try:
                    last_ts = int(self.store.get_meta(f"reply_gate_last:{window_tag}") or 0)
                except (TypeError, ValueError):
                    last_ts = 0
                try:
                    today_count = int(
                        self.store.get_meta(self._reply_gate_counter_key("day", window_tag)) or 0
                    )
                except (TypeError, ValueError):
                    today_count = 0
            cooldown_ok = cooldown <= 0 or (now - last_ts) >= cooldown
            daily_ok = daily_limit <= 0 or today_count < daily_limit
            mode_hit = False
            mode_reason = ""
            if mode == "keyword":
                mode_hit, mode_reason = keyword_hit(
                    text, parse_targets(self._cfg_value("reply_gate_keywords", ""))
                )
            elif mode == "memory":
                route = classify_route(text)
                if route == "low_info":
                    mode_hit, mode_reason = False, "low_info"
                else:
                    speaker_id = ""
                    try:
                        speaker_id = str(event.get_sender_id() or "")
                    except Exception:  # noqa: BLE001
                        speaker_id = ""
                    result = await self.retrieve_for(
                        text,
                        speaker_id,
                        persona_id=persona_id,
                        window_tag=window_tag,
                    )
                    mode_hit, mode_reason = memory_hit(result)
                    meta["route"] = getattr(result, "route", "")
            else:
                try:
                    probability = float(self._cfg_value("reply_gate_probability", 0.05))
                except (TypeError, ValueError):
                    probability = 0.05
                mode_hit = probability_hit(probability)
                mode_reason = "probability" if mode_hit else "probability_miss"
            fire, reason = reply_gate_evaluate(
                enabled=True,
                is_group=window_kind(window_tag) == "group",
                already_handled=bool(handled),
                is_self=is_self,
                window_tag=window_tag,
                targets=targets,
                text=text,
                min_chars=min_chars,
                skip_commands=bool(self._cfg_value("reply_gate_skip_commands", True)),
                cooldown_ok=cooldown_ok,
                daily_ok=daily_ok,
                mode_hit=mode_hit,
                mode_reason=mode_reason,
            )
            meta.update(
                {
                    "fire": fire,
                    "reason": reason,
                    "mode": mode,
                    "cooldown_ok": cooldown_ok,
                    "daily_ok": daily_ok,
                    "today": today_count,
                    "chars": len(text.strip()),
                }
            )
            if fire and window_tag:
                self.store.set_meta(f"reply_gate_last:{window_tag}", str(now))
                self.store.set_meta(
                    self._reply_gate_counter_key("day", window_tag), str(today_count + 1)
                )
            self.store.add_diag("reply_gate", meta)
            return fire, meta
        except Exception as exc:  # noqa: BLE001
            meta["error"] = str(exc)[:200]
            self.store.add_diag("reply_gate", meta)
            return False, meta

    async def build_injection(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        window_tag: str = "",
    ) -> tuple[str, Any, dict[str, Any]]:
        skip_ids = self._recent_ids(window_tag)
        event_skip_ids = self._recent_event_ids(window_tag)
        result = await self.retrieve_for(
            query,
            speaker_id,
            persona_id=persona_id,
            skip_ids=skip_ids,
            skip_query_mentions=bool(self._cfg_value("inject_novelty_filter", True)),
            window_tag=window_tag,
            event_skip_ids=event_skip_ids,
        )
        learning = self.learning.pack_for(query, persona_id=persona_id, route=result.route)
        dossier = self.dossier_for(
            speaker_id,
            persona_id=persona_id,
            window_tag=window_tag,
            query=query,
            isolation=self.session_isolation_mode(),
        )
        card = dossier.get("card") or ""
        profile_card, profile_meta = self.profile_card_for(
            speaker_id,
            persona_id=persona_id,
            window_tag=window_tag,
            isolation=self.session_isolation_mode(),
        )
        cross_block, cross_meta = self.cross_window_for(
            speaker_id,
            window_tag=window_tag,
            persona_id=persona_id,
        )
        shown_ids = (
            {f.id for f in result.core}
            | {f.id for f in result.related}
            | {f.id for f in result.uncertain}
        )
        dossier_ids = {int(x) for x in (dossier.get("fact_ids") or [])}
        suppressed_ids = {
            h.fact.id
            for h in result.blocked
            if h.filter_reason in {"recently_injected", "query_mentioned"}
        }
        if card and dossier_ids and dossier_ids.issubset(shown_ids | suppressed_ids):
            # 档案内容要么已在本轮事实里，要么被去重/新颖度有意压掉：不重复占预算。
            card = ""
        bot_facts = [
            f
            for f in self.store.person_facts(ROLE_BOT_ID, limit=12)
            # Bot 设定也要遵守人格隔离：全局（空 persona）可见，特定人格的只在自己人格下注入。
            if not f.persona_id or f.persona_id == persona_id
        ][:6]
        injected_ids: list[int] = []
        injected_event_ids: list[int] = []
        pack = build_pack(
            result,
            # 0 表示不限（见 inject._fits 的预算语义），显式 0 不能被 or 默认值吞掉。
            budget=int(self._cfg_value("inject_budget_chars", 800)),
            companion_present=any("companion" in d for d in self.coexistence.detected),
            learning=learning,
            dossier=card,
            warm_triggered=bool(self._cfg_value("inject_warm_triggered", True)),
            bot_facts=bot_facts,
            out_ids=injected_ids,
            events=list(getattr(result, "events", None) or []),
            event_budget=int(self._cfg_value("event_budget_chars", 300)),
            event_limit=max(1, int(self._cfg_value("event_max_inject", 2))),
            out_event_ids=injected_event_ids,
            history=list(getattr(result, "history", None) or []),
            history_current=dict(getattr(result, "history_current", None) or {}),
            history_label=str(getattr(result, "history_label", "") or ""),
            history_limit=max(1, int(self._cfg_value("history_max_facts", 6))),
            profile=profile_card,
            cross_window=cross_block,
            profile_budget=int(self._cfg_value("profile_max_chars", 300)),
            cross_budget=int(self._cfg_value("cross_window_max_chars", 320)),
        )
        if pack:
            # 只有真的进了包的事实/事件才算「最近注入过」；被预算裁掉/未触发的都不占名额。
            self._remember_injected(window_tag, injected_ids)
            self._remember_injected_events(window_tag, injected_event_ids)
            for event_id in injected_event_ids:
                self.store.bump_event_access(event_id)
        snapshot = {
            "query": clip(query, 80),
            "speaker_id": speaker_id,
            "persona_id": persona_id,
            "window": clip(window_tag, 80),
            "route": result.route,
            "path": result.path,
            "cache": result.cache,
            "pack_chars": len(pack),
            "pack_tokens": estimate_tokens(pack),
            "injected_ids": injected_ids,
            "injected_event_ids": injected_event_ids,
            "isolation": self.session_isolation_mode(),
            "bot_facts": [f.id for f in bot_facts],
            "core": [f.id for f in result.core],
            "related": [f.id for f in result.related],
            "uncertain": [f.id for f in result.uncertain],
            "superseded": [f.id for f in result.superseded],
            "events": [e.id for e in (getattr(result, "events", None) or [])],
            "history": [f.id for f in (getattr(result, "history", None) or [])],
            "history_label": str(getattr(result, "history_label", "") or ""),
            "entity_weight": self.entity_boost_weight(),
            "blocked": [
                {"id": h.fact.id, "reason": h.filter_reason}
                for h in result.blocked[:8]
            ],
            "event_blocked": [
                {"id": e.id, "reason": "filter"}
                for e in (getattr(result, "event_blocked", None) or [])[:8]
            ],
            "jargon": [j.get("term") for j in (learning.jargon or [])],
            "fewshots": len(learning.fewshots or []),
            "persona_draft": bool(learning.persona_draft),
            "dossier": bool(card),
            "profile": profile_meta,
            "cross_window": cross_meta,
            "injected": bool(pack),
            "dedup": len(skip_ids),
        }
        self.store.add_diag("inject", snapshot)
        return pack, result, snapshot

    def remember(self, speaker: dict[str, str], content: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        extra = extra or {}
        speaker_id = str(speaker.get("speaker_id") or "admin").strip() or "admin"
        known = self.store.get_profile(speaker_id)
        speaker_name = (
            str(speaker.get("speaker_name") or "").strip()
            or (known.speaker_name if known else "")
            or speaker_id
        )
        owner = self.is_owner_speaker(speaker_id)
        if speaker_id == ROLE_BOT_ID:
            # Bot 自己的记忆不建人物档案。
            pass
        elif owner:
            # 主人不建人物档案，只保证事实上的称呼最新。
            if not speaker_name or speaker_name == speaker_id:
                rows = self.store.query(
                    "SELECT speaker_name FROM facts WHERE speaker_id=? AND speaker_name!='' "
                    "ORDER BY id DESC LIMIT 1",
                    (speaker_id,),
                )
                if rows and rows[0]["speaker_name"]:
                    speaker_name = str(rows[0]["speaker_name"])
            self.store.sync_speaker_name(speaker_id, speaker_name)
        else:
            platform = platform_of(str(speaker.get("window_tag") or ""))
            if platform in {"", "console"}:
                # 面板/工具写入没有真实平台信息：留空，别覆盖已有档案的平台。
                platform = ""
            self.store.upsert_profile(
                speaker_id,
                speaker_name,
                platform,
                is_owner=False,
            )
        attribute = str(extra.get("attribute") or "")
        value = str(extra.get("value") or "")
        plain = str(extra.get("plain") or "")

        def build_payload(attr: str, val: str, text_plain: str) -> dict[str, Any]:
            return apply_slot(
                {
                    "subject": extra.get("subject") or "self",
                    "attribute": attr or "note",
                    "value": val or clip(content, 80),
                    "plain": text_plain or clip(content, 160),
                    "content": clip(content, 240),
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "bot_id": speaker.get("bot_id") or "",
                    "window_tag": speaker.get("window_tag") or "",
                    "persona_id": speaker.get("persona_id") or extra.get("persona_id") or "",
                    "confidence": float(extra.get("confidence") or 0.9),
                    "first_person": 1,
                    "explicit_correction": int(bool(extra.get("explicit_correction"))),
                    "source": extra.get("source") or "tool",
                    "mention_policy": extra.get("mention_policy") or "mention",
                    "origin": extra.get("origin") or ORIGIN_MANUAL,
                    "review_status": extra.get("review_status") or REVIEW_MANUAL,
                    "scope": SCOPE_OWNER if owner else SCOPE_PERSON,
                    "keywords": extra.get("keywords") or [],
                }
            )

        if not attribute and not value:
            # 手动补记也识别偏好句（可能多条）：避免「喜欢X」全进 note 单槽互相覆盖。
            inferred = self.extractor.infer_preferences(content)
            if inferred:
                results = [
                    self.contradiction.ingest(
                        build_payload(
                            item["attribute"],
                            item["value"],
                            plain or item["plain"],
                        ),
                        source_text=content,
                    )
                    for item in inferred
                ]
                last = dict(results[-1])
                last["facts"] = results
                return last
        payload = build_payload(attribute, value, plain)
        return self.contradiction.ingest(payload, source_text=content)

    async def fill_embeddings(self, limit: int = 16) -> int:
        self._sync_embed_fn()
        if not self.embedding_wanted():
            return 0
        if self.retriever.embed is None:
            status = self.embedding_status()
            if status["reason"] == "need_provider":
                self.store.add_diag("embed_skip", status)
            return 0
        facts = self.store.missing_embeddings(limit=limit)
        if not facts:
            return 0
        try:
            async with self._embed_lock:
                vectors = await self._embed([f.content for f in facts])
        except LLMBudgetExceeded:
            return 0
        n = 0
        for fact, vec in zip(facts, vectors):
            if vec:
                self.store.update_fact(fact.id, embedding=vec)
                n += 1
        return n

    def sleep_maintenance(self) -> dict[str, Any]:
        merged = self.store.resolve_all_slot_conflicts()
        folded = fold_preference_slots(self.store)
        expired_status = expire_status_facts(self.store)
        retain_days = int(self.config.get("sleep_timeline_retain_days") or 30)
        compacted = compact_summarized_timeline(self.store, retain_days=retain_days)
        compacted_superseded = compact_superseded(
            self.store,
            retain_days=int(self.config.get("sleep_superseded_retain_days") or 90),
        )
        archived = archive_low_value(
            self.store,
            min_age_days=int(self.config.get("sleep_low_value_days") or 30),
            max_confidence=float(self.config.get("sleep_low_value_confidence") or 0.45),
        )
        decayed = archive_decayed(
            self.store,
            min_age_days=int(self.config.get("sleep_low_value_days") or 30),
            threshold=float(self._cfg_value("importance_prune_threshold", 0.12)),
            half_life_days=float(self._cfg_value("importance_half_life_days", 30)),
            reinforce_factor=float(self._cfg_value("importance_reinforce_factor", 0.5)),
            max_multiplier=float(self._cfg_value("importance_max_half_life_multiplier", 3)),
        )
        decayed_events = archive_decayed_events(
            self.store,
            min_age_days=int(self._cfg_value("event_archive_days", 90)),
            threshold=float(self._cfg_value("importance_prune_threshold", 0.12)),
            half_life_days=float(self._cfg_value("importance_half_life_days", 30)),
            reinforce_factor=float(self._cfg_value("importance_reinforce_factor", 0.5)),
            max_multiplier=float(self._cfg_value("importance_max_half_life_multiplier", 3)),
        )
        expired = expire_persona_drafts(
            self.store,
            ttl_seconds=int(self.config.get("persona_draft_ttl_seconds") or 14 * 86400),
        )
        empty_profiles = self.store.delete_empty_profiles(
            ttl_days=int(self._cfg_value("empty_profile_ttl_days", 7)),
        )
        pruned_jargon = prune_jargon_stats(self.store)
        expired_pending = expire_pending_overrides(self.store)
        counts = self.store.counts()
        result = {
            "merged_duplicates": merged,
            "folded_preferences": folded,
            "expired_status": expired_status,
            "compacted_timeline": compacted,
            "compacted_superseded": compacted_superseded,
            "archived_low_value": archived,
            "archived_decayed": decayed,
            "archived_events": decayed_events,
            "expired_persona_drafts": expired,
            "deleted_empty_profiles": empty_profiles,
            "pruned_jargon": pruned_jargon,
            "expired_pending_overrides": expired_pending,
            **counts,
        }
        self.store.add_diag("sleep", result)
        return result

    def backup_now(self, dest_dir: Path) -> Path:
        return backup_db(self.store, dest_dir)

    def preview_archive(self, path: Path) -> dict[str, Any]:
        return preview_jsonl(path)

    def import_archive(self, path: Path, dest_dir: Path) -> dict[str, Any]:
        backup = backup_db(self.store, dest_dir)
        result = import_jsonl(self.store, path)
        result["resolved_conflicts"] = self.store.resolve_all_slot_conflicts()
        result["backup"] = str(backup)
        self.store.add_diag("import_jsonl", {k: v for k, v in result.items() if k != "backup"})
        return result

    def preview_chat(self, text: str, user_names: list[str] | None = None, bot_names: list[str] | None = None) -> dict[str, Any]:
        parsed = parse_transcript(text, user_names=user_names, bot_names=bot_names)
        preview = dict(parsed)
        preview["events"] = parsed["events"][:8]
        preview["preview_only"] = True
        return preview

    def import_chat(self, text: str, user_names: list[str] | None = None, bot_names: list[str] | None = None) -> dict[str, Any]:
        parsed = parse_transcript(text, user_names=user_names, bot_names=bot_names)
        result = import_transcript_events(self.store, parsed["events"])
        result["speakers"] = parsed["speakers"]
        self.store.add_diag("import_chat", {"added": result.get("added"), "skipped": result.get("skipped")})
        return result

    def alias_suggestions(self) -> list[dict[str, Any]]:
        rows = self.store.speaker_name_map()
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            name = (row.get("speaker_name") or "").strip()
            if len(name) < 2:
                continue
            by_name.setdefault(name, []).append(row)
        mapped = {(a["alias"], a["canonical_id"]) for a in self.store.list_aliases()}
        suggestions = []
        for name, items in by_name.items():
            ids = {i["speaker_id"] for i in items}
            if len(ids) < 2:
                continue
            ranked = sorted(items, key=lambda x: x["count"], reverse=True)
            canonical = ranked[0]["speaker_id"]
            for item in ranked[1:]:
                alias = item["speaker_id"]
                if alias == canonical or (alias, canonical) in mapped:
                    continue
                suggestions.append(
                    {
                        "name": name,
                        "alias": alias,
                        "canonical_id": canonical,
                        "alias_count": item["count"],
                        "canonical_count": ranked[0]["count"],
                    }
                )
        return suggestions[:20]

    def speaker_options(self) -> list[dict[str, str]]:
        seen: dict[str, str] = {"admin": "admin"}
        for profile in self.store.list_profiles(limit=300):
            sid = str(profile.speaker_id or "")
            if sid and sid != ROLE_BOT_ID and sid not in seen:
                seen[sid] = str(profile.speaker_name or sid)
        for row in self.store.speaker_name_map():
            sid = str(row.get("speaker_id") or "")
            if sid and sid != ROLE_BOT_ID and sid not in seen:
                seen[sid] = str(row.get("speaker_name") or sid)
        owner = self.owner_qq()
        if owner:
            # 配置了主人就保证主人可选，避免手动补记默认写到 admin 占位说话人。
            canonical = self.store.resolve_speaker(owner)
            if canonical and canonical not in seen:
                seen[canonical] = "主人"
        return [{"id": k, "name": v} for k, v in seen.items()]

    def overview(self) -> dict[str, Any]:
        counts = self.store.counts()
        return {
            "counts": counts,
            "revision": self.store.revision(),
            "coexistence": self.coexistence.snapshot(),
            "usage": self.store.usage_summary(),
            "aliases": self.store.list_aliases(),
            "alias_suggestions": self.alias_suggestions(),
            "speakers": self.speaker_options(),
            "data_dir": str(self.store.db_path.parent),
            "embedding": self.embedding_status(),
            "tokens": self.tokens_status(),
            "providers": {
                "normalize": self._provider_for("normalize"),
                "verify": self._provider_for("verify"),
                "event": self._provider_for("event"),
                "learn": self._provider_for("learn"),
            },
            "owner": {
                "qq": self.owner_qq(),
                "ids": sorted(self._owner_ids),
                "notify_umo": self.owner_notify_umo(),
            },
            "config": {
                "enabled": self.enabled(),
                "capture": self.capture_ok(),
                "inject": self.inject_ok(),
                "retrieval_mode": self.config.get("retrieval_mode"),
                "bm25": bool(self.retriever.bm25),
                "tokenizer": tokenizer_mod.name(),
                "embedding_enabled": bool(self.config.get("embedding_enabled")),
                "pipeline_enabled": bool(self.config.get("pipeline_enabled", True)),
                "event_enabled": self.events.enabled(),
                "session_isolation": self.session_isolation_mode(),
                "platforms": self.allowed_platforms(),
                "theme_color": str(self.config.get("ui_theme_color") or "#7c5cff"),
                "theme_color2": str(self.config.get("ui_theme_color2") or "#22d3ee"),
                "theme_color3": str(self.config.get("ui_theme_color3") or "#f472b6"),
                "dynamic_colors": bool(self.config.get("ui_dynamic_colors")),
                "capture_skip": self.last_capture_skip(),
            },
        }

    def export_jsonl(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = self.store.export_rows()
        with dest.open("w", encoding="utf-8") as f:
            for kind, rows in data.items():
                for row in rows:
                    payload = {k: row[k] for k in row.keys()}
                    payload["table"] = kind
                    if kind == "reviews":
                        payload["review_kind"] = payload.get("review_kind") or payload.get("kind")
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return dest

    def _provider_for(self, kind: str) -> str:
        """显式配置 > 档位 > 旧回退链 > 当前会话模型（空串表示跟随会话）。"""
        return resolve_provider(kind, self.config)[0]

    def _provider_source(self, kind: str) -> str:
        return resolve_provider(kind, self.config)[1]

    def llm_guard(self) -> BudgetGuard:
        if self._llm_guard is None:
            self._llm_guard = BudgetGuard(self.config, lambda: self.store.tokens_today())
        return self._llm_guard

    def tokens_status(self) -> dict[str, Any]:
        status = self.llm_guard().status()
        status["by_task"] = self.store.usage_by_task_today()
        return status

    async def _llm_with_provider(self, prompt: str, provider_id: str, task: str = "default") -> str:
        guard = self.llm_guard()
        source = self._provider_source(task)
        decision = guard.check(task, prompt)
        if not decision.allowed:
            self.store.add_usage(
                "llm", provider_id, ok=False, chars_in=len(prompt or ""),
                task=task, source=source, reason=decision.reason,
            )
            if self.logger:
                self.logger.info(
                    "Savage Type llm budget blocked task=%s reason=%s used=%s",
                    task, decision.reason, guard.status()["used"],
                )
            raise LLMBudgetExceeded(decision.reason)
        if decision.provider_override:
            provider_id = decision.provider_override
            source = "single_call_cap"
        return await self._chat(prompt, provider_id, task=task, source=source, allow_refusal_retry=True)

    async def _chat(
        self,
        prompt: str,
        provider_id: str,
        task: str = "default",
        source: str = "",
        allow_refusal_retry: bool = False,
    ) -> str:
        try:
            result = await self.llm_generate(prompt, provider_id)
        except Exception:
            self.store.add_usage("llm", provider_id, False, len(prompt or ""), 0, task=task, source=source, reason="error")
            raise
        text, tokens_in, tokens_out = _unpack_llm_result(result)
        if tokens_in <= 0:
            tokens_in = estimate_tokens(prompt or "")
        if tokens_out <= 0:
            tokens_out = estimate_tokens(text or "")
        self.store.add_usage(
            "llm",
            provider_id,
            True,
            len(prompt or ""),
            len(text or ""),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            task=task,
            source=source,
        )
        if allow_refusal_retry and looks_refusal(text):
            fallback = self.llm_guard().fallback_provider()
            if fallback and fallback != provider_id:
                # 拒答按「被子弹拦截」记账，然后用备用模型重试一次。
                self.store.add_usage(
                    "llm", provider_id, ok=False, task=task, source=source, reason="refusal"
                )
                if self.logger:
                    self.logger.info(
                        "Savage Type llm refusal task=%s, retrying with fallback %s", task, fallback
                    )
                return await self._chat(
                    prompt, fallback, task=task, source="fallback", allow_refusal_retry=False
                )
            self.store.add_diag("llm_refusal", {"task": task, "provider": provider_id})
        return text

    async def _llm(self, prompt: str) -> str:
        return await self._llm_with_provider(prompt, self._provider_for("default"), task="default")

    def _llm_for(self, kind: str):
        kind = kind or "default"

        async def call(prompt: str) -> str:
            return await self._llm_with_provider(prompt, self._provider_for(kind), task=kind)

        return call

    async def navigate(
        self,
        query: str,
        speaker_id: str,
        persona_id: str = "",
        fact_id: int = 0,
        max_steps: int = 3,
        window_tag: str = "",
    ) -> dict[str, Any]:
        steps = []
        seen: set[int] = set()
        current = query
        hops = max(1, min(int(max_steps or 3), 3))
        if fact_id:
            seed = self.store.get_fact(fact_id)
            if seed:
                seen.add(seed.id)
                current = seed.content or query
        for i in range(hops):
            result = await self.retrieve_for(
                current, speaker_id, persona_id=persona_id, window_tag=window_tag
            )
            batch = []
            for fact in result.core + result.related + result.uncertain:
                if fact.id in seen:
                    continue
                seen.add(fact.id)
                batch.append({"id": fact.id, "content": clip(fact.content, 80), "attribute": fact.attribute})
                if len(batch) >= 6:
                    break
            steps.append({"step": i + 1, "query": current, "hits": batch})
            if not batch:
                break
            current = batch[0]["content"]
        return {"ok": True, "steps": steps, "unique": len(seen)}

    def _provider_timeout(self) -> float:
        try:
            return max(0.0, float(self._cfg_value("provider_timeout_seconds", 5)))
        except (TypeError, ValueError):
            return 5.0

    async def _wait_provider(self, call: Any, timeout: float) -> Any:
        """Await a provider call with a hard timeout; sync results pass through."""
        if not asyncio.iscoroutine(call):
            return call
        if timeout > 0:
            return await asyncio.wait_for(call, timeout=timeout)
        return await call

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        provider = self.get_provider("embedding", str(self.config.get("embedding_provider_id") or ""))
        if provider is None:
            raise RuntimeError("no embedding provider")
        pid = ""
        try:
            pid = provider.meta().id
        except Exception:
            pid = ""
        timeout = self._provider_timeout()
        prompt_text = " ".join(texts)
        decision = self.llm_guard().check("embed", prompt_text[:4000])
        if not decision.allowed:
            self.store.add_usage(
                "embed", pid, ok=False, chars_in=sum(len(t) for t in texts),
                task="embed", source="explicit:embedding_provider_id", reason=decision.reason,
            )
            raise LLMBudgetExceeded(decision.reason)
        try:
            if hasattr(provider, "get_embeddings"):
                vectors = await self._wait_provider(provider.get_embeddings(texts), timeout)
            else:
                vectors = []
                for text in texts:
                    vectors.append(await self._wait_provider(provider.get_embedding(text), timeout))
            self.store.add_usage(
                "embed", pid, True, sum(len(t) for t in texts), 0,
                tokens_in=estimate_tokens(prompt_text), task="embed",
                source="explicit:embedding_provider_id",
            )
            return vectors
        except Exception as exc:
            self.store.add_usage(
                "embed", pid, False, sum(len(t) for t in texts), 0, detail=f"{type(exc).__name__}"
            )
            if isinstance(exc, asyncio.TimeoutError):
                self.store.add_diag("provider_timeout", {"kind": "embed", "timeout": timeout})
            raise

    async def _rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        provider = self.get_provider("rerank", str(self.config.get("rerank_provider_id") or ""))
        if provider is None:
            raise RuntimeError("no rerank provider")
        pid = ""
        try:
            pid = provider.meta().id
        except Exception:
            pid = ""
        timeout = self._provider_timeout()
        prompt_text = f"{query} " + " ".join(documents)
        decision = self.llm_guard().check("rerank", prompt_text[:4000])
        if not decision.allowed:
            self.store.add_usage(
                "rerank", pid, ok=False, chars_in=len(query), task="rerank",
                source="explicit:rerank_provider_id", reason=decision.reason,
            )
            raise LLMBudgetExceeded(decision.reason)
        try:
            results = await self._wait_provider(provider.rerank(query, documents, top_n=top_n), timeout)
            out: list[tuple[int, float]] = []
            for item in results or []:
                idx = getattr(item, "index", None)
                score = getattr(item, "relevance_score", None)
                if idx is None and isinstance(item, dict):
                    idx = item.get("index")
                    score = item.get("relevance_score")
                if idx is None:
                    continue
                out.append((int(idx), float(score or 0)))
            self.store.add_usage(
                "rerank", pid, True, len(query) + sum(len(d) for d in documents), 0,
                tokens_in=estimate_tokens(prompt_text), task="rerank",
                source="explicit:rerank_provider_id",
            )
            return out
        except Exception as exc:
            self.store.add_usage("rerank", pid, False, len(query), 0, detail=f"{type(exc).__name__}")
            if isinstance(exc, asyncio.TimeoutError):
                self.store.add_diag("provider_timeout", {"kind": "rerank", "timeout": timeout})
            raise
