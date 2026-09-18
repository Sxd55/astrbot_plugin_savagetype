"""SavageType 场景化预设系统（Presets）：提供开箱即用的场景化参数方案与变更清单（Diff）。"""

from __future__ import annotations

from typing import Any

# 预设显示名称映射
PRESET_NAMES: dict[str, str] = {
    "daily": "日常陪伴（推荐平衡档）",
    "frugal": "极致省Token（大幅降本70%）",
    "assistant": "群聊助手（精准事实/知识优先）",
    "rpg": "跑团沉浸扮演（长事件窗口）",
    "custom": "专家自定义（手动自由微调）",
}

# 核心接管参数的元数据定义（用于格式化输出与 Diff 比较）
PRESET_METADATA: dict[str, dict[str, str]] = {
    "inject_budget_chars": {
        "name": "注入字符预算",
        "unit": "字",
        "desc": "单次送入Prompt的记忆字符上限，直接决定Token开销",
    },
    "top_k": {
        "name": "初筛候选条数",
        "unit": "条",
        "desc": "向量/混合检索初筛候选集大小",
    },
    "core_fact_limit": {
        "name": "核心事实条数",
        "unit": "条",
        "desc": "单次最多注入的核心设定条数",
    },
    "related_fact_limit": {
        "name": "相关事实条数",
        "unit": "条",
        "desc": "单次最多注入的关联事实条数",
    },
    "event_max_inject": {
        "name": "近期事件条数",
        "unit": "条",
        "desc": "单次最多注入的时序近期事件条数",
    },
    "extract_min_messages": {
        "name": "抽取触发阈值",
        "unit": "条",
        "desc": "消息累积达到该条数后触发一次AI抽取",
    },
    "extract_cooldown_seconds": {
        "name": "抽取冷却时间",
        "unit": "秒",
        "desc": "两次抽取之间的冷却间隔，抑制后台LLM提炼调用频次",
    },
    "cross_window_enabled": {
        "name": "跨窗口联想",
        "unit": "开关",
        "desc": "是否检索其他群聊/私聊的相关记忆",
    },
    "window_flow_enabled": {
        "name": "时间流记忆",
        "unit": "开关",
        "desc": "是否注入最近时间线的对话上下文流",
    },
    "retrieval_bm25": {
        "name": "BM25词法检索",
        "unit": "开关",
        "desc": "是否开启精确词法关键词匹配与向量混合",
    },
    "entity_linking_enabled": {
        "name": "实体同义扩展",
        "unit": "开关",
        "desc": "是否在检索时进行实体识别与同义词扩展",
    },
}

# 历史与别名映射（确保向后完全兼容已有调用与测试）
PARAM_ALIASES: dict[str, str] = {
    "inject_max_chars": "inject_budget_chars",
    "related_top_k": "related_fact_limit",
    "event_top_k": "event_max_inject",
    "extract_batch_size": "extract_min_messages",
}

# 四套预设方案默认参数定义
PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "daily": {
        "inject_budget_chars": 1200,
        "top_k": 3,
        "core_fact_limit": 3,
        "related_fact_limit": 3,
        "event_max_inject": 2,
        "extract_min_messages": 6,
        "extract_cooldown_seconds": 45,
        "cross_window_enabled": True,
        "window_flow_enabled": True,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
    "frugal": {
        # 极致省 Token 档位
        "inject_budget_chars": 400,
        "top_k": 2,
        "core_fact_limit": 1,
        "related_fact_limit": 1,
        "event_max_inject": 1,
        "extract_min_messages": 12,
        "extract_cooldown_seconds": 90,
        "cross_window_enabled": False,
        "window_flow_enabled": False,
        "retrieval_bm25": True,
        "entity_linking_enabled": False,
    },
    "assistant": {
        # 群聊助手 / 事实知识档位
        "inject_budget_chars": 1500,
        "top_k": 5,
        "core_fact_limit": 4,
        "related_fact_limit": 2,
        "event_max_inject": 1,
        "extract_min_messages": 6,
        "extract_cooldown_seconds": 30,
        "cross_window_enabled": False,
        "window_flow_enabled": False,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
    "rpg": {
        # 跑团与角色扮演档位
        "inject_budget_chars": 2000,
        "top_k": 4,
        "core_fact_limit": 5,
        "related_fact_limit": 4,
        "event_max_inject": 5,
        "extract_min_messages": 4,
        "extract_cooldown_seconds": 30,
        "cross_window_enabled": True,
        "window_flow_enabled": True,
        "retrieval_bm25": True,
        "entity_linking_enabled": True,
    },
}

SUPPORTED_PRESETS = tuple(PRESET_DEFINITIONS.keys()) + ("custom",)


def get_preset_defaults(preset_name: str) -> dict[str, Any]:
    """获取指定预设的参数字典（含别名兼容字段）。"""
    name = (preset_name or "daily").strip().lower()
    base = dict(PRESET_DEFINITIONS.get(name, PRESET_DEFINITIONS["daily"]))
    # 填充向后兼容的别名键
    for alias_key, canon_key in PARAM_ALIASES.items():
        if canon_key in base and alias_key not in base:
            base[alias_key] = base[canon_key]
    return base


def resolve_effective_config(config: dict[str, Any], key: str, default: Any) -> Any:
    """根据所选场景预设动态返回有效配置。

    设计原则：
    1. 当处于预设模式（daily/frugal/assistant/rpg）时，预设所声明的核心算法参数由预设接管，
       确保一键生效真实调优，避免因旧配置残留导致预设失效；
       其它未被预设托管的配置（如 owner_qq、模型选择、黑名单、开关等）100% 保持用户配置。
    2. 预设只在内存中动态生效（Overlay 遮罩），绝不擦除或覆盖用户在磁盘上保存的任何配置值。
    3. 只要随时切回 'custom'（专家自定义），用户所有的微调参数全盘原封不动恢复生效。
    """
    # 规范化别名键名
    canon_key = PARAM_ALIASES.get(key, key)
    raw = config.get(canon_key)
    if raw is None and key != canon_key:
        raw = config.get(key)

    preset_raw = config.get("config_preset")
    if preset_raw is None:
        # 未配置场景预设时，默认完全遵循用户已有配置与默认值（零覆盖安全原则）
        return default if raw is None else raw

    preset_name = str(preset_raw).strip().lower()

    # custom 模式：完全放权给用户自定义配置
    if preset_name in ("custom", "none", ""):
        return default if raw is None else raw

    # 预设模式：托管的核心参数按预设值生效
    preset_vals = PRESET_DEFINITIONS.get(preset_name, PRESET_DEFINITIONS["daily"])
    if canon_key in preset_vals:
        return preset_vals[canon_key]

    # 非预设托管项：原样读取用户配置或默认值
    return default if raw is None else raw


def _format_display_val(val: Any, unit: str) -> str:
    """辅助格式化参数展示文本。"""
    if isinstance(val, bool):
        return "开启" if val else "关闭"
    if unit == "字":
        return f"{val}字"
    if unit == "条":
        return f"{val}条"
    if unit == "秒":
        return f"{val}秒"
    return str(val)


def diff_preset(config: dict[str, Any], target_preset: str) -> list[dict[str, Any]]:
    """比对当前有效配置与目标预设推荐值之间的差异清单（Diff）。

    返回列表元素结构：
    - key: 参数配置键名
    - name: 参数中文友好名称
    - unit: 参数单位或类型说明
    - desc: 参数影响说明
    - current_value: 当前生效值
    - target_value: 目标预设推荐值
    - current_display: 当前生效值展示文本
    - target_display: 目标预设推荐值展示文本
    - changed: 是否变动（bool）
    - direction: 变动方向 ('up' | 'down' | 'toggle' | 'same')
    - symbol: 变动符号 ('🔼' | '🔽' | '🔄' | '➖')
    """
    target = (target_preset or "daily").strip().lower()
    if target not in SUPPORTED_PRESETS:
        target = "daily"

    # 目标参数集：如果目标是 custom，则取用户在磁盘上保存的手动值（未配置则参考 daily 默认值）
    target_values: dict[str, Any] = {}
    if target == "custom":
        for key in PRESET_METADATA:
            raw = config.get(key)
            if raw is None and key in PARAM_ALIASES.values():
                for a_k, a_v in PARAM_ALIASES.items():
                    if a_v == key and a_k in config:
                        raw = config[a_k]
                        break
            if raw is None:
                raw = PRESET_DEFINITIONS["daily"].get(key)
            target_values[key] = raw
    else:
        target_values = PRESET_DEFINITIONS.get(target, PRESET_DEFINITIONS["daily"])

    diffs: list[dict[str, Any]] = []
    for key, meta in PRESET_METADATA.items():
        name = meta["name"]
        unit = meta["unit"]
        desc = meta["desc"]

        current_val = resolve_effective_config(config, key, PRESET_DEFINITIONS["daily"].get(key))
        target_val = target_values.get(key)

        current_display = _format_display_val(current_val, unit)
        target_display = _format_display_val(target_val, unit)

        changed = (current_val != target_val)
        direction = "same"
        symbol = "➖"

        if changed:
            if unit == "开关" or isinstance(current_val, bool):
                direction = "toggle"
                symbol = "🔄"
            elif isinstance(current_val, (int, float)) and isinstance(target_val, (int, float)):
                if target_val > current_val:
                    direction = "up"
                    symbol = "🔼"
                else:
                    direction = "down"
                    symbol = "🔽"
            else:
                direction = "toggle"
                symbol = "🔄"

        diffs.append({
            "key": key,
            "name": name,
            "unit": unit,
            "desc": desc,
            "current_value": current_val,
            "target_value": target_val,
            "current_display": current_display,
            "target_display": target_display,
            "changed": changed,
            "direction": direction,
            "symbol": symbol,
        })

    return diffs


def format_preset_diff_text(
    current_preset: str,
    target_preset: str,
    diffs: list[dict[str, Any]],
    is_applied: bool = False,
) -> str:
    """将 Diff 清单格式化为清晰美观的文本对比卡。"""
    cur_name = PRESET_NAMES.get(current_preset, current_preset)
    tgt_name = PRESET_NAMES.get(target_preset, target_preset)

    lines: list[str] = []
    if is_applied:
        lines.append(f"✅【预设切换成功】已生效为：{tgt_name}")
        lines.append(f"📊 从 [{cur_name}] 切换至 [{tgt_name}] 参数明细：")
    else:
        lines.append(f"📋【预设变更清单预览】")
        lines.append(f"当前：{cur_name}")
        lines.append(f"目标：{tgt_name}")
    lines.append("─────────────────────────────")

    changed_count = 0
    for idx, d in enumerate(diffs, 1):
        if d["changed"]:
            changed_count += 1
            mark = f"[{d['symbol']} 变动]"
            lines.append(f"{idx:2d}. {d['name']} ({d['key']}): {d['current_display']} ➔ {d['target_display']} {mark}")
        else:
            lines.append(f"{idx:2d}. {d['name']} ({d['key']}): {d['current_display']} [➖ 保持一致]")

    lines.append("─────────────────────────────")
    lines.append(f"💡 变动统计：共 {len(diffs)} 项核心算法参数，{changed_count} 项发生变更。")
    lines.append("🛡️ 安全机制：预设采用内存 Overlay 遮罩，绝不擦除或覆盖您手动修改过的配置。")
    if is_applied:
        lines.append("💡 提示：随时输入「/stype preset custom」即可 100% 恢复所有手动自定义值。")
    else:
        lines.append(f"👉 确认应用此预设请执行：「/stype preset apply {target_preset}」")

    return "\n".join(lines)

