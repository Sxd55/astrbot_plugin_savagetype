"""自动清理 AstrBot 缓存模块：在每日凌晨空闲时自动调用原生 StorageCleaner 清理磁盘临时缓存。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import time
from typing import Any


CACHE_CLEANUP_TARGET = "cache"
CLEANUP_HOUR = 0
CLEANUP_MINUTE = 0
STARTUP_GRACE_SECONDS = 10 * 60      # 开机 10 分钟内不清理
IDLE_GRACE_SECONDS = 10 * 60         # 距离上次活动 10 分钟内不清理
RETRY_DELAY_SECONDS = 30 * 60        # 失败或忙碌时 30 分钟后重试


class AutoCacheCleanupModule:
    def __init__(self, logger: Any):
        self.logger = logger
        self._enabled = False
        self._task: asyncio.Task | None = None
        self._cleanup_lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_activity_at = self._started_at
        self._active_requests = 0
        self._last_success_date: str | None = None

    def configure(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def start(self) -> None:
        if not self._enabled:
            self.terminate()
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_scheduler())
            self._log("info", "SavageType 已启动 AstrBot 缓存自动定时清理服务 (每日 00:00 空闲执行)")
        except RuntimeError:
            pass

    def terminate(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._enabled = False

    def mark_activity(self) -> None:
        self._last_activity_at = time.monotonic()

    def begin_request_activity(self) -> None:
        self._active_requests += 1
        self.mark_activity()

    def end_request_activity(self) -> None:
        if self._active_requests > 0:
            self._active_requests -= 1
        self.mark_activity()

    def _is_idle(self) -> tuple[bool, str]:
        now = time.monotonic()
        uptime = now - self._started_at
        if uptime < STARTUP_GRACE_SECONDS:
            return False, f"开机等待缓冲中(剩余 {int(STARTUP_GRACE_SECONDS - uptime)} 秒)"
        if self._active_requests > 0:
            return False, f"存在 {self._active_requests} 个正在处理的请求"
        idle_time = now - self._last_activity_at
        if idle_time < IDLE_GRACE_SECONDS:
            return False, f"近期有活动(静默时间不足，剩余 {int(IDLE_GRACE_SECONDS - idle_time)} 秒)"
        return True, ""

    def _seconds_until_next_cleanup(self) -> float:
        now = datetime.now()
        scheduled = now.replace(
            hour=CLEANUP_HOUR,
            minute=CLEANUP_MINUTE,
            second=0,
            microsecond=0,
        )
        if scheduled <= now:
            scheduled += timedelta(days=1)
        return max((scheduled - now).total_seconds(), 0.0)

    async def try_cleanup_now(self) -> dict[str, Any]:
        if not self._enabled:
            return {"status": "skipped", "reason": "disabled"}
        if self._cleanup_lock.locked():
            return {"status": "skipped", "reason": "already_running"}

        is_idle, reason = self._is_idle()
        if not is_idle:
            self._log("debug", f"SavageType 暂缓清理缓存: {reason}")
            return {"status": "skipped", "reason": reason}

        async with self._cleanup_lock:
            return await self._cleanup_cache()

    async def _cleanup_cache(self) -> dict[str, Any]:
        try:
            from astrbot.core.utils.storage_cleaner import StorageCleaner
            cleaner = StorageCleaner({})
            res = await asyncio.to_thread(cleaner.cleanup, CACHE_CLEANUP_TARGET)
            removed = res.get("removed_bytes", 0) if isinstance(res, dict) else 0
            deleted = res.get("deleted_files", 0) if isinstance(res, dict) else 0
            self._log("info", f"SavageType 自动清理 AstrBot 缓存完成: 释放 {removed} 字节，清理 {deleted} 个文件")
            return {"status": "success", "result": res}
        except Exception as exc:
            self._log("warning", f"SavageType 自动清理 AstrBot 缓存异常: {exc}")
            return {"status": "failed", "error": str(exc)}

    async def _run_scheduler(self) -> None:
        while self._enabled:
            try:
                wait_sec = self._seconds_until_next_cleanup()
                await asyncio.sleep(wait_sec)
                today = datetime.now().date().isoformat()
                while self._enabled and self._last_success_date != today:
                    res = await self.try_cleanup_now()
                    if res.get("status") == "success":
                        self._last_success_date = today
                        break
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log("warning", f"SavageType 缓存清理调度器发生异常: {exc}")
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    def _log(self, level: str, msg: str) -> None:
        logger_func = getattr(self.logger, level, None)
        if callable(logger_func):
            logger_func(msg)
