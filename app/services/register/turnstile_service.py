"""
Turnstile 验证服务

提供 Cloudflare Turnstile 验证码破解功能。
仅支持本地 Solver 服务调用。
"""

import asyncio
from typing import Optional

import httpx

from app.core.config import get_config
from app.core.logger import logger


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self, solver_url: Optional[str] = None):
        """
        初始化 Turnstile 服务

        Args:
            solver_url: Solver 服务地址，默认从配置读取
        """
        self.solver_url = solver_url or get_config(
            "register.turnstile_solver_url", "http://127.0.0.1:5072"
        )
        self._timeout = get_config("register.turnstile_timeout", 60.0)

    async def create_task(self, site_url: str, site_key: str) -> Optional[str]:
        """
        创建 Turnstile 验证任务

        Args:
            site_url: 目标网站 URL
            site_key: Turnstile site key

        Returns:
            task_id 或 None 如果失败
        """
        url = f"{self.solver_url}/turnstile"
        params = {"url": site_url, "sitekey": site_key}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("errorId") == 0:
                        task_id = data.get("taskId")
                        logger.debug(f"Turnstile 任务创建成功: {task_id}")
                        return task_id
                    else:
                        logger.warning(
                            f"Turnstile 任务创建失败: {data.get('errorDescription')}"
                        )
                        return None
                else:
                    logger.warning(f"Turnstile Solver 返回错误: {res.status_code}")
                    return None
        except httpx.ConnectError:
            logger.warning(f"无法连接 Turnstile Solver: {self.solver_url}")
            return None
        except Exception as e:
            logger.warning(f"创建 Turnstile 任务异常: {e}")
            return None

    async def get_response(
        self,
        task_id: str,
        max_retries: int = 30,
        initial_delay: float = 5.0,
        retry_delay: float = 2.0,
    ) -> Optional[str]:
        """
        获取 Turnstile 验证结果

        Args:
            task_id: 任务 ID
            max_retries: 最大重试次数
            initial_delay: 首次查询前等待时间（秒）
            retry_delay: 重试间隔（秒）

        Returns:
            验证 token 或 None 如果失败/超时
        """
        # 首次等待
        await asyncio.sleep(initial_delay)

        url = f"{self.solver_url}/result"
        params = {"id": task_id}

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get(url, params=params)
                    if res.status_code != 200:
                        logger.debug(f"Turnstile 查询失败: {res.status_code}")
                        await asyncio.sleep(retry_delay)
                        continue

                    data = res.json()

                    # 检查是否仍在处理中
                    if data.get("status") == "processing":
                        await asyncio.sleep(retry_delay)
                        continue

                    # 检查错误
                    if data.get("errorId") != 0:
                        error_code = data.get("errorCode", "UNKNOWN")
                        if error_code == "ERROR_CAPTCHA_UNSOLVABLE":
                            logger.warning("Turnstile 验证失败: 无法解决")
                            return None
                        logger.warning(f"Turnstile 错误: {error_code}")
                        return None

                    # 获取 token
                    token = data.get("solution", {}).get("token")
                    if token and token != "CAPTCHA_FAIL":
                        logger.debug(f"Turnstile 验证成功: {token[:20]}...")
                        return token
                    elif token == "CAPTCHA_FAIL":
                        logger.warning("Turnstile 验证失败: CAPTCHA_FAIL")
                        return None

                    # 继续等待
                    await asyncio.sleep(retry_delay)

            except httpx.ConnectError:
                logger.warning(f"无法连接 Turnstile Solver: {self.solver_url}")
                return None
            except Exception as e:
                logger.debug(f"获取 Turnstile 结果异常: {e}")
                await asyncio.sleep(retry_delay)

        logger.warning(f"Turnstile 验证超时: 已重试 {max_retries} 次")
        return None