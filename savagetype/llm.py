"""模型调用策略：任务分档、Provider 回退链、Token 预算闸、拒答重试。

策略参考 astrbot_plugin_private_companion 的「快速/精准模型 + 回退链 + Token 限额 +
替换策略」公开设计（未复制代码），按本插件实际任务裁剪为两个档位：

- quality（推理）：事实缩写、事件摘要；
- fast（低延迟）：对照审核、黑话/few-shot/人格草稿。

优先级：显式单任务配置 > 档位配置 > 旧回退链 > AstrBot 当前会话模型；
解析出的「来源」会写进 usage_ledger，面板可看实际用了哪一档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

TASKS: dict[str, dict[str, str]] = {
    "normalize": {"tier": "quality", "priority": "high", "label": "事实缩写"},
    "event": {"tier": "quality", "priority": "high", "label": "事件摘要"},
    "verify": {"tier": "fast", "priority": "high", "label": "对照审核"},
    "image": {"tier": "fast", "priority": "high", "label": "图片转述"},
    "learn": {"tier": "fast", "priority": "low", "label": "表达学习"},
    "embed": {"tier": "", "priority": "low", "label": "向量化"},
    "rerank": {"tier": "", "priority": "low", "label": "重排"},
    "default": {"tier": "quality", "priority": "low", "label": "其他"},
}

EXPLICIT_KEYS: dict[str, tuple[str, ...]] = {
    "normalize": ("normalize_provider_id", "summary_provider_id"),
    "event": ("event_provider_id", "normalize_provider_id", "summary_provider_id"),
    "verify": ("verify_provider_id", "normalize_provider_id", "summary_provider_id"),
    "image": ("image_caption_provider_id",),
    "learn": ("normalize_provider_id", "summary_provider_id"),
    "embed": ("embedding_provider_id",),
    "rerank": ("rerank_provider_id",),
    "default": ("summary_provider_id",),
}

TIER_KEYS = {"quality": "quality_provider_id", "fast": "fast_provider_id"}

TIER_LABELS = {"quality": "精准档", "fast": "快速档"}

REFUSAL_RE = re.compile(
    r"(无法协助|不能协助|无法提供|无法帮助|不方便提供|抱歉[，,\s]{0,3}我(不能|无法|不可以)|"
    r"作为一个?(AI|人工智能|语言模型)|我(不能|无法|不会)(提供|回答|协助)|"
    r"I can'?t (help|assist)|I cannot (help|assist|provide)|as an AI)",
    re.IGNORECASE,
)

CJK_RANGES = ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF))


def estimate_tokens(text: str) -> int:
    """粗略估算 token：CJK ≈ 1/字，其它 ≈ 4 字符/token。"""
    cjk = 0
    other = 0
    for ch in text or "":
        code = ord(ch)
        if any(low <= code <= high for low, high in CJK_RANGES):
            cjk += 1
        else:
            other += 1
    return int(cjk + (other + 3) // 4)


def looks_refusal(text: str) -> bool:
    return bool(REFUSAL_RE.search(text or ""))


def _pick(config: Any, key: str) -> str:
    if not key:
        return ""
    try:
        return str(config.get(key) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def resolve_provider(task: str, config: Any) -> tuple[str, str]:
    """返回 (provider_id, source)。provider_id 为空表示跟随 AstrBot 当前会话模型。"""
    for key in EXPLICIT_KEYS.get(task, ()):
        provider_id = _pick(config, key)
        if provider_id:
            return provider_id, f"explicit:{key}"
    tier = str(TASKS.get(task, {}).get("tier") or "")
    provider_id = _pick(config, TIER_KEYS.get(tier, "")) if tier else ""
    if provider_id:
        return provider_id, f"tier:{tier}"
    return "", "default"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    provider_override: str = ""


class BudgetGuard:
    """滚动 24 小时 Token 预算闸。

    - 硬限额（daily_token_limit）：达到后停止一切自动 LLM 任务；
    - 软限额（soft_token_limit）：达到后只停低优先级任务（表达学习、向量化回填、重排）；
    - 单次上限（single_call_token_cap）：预估输入超限时改用备用模型，未配备用则照发。
    """

    def __init__(self, config: Any, tokens_used: Callable[[], int]):
        self.config = config
        self.tokens_used = tokens_used

    def hard_limit(self) -> int:
        return max(0, _as_int(self.config.get("daily_token_limit"), 0))

    def soft_limit(self) -> int:
        return max(0, _as_int(self.config.get("soft_token_limit"), 0))

    def single_call_cap(self) -> int:
        return max(0, _as_int(self.config.get("single_call_token_cap"), 0))

    def fallback_provider(self) -> str:
        return _pick(self.config, "fallback_provider_id")

    def check(self, task: str, prompt: str = "") -> BudgetDecision:
        used = max(0, int(self.tokens_used() or 0))
        hard = self.hard_limit()
        if hard and used >= hard:
            return BudgetDecision(False, "daily_token_limit")
        priority = TASKS.get(task, {}).get("priority", "low")
        soft = self.soft_limit()
        if soft and used >= soft and priority != "high":
            return BudgetDecision(False, "soft_token_limit")
        cap = self.single_call_cap()
        if cap and estimate_tokens(prompt) > cap:
            fallback = self.fallback_provider()
            if fallback:
                return BudgetDecision(True, "single_call_cap", fallback)
            return BudgetDecision(True, "single_call_cap_no_fallback")
        return BudgetDecision(True)

    def status(self) -> dict[str, Any]:
        return {
            "used": max(0, int(self.tokens_used() or 0)),
            "hard_limit": self.hard_limit(),
            "soft_limit": self.soft_limit(),
            "single_call_cap": self.single_call_cap(),
            "fallback_provider": self.fallback_provider(),
        }


class LLMBudgetExceeded(RuntimeError):
    """预算闸拦截：调用方应跳过本次自动任务，而不是把它当成模型故障。"""

    def __init__(self, reason: str = "budget"):
        super().__init__(f"llm budget exceeded: {reason}")
        self.reason = reason
