"""SavageType 场景化预设系统（Presets）：提供开箱即用的场景化参数方案。"""

from __future__ import annotations

from typing import Any

# 四套预设方案默认参数定义
PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "daily": {
        "top_k": 3,
        "related_top_k": 3,
        "event_top_k": 2,
        "inject_max_chars": 1200,
        "extract_batch_size": 6,
        "extract_cooldown_seconds": 45,
        "cross_window_enabled": True,
        "window_flow_enabled": True,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
    "frugal": {
        # 极致省 Token 档位
        "top_k": 2,
        "related_top_k": 1,
        "event_top_k": 1,
        "inject_max_chars": 400,
        "extract_batch_size": 12,
        "extract_cooldown_seconds": 90,
        "cross_window_enabled": False,
        "window_flow_enabled": False,
        "retrieval_bm25": True,
        "entity_linking_enabled": False,
    },
    "assistant": {
        # 群聊助手 / 事实知识档位
        "top_k": 5,
        "related_top_k": 2,
        "event_top_k": 1,
        "inject_max_chars": 1500,
        "extract_batch_size": 6,
        "extract_cooldown_seconds": 30,
        "cross_window_enabled": False,
        "window_flow_enabled": False,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
    "rpg": {
        # 跑团与角色扮演档位
        "top_k": 4,
        "related_top_k": 4,
        "event_top_k": 5,
        "inject_max_chars": 2000,
        "extract_batch_size": 4,
        "extract_cooldown_seconds": 30,
        "cross_window_enabled": True,
        "window_flow_enabled": True,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
}

SUPPORTED_PRESETS = tuple(PRESET_DEFINITIONS.keys()) + ("custom",)


def get_preset_defaults(preset_name: str) -> dict[str, Any]:
    name = (preset_name or "daily").strip().lower()
    return dict(PRESET_DEFINITIONS.get(name, PRESET_DEFINITIONS["daily"]))


def resolve_effective_config(config: dict[str, Any], key: str, default: Any) -> Any:
    """根据所选场景预设动态返回有效配置。"""
    preset_name = str(config.get("config_preset", "daily") or "daily").strip().lower()
    raw = config.get(key)

    # 如果是自定义模式，直接返回用户配置
    if preset_name == "custom":
        return default if raw is None else raw

    # 处于预设模式下：
    preset_vals = PRESET_DEFINITIONS.get(preset_name, PRESET_DEFINITIONS["daily"])
    if key in preset_vals:
        # 用户若未在配置中显式修改（或者值为 None），返回预设推荐值
        if raw is None:
            return preset_vals[key]
        # 若用户有显式设定值，仍以用户设定为准，赋予用户覆盖权限
        return raw

    return default if raw is None else raw
