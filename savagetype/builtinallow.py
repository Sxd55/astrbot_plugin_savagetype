"""AstrBot 原生内置指令白名单放行控制：按白名单精细化放行 / 拦截内置指令。"""

from __future__ import annotations

import contextvars
import inspect
import types
from typing import Any


BUILTIN_COMMANDS_MODULE = "astrbot.builtin_stars.builtin_commands.main"

CANONICAL_COMMAND_BY_HANDLER = {
    "help": "help",
    "sid": "sid",
    "name": "name",
    "reset": "reset",
    "stop": "stop",
    "new_conv": "new",
    "stats": "stats",
    "provider": "provider",
    "update_dashboard": "dashboard_update",
    "set_variable": "set",
    "unset_variable": "unset",
}

SUPPORTED_BUILTIN_COMMANDS = tuple(
    [
        "help",
        "sid",
        "name",
        "reset",
        "stop",
        "new",
        "stats",
        "provider",
        "dashboard_update",
        "set",
        "unset",
    ]
)

_allowed_builtin_commands: contextvars.ContextVar[set[str] | None] = (
    contextvars.ContextVar("savage_allowed_builtin_commands", default=None)
)


def sanitize_command_allowlist(value: Any) -> set[str]:
    candidates: list[Any]
    if isinstance(value, str):
        candidates = value.replace(",", "\n").replace(";", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = []

    allowed: set[str] = set()
    supported = set(SUPPORTED_BUILTIN_COMMANDS)
    for item in candidates:
        normalized = str(item or "").strip().lstrip("/").strip().lower()
        if normalized in supported:
            allowed.add(normalized)
    return allowed


def should_keep_handler(handler: Any, allowed_commands: set[str]) -> bool:
    if getattr(handler, "handler_module_path", "") != BUILTIN_COMMANDS_MODULE:
        return True
    handler_name = str(getattr(handler, "handler_name", "") or "")
    canonical = CANONICAL_COMMAND_BY_HANDLER.get(handler_name)
    return canonical in allowed_commands


class _DisableBuiltinCommandsStageProxy:
    __slots__ = ("_disable_builtin_commands", "_target")

    def __init__(self, target: Any):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_disable_builtin_commands", False)

    @property
    def __class__(self) -> type:
        return self._target.__class__

    @property
    def disable_builtin_commands(self) -> bool:
        return self._disable_builtin_commands

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "disable_builtin_commands":
            object.__setattr__(self, "_disable_builtin_commands", bool(value))
            return
        setattr(self._target, name, value)


class BuiltinCommandAllowlistModule:
    """按白名单精细化 AstrBot 内置指令放行。"""

    _stage_cls: type | None = None
    _registry: Any = None
    _adapter_event_type: Any = None
    _original_process: Any = None
    _original_get_handlers: Any = None
    _active_module: BuiltinCommandAllowlistModule | None = None

    def __init__(self, *, logger: Any, enabled: bool = False, allowlist: Any = None):
        self.logger = logger
        self._enabled = bool(enabled)
        self._allowlist = sanitize_command_allowlist(allowlist)
        self._installed = False

    def configure(self, *, enabled: bool, allowlist: Any) -> None:
        self._enabled = bool(enabled)
        self._allowlist = sanitize_command_allowlist(allowlist)

    @property
    def allowed_commands(self) -> set[str]:
        return set(self._allowlist)

    def install(self) -> bool:
        if not self._enabled:
            return False
        cls = type(self)
        if self._installed and cls._active_module is self:
            return True

        stage_cls = self._load_waking_check_stage()
        registry, adapter_event_type = self._load_star_registry()
        if stage_cls is None or registry is None:
            self._log("debug", "未找到 AstrBot 指令唤醒入口，跳过内置指令白名单安装")
            return False

        if cls._original_process is None:
            cls._stage_cls = stage_cls
            cls._original_process = stage_cls.process
            original_process = stage_cls.process

            async def savage_allowlist_process(stage_self: Any, event: Any):
                active = cls._active_module
                if active is None or not active._enabled:
                    res = original_process(stage_self, event)
                    return await res if inspect.isawaitable(res) else res

                token = _allowed_builtin_commands.set(active.allowed_commands)
                had_disable = hasattr(stage_self, "disable_builtin_commands")
                proxy = _DisableBuiltinCommandsStageProxy(stage_self) if had_disable else stage_self
                try:
                    res = original_process(proxy, event)
                    return await res if inspect.isawaitable(res) else res
                finally:
                    _allowed_builtin_commands.reset(token)

            stage_cls.process = savage_allowlist_process

        if cls._original_get_handlers is None:
            cls._registry = registry
            cls._adapter_event_type = adapter_event_type
            original_get_handlers = registry.get_handlers_by_event_type
            cls._original_get_handlers = original_get_handlers

            def savage_get_handlers_by_event_type(registry_self: Any, *args: Any, **kwargs: Any):
                event_type = args[0] if args else kwargs.get("event_type")
                handlers = original_get_handlers(*args, **kwargs)
                allowed = _allowed_builtin_commands.get()
                if allowed is None or (cls._adapter_event_type and event_type != cls._adapter_event_type):
                    return handlers
                return [h for h in handlers if should_keep_handler(h, allowed)]

            registry.get_handlers_by_event_type = types.MethodType(
                savage_get_handlers_by_event_type,
                registry,
            )

        cls._active_module = self
        self._installed = True
        self._log("info", f"SavageType 已启用内置指令白名单过滤: {sorted(self._allowlist)}")
        return True

    def terminate(self) -> None:
        cls = type(self)
        if self._installed and cls._active_module is self:
            cls.restore_patch()
        self._installed = False

    @classmethod
    def restore_patch(cls) -> None:
        if cls._stage_cls and cls._original_process:
            cls._stage_cls.process = cls._original_process
        if cls._registry and cls._original_get_handlers:
            cls._registry.get_handlers_by_event_type = cls._original_get_handlers
        cls._stage_cls = None
        cls._registry = None
        cls._adapter_event_type = None
        cls._original_process = None
        cls._original_get_handlers = None
        cls._active_module = None

    def _load_waking_check_stage(self) -> type | None:
        try:
            from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
            return WakingCheckStage
        except Exception:
            return None

    def _load_star_registry(self) -> tuple[Any | None, Any | None]:
        try:
            from astrbot.core.star.star_handler import EventType, star_handlers_registry
            return star_handlers_registry, getattr(EventType, "AdapterMessageEvent", None)
        except Exception:
            return None, None

    def _log(self, level: str, msg: str) -> None:
        logger_func = getattr(self.logger, level, None)
        if callable(logger_func):
            logger_func(msg)
