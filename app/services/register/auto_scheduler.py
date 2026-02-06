"""自动注册调度器"""

import asyncio
import time
from typing import Optional
from datetime import datetime

from app.core.config import get_config
from app.core.logger import logger


class AutoRegisterScheduler:
    """自动注册调度器 — 按定时/阈值触发批量注册"""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_trigger: Optional[str] = None
        self._last_reason: Optional[str] = None
        self._last_trigger_ts: float = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict:
        interval = get_config("register.auto_register_interval", 0)
        next_trigger = None
        if self._running and interval > 0 and self._last_trigger_ts > 0:
            next_ts = self._last_trigger_ts + interval * 60
            next_trigger = datetime.fromtimestamp(next_ts).isoformat()
        return {
            "running": self._running,
            "last_trigger": self._last_trigger,
            "last_reason": self._last_reason,
            "next_trigger": next_trigger,
        }

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("AutoRegisterScheduler: started")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("AutoRegisterScheduler: stopped")

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(60)
                if not self._running:
                    break
                if not get_config("register.auto_register", False):
                    continue
                reason = await self._check_triggers()
                if reason:
                    await self._trigger(reason)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AutoRegisterScheduler error: {e}")

    async def _check_triggers(self) -> Optional[str]:
        """检查触发条件，返回触发原因或 None"""
        from app.services.register.task_manager import get_task_manager
        mgr = get_task_manager()
        if mgr.is_running:
            return None

        now = time.time()

        # 定时触发
        interval = get_config("register.auto_register_interval", 0)
        if interval > 0:
            if self._last_trigger_ts == 0:
                self._last_trigger_ts = now
            elif (now - self._last_trigger_ts) >= interval * 60:
                return "interval"

        # Token 数量阈值
        token_threshold = get_config("register.auto_register_token_threshold", 0)
        if token_threshold > 0:
            try:
                from app.services.token.manager import get_token_manager
                tmgr = await get_token_manager()
                stats = tmgr.get_stats()
                total_active = sum(s.get("active", 0) for s in stats.values())
                if total_active < token_threshold:
                    return f"token_count:{total_active}<{token_threshold}"
            except Exception as e:
                logger.debug(f"AutoRegister token check error: {e}")

        # Chat 剩余配额阈值
        chat_threshold = get_config("register.auto_register_chat_threshold", 0)
        if chat_threshold > 0:
            try:
                from app.services.token.manager import get_token_manager
                tmgr = await get_token_manager()
                stats = tmgr.get_stats()
                total_quota = sum(s.get("total_quota", 0) for s in stats.values())
                if total_quota < chat_threshold:
                    return f"chat_quota:{total_quota}<{chat_threshold}"
            except Exception as e:
                logger.debug(f"AutoRegister quota check error: {e}")

        return None

    async def _trigger(self, reason: str):
        """执行自动注册"""
        from app.services.register.task_manager import get_task_manager

        count = get_config("register.auto_register_count", 10)
        concurrent = get_config("register.auto_register_concurrent", 8)
        mode = get_config("register.auto_register_mode", "normal")

        try:
            mgr = get_task_manager()
            task_id = await mgr.start(count, concurrent, mode=mode)
            self._last_trigger = datetime.now().isoformat()
            self._last_trigger_ts = time.time()
            self._last_reason = reason
            logger.info(
                f"AutoRegister triggered: reason={reason}, count={count}, "
                f"mode={mode}, task_id={task_id}"
            )
        except RuntimeError as e:
            logger.warning(f"AutoRegister trigger failed: {e}")
        except Exception as e:
            logger.error(f"AutoRegister trigger error: {e}")

    async def trigger_manual(self) -> Optional[str]:
        """手动触发一次，返回 task_id"""
        from app.services.register.task_manager import get_task_manager
        mgr = get_task_manager()
        if mgr.is_running:
            return None

        count = get_config("register.auto_register_count", 10)
        concurrent = get_config("register.auto_register_concurrent", 8)
        mode = get_config("register.auto_register_mode", "normal")

        task_id = await mgr.start(count, concurrent, mode=mode)
        self._last_trigger = datetime.now().isoformat()
        self._last_trigger_ts = time.time()
        self._last_reason = "manual"
        return task_id


_auto_register_scheduler: Optional[AutoRegisterScheduler] = None


def get_auto_register_scheduler() -> AutoRegisterScheduler:
    global _auto_register_scheduler
    if _auto_register_scheduler is None:
        _auto_register_scheduler = AutoRegisterScheduler()
    return _auto_register_scheduler
