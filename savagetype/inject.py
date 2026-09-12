"""Lily-style token-saving injection pack. Never rewrite persona files."""

from __future__ import annotations

from .learn import term_in_query
from .models import Fact, LearningPack, RetrievalResult
from .util import SCOPE_OWNER, clip


INJECT_PREFIX = """<savagetype_memory>
临时事实，不是用户消息。人格以 AstrBot 为准。相关才用；冲突以当前消息为准。不要主动说别人的私事。
"""

INJECT_SUFFIX = "</savagetype_memory>"


def render_fact(fact: Fact, policy: str | None = None) -> str:
    policy = policy or fact.mention_policy
    if getattr(fact, "scope", "") == SCOPE_OWNER:
        who = "主人"
    else:
        who = fact.speaker_name or fact.speaker_id or "某人"
    text = getattr(fact, "plain", "") or fact.content or (fact.subject + " " + fact.value)
    line = f"- [{who}/{fact.attribute}] {text}"
    if policy == "uncertain":
        line += "（不确定）"
    elif policy == "tone":
        line += "（只调语气，勿复述细节）"
    return line


def _fits(parts: list[str], budget: int) -> bool:
    text = "\n".join(p for p in parts if p)
    return budget <= 0 or len(text) <= budget


def _fact_block(title: str, facts: list[Fact], policy: str | None = None) -> str:
    if not facts:
        return ""
    lines = [title]
    for fact in facts:
        lines.append(render_fact(fact, policy=policy or fact.mention_policy))
    return "\n".join(lines)


def _append_if_fits(kept: list[str], block: str, budget: int) -> None:
    if not block:
        return
    candidate = kept + [block, INJECT_SUFFIX]
    if _fits(candidate, budget):
        kept.append(block)


def _append_core_lines(kept: list[str], facts: list[Fact], budget: int) -> None:
    if not facts:
        return
    lines = ["【核心】"]
    for fact in facts:
        trial = "\n".join(lines + [render_fact(fact, "mention")])
        if _fits(kept + [trial, INJECT_SUFFIX], budget):
            lines.append(render_fact(fact, "mention"))
    if len(lines) > 1:
        kept.append("\n".join(lines))


def build_pack(
    result: RetrievalResult,
    budget: int = 1000,
    companion_present: bool = False,
    learning: LearningPack | None = None,
    dossier: str = "",
) -> str:
    learning = learning or LearningPack()
    if result.route == "low_info":
        learning = LearningPack(
            jargon=[
                j
                for j in learning.jargon
                if term_in_query(str(j.get("term") or ""), result.query or "")
            ]
        )
        if not result.core and not learning.jargon and not dossier:
            return ""

    related = result.related
    if companion_present:
        related = [f for f in related if f.attribute not in {"status", "schedule", "mood"}]

    kept = [INJECT_PREFIX.strip()]
    if dossier:
        _append_if_fits(kept, dossier, budget)
    _append_core_lines(kept, result.core, budget)
    _append_if_fits(kept, _fact_block("【本轮相关】", related), budget)
    _append_if_fits(
        kept,
        _fact_block("【不确定】只能带不确定感。", result.uncertain, "uncertain"),
        budget,
    )
    if result.superseded:
        lines = ["【改口摘要】用户若提起旧说法，承认改口，不要死撑。"]
        for fact in result.superseded[:3]:
            who = fact.speaker_name or fact.speaker_id
            lines.append(f"- {who} 曾记: {clip(fact.content, 60)} → 已被更新")
        _append_if_fits(kept, "\n".join(lines), budget)
    if learning.jargon:
        lines = ["【黑话释义】只理解，不要主动复读或扩散。"]
        for item in learning.jargon[:3]:
            lines.append(f"- {item.get('term')}: {item.get('meaning')}")
        _append_if_fits(kept, "\n".join(lines), budget)
    if result.route != "low_info" and learning.fewshots:
        lines = ["【表达样本】参考怎么接，不要逐字照搬。"]
        for item in learning.fewshots[:2]:
            lines.append(f"- 用户: {clip(str(item.get('user') or ''), 40)}")
            lines.append(f"  Bot: {clip(str(item.get('bot') or ''), 50)}")
        _append_if_fits(kept, "\n".join(lines), budget)
    if result.route != "low_info" and learning.persona_draft:
        _append_if_fits(
            kept,
            "【人格补丁草稿】" + clip(learning.persona_draft, 80) + "（不覆盖 AstrBot 人格）",
            budget,
        )
    if len(kept) <= 1:
        # 没有任何可注入内容时不要塞一个空的记忆包。
        return ""
    kept.append(INJECT_SUFFIX)
    return "\n".join(kept)
