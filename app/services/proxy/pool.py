"""
代理池服务

从 GitHub 和网络抓取免费代理，并进行测活管理。
"""

import asyncio
import time
import re
from typing import Optional, List, Dict, Any, Set
from dataclasses import dataclass, field, asdict
from datetime import datetime

import httpx

from app.core.config import get_config
from app.core.logger import logger


@dataclass
class ProxyInfo:
    """代理信息"""
    proxy: str  # 格式: http://ip:port 或 socks5://ip:port
    alive: bool = False
    latency: float = 0.0  # 毫秒
    last_check: Optional[str] = None
    fail_count: int = 0
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 免费代理源
PROXY_SOURCES = [
    # GitHub 代理列表
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt",
]

# 测试 URL
TEST_URL = "https://httpbin.org/ip"
TEST_TIMEOUT = 10


class ProxyPool:
    """代理池管理"""

    def __init__(self):
        self._proxies: Dict[str, ProxyInfo] = {}
        self._alive_proxies: List[str] = []
        self._lock = asyncio.Lock()
        self._fetching = False
        self._checking = False
        self._last_fetch: Optional[str] = None
        self._last_check: Optional[str] = None
        # 进度追踪
        self._fetch_progress: Dict[str, Any] = {"done": 0, "total": 0, "new": 0}
        self._check_progress: Dict[str, Any] = {"done": 0, "total": 0, "alive": 0}

    @property
    def total_count(self) -> int:
        return len(self._proxies)

    @property
    def alive_count(self) -> int:
        return len(self._alive_proxies)

    @property
    def is_fetching(self) -> bool:
        return self._fetching

    @property
    def is_checking(self) -> bool:
        return self._checking

    def get_status(self) -> Dict[str, Any]:
        """获取代理池状态"""
        return {
            "total": self.total_count,
            "alive": self.alive_count,
            "fetching": self._fetching,
            "checking": self._checking,
            "last_fetch": self._last_fetch,
            "last_check": self._last_check,
            "fetch_progress": self._fetch_progress if self._fetching else None,
            "check_progress": self._check_progress if self._checking else None,
        }

    def get_alive_proxies(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取存活代理列表"""
        result = []
        for proxy_str in self._alive_proxies[:limit]:
            if proxy_str in self._proxies:
                result.append(self._proxies[proxy_str].to_dict())
        return result

    def get_random_proxy(self) -> Optional[str]:
        """随机获取一个存活代理"""
        if not self._alive_proxies:
            return None
        import random
        return random.choice(self._alive_proxies)

    async def fetch_proxies(self) -> int:
        """从源抓取代理"""
        if self._fetching:
            return 0

        self._fetching = True
        new_count = 0
        total_sources = len(PROXY_SOURCES)
        self._fetch_progress = {"done": 0, "total": total_sources, "new": 0}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for i, url in enumerate(PROXY_SOURCES):
                    try:
                        result = await self._fetch_from_source(client, url)
                        for proxy in result:
                            if proxy not in self._proxies:
                                self._proxies[proxy] = ProxyInfo(
                                    proxy=proxy,
                                    source=url,
                                )
                                new_count += 1
                    except Exception as e:
                        logger.debug(f"代理源抓取失败: {url} - {e}")
                    self._fetch_progress = {
                        "done": i + 1,
                        "total": total_sources,
                        "new": new_count,
                    }

            self._last_fetch = datetime.now().isoformat()
            logger.info(f"代理抓取完成: 新增 {new_count}, 总计 {self.total_count}")

        except Exception as e:
            logger.error(f"代理抓取异常: {e}")
        finally:
            self._fetching = False

        return new_count

    async def _fetch_from_source(self, client: httpx.AsyncClient, url: str) -> List[str]:
        """从单个源抓取代理"""
        proxies = []
        try:
            response = await client.get(url)
            if response.status_code == 200:
                text = response.text
                # 解析代理格式: ip:port
                pattern = r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5})'
                matches = re.findall(pattern, text)

                for match in matches:
                    # 根据源 URL 判断协议
                    if 'socks5' in url.lower():
                        proxy = f"socks5://{match}"
                    else:
                        proxy = f"http://{match}"
                    proxies.append(proxy)

        except Exception as e:
            logger.debug(f"抓取 {url} 失败: {e}")

        return proxies

    async def check_proxies(self, max_concurrent: int = 100) -> int:
        """测活所有代理"""
        if self._checking:
            return 0

        self._checking = True
        alive_count = 0
        proxy_keys = list(self._proxies.keys())
        total = len(proxy_keys)
        done = 0
        self._check_progress = {"done": 0, "total": total, "alive": 0}

        try:
            semaphore = asyncio.Semaphore(max_concurrent)

            async def _check_and_track(proxy_str: str):
                nonlocal done, alive_count
                result = await self._check_single_proxy(semaphore, proxy_str)
                done += 1
                if result:
                    alive_count += 1
                self._check_progress = {"done": done, "total": total, "alive": alive_count}

            tasks = [_check_and_track(p) for p in proxy_keys]
            await asyncio.gather(*tasks, return_exceptions=True)

            # 更新存活列表
            self._alive_proxies = []
            for proxy_str, info in self._proxies.items():
                if info.alive:
                    self._alive_proxies.append(proxy_str)

            # 按延迟排序
            self._alive_proxies.sort(key=lambda p: self._proxies[p].latency)

            self._last_check = datetime.now().isoformat()
            logger.info(f"代理测活完成: 存活 {alive_count}/{total}")

        except Exception as e:
            logger.error(f"代理测活异常: {e}")
        finally:
            self._checking = False

        return alive_count

    async def _check_single_proxy(self, semaphore: asyncio.Semaphore, proxy_str: str) -> bool:
        """测试单个代理"""
        async with semaphore:
            info = self._proxies.get(proxy_str)
            if not info:
                return False

            start_time = time.time()
            try:
                proxies = {"http://": proxy_str, "https://": proxy_str}
                async with httpx.AsyncClient(proxies=proxies, timeout=TEST_TIMEOUT) as client:
                    response = await client.get(TEST_URL)
                    if response.status_code == 200:
                        latency = (time.time() - start_time) * 1000
                        info.alive = True
                        info.latency = round(latency, 2)
                        info.last_check = datetime.now().isoformat()
                        info.fail_count = 0
                        return True

            except Exception:
                pass

            info.alive = False
            info.fail_count += 1
            info.last_check = datetime.now().isoformat()

            # 失败次数过多则移除
            if info.fail_count >= 3:
                del self._proxies[proxy_str]

            return False

    def clear(self) -> None:
        """清空代理池"""
        self._proxies.clear()
        self._alive_proxies.clear()
        logger.info("代理池已清空")

    def add_proxy(self, proxy: str) -> bool:
        """手动添加代理"""
        if not proxy:
            return False

        # 标准化格式
        if not proxy.startswith(('http://', 'https://', 'socks5://')):
            proxy = f"http://{proxy}"

        if proxy not in self._proxies:
            self._proxies[proxy] = ProxyInfo(proxy=proxy, source="manual")
            return True
        return False

    def remove_proxy(self, proxy: str) -> bool:
        """移除代理"""
        if proxy in self._proxies:
            del self._proxies[proxy]
            if proxy in self._alive_proxies:
                self._alive_proxies.remove(proxy)
            return True
        return False


# 全局代理池实例
_proxy_pool: Optional[ProxyPool] = None


def get_proxy_pool() -> ProxyPool:
    """获取全局代理池实例"""
    global _proxy_pool
    if _proxy_pool is None:
        _proxy_pool = ProxyPool()
    return _proxy_pool
