from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_plugin_data_path

try:
    from .savagetype.service import SavageTypeService
    from .savagetype.store import Store
    from .savagetype.util import PLUGIN_NAME, clip
except ImportError:
    from savagetype.service import SavageTypeService
    from savagetype.store import Store
    from savagetype.util import PLUGIN_NAME, clip

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
    "2.3.4",
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
        )
        self._register_pages()
        logger.info("Savage Type loaded, db=%s", self.store.db_path)

    async def initialize(self):
        self.service.refresh_coexistence(self.context.get_all_stars())
        logger.info("Savage Type coexistence: %s", self.service.coexistence.snapshot())

    async def terminate(self):
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
            self.service.refresh_coexistence(self.context.get_all_stars())
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return
            persona_id = await self._persona_id(event)
            ident = self.service.identity_from_event(event, persona_id=persona_id)
            event.set_extra("_stype_ident", ident)
            self.service.capture_user(event, text)
            self.service.schedule_learn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type capture failed: %s", exc)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            self.service.refresh_coexistence(self.context.get_all_stars())
            if not self.service.inject_ok():
                return
            query = (event.message_str or req.prompt or "").strip()
            if not query:
                return
            persona_id = await self._persona_id(event)
            ident = self.service.identity_from_event(event, persona_id=persona_id)
            event.set_extra("_stype_ident", ident)
            pack, result, snapshot = await self.service.build_injection(
                query,
                ident["speaker_id"],
                persona_id=persona_id,
            )
            if not pack:
                return
            self._append_pack(req, pack)
            if self.config.get("debug_log_injection"):
                logger.info(
                    "Savage Type inject route=%s path=%s cache=%s core=%s related=%s chars=%s",
                    snapshot.get("route"),
                    snapshot.get("path"),
                    snapshot.get("cache"),
                    snapshot.get("core"),
                    snapshot.get("related"),
                    snapshot.get("pack_chars"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type inject failed: %s", exc)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            text = getattr(resp, "completion_text", "") or ""
            self.service.capture_bot(event, text)
            event.set_extra("_stype_bot_captured", True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Savage Type bot capture failed: %s", exc)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        # Bot 正文以 on_llm_response 为准，这里只补无 LLM 的主动发送。
        try:
            if event.get_extra("_stype_bot_captured"):
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

    @filter.command_group("stype")
    def stype(self):
        pass

    @stype.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 Savage Type 状态"""
        ov = self.service.overview()
        c = ov["counts"]
        co = ov["coexistence"]
        yield event.plain_result(
            "Savage Type 状态\n"
            f"时间线 {c['timeline']} / 未总结 {c['unsummarized']}\n"
            f"live {c['facts_live']} / superseded {c['facts_superseded']} / 覆盖待确认 {c['pending']}\n"
            f"学习待审 {c.get('reviews_pending', 0)} / 黑话 {c.get('jargon_approved', 0)} / few-shot {c.get('fewshot_approved', 0)}\n"
            f"采集 {'开' if ov['config']['capture'] else '关'} 注入 {'开' if ov['config']['inject'] else '关'}\n"
            f"检索 {ov['config']['retrieval_mode']} embedding {ov['config']['embedding_enabled']}\n"
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
        self.store.set_alias(alias, canonical)
        yield event.plain_result(f"已映射 {alias} -> {canonical}")

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
        )
        facts = result.core + result.related + result.uncertain
        if not facts:
            return "没有找到直接相关的 live 事实。"
        lines = [f"{f.id}: {clip(f.content, 80)}" for f in facts[:8]]
        return "相关事实：\n" + "\n".join(lines)

    @filter.llm_tool(name="savagetype_remember")
    async def tool_remember(self, event: AstrMessageEvent, content: str) -> str:
        """在用户明确要求或长期价值明显时写入事实。只有返回 ok 才算记住。

        Args:
            content(string): 要记住的稳定事实
        """
        ident = await self._ident(event)
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
        facts = self.store.search_facts(keyword, speaker_id=speaker_id, limit=k)
        return json_response({"items": [self._fact_view(f) for f in facts]})

    async def page_facts(self):
        status = request.query.get("status", "live")
        facts = self.store.facts_by_status(status, limit=80)
        return json_response({"items": [self._fact_view(f) for f in facts]})

    async def page_pending(self):
        items = self.store.pending_open(80)
        return json_response(
            {
                "items": [
                    {
                        "id": p.id,
                        "old_fact_id": p.old_fact_id,
                        "reason": p.reason,
                        "created_at": p.created_at,
                        "new_payload": p.new_payload,
                    }
                    for p in items
                ]
            }
        )

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
        speaker = {
            "speaker_id": str(payload.get("speaker_id") or "manual"),
            "speaker_name": str(payload.get("speaker_name") or payload.get("speaker_id") or "manual"),
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
        self.store.set_alias(alias, canonical, str(payload.get("label") or ""))
        return json_response({"ok": True, "alias": alias, "canonical_id": canonical})

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
        review_id = int(payload.get("id") or 0)
        status = str(payload.get("status") or "").strip()
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
        return json_response({"path": str(path)})

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
        users = [s.strip() for s in str(payload.get("user_names") or "").split(",") if s.strip()]
        bots = [s.strip() for s in str(payload.get("bot_names") or "").split(",") if s.strip()]
        return json_response(self.service.preview_chat(text, user_names=users, bot_names=bots))

    async def page_chat_import(self):
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("missing text", status_code=400)
        users = [s.strip() for s in str(payload.get("user_names") or "").split(",") if s.strip()]
        bots = [s.strip() for s in str(payload.get("bot_names") or "").split(",") if s.strip()]
        return json_response(self.service.import_chat(text, user_names=users, bot_names=bots))

    def _fact_view(self, f) -> dict:
        return {
            "id": f.id,
            "subject": f.subject,
            "attribute": f.attribute,
            "value": f.value,
            "content": f.content,
            "speaker_id": f.speaker_id,
            "speaker_name": f.speaker_name,
            "status": f.status,
            "confidence": f.confidence,
            "mention_policy": f.mention_policy,
            "superseded_by": f.superseded_by,
            "supersedes": f.supersedes,
            "updated_at": f.updated_at,
            "reason": f.reason,
            "persona_id": getattr(f, "persona_id", ""),
            "slot_key": f.slot_key(),
        }
