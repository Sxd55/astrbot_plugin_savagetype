"""Detect overlapping memory plugins and degrade capture/inject."""

from __future__ import annotations

from typing import Any

KNOWN_INJECTORS = {
    "astrbot_plugin_memory_companion",
    "memory_companion",
    "astrbot_plugin_livingmemory",
    "livingmemory",
    "astrbot_plugin_LivingMemory",
}
KNOWN_CAPTURE = {
    "astrbot_plugin_memory_companion",
    "memory_companion",
    "astrbot_plugin_livingmemory",
    "livingmemory",
}


def _norm(name: str | None) -> str:
    return (name or "").strip().lower().replace("-", "_")


class Coexistence:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.skip_inject = False
        self.skip_capture = False
        self.skip_style = False
        self.reasons: list[str] = []
        self.detected: list[str] = []

    def refresh(self, stars: list[Any]) -> None:
        self.skip_inject = False
        self.skip_capture = False
        self.skip_style = False
        self.reasons = []
        self.detected = []
        if not self.enabled:
            return
        for star in stars or []:
            names = [
                getattr(star, "name", None),
                getattr(star, "root_dir_name", None),
                getattr(star, "display_name", None),
            ]
            activated = getattr(star, "activated", True)
            if not activated:
                continue
            for raw in names:
                n = _norm(raw)
                if not n:
                    continue
                if n in {_norm(x) for x in KNOWN_INJECTORS} or "memory_companion" in n or "livingmemory" in n:
                    self.skip_inject = True
                    self.detected.append(n)
                    self.reasons.append(f"inject skipped: {n}")
                if n in {_norm(x) for x in KNOWN_CAPTURE} or "memory_companion" in n or "livingmemory" in n:
                    self.skip_capture = True
                    if n not in self.detected:
                        self.detected.append(n)
                    if f"capture skipped: {n}" not in self.reasons:
                        self.reasons.append(f"capture skipped: {n}")
                if "self_learning" in n:
                    self.detected.append(n)
                    self.skip_style = True
                    self.reasons.append("style/jargon left to self_learning")

        self.detected = sorted(set(self.detected))
        self.reasons = sorted(set(self.reasons))

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "skip_inject": self.skip_inject,
            "skip_capture": self.skip_capture,
            "skip_style": self.skip_style,
            "detected": self.detected,
            "reasons": self.reasons,
        }
