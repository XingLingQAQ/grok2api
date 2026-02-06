"""
Playwright 浏览器池管理

提供浏览器实例的池化管理，支持并发复用。
用于 Turnstile 验证码破解。
"""

import asyncio
import random
from typing import Optional, Tuple, Any
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.config import get_config
from app.core.logger import logger


@dataclass
class BrowserConfig:
    """浏览器配置"""

    user_agent: str
    sec_ch_ua: str


class BrowserPool:
    """浏览器实例池管理类"""

    # Chrome 版本配置池
    CHROME_VERSIONS = ["120.0.0.0", "121.0.0.0", "122.0.0.0", "124.0.0.0", "125.0.0.0"]

    def __init__(self, pool_size: int = 5, headless: bool = True):
        """
        初始化浏览器池

        Args:
            pool_size: 池大小（浏览器实例数）
            headless: 是否无头模式
        """
        self.pool_size = pool_size
        self.headless = headless
        self._pool: asyncio.Queue = asyncio.Queue()
        self._playwright = None
        self._browsers: list = []
        self._initialized = False
        self._lock = asyncio.Lock()

    @staticmethod
    def _generate_browser_config() -> BrowserConfig:
        """生成随机浏览器配置"""
        version = random.choice(BrowserPool.CHROME_VERSIONS)
        major_version = version.split(".")[0]
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
        sec_ch_ua = (
            f'"Not(A:Brand";v="99", "Google Chrome";v="{major_version}", '
            f'"Chromium";v="{major_version}"'
        )
        return BrowserConfig(user_agent=user_agent, sec_ch_ua=sec_ch_ua)

    async def initialize(self) -> None:
        """初始化浏览器池"""
        async with self._lock:
            if self._initialized:
                return

            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()

                for i in range(self.pool_size):
                    config = self._generate_browser_config()
                    browser_args = [
                        "--window-position=0,0",
                        "--force-device-scale-factor=1",
                        f"--user-agent={config.user_agent}",
                    ]

                    browser = await self._playwright.chromium.launch(
                        headless=self.headless,
                        args=browser_args,
                    )
                    self._browsers.append(browser)
                    await self._pool.put((i + 1, browser, config))
                    logger.debug(f"浏览器 {i + 1} 初始化成功")

                self._initialized = True
                logger.info(f"浏览器池初始化完成，共 {self.pool_size} 个实例")

            except ImportError:
                logger.error("Playwright 未安装，请运行: pip install playwright && playwright install chromium")
                raise
            except Exception as e:
                logger.error(f"浏览器池初始化失败: {e}")
                await self.close()
                raise

    async def close(self) -> None:
        """关闭浏览器池，释放所有资源"""
        async with self._lock:
            # 关闭所有浏览器实例
            for browser in self._browsers:
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"关闭浏览器异常: {e}")

            self._browsers.clear()

            # 清空队列
            while not self._pool.empty():
                try:
                    self._pool.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # 关闭 Playwright
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    logger.debug(f"关闭 Playwright 异常: {e}")
                self._playwright = None

            self._initialized = False
            logger.info("浏览器池已关闭")

    @asynccontextmanager
    async def acquire(self, timeout: float = 30.0):
        """
        获取一个浏览器实例（上下文管理器）

        Args:
            timeout: 获取超时时间（秒）

        Yields:
            (index, browser, config) 元组
        """
        if not self._initialized:
            raise RuntimeError("浏览器池未初始化")

        item = None
        try:
            item = await asyncio.wait_for(self._pool.get(), timeout=timeout)
            yield item
        except asyncio.TimeoutError:
            logger.warning(f"获取浏览器实例超时 ({timeout}s)")
            raise
        finally:
            if item is not None:
                # 检查浏览器是否仍然连接
                _, browser, _ = item
                try:
                    if browser.is_connected():
                        await self._pool.put(item)
                    else:
                        logger.warning("浏览器已断开连接，不归还到池中")
                except Exception as e:
                    logger.debug(f"归还浏览器异常: {e}")

    @property
    def available(self) -> int:
        """当前可用的浏览器实例数"""
        return self._pool.qsize()

    @property
    def is_initialized(self) -> bool:
        """是否已初始化"""
        return self._initialized


# 全局浏览器池实例
_browser_pool: Optional[BrowserPool] = None


async def get_browser_pool() -> BrowserPool:
    """获取全局浏览器池实例"""
    global _browser_pool
    if _browser_pool is None:
        pool_size = get_config("register.turnstile_solver_threads", 5)
        headless = get_config("register.turnstile_headless", True)
        _browser_pool = BrowserPool(pool_size=pool_size, headless=headless)
    return _browser_pool


async def init_browser_pool() -> None:
    """初始化全局浏览器池"""
    pool = await get_browser_pool()
    await pool.initialize()


async def close_browser_pool() -> None:
    """关闭全局浏览器池"""
    global _browser_pool
    if _browser_pool is not None:
        await _browser_pool.close()
        _browser_pool = None