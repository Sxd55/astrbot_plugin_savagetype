from pathlib import Path
import asyncio
import base64
import json
import mimetypes
import re
import time
import urllib.parse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_plugin_data_path

try:
    from .savagetype import __version__ as PLUGIN_VERSION
    from .savagetype.service import SavageTypeService
    from .savagetype.addressee import has_bot_mention
    from .savagetype.debounce import is_probably_incomplete, merge_fragments
    from .savagetype.replygate import question_like as savagetype_question_like
    from .savagetype.crosswin import window_kind
    from .savagetype.slots import apply_slot
    from .savagetype.speak import group_label
    from .savagetype.store import Store
    from .savagetype.util import (
        PLUGIN_NAME,
        clip,
        estimate_tokens,
        fact_weight,
        fmt_ts,
        make_slot_key,
        now_ts,
        parse_csv,
    )
except ImportError:
    from savagetype import __version__ as PLUGIN_VERSION
    from savagetype.service import SavageTypeService
    from savagetype.addressee import has_bot_mention
    from savagetype.debounce import is_probably_incomplete, merge_fragments
    from savagetype.replygate import question_like as savagetype_question_like
    from savagetype.crosswin import window_kind
    from savagetype.slots import apply_slot
    from savagetype.speak import group_label
    from savagetype.store import Store
    from savagetype.util import (
        PLUGIN_NAME,
        clip,
        estimate_tokens,
        fact_weight,
        fmt_ts,
        make_slot_key,
        now_ts,
        parse_csv,
    )

SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"

PLUGIN_NAME_CONST = PLUGIN_NAME


def _data_dir() -> Path:
    try:
        root = Path(get_astrbot_plugin_data_path())
    except Exception:
        root = Path(get_astrbot_data_path()) / "plugin_data"
    path = root / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@register(
    PLUGIN_NAME,
    "24122",
    "Savage Type 全局人格记忆中枢：事实、改口、审查后的黑话释义与表达样本。",
    "5.0.0",
)
class SavageTypePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = _data_dir()
        self.store = Store(self.data_dir / "savagetype.db")
        self.service = SavageTypeService(
            store=self.store,
            config=self.config,
            llm_generate=self._llm_generate,
            get_provider=self._get_special_provider,
            logger=logger,
            get_persona_text=self._persona_text,
            send_message=self._send_message,
        )
        self._register_pages()
        self._debounce_hold: dict[str, dict] = {}
        self._debounce_skip: set[str] = set()
        self._gate_pending: dict[str, dict] = {}
        logger.info("Savage Type loaded, db=%s", self.store.db_path)

    async def initialize(self):
        self.service.refresh_coexistence(self.context.get_all_stars())
        if self.store.get_meta("cleaned_v280") != "1":
            try:
                backup = self.service.backup_now(self.data_dir / "backups")
                counts = self.store.clear_dirty_v280()
                self.store.set_meta("cleaned_v280", "1")
                logger.info("Savage Type clean rebuild done: backup=%s cleared=%s", backup, counts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Savage Type clean rebuild failed: %s", exc)
        if self.store.get_meta("plugin_version") != PLUGIN_VERSION:
            try:
                backup = self.service.backup_now(self.data_dir / "backups")
                self.store.set_meta("plugin_version", PLUGIN_VERSION)
                logger.info("Savage Type version backup: %s -> %s", PLUGIN_VERSION, backup)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Savage Type version backup failed: %s", exc)
        logger.info("Savage Type coexistence: %s", self.service.coexistence.snapshot())

    async def _send_message(self, umo: str, text: str) -> None:
        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)

    async def terminate(self):
        task = getattr(self.service, "_learn_task", None)
        if task and not task.done():
            task.cancel()
        try:
            self.store.close()
        except Exception:
            pass

    def _register_pages(self) -> None:
        apis = [
            ("overview", self.page_overview, ["GET"], "Overview"),
            ("search", self.page_search, ["GET"], "Search facts"),
            ("facts", self.page_facts, ["GET"], "List facts"),
            ("pending", self.page_pending, ["GET"], "Pending overrides"),
            ("pending/confirm", self.page_pending_confirm, ["POST"], "Confirm pending"),
            ("pending/reject", self.page_pending_reject, ["POST"], "Reject pending"),
            ("rollback", self.page_rollback, ["POST"], "Rollback supersede"),
            ("remember", self.page_remember, ["POST"], "Add fact"),
            ("extract", self.page_extract, ["POST"], "Run extract"),
            ("sleep", self.page_sleep, ["POST"], "Sleep maintenance"),
            ("diagnostics", self.page_diagnostics, ["GET"], "Diagnostics"),
            ("export", self.page_export, ["GET"], "Export jsonl"),
            ("aliases", self.page_aliases, ["GET"], "Speaker aliases"),
            ("aliases/set", self.page_alias_set, ["POST"], "Set speaker alias"),
            ("reviews", self.page_reviews, ["GET"], "Learning reviews"),
            ("reviews/set", self.page_review_set, ["POST"], "Approve or reject review"),
            ("learn", self.page_learn, ["POST"], "Run learning pass"),
            ("archive/preview", self.page_archive_preview, ["POST"], "Preview jsonl archive"),
            ("archive/import", self.page_archive_import, ["POST"], "Import jsonl archive"),
            ("chat/preview", self.page_chat_preview, ["POST"], "Preview chat transcript"),
            ("chat/import", self.page_chat_import, ["POST"], "Import chat transcript"),
            ("microscope", self.page_microscope, ["GET"], "Recent injection snapshots"),
            ("facts/archive", self.page_facts_archive, ["POST"], "Archive facts"),
            ("events", self.page_events, ["GET"], "List events"),
            ("events/update", self.page_event_update, ["POST"], "Edit one event"),
            ("events/pin", self.page_event_pin, ["POST"], "Pin or unpin an event"),
            ("events/archive", self.page_events_archive, ["POST"], "Archive events"),
            ("events/restore", self.page_events_restore, ["POST"], "Restore events"),
            ("events/evidence", self.page_event_evidence, ["GET"], "Event source messages"),
            ("entities", self.page_entities, ["GET"], "Entity links for one ref"),
            ("config", self.page_config_get, ["GET"], "Plugin config and schema"),
            ("config/save", self.page_config_save, ["POST"], "Save plugin config"),
            ("dossiers", self.page_dossiers, ["GET"], "List QQ dossiers"),
            ("dossier", self.page_dossier, ["GET"], "One QQ dossier"),
            ("memory", self.page_memory, ["GET"], "Owner memory library"),
            ("memory/bot", self.page_memory_bot, ["GET"], "Bot memory library"),
            ("memory/pending", self.page_memory_pending, ["GET"], "Pending memory reviews"),
            ("memory/review", self.page_memory_review, ["POST"], "Approve or reject memory"),
            ("profiles", self.page_profiles, ["GET"], "List profiles"),
            ("profile", self.page_profile, ["GET"], "One profile with facts"),
            ("profile/update", self.page_profile_update, ["POST"], "Update profile"),
            ("facts/update", self.page_fact_update, ["POST"], "Edit one fact"),
            ("facts/restore", self.page_facts_restore, ["POST"], "Restore archived facts"),
            ("facts/pin", self.page_fact_pin, ["POST"], "Pin or unpin a fact"),
            ("reset", self.page_reset, ["POST"], "Clean rebuild with backup"),
            ("providers", self.page_providers, ["GET"], "List providers by type"),
            ("ui/theme", self.page_theme_set, ["POST"], "Save panel theme colors"),
            ("ui/dynamic", self.page_dynamic_set, ["POST"], "Toggle dynamic colors"),
        ]
        for route, handler, methods, desc in apis:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}",
                handler,
                methods,
                desc,
            )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            text = (event.message_str or "").strip()
            if self._has_image(event):
                image_text = await self._image_text(event)
                if image_text:
                    if not text or text in {"[图片]", "[image]", "[Image]", "<attachment>"}:
                        text = image_text
                    elif image_text not in text:
                        text = f"{text} {image_text}"
            try:
                event.set_extra("_stype_text", text)
            except Exception:
                pass
            if not text:
                return
            try:
                self.service.refresh_coexistence(self.context.get_all_stars())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Savage Type coexistence refresh failed: %s", exc)
            try:
                self.service.remember_owner_window(event)
                if self.service.is_owner_event(event):
                    spoken = await self.service.handle_speak_request(event, text)
                    if spoken:
                        await self.service.send_text(event.unified_msg_origin, spoken)
                        try:
                            event.stop_event()
                        except Exception:
                            pass
                        return
                    reply = await self.service.handle_owner_reply(text)
                    if reply:
                        await self.service.send_text(event.unified_msg_origin, reply)
                        try:
                            event.stop_event()
                        except Exception:
                            pass
                        return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Savage Type owner reply failed: %s", exc)
            if self.service.is_command_text(text, event):
                return
            try:
                persona_id = await self._persona_id(event)
            except Exception:
                persona_id = ""
            ident = self.service.identity_from_event(event, persona_id=persona_id)
            event.set_extra("_stype_ident", ident)
            self.service.capture_user(event, text)
            self.service.schedule_learn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type capture failed: %s", exc)

    if hasattr(filter, "on_waiting_llm_request"):
        @filter.on_waiting_llm_request()
        async def on_waiting_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
            """会话锁等待期间预热检索缓存，让检索和排队时间重叠（AstrBot 支持时生效）。"""
            try:
                if not self.service.inject_ok(event):
                    return
                cached_text = ""
                try:
                    cached_text = str(event.get_extra("_stype_text") or "")
                except Exception:
                    cached_text = ""
                query = (cached_text or event.message_str or "").strip()
                if not query:
                    return
                persona_id = await self._persona_id(event)
                ident = await self._ident(event)
                await self.service.warm_retrieval(
                    query,
                    ident["speaker_id"],
                    persona_id=persona_id,
                    window_tag=ident.get("window_tag") or "",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Savage Type warm retrieval skipped: %s", exc)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if event.get_extra("_stype_debounce_merge"):
                # 防抖：这条碎片已经并入待发送的消息，本次不回复。
                event.stop_event()
                return
            self.service.refresh_coexistence(self.context.get_all_stars())
            if not self.service.inject_ok(event):
                return
            cached_text = ""
            try:
                cached_text = str(event.get_extra("_stype_text") or "")
            except Exception:
                cached_text = ""
            query = (cached_text or event.message_str or req.prompt or "").strip()
            persona_id = await self._persona_id(event)
            ident = self.service.identity_from_event(event, persona_id=persona_id)
            event.set_extra("_stype_ident", ident)
            mention_hint = self.service.blank_mention_hint(event, ident)
            pack = ""
            flow_block = ""
            snapshot: dict = {}
            flow_meta: dict = {}
            if query:
                pack, result, snapshot = await self.service.build_injection(
                    query,
                    ident["speaker_id"],
                    persona_id=persona_id,
                    window_tag=ident.get("window_tag") or "",
                )
                flow_block, flow_meta = self.service.window_flow_for(
                    query,
                    window_tag=ident.get("window_tag") or "",
                    persona_id=persona_id,
                )
            if not pack and not flow_block and not mention_hint:
                return
            if pack:
                self._append_pack(req, pack)
            if flow_block:
                self._append_pack(req, flow_block)
            if mention_hint:
                self._append_pack(req, mention_hint)
            if self.config.get("debug_log_injection"):
                logger.info(
                    "Savage Type inject route=%s path=%s cache=%s core=%s related=%s chars=%s window_flow=%s blank_mention=%s",
                    snapshot.get("route"),
                    snapshot.get("path"),
                    snapshot.get("cache"),
                    snapshot.get("core"),
                    snapshot.get("related"),
                    snapshot.get("pack_chars"),
                    flow_meta.get("items"),
                    bool(mention_hint),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type inject failed: %s", exc)

    @filter.on_waiting_llm_request()
    async def debounce_collect(self, event: AstrMessageEvent):
        """防抖：短时间连发的短消息合并成一条再提交（启发式，无模型依赖）。"""
        try:
            if not self.config.get("debounce_enabled"):
                return
            message_id = ""
            try:
                message_id = str(event.message_obj.message_id or "")
            except Exception:
                message_id = ""
            if message_id and message_id in self._debounce_skip:
                self._debounce_skip.discard(message_id)
                return
            if event.get_extra("_stype_debounce_merge"):
                event.stop_event()
                return
            if not self._debounce_qualifies(event):
                return
            key = self._debounce_key(event)
            text = str(event.message_str or "").strip()
            now = time.time()
            window = max(0.5, float(self.config.get("debounce_window_seconds", 2.5) or 2.5))
            max_seconds = max(1.0, float(self.config.get("debounce_max_seconds", 8) or 8))
            max_fragments = max(2, int(self.config.get("debounce_max_fragments", 4) or 4))
            hold = self._debounce_hold.get(key)
            if hold and str(hold.get("speaker")) == str(event.get_sender_id()):
                hold["fragments"].append(text)
                hold["count"] = int(hold.get("count", 1)) + 1
                hold["deadline"] = min(now + window, float(hold.get("start", now)) + max_seconds)
                if hold.get("task"):
                    hold["task"].cancel()
                if hold["count"] >= max_fragments:
                    await self._debounce_flush(key)
                    event.stop_event()
                    return
                hold["task"] = asyncio.create_task(self._debounce_timer(key, hold["deadline"] - now))
                event.stop_event()
                return
            if hold:
                # 换人或超时：先把之前合并的放出去（异步），本条按正常流程走。
                asyncio.create_task(self._debounce_flush(key))
            hold = {
                "speaker": str(event.get_sender_id()),
                "fragments": [text],
                "count": 1,
                "start": now,
                "deadline": now + window,
                "event": event,
            }
            hold["task"] = asyncio.create_task(self._debounce_timer(key, window))
            self._debounce_hold[key] = hold
            event.stop_event()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type debounce failed: %s", exc)

    async def _debounce_timer(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.2, float(delay)))
            await self._debounce_flush(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type debounce timer failed: %s", exc)

    async def _debounce_flush(self, key: str) -> None:
        hold = self._debounce_hold.pop(key, None)
        if not hold:
            return
        task = hold.get("task")
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        merged = merge_fragments(
            [str(item) for item in (hold.get("fragments") or [])],
            max_chars=max(60, int(self.config.get("debounce_max_chars", 600) or 600)),
        )
        event = hold.get("event")
        if not merged or event is None:
            return
        self.store.add_diag("debounce", {"chars": len(merged), "fragments": int(hold.get("count", 1))})
        await self._reinject(event, merged)

    async def _reinject(self, event: AstrMessageEvent, text: str) -> None:
        """把文本重新提交进 AstrBot 管道（走完整的人格/记忆流程）。防抖合并与延迟接话共用。"""
        from astrbot.core.message.components import Plain
        from astrbot.core.star.star_tools import StarTools

        components = [c for c in (event.message_obj.message or []) if not isinstance(c, Plain)]
        components.insert(0, Plain(text))
        message = await StarTools.create_message(
            type=str(event.message_obj.type.value),
            self_id=event.get_self_id(),
            session_id=event.session_id,
            sender=event.message_obj.sender,
            message=components,
            message_str=text,
            group_id=event.get_group_id() or "",
            message_id=event.message_obj.message_id,
        )
        try:
            self._debounce_skip.add(str(message.message_id))
        except Exception:  # noqa: BLE001
            pass
        await StarTools.create_event(
            abm=message,
            platform=event.get_platform_name(),
            is_wake=True,
        )
        if self.config.get("debug_log_injection"):
            logger.info("Savage Type debounce flushed: %s", text[:60])

    def _schedule_unanswered_check(self, event: AstrMessageEvent, meta: dict) -> None:
        """(d) 问句发出后 N 秒没人应答 → Bot 再接话（延迟任务，同群只留一个）。"""
        try:
            window = str(event.unified_msg_origin or "")
            if not window:
                return
            text = str(event.message_str or "").strip()
            if not savagetype_question_like(text):
                return
            old = self._gate_pending.pop(window, None)
            if old and old.get("task"):
                old["task"].cancel()
            try:
                delay = max(3, int(self.config.get("reply_gate_unanswered_seconds", 20) or 20))
            except (TypeError, ValueError):
                delay = 20
            try:
                asker = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                asker = ""
            asked_ts = int(time.time()) - 1
            task = asyncio.create_task(
                self._unanswered_check(window, text, asker, meta, delay, event, asked_ts)
            )
            self._gate_pending[window] = {"task": task, "ts": asked_ts}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type unanswered schedule failed: %s", exc)

    async def _unanswered_check(
        self,
        window: str,
        text: str,
        asker: str,
        meta: dict,
        delay: int,
        event: AstrMessageEvent,
        asked_ts: int | None = None,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            self._gate_pending.pop(window, None)
            since_ts = int(asked_ts) if asked_ts else int(time.time()) - max(1, int(delay))
            ok, reason = self.service.reply_gate_delayed_ok(window, since_ts, asker)
            self.store.add_diag("reply_gate_delayed", {"window": window, "ok": ok, "reason": reason})
            if not ok:
                return
            if event is None:
                return
            await self._reinject(event, text)
            if self.config.get("debug_log_injection"):
                logger.info("Savage Type unanswered gate fired: %s", text[:40])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type unanswered check failed: %s", exc)

    def _debounce_key(self, event: AstrMessageEvent) -> str:
        try:
            return f"{event.get_platform_name()}:{event.session_id}"
        except Exception:  # noqa: BLE001
            return str(event.unified_msg_origin or "unknown")

    def _debounce_qualifies(self, event: AstrMessageEvent) -> bool:
        scope = str(self.config.get("debounce_scope", "both") or "both").strip().lower()
        is_private = window_kind(event.unified_msg_origin) == "private"
        if scope == "group" and is_private:
            return False
        if scope == "private" and not is_private:
            return False
        if bool(self.config.get("debounce_skip_wake", True)):
            try:
                self_id = str(getattr(event.message_obj, "self_id", "") or "")
            except Exception:  # noqa: BLE001
                self_id = ""
            if self_id and has_bot_mention(self.service.addressee_from_event(event), self_id):
                # 明确 @ 机器人的消息立即回复，不等待。
                return False
        text = str(event.message_str or "").strip()
        if not text or text.startswith(("/", "／", "!")):
            return False
        if self._has_image(event):
            return False
        try:
            short_chars = max(2, int(self.config.get("debounce_short_chars", 12) or 12))
        except (TypeError, ValueError):
            short_chars = 12
        return is_probably_incomplete(text, short_chars=short_chars)


    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            text = getattr(resp, "completion_text", "") or ""
            self.service.capture_bot(event, text)
            event.set_extra("_stype_bot_captured", True)
            ident = event.get_extra("_stype_ident")
            if not ident:
                ident = self.service.identity_from_event(event)
            self.service.note_reply_target(event, ident)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type bot capture failed: %s", exc)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        # Bot 正文以 on_llm_response 为准，这里只补无 LLM 的主动发送。
        try:
            if event.get_extra("_stype_bot_captured"):
                return
            if self.service.is_command_text(str(event.message_str or ""), event):
                # 命令回复（/stype flow 等）是人给插件的指令回执，不是 Bot 的聊天发言。
                return
            result = event.get_result()
            if result is None:
                return
            text = ""
            try:
                text = result.get_plain_text() if hasattr(result, "get_plain_text") else ""
            except Exception:
                chain = getattr(result, "chain", None) or []
                text = "".join(getattr(c, "text", "") for c in chain)
            if text:
                self.service.capture_bot(event, text)
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def reply_gate(self, event: AstrMessageEvent):
        """免@主动接话：按配置判定是否让本条群消息进入默认 LLM 回复。

        命中时把事件标记为「已唤醒」（is_at_or_wake_command=True），
        后续完全走 AstrBot 默认 LLM 通路：人格、记忆注入、分段、TTS 全部照旧。
        """
        try:
            if not self.service.reply_gate_enabled():
                return
            if getattr(event, "is_at_or_wake_command", False) or event.is_wake_up():
                return
            handled = bool(
                event.get_result()
                or event.get_extra("provider_request")
                or getattr(event, "_has_send_oper", False)
            )
            fire, meta = await self.service.reply_gate_for(
                event,
                handled=handled,
                persona_id=await self._persona_id(event),
            )
            if fire:
                event.is_at_or_wake_command = True
                event.set_extra("_stype_reply_gate", meta)
                if self.config.get("debug_log_injection"):
                    logger.info(
                        "Savage Type reply gate fired: mode=%s reason=%s window=%s",
                        meta.get("mode"),
                        meta.get("reason"),
                        event.unified_msg_origin,
                    )
                return
            if self.service.reply_gate_delayed_eligible(meta):
                self._schedule_unanswered_check(event, meta)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type reply gate failed: %s", exc)

    @filter.command_group("stype")
    def stype(self):
        pass

    @stype.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 Savage Type 状态"""
        ov = self.service.overview()
        c = ov["counts"]
        co = ov["coexistence"]
        tk = ov.get("tokens") or {}
        hard = tk.get("hard_limit") or "不限"
        soft = tk.get("soft_limit") or "不限"
        skipped = sum(int(row.get("skipped") or 0) for row in (tk.get("by_task") or []))
        skip = ov["config"].get("capture_skip") or {}
        skip_line = f"{skip.get('reason')}（{skip.get('platform') or '?'}）" if skip else "无"
        yield event.plain_result(
            f"Savage Type 状态 v{PLUGIN_VERSION}\n"
            f"时间线 {c['timeline']} / 未总结 {c['unsummarized']}\n"
            f"live {c['facts_live']} / superseded {c['facts_superseded']} / 覆盖待确认 {c['pending']}\n"
            f"事件 {c.get('events', 0)} / 事件待审 {c.get('events_needs_review', 0)}\n"
            f"主人记忆 {c.get('owner_facts', 0)} / 档案 {c.get('profiles', 0)} / 待审记忆 {c.get('memory_pending', 0)}\n"
            f"学习待审 {c.get('reviews_pending', 0)} / 黑话 {c.get('jargon_approved', 0)} / few-shot {c.get('fewshot_approved', 0)}\n"
            f"采集 {'开' if ov['config']['capture'] else '关'} 注入 {'开' if ov['config']['inject'] else '关'}\n"
            f"今日Token {tk.get('used', 0)}（硬限 {hard} / 软限 {soft} / 预算跳过 {skipped} 次）\n"
            f"本会话平台 {self.service.event_platform(event) or '未知'}\n"
            f"允许平台 {', '.join(ov['config'].get('platforms') or []) or '不限'}\n"
            f"上次采集跳过 {skip_line}\n"
            f"检索 {ov['config']['retrieval_mode']} bm25 {'开' if ov['config'].get('bm25') else '关'}({ov['config'].get('tokenizer') or 'builtin'}) embedding {ov['config']['embedding_enabled']}\n"
            f"降级 {', '.join(co['reasons']) or '无'}"
        )

    def _rest_after(self, event: AstrMessageEvent, token: str) -> str:
        msg = event.message_str or ""
        idx = msg.lower().find(token.lower())
        return msg[idx + len(token) :].strip() if idx >= 0 else msg.strip()

    @stype.command("search")
    async def cmd_search(self, event: AstrMessageEvent):
        """检索当前说话人可见事实"""
        keyword = self._rest_after(event, "search")
        if not keyword:
            yield event.plain_result("用法: /stype search <关键词>")
            return
        ident = await self._ident(event)
        facts = self.store.search_facts(
            keyword,
            speaker_id=ident["speaker_id"],
            limit=8,
            persona_id=ident.get("persona_id") or "",
            speaker_ids=self.store.speaker_ids_for(ident["speaker_id"]),
        )
        if not facts:
            yield event.plain_result("没有命中 live 事实。")
            return
        lines = [f"{f.id} [{f.speaker_name or f.speaker_id}/{f.attribute}] {clip(f.content, 80)}" for f in facts]
        yield event.plain_result("\n".join(lines))

    @stype.command("dossier")
    async def cmd_dossier(self, event: AstrMessageEvent, speaker_id: str = ""):
        """查看某人按 QQ 汇总的短档案（查别人需管理员）"""
        ident = await self._ident(event)
        sid = (speaker_id or ident["speaker_id"]).strip()
        if (
            sid != ident["speaker_id"]
            and not self.service.is_admin_event(event)
            and not self.service.is_owner_event(event)
        ):
            yield event.plain_result("查看别人的档案需要管理员权限。")
            return
        card = self.service.dossier_for(
            sid,
            persona_id=ident.get("persona_id") or "",
            window_tag=ident.get("window_tag") or "",
            isolation=self.service.session_isolation_mode(),
        )
        if not card.get("card"):
            yield event.plain_result(f"{sid} 还没有短档案（需要至少一条 live 事实）。")
            return
        yield event.plain_result(card["card"])

    @stype.command("profile")
    async def cmd_profile(self, event: AstrMessageEvent):
        """看自己的跨会话画像（私聊/群里同一份）"""
        ident = await self._ident(event)
        card, meta = self.service.profile_card_for(
            ident["speaker_id"],
            persona_id=ident.get("persona_id") or "",
        )
        if not meta.get("enabled", True):
            yield event.plain_result("跨会话画像已在配置里关闭。")
            return
        if not card:
            yield event.plain_result("还没有画像（需要至少一条已整理的稳定事实）。")
            return
        yield event.plain_result(
            f"{card}\n\n（{meta.get('facts', 0)} 条事实 · {meta.get('chars', 0)} 字"
            f" · 语气条目 {meta.get('tone', 0)}）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("cross")
    async def cmd_cross(self, event: AstrMessageEvent):
        """预览跨会话衔接块（当前会话视角）"""
        ident = await self._ident(event)
        block, meta = self.service.cross_window_for(
            ident["speaker_id"],
            window_tag=ident.get("window_tag") or "",
            persona_id=ident.get("persona_id") or "",
        )
        if not meta.get("enabled", True):
            yield event.plain_result("跨会话衔接已在配置里关闭。")
            return
        detail = (
            f"目标会话 {meta.get('target') or '-'} · 条数 {meta.get('items', 0)} · "
            f"字数 {meta.get('chars', 0)} · 因方向被跳过 {meta.get('skipped_direction', 0)}"
        )
        if not block:
            yield event.plain_result(f"没有可衔接内容。\n{detail}")
            return
        yield event.plain_result(f"{block}\n\n（{detail}）")

    @stype.command("groups")
    async def cmd_groups(self, event: AstrMessageEvent):
        """列出已知群与编号（指派发言选目标用）"""
        rows = self.service.speak_groups()
        if not rows:
            yield event.plain_result("还没有记录到任何群（先在群里说句话）。")
            return
        default = self.service.speak_default_umo()
        lines = ["已知群（按最近活跃排序）："]
        for index, row in enumerate(rows, 1):
            window = str(row.get("window_tag") or "")
            mark = " ← 默认" if window == default else ""
            lines.append(f"{index}. {group_label(window)}（{row.get('count', 0)} 条）{mark}")
        lines.append("用法：/stype default <群号 或 序号> 设置默认群；/stype default clear 清除。")
        yield event.plain_result("\n".join(lines))

    @stype.command("default")
    async def cmd_default(self, event: AstrMessageEvent, target: str = ""):
        """设置默认群：/stype default <群号 或 序号>"""
        value = (target or "").strip() or self._rest_after(event, "default")
        if not value:
            yield event.plain_result("用法：/stype default <群号 或 序号>（/stype groups 查看）")
            return
        if not (self.service.is_owner_event(event) or event.is_admin()):
            yield event.plain_result("只有主人或管理员能设置默认群。")
            return
        yield event.plain_result(self.service.set_speak_default(value))

    @stype.command("flow")
    async def cmd_flow(self, event: AstrMessageEvent):
        """预览窗口全流上下文（其他窗口最近消息流，含群成员与 Bot）"""
        ident = await self._ident(event)
        block, meta = self.service.window_flow_for(
            event.message_str or "",
            window_tag=ident.get("window_tag") or "",
            persona_id=ident.get("persona_id") or "",
            force=True,
        )
        if not meta.get("enabled", True):
            yield event.plain_result("窗口全流上下文已在配置里关闭。")
            return
        detail = (
            f"窗口 {meta.get('windows', 0)} 个 · 条数 {meta.get('items', 0)} · 字符 {meta.get('chars', 0)}"
        )
        if not block:
            yield event.plain_result(f"没有可用的窗口全流。\n{detail}")
            return
        yield event.plain_result(f"{block}\n\n（{detail}）")

    @stype.command("explain")
    async def cmd_explain(self, event: AstrMessageEvent):
        """解释召回路径和过滤原因"""
        keyword = self._rest_after(event, "explain")
        if not keyword:
            yield event.plain_result("用法: /stype explain <关键词>")
            return
        ident = await self._ident(event)
        result = await self.service.retrieve_for(
            keyword,
            ident["speaker_id"],
            persona_id=ident.get("persona_id") or "",
            window_tag=ident.get("window_tag") or "",
        )
        lines = [
            f"route={result.route} path={result.path} cache={result.cache}",
            f"core={len(result.core)} related={len(result.related)} uncertain={len(result.uncertain)} blocked={len(result.blocked)}",
        ]
        for f in result.core + result.related:
            lines.append(f"hit {f.id} {f.attribute} {clip(f.content, 60)}")
        for h in result.blocked[:6]:
            lines.append(f"block {h.fact.id} {h.filter_reason}")
        yield event.plain_result("\n".join(lines))

    @stype.command("add")
    async def cmd_add(self, event: AstrMessageEvent):
        """手动写入当前说话人事实"""
        msg = event.message_str or ""
        idx = msg.lower().find("add")
        content = msg[idx + 3 :].strip() if idx >= 0 else msg.strip()
        if not content:
            yield event.plain_result("用法: /stype add <事实>")
            return
        ident = await self._ident(event)
        result = self.service.remember(ident, content)
        yield event.plain_result(f"写入结果: {result}")

    @stype.command("recent")
    async def cmd_recent(self, event: AstrMessageEvent, n: int = 8):
        """最近时间线"""
        ident = await self._ident(event)
        rows = self.store.timeline_recent(limit=n, speaker_id=ident["speaker_id"])
        if not rows:
            yield event.plain_result("时间线为空。")
            return
        lines = [f"{r.id} {r.role} {clip(r.content, 60)}" for r in rows]
        yield event.plain_result("\n".join(lines))

    @stype.command("events")
    async def cmd_events(self, event: AstrMessageEvent, n: int = 5):
        """最近事件（整件事记忆）"""
        ident = await self._ident(event)
        window = ident.get("window_tag") or ""
        events = self.store.live_events(
            limit=max(1, min(int(n or 5), 20)),
            window_tag=window or None,
            persona_id=ident.get("persona_id") or "",
        )
        if not events:
            yield event.plain_result("还没有事件记忆。聊一段后会自动整理；也可以 /stype extract 手动触发。")
            return
        lines = []
        for item in events:
            when = fmt_ts(item.start_ts)
            flag = "（待审）" if item.review_status == "needs_review" else ""
            lines.append(f"{item.id} {when} 【{item.title}】{item.summary}{flag}")
        yield event.plain_result("\n".join(lines))

    @stype.command("history")
    async def cmd_history(self, event: AstrMessageEvent):
        """看某段时间的状态：/stype history 去年12月 或者 /stype history 以前喜欢什么"""
        keyword = self._rest_after(event, "history").strip()
        ident = await self._ident(event)
        result = await self.service.retrieve_for(
            keyword or "以前",
            ident["speaker_id"],
            persona_id=ident.get("persona_id") or "",
            window_tag=ident.get("window_tag") or "",
        )
        lines = [f"路线 {result.route}" + (f"｜范围 {result.history_label}" if result.history_label else "")]
        for fact in result.history[:8]:
            current = (result.history_current or {}).get(fact.id, "")
            line = f"{fact.id} [{fmt_ts(fact.created_at)}~{fmt_ts(fact.updated_at)}] {fact.plain or fact.value}"
            if current:
                line += f"（现在：{current}）"
            lines.append(line)
        for item in result.events[:3]:
            lines.append(f"事件 {item.id} [{fmt_ts(item.start_ts)}] 【{item.title}】{item.summary}")
        if len(lines) == 1:
            lines.append("没有找到那段时间的记忆。换一个时间说法，或先让插件多整理几轮。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("supersede")
    async def cmd_supersede(self, event: AstrMessageEvent, pending_id: int):
        """确认一条待覆盖"""
        result = self.service.contradiction.confirm_pending(pending_id)
        yield event.plain_result(str(result))

    @stype.command("rollback")
    async def cmd_rollback(self, event: AstrMessageEvent, fact_id: int):
        """回滚一条覆盖，恢复被归档的旧事实"""
        result = self.service.contradiction.rollback(fact_id)
        yield event.plain_result(str(result))

    @stype.command("diagnostics")
    async def cmd_diagnostics(self, event: AstrMessageEvent):
        """诊断：共存、检索、库规模"""
        ov = self.service.overview()
        yield event.plain_result(str(ov))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("alias")
    async def cmd_alias(self, event: AstrMessageEvent, alias: str, canonical: str):
        """把说话人 id 归并到稳定 id：/stype alias 旧id 主id"""
        moved = self.store.reassign_speaker(alias, canonical)
        yield event.plain_result(f"已归并 {alias} -> {canonical}（迁移事实 {moved} 条，同槽冲突已自动消解）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("aliases")
    async def cmd_aliases(self, event: AstrMessageEvent):
        """列出已映射别名，以及同名不同 id 的建议（不会自动合并）"""
        mapped = self.store.list_aliases()
        suggestions = self.service.alias_suggestions()
        lines = ["已映射:"]
        if mapped:
            lines.extend(f"  {a['alias']} -> {a['canonical_id']}" for a in mapped)
        else:
            lines.append("  （无）")
        lines.append("建议（需手动 /stype alias 旧id 主id）:")
        if suggestions:
            lines.extend(
                f"  {s['alias']} -> {s['canonical_id']} （同名 {s['name']}，{s['alias_count']}/{s['canonical_count']}）"
                for s in suggestions
            )
        else:
            lines.append("  （无）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("reviews")
    async def cmd_reviews(self, event: AstrMessageEvent, kind: str = ""):
        """列出待审学习项"""
        items = self.store.list_reviews("pending", kind=kind or None, limit=12)
        if not items:
            yield event.plain_result("没有待审学习项。")
            return
        lines = [f"{r.id} [{r.kind}] Q{(r.payload or {}).get('quality', 0)} {r.title} · {r.reason}" for r in items]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("approve")
    async def cmd_approve(self, event: AstrMessageEvent, review_id: int):
        """批准一条学习草稿"""
        yield event.plain_result(str(self.service.learning.set_status(review_id, "approved")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("reject")
    async def cmd_reject(self, event: AstrMessageEvent, review_id: int):
        """驳回一条学习草稿"""
        yield event.plain_result(str(self.service.learning.set_status(review_id, "rejected")))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("learn")
    async def cmd_learn(self, event: AstrMessageEvent):
        """立刻跑一轮黑话/few-shot/人格草稿学习"""
        result = await self.service.run_learning(force=True)
        yield event.plain_result(str(result))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("pending")
    async def cmd_pending(self, event: AstrMessageEvent):
        """列出待审记忆"""
        items = self.store.list_memory_reviews("pending", limit=12)
        if not items:
            yield event.plain_result("没有待审记忆。")
            return
        lines = [f"#{r.id} [{r.scope}] {clip(r.plain or r.raw_text, 60)}（{r.speaker_name or r.speaker_id}）" for r in items]
        lines.append("用 /stype pass <id> 通过，/stype drop <id> 删除。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("pass")
    async def cmd_pass(self, event: AstrMessageEvent, review_id: int):
        """通过一条待审记忆"""
        yield event.plain_result(self.service.resolve_memory_review(review_id, True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("drop")
    async def cmd_drop(self, event: AstrMessageEvent, review_id: int):
        """删除一条待审记忆"""
        yield event.plain_result(self.service.resolve_memory_review(review_id, False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("export")
    async def cmd_export(self, event: AstrMessageEvent):
        """导出 JSONL 档案"""
        dest = self.data_dir / "exports" / f"savagetype-{self.store.revision()}.jsonl"
        path = self.service.export_jsonl(dest)
        yield event.plain_result(f"已导出: {path}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("import")
    async def cmd_import(self, event: AstrMessageEvent):
        """预览或导入 JSONL：/stype import 预览 <路径> 或 /stype import 确认 <路径>"""
        rest = self._rest_after(event, "import")
        parts = rest.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法: /stype import 预览 <jsonl路径> 或 /stype import 确认 <jsonl路径>")
            return
        action, raw_path = parts[0], parts[1].strip().strip('"')
        path = Path(raw_path)
        if not path.is_file():
            yield event.plain_result(f"找不到文件: {path}")
            return
        if action in {"预览", "preview"}:
            yield event.plain_result(str(self.service.preview_archive(path)))
            return
        if action in {"确认", "run", "导入"}:
            result = self.service.import_archive(path, self.data_dir / "backups")
            yield event.plain_result(str(result))
            return
        yield event.plain_result("用法: /stype import 预览|确认 <jsonl路径>")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("sleep")
    async def cmd_sleep(self, event: AstrMessageEvent):
        """睡眠维护：近重合并、时间线压缩、低价值归档"""
        yield event.plain_result(str(self.service.sleep_maintenance()))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @stype.command("microscope")
    async def cmd_microscope(self, event: AstrMessageEvent, n: int = 3):
        """查看最近注入快照：路由、选中事实、过滤原因"""
        items = self.store.recent_diag(max(1, min(n, 10)))
        injects = [x for x in items if x.get("kind") == "inject"][: max(1, min(n, 8))]
        if not injects:
            yield event.plain_result("还没有注入记录。")
            return
        lines = []
        for item in injects:
            p = item.get("payload") or {}
            lines.append(
                f"#{item.get('id')} route={p.get('route')} path={p.get('path')} "
                f"core={p.get('core')} related={p.get('related')} blocked={p.get('blocked')} "
                f"chars={p.get('pack_chars')} q={p.get('query')}"
            )
        yield event.plain_result("\n".join(lines))

    @stype.command("extract")
    async def cmd_extract(self, event: AstrMessageEvent):
        """立刻抽取未总结时间线"""
        result = await self.service.maybe_extract(force=True)
        yield event.plain_result(str(result))

    @filter.llm_tool(name="savagetype_recall")
    async def tool_recall(self, event: AstrMessageEvent, query: str) -> str:
        """检索当前说话人可见的长期事实。

        Args:
            query(string): 要回忆的问题或关键词
        """
        ident = await self._ident(event)
        result = await self.service.retrieve_for(
            query,
            ident["speaker_id"],
            persona_id=ident.get("persona_id") or "",
            window_tag=ident.get("window_tag") or "",
        )
        facts = result.core + result.related + result.uncertain
        events = list(getattr(result, "events", None) or [])
        if not facts and not events:
            return "没有找到直接相关的 live 事实或事件。"
        lines = [f"事实 {f.id}: {clip(f.content, 80)}" for f in facts[:8]]
        for item in events[:3]:
            lines.append(f"事件 {item.id}: [{item.title}] {clip(item.summary, 120)}")
        return "相关记忆：\n" + "\n".join(lines)

    @filter.llm_tool(name="savagetype_remember")
    async def tool_remember(self, event: AstrMessageEvent, content: str) -> str:
        """在用户明确要求或长期价值明显时写入事实。只有返回 ok 才算记住。

        Args:
            content(string): 要记住的稳定事实
        """
        ident = await self._ident(event)
        if not self.service.is_owner_event(event):
            return "ok=false action=denied reason=owner_only"
        result = self.service.remember(ident, content)
        if result.get("action") in {"insert", "refresh", "supersede", "wrote_uncertain"}:
            return f"ok=true action={result.get('action')} fact_id={result.get('fact_id')}"
        return f"ok=false action={result.get('action')} reason={result.get('reason')}"

    @filter.llm_tool(name="savagetype_navigate")
    async def tool_navigate(self, event: AstrMessageEvent, query: str, fact_id: int = 0) -> str:
        """普通召回不够时，按线索再跳一两步。最多 3 步，每步最多 6 条。

        Args:
            query(string): 下一步要找的人物、主题或时间线索
            fact_id(number): 可选，从上一条记忆 id 继续跳
        """
        ident = await self._ident(event)
        result = await self.service.navigate(
            query=query,
            speaker_id=ident["speaker_id"],
            persona_id=ident.get("persona_id") or "",
            fact_id=int(fact_id or 0),
            window_tag=ident.get("window_tag") or "",
        )
        lines = []
        for step in result.get("steps") or []:
            lines.append(f"step {step['step']}: {clip(step.get('query') or '', 40)}")
            for hit in step.get("hits") or []:
                lines.append(f"  {hit['id']} [{hit.get('attribute')}] {hit.get('content')}")
        return "\n".join(lines) or "没有更多可见证据。"

    def _append_pack(self, req: ProviderRequest, pack: str) -> None:
        part = TextPart(text=pack)
        if hasattr(part, "mark_as_temp"):
            part.mark_as_temp()
        extra = getattr(req, "extra_user_content_parts", None)
        if extra is not None:
            extra.append(part)
            return
        if req.prompt:
            req.prompt = f"{pack}\n\n{req.prompt}"
        else:
            req.prompt = pack

    def _message_components(self, event: AstrMessageEvent) -> list:
        try:
            return list(getattr(getattr(event, "message_obj", None), "message", None) or [])
        except Exception:
            return []

    def _has_image(self, event: AstrMessageEvent) -> bool:
        return any(
            "image" in type(comp).__name__.lower() for comp in self._message_components(event)
        )

    def _image_caption_from_event(self, event: AstrMessageEvent) -> str:
        """Caption that AstrBot already produced for an image, if present on the component."""
        parts: list[str] = []
        for comp in self._message_components(event):
            if "image" not in type(comp).__name__.lower():
                continue
            caption = getattr(comp, "text", None)
            if caption:
                parts.append(str(caption).strip())
        return " ".join(p for p in parts if p)

    def _image_urls(self, event: AstrMessageEvent) -> tuple[list[str], str]:
        urls: list[str] = []
        used = ""
        for comp in self._message_components(event):
            if "image" not in type(comp).__name__.lower():
                continue
            for attr in ("url", "file", "path", "base64", "image_url"):
                value = getattr(comp, attr, None)
                if callable(value):
                    try:
                        value = value()
                    except Exception:
                        value = None
                if not value:
                    continue
                text = str(value)
                if attr in {"file", "path"} or text.startswith("file://"):
                    path_text = text[7:] if text.startswith("file://") else text
                    path_text = urllib.parse.unquote(path_text)
                    if re.match(r"^/[A-Za-z]:", path_text):
                        path_text = path_text[1:]
                    text = self._local_image_data_url(path_text) or text
                elif attr == "base64" and not text.startswith("data:"):
                    text = f"data:image/png;base64,{text}"
                urls.append(text)
                used = attr
                break
        return urls, used

    @staticmethod
    def _local_image_data_url(raw: str) -> str:
        """Convert a local image path to a data URL so vision providers can read it."""
        try:
            path = Path(raw)
            if not path.is_file():
                return ""
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        except Exception:
            return ""

    def _caption_timeout(self) -> int:
        raw = self.config.get("image_caption_timeout_seconds")
        return 30 if raw is None else int(raw)

    async def _caption_via_provider(self, event: AstrMessageEvent, provider_id: str) -> str:
        def note(kind: str, payload: dict) -> None:
            try:
                self.store.add_diag(kind, payload)
            except Exception:
                pass

        timeout = self._caption_timeout()
        if timeout <= 0:
            note("image_caption_skip", {"reason": "timeout_disabled"})
            return ""
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            note("image_caption_fail", {"error": f"provider_not_found: {provider_id}"[:200]})
            return ""
        urls, used = self._image_urls(event)
        if not urls:
            note("image_caption_fail", {"error": "no_image_url"})
            return ""
        call = getattr(provider, "text_chat", None)
        if not callable(call):
            note("image_caption_fail", {"error": "provider_has_no_text_chat"})
            return ""
        prompt = "用一句中文客观描述这张图片，不要推测。"
        decision = self.service.llm_guard().check("image", prompt)
        if not decision.allowed:
            self.store.add_usage(
                "llm", provider_id, ok=False, task="image",
                source="explicit:image_caption_provider_id", reason=decision.reason,
            )
            note("image_caption_skip", {"reason": decision.reason})
            return ""
        result = call(prompt=prompt, image_urls=urls)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=timeout)
        text = getattr(result, "completion_text", "") or ""
        text = str(text).strip()
        if text:
            note("image_caption_ok", {"field": used, "chars": len(text)})
            self.store.add_usage(
                "llm", provider_id, True, len(prompt), len(text),
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(text),
                task="image", source="explicit:image_caption_provider_id",
            )
        else:
            note("image_caption_fail", {"error": "empty_response"})
        return text

    async def _image_text(self, event: AstrMessageEvent) -> str:
        """Turn an image message into text. Placeholder alone if no caption is available."""
        caption = self._image_caption_from_event(event)
        if not self._has_image(event):
            return ""
        if not self.service.capture_ok(event) and not self.service.inject_ok(event):
            return ""
        if not caption:
            provider_id = str(self.config.get("image_caption_provider_id") or "").strip()
            if not provider_id:
                try:
                    now = now_ts()
                    last = int(self.store.get_meta("image_caption_skip_at") or "0")
                    if now - last >= 60:
                        self.store.set_meta("image_caption_skip_at", str(now))
                        self.store.add_diag("image_caption_skip", {"reason": "no_provider"})
                except Exception:
                    pass
            if provider_id:
                try:
                    caption = await self._caption_via_provider(event, provider_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Savage Type image caption failed: %s", exc)
                    try:
                        self.store.add_diag(
                            "image_caption_fail",
                            {"error": f"{type(exc).__name__}: {exc}"[:200], "timeout": self._caption_timeout()},
                        )
                    except Exception:
                        pass
        return f"[图片] {caption}".strip() if caption else "[图片]"

    async def _persona_id(self, event: AstrMessageEvent) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        try:
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if cid:
                conv = await conv_mgr.get_conversation(umo, cid)
                persona = getattr(conv, "persona_id", None) or ""
                if persona:
                    return str(persona)
        except Exception:
            pass
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            if isinstance(persona, dict):
                return str(persona.get("name") or "")
            return str(getattr(persona, "name", "") or "")
        except Exception:
            return ""

    async def _persona_text(self, persona_id: str = "") -> str:
        try:
            mgr = self.context.persona_manager
            if persona_id:
                try:
                    persona = await mgr.get_persona(persona_id)
                except Exception:
                    persona = None
                if persona is not None:
                    return str(getattr(persona, "system_prompt", "") or "")[:800]
            persona = await mgr.get_default_persona_v3(None)
            if isinstance(persona, dict):
                return str(persona.get("prompt") or persona.get("system_prompt") or "")[:800]
            return str(getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "") or "")[:800]
        except Exception:
            return ""

    async def _ident(self, event: AstrMessageEvent) -> dict:
        cached = event.get_extra("_stype_ident")
        if cached:
            return cached
        persona_id = await self._persona_id(event)
        ident = self.service.identity_from_event(event, persona_id=persona_id)
        event.set_extra("_stype_ident", ident)
        return ident

    async def _llm_generate(self, prompt: str, provider_id: str) -> str:
        pid = (provider_id or "").strip()
        if not pid:
            try:
                pid = await self.context.get_current_chat_provider_id("")
            except Exception:
                providers = self.context.get_all_providers()
                if not providers:
                    raise RuntimeError("no chat provider for fact extract")
                pid = providers[0].meta().id
        resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt)
        text = getattr(resp, "completion_text", "") or ""
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "input", 0) or 0) if usage is not None else 0
        tokens_out = int(getattr(usage, "output", 0) or 0) if usage is not None else 0
        return text, tokens_in, tokens_out

    def _get_special_provider(self, kind: str, provider_id: str):
        if kind == "embedding":
            if provider_id:
                prov = self.context.get_provider_by_id(provider_id)
                if isinstance(prov, EmbeddingProvider):
                    return prov
            items = self.context.get_all_embedding_providers()
            return items[0] if items else None
        if kind == "rerank":
            if provider_id:
                prov = self.context.get_provider_by_id(provider_id)
                if isinstance(prov, RerankProvider):
                    return prov
            inst_map = getattr(self.context.provider_manager, "inst_map", {}) or {}
            for prov in inst_map.values():
                if isinstance(prov, RerankProvider):
                    return prov
        return None

    async def page_overview(self):
        return json_response(self.service.overview())

    async def page_search(self):
        keyword = request.query.get("q", "")
        speaker_id = request.query.get("speaker_id", "") or None
        k = request.query.get("k", 12, type=int)
        canonical = self.store.resolve_speaker(speaker_id) if speaker_id else None
        ids = self.store.speaker_ids_for(canonical) if canonical else None
        facts = self.store.search_facts(keyword, speaker_id=canonical, limit=k, speaker_ids=ids)
        return json_response({"items": [self._fact_view(f) for f in facts]})

    async def page_facts(self):
        status = request.query.get("status", "live")
        scope = request.query.get("scope", "") or ""
        facts = self.store.facts_by_status(status, limit=200)
        if scope:
            facts = [f for f in facts if (f.scope or "") == scope]
        return json_response({"items": [self._fact_view(f) for f in facts]})

    async def page_memory(self):
        q = (request.query.get("q", "") or "").strip()
        facts = self.store.owner_facts(limit=200)
        if q:
            needle = q.lower()
            facts = [
                f
                for f in facts
                if needle in (f.plain or f.content or "").lower()
                or needle in (f.speaker_name or "").lower()
                or needle in (f.value or "").lower()
                or needle in " ".join(str(k) for k in (getattr(f, "keywords", None) or [])).lower()
            ]
        overview = self.service.overview()
        return json_response(
            {
                "items": [self._fact_view(f) for f in facts],
                "owner": overview.get("owner", {}),
                "data_dir": overview.get("data_dir", ""),
            }
        )

    async def page_memory_bot(self):
        facts = self.store.person_facts("bot_self", limit=200)
        return json_response({"items": [self._fact_view(f) for f in facts]})

    def _memory_review_view(self, r) -> dict:
        return {
            "id": r.id,
            "scope": r.scope,
            "speaker_id": r.speaker_id,
            "speaker_name": r.speaker_name,
            "platform": r.platform,
            "window_tag": r.window_tag,
            "source_event_id": r.source_event_id,
            "raw_text": r.raw_text,
            "plain": r.plain,
            "keywords": r.keywords,
            "payload": r.payload,
            "status": r.status,
            "attempts": r.attempts,
            "trace": r.trace,
            "notified_at": r.notified_at,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    async def page_memory_pending(self):
        items = self.store.list_memory_reviews("pending", limit=80)
        return json_response({"items": [self._memory_review_view(r) for r in items]})

    async def page_memory_review(self):
        payload = await request.json(default={})
        review_id = int(payload.get("id") or 0)
        if not review_id:
            return error_response("missing id", status_code=400)
        status = str(payload.get("status") or "").strip().lower()
        approve = status in {"approved", "approve", "pass", "yes"}
        reject = status in {"rejected", "reject", "delete", "no"}
        if not approve and not reject:
            return error_response("bad status", status_code=400)
        plain = str(payload.get("plain") or "").strip()
        if plain:
            item = self.store.get_memory_review(review_id)
            if item and item.status == "pending":
                new_payload = dict(item.payload or {})
                new_payload["plain"] = plain
                self.store.update_memory_review(review_id, plain=plain, payload=new_payload)
        return json_response(
            {"ok": True, "message": self.service.resolve_memory_review(review_id, approve)}
        )

    def _profile_view(self, profile) -> dict:
        if profile is None:
            return {}
        return {
            "speaker_id": profile.speaker_id,
            "speaker_name": profile.speaker_name,
            "platform": profile.platform,
            "is_owner": profile.is_owner,
            "note": profile.note,
            "first_seen": profile.first_seen,
            "last_seen": profile.last_seen,
            "seen_count": profile.seen_count,
            "fact_count": profile.fact_count,
        }

    async def page_profiles(self):
        # 主人身份与人物档案分开：人物档案列表不展示主人。
        items = [p for p in self.store.list_profiles(limit=300) if not p.is_owner]
        return json_response({"items": [self._profile_view(p) for p in items]})

    async def page_profile(self):
        speaker_id = (request.query.get("speaker_id", "") or "").strip()
        if not speaker_id:
            return error_response("missing speaker_id", status_code=400)
        canonical = self.store.resolve_speaker(speaker_id)
        ids = self.store.speaker_ids_for(canonical)
        profile = self.store.get_profile(canonical)
        facts = self.store.person_facts(canonical, limit=200, speaker_ids=ids)
        return json_response(
            {
                "profile": self._profile_view(profile),
                "items": [self._fact_view(f) for f in facts],
            }
        )

    async def page_profile_update(self):
        payload = await request.json(default={})
        speaker_id = str(payload.get("speaker_id") or "").strip()
        if not speaker_id:
            return error_response("missing speaker_id", status_code=400)
        updated = self.store.update_profile(
            speaker_id,
            speaker_name=payload.get("speaker_name"),
            note=payload.get("note"),
        )
        if not updated:
            return error_response("profile not found", status_code=404)
        return json_response({"ok": True, "profile": self._profile_view(self.store.get_profile(speaker_id))})

    async def page_fact_update(self):
        payload = await request.json(default={})
        fact_id = int(payload.get("id") or 0)
        if not fact_id:
            return error_response("missing id", status_code=400)
        fact = self.store.get_fact(fact_id)
        if fact is None:
            return error_response("fact not found", status_code=404)
        fields = {}
        if "plain" in payload:
            fields["plain"] = str(payload.get("plain") or "")
        if "value" in payload:
            fields["value"] = str(payload.get("value") or "")
        if "content" in payload:
            fields["content"] = str(payload.get("content") or "")
        if "keywords" in payload:
            fields["keywords"] = payload.get("keywords") or []
        if "importance" in payload:
            try:
                importance = float(payload.get("importance"))
            except (TypeError, ValueError):
                return error_response("bad importance", status_code=400)
            fields["importance"] = max(0.0, min(1.0, importance))
        if fields:
            normalized = apply_slot(
                {
                    "subject": fact.subject,
                    "attribute": fact.attribute,
                    "value": fields.get("value", fact.value),
                    "content": fields.get("content", fact.content),
                    "speaker_id": fact.speaker_id,
                    "speaker_name": fact.speaker_name,
                    "persona_id": fact.persona_id,
                    "topic": getattr(fact, "topic", ""),
                }
            )
            fields["attribute"] = normalized["attribute"]
            fields["value"] = normalized["value"]
            fields["kind"] = normalized["kind"]
            fields["topic"] = normalized.get("topic", "")
            new_value = str(normalized["value"])
            if new_value != fact.value or not fact.slot_key_value:
                base_key = make_slot_key(
                    fact.persona_id or "",
                    fact.speaker_id,
                    normalized["subject"],
                    normalized["attribute"],
                    new_value,
                )
                old_base = make_slot_key(
                    fact.persona_id or "",
                    fact.speaker_id,
                    fact.subject,
                    fact.attribute,
                    fact.value,
                )
                if (
                    fact.slot_key_value
                    and fact.slot_key_value != old_base
                    and fields.get("topic")
                ):
                    # 这条在「同词不同义」的分槽里，改值也要保持分槽，别撞回基础槽。
                    base_key = f"{base_key}|{fields['topic']}"
                fields["slot_key"] = base_key
            fields["edited_at"] = now_ts()
            fields["edited_by"] = "ui"
            fields["review_status"] = "manual"
            self.store.update_fact(fact_id, **fields)
            self.store.resolve_slot_conflicts(fact.speaker_id)
        return json_response({"ok": True, "fact": self._fact_view(self.store.get_fact(fact_id))})

    async def page_reset(self):
        payload = await request.json(default={})
        if str(payload.get("confirm") or "") != "reset":
            return error_response("missing confirm=reset", status_code=400)
        backup = self.service.backup_now(self.data_dir / "backups")
        counts = self.store.clear_dirty_v280()
        self.service.clear_runtime_caches()
        self.store.add_diag("clean_rebuild", {"backup": str(backup), "cleared": counts})
        return json_response({"ok": True, "backup": str(backup), "cleared": counts})

    def _provider_entry(self, provider) -> dict:
        pid = ""
        model = ""
        try:
            meta = provider.meta()
            pid = str(getattr(meta, "id", "") or "")
            model = str(getattr(meta, "model", "") or "")
        except Exception:
            pid = ""
        return {"id": pid, "model": model, "name": f"{pid} · {model}".strip(" ·") if (pid or model) else "未命名"}

    def page_providers_sync(self) -> dict:
        def collect(providers) -> list[dict]:
            out: list[dict] = []
            seen: set[str] = set()
            for provider in providers or []:
                entry = self._provider_entry(provider)
                key = entry.get("id") or entry.get("name") or ""
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(entry)
            return out

        chat: list[dict] = []
        embedding: list[dict] = []
        rerank: list[dict] = []
        try:
            chat = collect(self.context.get_all_providers())
        except Exception:
            pass
        try:
            manager = self.context.provider_manager
            insts = getattr(manager, "embedding_provider_insts", None)
            if insts is None:
                insts = self.context.get_all_embedding_providers()
            embedding = collect(insts)
        except Exception:
            pass
        try:
            manager = self.context.provider_manager
            insts = getattr(manager, "rerank_provider_insts", None)
            if insts:
                rerank = collect(insts)
            else:
                inst_map = getattr(manager, "inst_map", {}) or {}
                rerank = collect([p for p in inst_map.values() if isinstance(p, RerankProvider)])
        except Exception:
            pass
        return {"chat": chat, "embedding": embedding, "rerank": rerank}

    async def page_providers(self):
        return json_response(self.page_providers_sync())

    async def page_theme_set(self):
        payload = await request.json(default={})
        color = str(payload.get("color") or "").strip()
        color2 = str(payload.get("color2") or "").strip()
        color3 = str(payload.get("color3") or "").strip()
        hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
        if not hex_re.match(color):
            return error_response("bad color", status_code=400)
        self.config["ui_theme_color"] = color.lower()
        for value, key, label in ((color2, "ui_theme_color2", "color2"), (color3, "ui_theme_color3", "color3")):
            if not value:
                continue
            if not hex_re.match(value):
                return error_response(f"bad {label}", status_code=400)
            self.config[key] = value.lower()
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        self.service.apply_config()
        return json_response(
            {
                "ok": True,
                "color": self.config["ui_theme_color"],
                "color2": self.config.get("ui_theme_color2", ""),
                "color3": self.config.get("ui_theme_color3", ""),
            }
        )

    async def page_dynamic_set(self):
        payload = await request.json(default={})
        raw = payload.get("enabled", True)
        if isinstance(raw, str):
            enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(raw)
        self.config["ui_dynamic_colors"] = enabled
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        self.service.apply_config()
        return json_response({"ok": True, "enabled": enabled})

    @staticmethod
    def _pending_new_view(payload: dict) -> dict:
        def as_int(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_float(value, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        evidence = []
        for item in payload.get("evidence") or []:
            text = str(item)
            if text.lstrip("-").isdigit():
                evidence.append(int(text))
        return {
            "subject": str(payload.get("subject") or ""),
            "attribute": str(payload.get("attribute") or ""),
            "value": str(payload.get("value") or ""),
            "plain": str(payload.get("plain") or ""),
            "content": str(payload.get("content") or ""),
            "topic": str(payload.get("topic") or ""),
            "speaker_id": str(payload.get("speaker_id") or ""),
            "speaker_name": str(payload.get("speaker_name") or ""),
            "window_tag": str(payload.get("window_tag") or ""),
            "confidence": as_float(payload.get("confidence")),
            "evidence": evidence[:8],
            "source_event_id": as_int(payload.get("source_event_id")),
            "first_person": as_int(payload.get("first_person")),
            "explicit_correction": as_int(payload.get("explicit_correction")),
        }

    async def page_pending(self):
        items = self.store.pending_open(80)
        out = []
        for p in items:
            old = self.store.get_fact(p.old_fact_id) if p.old_fact_id else None
            out.append(
                {
                    "id": p.id,
                    "old_fact_id": p.old_fact_id,
                    "reason": p.reason,
                    "created_at": p.created_at,
                    "old_fact": self._fact_view(old) if old else None,
                    "new_fact": self._pending_new_view(dict(p.new_payload or {})),
                }
            )
        return json_response({"items": out})

    async def page_pending_confirm(self):
        payload = await request.json(default={})
        pending_id = int(payload.get("id") or 0)
        if not pending_id:
            return error_response("missing id", status_code=400)
        return json_response(self.service.contradiction.confirm_pending(pending_id))

    async def page_pending_reject(self):
        payload = await request.json(default={})
        pending_id = int(payload.get("id") or 0)
        if not pending_id:
            return error_response("missing id", status_code=400)
        return json_response(self.service.contradiction.reject_pending(pending_id))

    async def page_rollback(self):
        payload = await request.json(default={})
        fact_id = int(payload.get("id") or 0)
        if not fact_id:
            return error_response("missing id", status_code=400)
        return json_response(self.service.contradiction.rollback(fact_id))

    async def page_remember(self):
        payload = await request.json(default={})
        content = str(payload.get("content") or "").strip()
        if not content:
            return error_response("missing content", status_code=400)
        speaker_id = str(payload.get("speaker_id") or "admin").strip() or "admin"
        speaker = {
            "speaker_id": speaker_id,
            # 不给名字时留空：由 service 回退到已有档案昵称，避免 QQ 号覆盖昵称。
            "speaker_name": str(payload.get("speaker_name") or ""),
            "bot_id": "",
            "window_tag": "console",
            "persona_id": str(payload.get("persona_id") or ""),
        }
        return json_response(self.service.remember(speaker, content, extra=payload))

    async def page_extract(self):
        return json_response(await self.service.maybe_extract(force=True))

    async def page_aliases(self):
        return json_response({"items": self.store.list_aliases()})

    async def page_alias_set(self):
        payload = await request.json(default={})
        alias = str(payload.get("alias") or "").strip()
        canonical = str(payload.get("canonical_id") or payload.get("canonical") or "").strip()
        if not alias or not canonical:
            return error_response("missing alias or canonical_id", status_code=400)
        moved = self.store.reassign_speaker(alias, canonical, str(payload.get("label") or ""))
        if moved == 0:
            # 没有可迁移的数据时也把别名写上，供检索归组。
            self.store.set_alias(alias, canonical, str(payload.get("label") or ""))
        return json_response({"ok": True, "alias": alias, "canonical_id": canonical, "moved": moved})

    def _review_view(self, r) -> dict:
        return {
            "id": r.id,
            "kind": r.kind,
            "status": r.status,
            "title": r.title,
            "reason": r.reason,
            "quality": int((r.payload or {}).get("quality") or 0),
            "speaker_id": r.speaker_id,
            "persona_id": r.persona_id,
            "payload": r.payload,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    async def page_reviews(self):
        status = request.query.get("status", "pending")
        kind = request.query.get("kind", "") or None
        items = self.store.list_reviews(status=status, kind=kind, limit=80)
        return json_response({"items": [self._review_view(r) for r in items]})

    async def page_review_set(self):
        payload = await request.json(default={})
        status = str(payload.get("status") or "").strip()
        raw_ids = payload.get("ids")
        if isinstance(raw_ids, list) and raw_ids:
            results = []
            for raw in raw_ids[:200]:
                try:
                    review_id = int(raw)
                except (TypeError, ValueError):
                    continue
                if review_id:
                    results.append(self.service.learning.set_status(review_id, status))
            return json_response({"ok": True, "count": len(results), "results": results})
        review_id = int(payload.get("id") or 0)
        if not review_id:
            return error_response("missing id", status_code=400)
        return json_response(self.service.learning.set_status(review_id, status))

    async def page_learn(self):
        return json_response(await self.service.run_learning(force=True))

    async def page_sleep(self):
        return json_response(self.service.sleep_maintenance())

    async def page_diagnostics(self):
        return json_response({"items": self.store.recent_diag(30), "overview": self.service.overview()})

    async def page_microscope(self):
        n = request.query.get("n", 8, type=int)
        items = [x for x in self.store.recent_diag(40) if x.get("kind") == "inject"][: max(1, min(n, 20))]
        return json_response({"items": items})

    async def page_export(self):
        dest = self.data_dir / "exports" / f"savagetype-{self.store.revision()}.jsonl"
        path = self.service.export_jsonl(dest)
        return file_response(path, filename=path.name, content_type="application/json")

    async def page_archive_preview(self):
        payload = await request.json(default={})
        raw = str(payload.get("path") or "").strip()
        if not raw:
            return error_response("missing path", status_code=400)
        path = Path(raw)
        if not path.is_file():
            return error_response("file not found", status_code=400)
        return json_response(self.service.preview_archive(path))

    async def page_archive_import(self):
        payload = await request.json(default={})
        raw = str(payload.get("path") or "").strip()
        if not raw:
            return error_response("missing path", status_code=400)
        path = Path(raw)
        if not path.is_file():
            return error_response("file not found", status_code=400)
        return json_response(self.service.import_archive(path, self.data_dir / "backups"))

    async def page_chat_preview(self):
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("missing text", status_code=400)
        users = parse_csv(str(payload.get("user_names") or ""))
        bots = parse_csv(str(payload.get("bot_names") or ""))
        return json_response(self.service.preview_chat(text, user_names=users, bot_names=bots))

    async def page_chat_import(self):
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("missing text", status_code=400)
        users = parse_csv(str(payload.get("user_names") or ""))
        bots = parse_csv(str(payload.get("bot_names") or ""))
        return json_response(self.service.import_chat(text, user_names=users, bot_names=bots))

    def _schema(self) -> dict:
        if SCHEMA_PATH.is_file():
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return {}

    def _config_values(self) -> dict:
        schema = self._schema()
        out = {}
        for key, spec in schema.items():
            if isinstance(self.config, dict):
                out[key] = self.config.get(key, spec.get("default"))
            else:
                try:
                    out[key] = self.config.get(key, spec.get("default"))
                except Exception:
                    out[key] = spec.get("default")
        return out

    def _coerce_config_value(self, spec: dict, raw):
        typ = spec.get("type")
        if typ == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)
        if typ == "int":
            return int(raw)
        if typ == "float":
            return float(raw)
        if raw is None:
            return spec.get("default", "")
        return str(raw)

    async def page_config_get(self):
        schema = self._schema()
        return json_response({"schema": schema, "values": self._config_values()})

    async def page_config_save(self):
        payload = await request.json(default={})
        incoming = payload.get("values") if isinstance(payload.get("values"), dict) else payload
        schema = self._schema()
        saved = {}
        for key, spec in schema.items():
            if key not in incoming:
                continue
            try:
                value = self._coerce_config_value(spec, incoming[key])
            except (TypeError, ValueError):
                return error_response(f"bad value for {key}", status_code=400)
            options = spec.get("options")
            if options and value not in options:
                return error_response(f"{key} must be one of {options}", status_code=400)
            if key in {"ui_theme_color", "ui_theme_color2", "ui_theme_color3"}:
                if value and not re.match(r"^#[0-9a-fA-F]{6}$", str(value)):
                    return error_response(f"{key} must be hex color", status_code=400)
                value = str(value).lower()
            self.config[key] = value
            saved[key] = value
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        self.service.config = self.config
        self.service.apply_config()
        return json_response({"ok": True, "saved": saved, "values": self._config_values()})

    async def page_facts_archive(self):
        payload = await request.json(default={})
        ids = payload.get("ids") or payload.get("id")
        if ids is None:
            return error_response("missing ids", status_code=400)
        if not isinstance(ids, list):
            ids = [ids]
        return json_response(self.store.archive_facts(ids, reason="ui_delete"))

    def _event_view(self, e) -> dict:
        return {
            "id": e.id,
            "kind": e.kind,
            "title": e.title,
            "summary": e.summary,
            "highlights": e.highlights or [],
            "keywords": e.keywords or [],
            "participants": e.participants or [],
            "speaker_id": e.speaker_id,
            "speaker_name": e.speaker_name,
            "scope": e.scope,
            "persona_id": e.persona_id,
            "window_tag": e.window_tag,
            "status": e.status,
            "review_status": e.review_status,
            "importance": round(float(e.importance or 0), 3),
            "weight": self._fact_weight(e),
            "confidence": e.confidence,
            "pinned": int(e.pinned or 0),
            "access_count": int(e.access_count or 0),
            "start_ts": int(e.start_ts or 0),
            "end_ts": int(e.end_ts or 0),
            "evidence": list(e.evidence or []),
            "reason": e.reason,
            "edited_at": int(e.edited_at or 0),
            "created_at": int(e.created_at or 0),
            "updated_at": int(e.updated_at or 0),
        }

    async def page_events(self):
        status = request.query.get("status", "live") or "live"
        pinned_only = str(request.query.get("pinned", "") or "").lower() in {"1", "true", "yes"}
        keyword = str(request.query.get("q", "") or "").strip().lower()
        if status == "pinned":
            items = [
                e for e in self.store.events_for_review("live", limit=200)
                if int(e.pinned or 0)
            ]
        else:
            items = self.store.events_for_review(status, limit=200)
        if pinned_only:
            items = [e for e in items if int(e.pinned or 0)]
        if keyword:
            items = [
                e
                for e in items
                if keyword in (e.title or "").lower()
                or keyword in (e.summary or "").lower()
                or any(keyword in str(k).lower() for k in (e.keywords or []))
            ]
        return json_response({"items": [self._event_view(e) for e in items[:120]]})

    async def page_event_update(self):
        payload = await request.json(default={})
        event_id = int(payload.get("id") or 0)
        if not event_id:
            return error_response("missing id", status_code=400)
        event = self.store.get_event(event_id)
        if event is None:
            return error_response("event not found", status_code=404)
        fields: dict = {}
        if "title" in payload:
            title = str(payload.get("title") or "").strip()
            if not title:
                return error_response("title required", status_code=400)
            fields["title"] = clip(title, 60)
        if "summary" in payload:
            fields["summary"] = clip(str(payload.get("summary") or ""), 400)
        if "highlights" in payload:
            raw = payload.get("highlights")
            if isinstance(raw, str):
                raw = [line.strip() for line in raw.splitlines() if line.strip()]
            fields["highlights"] = [clip(str(x), 40) for x in (raw or [])][:4]
        if "keywords" in payload:
            raw = payload.get("keywords")
            if isinstance(raw, str):
                raw = [x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip()]
            fields["keywords"] = [clip(str(x), 16) for x in (raw or [])][:6]
        if "importance" in payload:
            try:
                importance = float(payload.get("importance"))
            except (TypeError, ValueError):
                return error_response("bad importance", status_code=400)
            fields["importance"] = max(0.0, min(1.0, importance))
        if "status" in payload:
            new_status = str(payload.get("status") or "").strip()
            if new_status not in {"live", "archived"}:
                return error_response("bad status", status_code=400)
            fields["status"] = new_status
        if str(payload.get("approve", "")).lower() in {"1", "true", "yes"}:
            fields["review_status"] = "manual"
            fields["confidence"] = max(float(event.confidence or 0), 0.8)
            fields["status"] = "live"
        if fields:
            fields["edited_at"] = now_ts()
            fields["edited_by"] = "ui"
            self.store.update_event(event_id, **fields)
        return json_response({"ok": True, "event": self._event_view(self.store.get_event(event_id))})

    async def page_event_pin(self):
        payload = await request.json(default={})
        event_id = int(payload.get("id") or 0)
        if not event_id:
            return error_response("missing id", status_code=400)
        raw_pinned = payload.get("pinned", True)
        if isinstance(raw_pinned, str):
            pinned = raw_pinned.strip().lower() in {"1", "true", "yes", "on"}
        else:
            pinned = bool(raw_pinned)
        if not self.store.set_event_pinned(event_id, pinned):
            return error_response("event not found", status_code=404)
        return json_response({"ok": True, "id": event_id, "pinned": int(pinned)})

    async def page_events_archive(self):
        payload = await request.json(default={})
        ids = payload.get("ids") or payload.get("id")
        if ids is None:
            return error_response("missing ids", status_code=400)
        if not isinstance(ids, list):
            ids = [ids]
        return json_response(self.store.archive_events(ids, reason="ui_delete"))

    async def page_events_restore(self):
        payload = await request.json(default={})
        ids = payload.get("ids") or payload.get("id")
        if ids is None:
            return error_response("missing ids", status_code=400)
        if not isinstance(ids, list):
            ids = [ids]
        return json_response(self.store.restore_events(ids))

    async def page_event_evidence(self):
        event_id = request.query.get("id", 0, type=int)
        if not event_id:
            return error_response("missing id", status_code=400)
        event = self.store.get_event(event_id)
        if event is None:
            return error_response("event not found", status_code=404)
        rows = self.store.timeline_by_ids(list(event.evidence or [])[:40])
        items = [
            {
                "id": r.id,
                "role": r.role,
                "speaker": r.speaker_name or r.speaker_id,
                "ts": r.ts,
                "content": clip(r.content, 500),
            }
            for r in rows
        ]
        return json_response({"items": items})

    async def page_entities(self):
        ref = str(request.query.get("ref", "fact") or "fact")
        ref_id = request.query.get("id", 0, type=int)
        if not ref_id:
            return error_response("missing id", status_code=400)
        if ref not in {"fact", "event"}:
            return error_response("bad ref", status_code=400)
        return json_response({"items": self.store.entities_for_ref(ref, ref_id)})

    async def page_facts_restore(self):
        payload = await request.json(default={})
        ids = payload.get("ids") or payload.get("id")
        if ids is None:
            return error_response("missing ids", status_code=400)
        if not isinstance(ids, list):
            ids = [ids]
        return json_response(self.store.restore_facts(ids))

    async def page_fact_pin(self):
        payload = await request.json(default={})
        fact_id = int(payload.get("id") or 0)
        if not fact_id:
            return error_response("missing id", status_code=400)
        raw_pinned = payload.get("pinned", True)
        if isinstance(raw_pinned, str):
            pinned = raw_pinned.strip().lower() in {"1", "true", "yes", "on"}
        else:
            pinned = bool(raw_pinned)
        if not self.store.set_pinned(fact_id, pinned):
            return error_response("fact not found", status_code=404)
        return json_response({"ok": True, "id": fact_id, "pinned": int(pinned)})

    async def page_dossiers(self):
        persona_id = request.query.get("persona_id", "") or ""
        return json_response({"items": self.service.list_dossiers(persona_id=persona_id)})

    async def page_dossier(self):
        speaker_id = request.query.get("speaker_id", "") or ""
        if not speaker_id:
            return error_response("missing speaker_id", status_code=400)
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(self.service.dossier_for(speaker_id, persona_id=persona_id))

    def _fact_weight(self, f) -> float:
        try:
            cfg = self.service.importance_cfg()
            return round(
                fact_weight(
                    f,
                    now_ts(),
                    cfg["half_life_days"],
                    cfg["reinforce_factor"],
                    cfg["max_multiplier"],
                ),
                3,
            )
        except Exception:
            return round(float(getattr(f, "importance", 0) or 0), 3)

    def _fact_view(self, f) -> dict:
        return {
            "id": f.id,
            "subject": f.subject,
            "attribute": f.attribute,
            "value": f.value,
            "content": f.content,
            "plain": getattr(f, "plain", ""),
            "keywords": getattr(f, "keywords", []),
            "scope": getattr(f, "scope", ""),
            "origin": getattr(f, "origin", ""),
            "review_status": getattr(f, "review_status", ""),
            "source_event_id": getattr(f, "source_event_id", 0),
            "edited_at": getattr(f, "edited_at", 0),
            "edited_by": getattr(f, "edited_by", ""),
            "importance": round(float(getattr(f, "importance", 0) or 0), 3),
            "weight": self._fact_weight(f),
            "kind": getattr(f, "kind", ""),
            "topic": getattr(f, "topic", ""),
            "pinned": int(getattr(f, "pinned", 0) or 0),
            "speaker_id": f.speaker_id,
            "speaker_name": f.speaker_name,
            "status": f.status,
            "confidence": f.confidence,
            "access_count": int(getattr(f, "access_count", 0) or 0),
            "mention_policy": f.mention_policy,
            "superseded_by": f.superseded_by,
            "supersedes": f.supersedes,
            "created_at": int(getattr(f, "created_at", 0) or 0),
            "updated_at": f.updated_at,
            "valid_to": int(f.updated_at) if f.status != "live" else 0,
            "reason": f.reason,
            "persona_id": getattr(f, "persona_id", ""),
            "slot_key": f.slot_key(),
        }
