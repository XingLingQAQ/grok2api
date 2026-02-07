"""代理池自动刷新/测活调度器"""

import asyncio
import time
from typing import Optional
from datetime import datetime

from app.core.config import get_config
from app.core.logger import logger


class ProxyScheduler:
    """代理池自动抓取/测活调度器"""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_fetch_ts: float = 0
        self._last_check_ts: float = 0
        self._last_threshold_ts: float = 0  # 阈值触发冷却

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "last_fetch": datetime.fromtimestamp(self._last_fetch_ts).isoformat() if self._last_fetch_ts else None,
            "last_check": datetime.fromtimestamp(self._last_check_ts).isoformat() if self._last_check_ts else None,
        }

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("ProxyScheduler: started")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("ProxyScheduler: stopped")

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(60)
                if not self._running:
                    break
                if not get_config("proxy.enabled", False):
                    continue
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ProxyScheduler error: {e}")

    async def _tick(self):
        from app.services.proxy.pool import get_proxy_pool
        pool = get_proxy_pool()
        now = time.time()

        # 跳过：如果已有任务在运行
        if pool.is_fetching or pool.is_checking:
            return

        # 自动抓取
        fetch_interval = get_config("proxy.auto_fetch_interval", 0)
        if fetch_interval > 0:
            if self._last_fetch_ts == 0:
                self._last_fetch_ts = now
            elif (now - self._last_fetch_ts) >= fetch_interval * 60:
                logger.info("ProxyScheduler: auto fetch triggered")
                asyncio.create_task(self._run_fetch(pool))
                return  # 一次 tick 只触发一个任务

        # 自动测活
        check_interval = get_config("proxy.auto_check_interval", 0)
        if check_interval > 0:
            if self._last_check_ts == 0:
                self._last_check_ts = now
            elif (now - self._last_check_ts) >= check_interval * 60:
                concurrent = get_config("proxy.check_concurrent", 100)
                logger.info("ProxyScheduler: auto check triggered")
                asyncio.create_task(self._run_check(pool, concurrent))
                return

        # 阈值触发（带冷却：至少间隔 10 分钟）
        threshold = get_config("proxy.alive_threshold", 0)
        threshold_cooldown = 600
        if threshold > 0 and pool.alive_count < threshold:
            if (now - self._last_threshold_ts) >= threshold_cooldown:
                self._last_threshold_ts = time.time()
                logger.info(f"ProxyScheduler: alive threshold triggered ({pool.alive_count}<{threshold})")
                asyncio.create_task(self._run_fetch_then_check(pool))
            else:
                remaining = int(threshold_cooldown - (now - self._last_threshold_ts))
                logger.debug(f"ProxyScheduler: threshold cooldown, {remaining}s remaining")

    async def _run_fetch(self, pool):
        """后台执行抓取"""
        try:
            await pool.fetch_proxies()
        except Exception as e:
            logger.error(f"ProxyScheduler: fetch error: {e}")
        finally:
            self._last_fetch_ts = time.time()

    async def _run_check(self, pool, concurrent: int):
        """后台执行测活"""
        try:
            await pool.check_proxies(concurrent)
        except Exception as e:
            logger.error(f"ProxyScheduler: check error: {e}")
        finally:
            self._last_check_ts = time.time()

    async def _run_fetch_then_check(self, pool):
        """后台执行抓取+测活"""
        try:
            await pool.fetch_proxies()
            self._last_fetch_ts = time.time()
            concurrent = get_config("proxy.check_concurrent", 100)
            await pool.check_proxies(concurrent)
            self._last_check_ts = time.time()
        except Exception as e:
            logger.error(f"ProxyScheduler: fetch+check error: {e}")


_proxy_scheduler: Optional[ProxyScheduler] = None


def get_proxy_scheduler() -> ProxyScheduler:
    global _proxy_scheduler
    if _proxy_scheduler is None:
        _proxy_scheduler = ProxyScheduler()
    return _proxy_scheduler
