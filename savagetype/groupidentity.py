"""群身份查询与元数据工具：为 LLM 提供群成员身份、群主/管理员及生日查询能力。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
import json
import re
from typing import Any

try:
    from astrbot.api import FunctionTool
except Exception:
    FunctionTool = None  # type: ignore[assignment]


GROUP_MEMBER_TOOL_NAME = "savage_query_group_member_identity"
GROUP_MANAGEMENT_TOOL_NAME = "savage_query_group_management_identity"
GROUP_MEMBER_BIRTHDAY_TOOL_NAME = "savage_query_group_member_birthday"
GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME = "savage_query_group_upcoming_birthdays"
GROUP_IDENTITY_TOOL_NAMES = (
    GROUP_MEMBER_TOOL_NAME,
    GROUP_MANAGEMENT_TOOL_NAME,
    GROUP_MEMBER_BIRTHDAY_TOOL_NAME,
    GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME,
)

QQ_ID_PATTERN = re.compile(r"\d{5,12}")
ROLE_NAME_MAP = {
    "owner": "群主",
    "admin": "管理员",
    "member": "普通成员",
}


def sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")


def format_tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class GroupQueryContext:
    event: Any
    group_id: Any | None = None
    group_name: Any | None = None
    sender_user_id: Any | None = None
    self_id: Any | None = None
    call_action: Any | None = None
    error: str | None = None


def get_group_query_context(event: Any) -> GroupQueryContext:
    platform_name = ""
    try:
        platform_name = getattr(event, "platform_name", "") or ""
    except Exception:
        pass
    if not platform_name:
        try:
            platform_name = getattr(getattr(event, "unified_msg_origin", None), "platform", "") or ""
        except Exception:
            pass

    # 检查适配器动作入口
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return GroupQueryContext(event=event, error="unsupported_platform")

    message_obj = getattr(event, "message_obj", None)
    group_id = getattr(message_obj, "group_id", None)
    if not group_id:
        return GroupQueryContext(event=event, error="not_group_chat")

    sender = getattr(message_obj, "sender", None)
    group = getattr(message_obj, "group", None)
    return GroupQueryContext(
        event=event,
        group_id=str(group_id),
        group_name=getattr(group, "group_name", None),
        sender_user_id=str(getattr(sender, "user_id", None) or ""),
        self_id=getattr(message_obj, "self_id", None),
        call_action=call_action,
    )


async def fetch_group_member_info(context: GroupQueryContext, user_id: Any) -> dict[str, Any] | None:
    if not user_id:
        return None
    params: dict[str, Any] = {
        "group_id": int(context.group_id) if str(context.group_id).isdigit() else context.group_id,
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "no_cache": False,
    }
    if context.self_id:
        params["self_id"] = context.self_id
    try:
        member = await context.call_action("get_group_member_info", **params)
        return member if isinstance(member, dict) else None
    except Exception:
        return None


async def fetch_group_member_list(context: GroupQueryContext) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "group_id": int(context.group_id) if str(context.group_id).isdigit() else context.group_id,
        "no_cache": False,
    }
    if context.self_id:
        params["self_id"] = context.self_id
    try:
        members = await context.call_action("get_group_member_list", **params)
        if isinstance(members, list):
            return [m for m in members if isinstance(m, dict)]
    except Exception:
        pass
    return []


async def fetch_user_birthday(context: GroupQueryContext, user_id: Any) -> dict[str, str] | None:
    if not user_id:
        return None
    params: dict[str, Any] = {
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "no_cache": False,
    }
    if context.self_id:
        params["self_id"] = context.self_id
    try:
        data = await context.call_action("get_stranger_info", **params)
        if isinstance(data, dict):
            m = data.get("birthday_month")
            d = data.get("birthday_day")
            if m and d and str(m).isdigit() and str(d).isdigit():
                m_int, d_int = int(m), int(d)
                if 1 <= m_int <= 12 and 1 <= d_int <= 31:
                    return {"month": str(m_int), "day": str(d_int)}
    except Exception:
        pass
    return None


def build_member_candidate(member: dict[str, Any]) -> dict[str, Any]:
    card = sanitize_text(member.get("card"))
    nickname = sanitize_text(member.get("nickname"))
    user_id = str(member.get("user_id") or "")
    display = card or nickname or user_id
    return {"user_id": user_id, "display_name": display}


def build_member_result(context: GroupQueryContext, member: dict[str, Any]) -> dict[str, Any]:
    user_id = str(member.get("user_id") or "")
    role_key = str(member.get("role") or "member")
    role_cn = ROLE_NAME_MAP.get(role_key, "普通成员")
    card = sanitize_text(member.get("card"))
    nickname = sanitize_text(member.get("nickname"))
    title = sanitize_text(member.get("title"))
    level = member.get("level")

    payload: dict[str, Any] = {
        "user_id": user_id,
        "role": role_cn,
        "nickname": nickname,
    }
    if card:
        payload["card"] = card
    if title:
        payload["special_title"] = title
    if level is not None:
        payload["level"] = level
    return {
        "group_id": str(context.group_id),
        "member": payload,
    }


async def resolve_member(context: GroupQueryContext, target: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    t = target.strip()
    if not t:
        m = await fetch_group_member_info(context, context.sender_user_id)
        return m, []

    # 尝试提取 QQ 纯数字
    uid_match = QQ_ID_PATTERN.search(t)
    if uid_match:
        m = await fetch_group_member_info(context, uid_match.group(0))
        if m:
            return m, []

    # 遍历群列表匹配群昵称/账号昵称
    members = await fetch_group_member_list(context)
    exact_matches = []
    fuzzy_matches = []
    t_lower = t.lower()

    for m in members:
        card = sanitize_text(m.get("card")).lower()
        nick = sanitize_text(m.get("nickname")).lower()
        uid = str(m.get("user_id") or "")
        if t_lower == card or t_lower == nick or t_lower == uid:
            exact_matches.append(m)
        elif t_lower in card or t_lower in nick:
            fuzzy_matches.append(m)

    if len(exact_matches) == 1:
        return exact_matches[0], []
    if len(exact_matches) > 1:
        return None, exact_matches
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], []
    if len(fuzzy_matches) > 1:
        return None, fuzzy_matches

    return None, []


async def query_group_member_identity(event: Any, *, target: str = "") -> str:
    ctx = get_group_query_context(event)
    if ctx.error:
        return format_tool_result({"ok": False, "error": ctx.error})
    member, candidates = await resolve_member(ctx, target)
    if candidates:
        return format_tool_result({
            "ok": False,
            "error": "multiple_candidates",
            "candidates": [build_member_candidate(c) for c in candidates[:8]],
        })
    if not member:
        return format_tool_result({"ok": False, "error": "member_not_found"})
    return format_tool_result({"ok": True, **build_member_result(ctx, member)})


async def query_group_management_identity(event: Any, *, scope: str = "all") -> str:
    ctx = get_group_query_context(event)
    if ctx.error:
        return format_tool_result({"ok": False, "error": ctx.error})
    members = await fetch_group_member_list(ctx)
    if not members:
        return format_tool_result({"ok": False, "error": "empty_or_failed"})

    owner = None
    admins = []
    for m in members:
        role = str(m.get("role") or "")
        if role == "owner" and owner is None:
            owner = m
        elif role == "admin":
            admins.append(m)

    scope_lower = scope.strip().lower()
    res: dict[str, Any] = {"ok": True, "group_id": str(ctx.group_id)}
    if scope_lower in ("all", "owner"):
        res["owner"] = build_member_result(ctx, owner)["member"] if owner else None
    if scope_lower in ("all", "admin"):
        res["admins"] = [build_member_result(ctx, a)["member"] for a in admins]
    return format_tool_result(res)


async def query_group_member_birthday(event: Any, *, target: str = "") -> str:
    ctx = get_group_query_context(event)
    if ctx.error:
        return format_tool_result({"ok": False, "error": ctx.error})
    member, candidates = await resolve_member(ctx, target)
    if candidates:
        return format_tool_result({
            "ok": False,
            "error": "multiple_candidates",
            "candidates": [build_member_candidate(c) for c in candidates[:8]],
        })
    if not member:
        return format_tool_result({"ok": False, "error": "member_not_found"})

    uid = member.get("user_id")
    bday = await fetch_user_birthday(ctx, uid)
    if not bday:
        return format_tool_result({"ok": False, "error": "birthday_not_found"})

    mem_res = build_member_result(ctx, member)
    return format_tool_result({
        "ok": True,
        "member": mem_res["member"],
        "birthday": bday,
    })


async def query_group_upcoming_birthdays(event: Any, *, days: int = 7) -> str:
    ctx = get_group_query_context(event)
    if ctx.error:
        return format_tool_result({"ok": False, "error": ctx.error})
    members = await fetch_group_member_list(ctx)
    if not members:
        return format_tool_result({"ok": False, "error": "empty_or_failed"})

    limit_days = max(1, min(int(days or 7), 366))
    today = date.today()
    sem = asyncio.Semaphore(5)

    async def check_one(m: dict[str, Any]):
        uid = m.get("user_id")
        if not uid:
            return None
        async with sem:
            bday = await fetch_user_birthday(ctx, uid)
        if not bday:
            return None
        try:
            m_int = int(bday["month"])
            d_int = int(bday["day"])
            for y in (today.year, today.year + 1):
                try:
                    bdate = date(y, m_int, d_int)
                    if bdate >= today:
                        diff = (bdate - today).days
                        if diff <= limit_days:
                            return {
                                "member": build_member_result(ctx, m)["member"],
                                "birthday": bday,
                                "days_until": diff,
                            }
                        break
                except ValueError:
                    continue
        except Exception:
            pass
        return None

    tasks = [check_one(m) for m in members]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid = [r for r in results if isinstance(r, dict) and r is not None]
    valid.sort(key=lambda x: x["days_until"])
    return format_tool_result({
        "ok": True,
        "group_id": str(ctx.group_id),
        "days": limit_days,
        "birthdays": valid,
    })


# 定义 FunctionTool 类
@dataclass
class QueryGroupMemberIdentityTool(FunctionTool if FunctionTool else object):
    logger: Any = None
    name: str = GROUP_MEMBER_TOOL_NAME
    description: str = "查询当前群内某个成员的群身份、群等级、专属头衔和昵称信息。当询问群主、管理员或群友身份时调用。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "要查询的成员QQ号、昵称或@，留空为当前发言人"}
            },
        }
    )

    async def run(self, event: Any, target: str = "") -> str:
        return await query_group_member_identity(event, target=target)


@dataclass
class QueryGroupManagementIdentityTool(FunctionTool if FunctionTool else object):
    logger: Any = None
    name: str = GROUP_MANAGEMENT_TOOL_NAME
    description: str = "查询当前群的群主和管理员名单。当询问群主或管理员是谁时调用。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "查询范围：all(群主和管理员), owner(仅群主), admin(仅管理员)"}
            },
        }
    )

    async def run(self, event: Any, scope: str = "all") -> str:
        return await query_group_management_identity(event, scope=scope)


@dataclass
class QueryGroupMemberBirthdayTool(FunctionTool if FunctionTool else object):
    logger: Any = None
    name: str = GROUP_MEMBER_BIRTHDAY_TOOL_NAME
    description: str = "查询当前群内成员的生日(月日)。当询问群友生日时调用。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "要查询的成员QQ号或昵称，留空为当前发言人"}
            },
        }
    )

    async def run(self, event: Any, target: str = "") -> str:
        return await query_group_member_birthday(event, target=target)


@dataclass
class QueryGroupUpcomingBirthdaysTool(FunctionTool if FunctionTool else object):
    logger: Any = None
    name: str = GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME
    description: str = "查询当前群未来一段时间内过生日的成员名单。当询问最近或未来几天谁过生日时调用。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "向后查询的天数(默认7天，最大366天)"}
            },
        }
    )

    async def run(self, event: Any, days: int = 7) -> str:
        return await query_group_upcoming_birthdays(event, days=days)


def _safe_create_tool(cls: type, name: str, description: str, params: dict[str, Any]) -> Any:
    # 策略 1: 零参调用（由 dataclass 提供默认值）
    try:
        return cls()
    except Exception:
        pass
    # 策略 2: 显式关键字实参调用（满足 Pydantic BaseModel 校验）
    try:
        return cls(name=name, description=description, parameters=params)
    except Exception:
        pass
    # 策略 3: 手动注入属性
    try:
        inst = cls.__new__(cls)
        inst.name = name
        inst.description = description
        inst.parameters = params
        return inst
    except Exception:
        return None


def get_all_group_identity_tools() -> list[Any]:
    if not FunctionTool:
        return []
    defs = [
        (
            QueryGroupMemberIdentityTool,
            GROUP_MEMBER_TOOL_NAME,
            "查询当前群内某个成员的群身份、群等级、专属头衔和昵称信息。当询问群主、管理员或群友身份时调用。",
            {"type": "object", "properties": {"target": {"type": "string", "description": "要查询的成员QQ号、昵称或@，留空为当前发言人"}}},
        ),
        (
            QueryGroupManagementIdentityTool,
            GROUP_MANAGEMENT_TOOL_NAME,
            "查询当前群的群主和管理员名单。当询问群主或管理员是谁时调用。",
            {"type": "object", "properties": {"scope": {"type": "string", "description": "查询范围：all(群主和管理员), owner(仅群主), admin(仅管理员)"}}},
        ),
        (
            QueryGroupMemberBirthdayTool,
            GROUP_MEMBER_BIRTHDAY_TOOL_NAME,
            "查询当前群内成员的生日(月日)。当询问群友生日时调用。",
            {"type": "object", "properties": {"target": {"type": "string", "description": "要查询的成员QQ号或昵称，留空为当前发言人"}}},
        ),
        (
            QueryGroupUpcomingBirthdaysTool,
            GROUP_UPCOMING_BIRTHDAYS_TOOL_NAME,
            "查询当前群未来一段时间内过生日的成员名单。当询问最近或未来几天谁过生日时调用。",
            {"type": "object", "properties": {"days": {"type": "integer", "description": "向后查询的天数(默认7天，最大366天)"}}},
        ),
    ]
    tools = []
    for cls, name, desc, p in defs:
        inst = _safe_create_tool(cls, name, desc, p)
        if inst is not None:
            tools.append(inst)
    return tools
