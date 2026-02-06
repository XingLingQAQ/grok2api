"""
Turnstile 验证服务

使用集成的原始 Solver 进行验证码破解。
"""

import asyncio
from typing import Optional

from app.core.config import get_config
from app.core.logger import logger


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self):
        self._timeout = get_config("register.turnstile_timeout", 60.0)

    async def create_task(self, site_url: str, site_key: str) -> Optional[str]:
        """创建 Turnstile 验证任务"""
        try:
            from app.services.register.turnstile_solver import get_turnstile_solver, TurnstileSolverWrapper

            solver = await get_turnstile_solver()
            if solver is None:
                logger.warning("Turnstile Solver 未初始化")
                return None

            wrapper = TurnstileSolverWrapper(solver)
            task_id = await wrapper.create_task(site_url, site_key)
            if task_id:
                logger.debug(f"Turnstile 任务创建成功: {task_id}")
            return task_id

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
        """获取 Turnstile 验证结果"""
        await asyncio.sleep(initial_delay)

        try:
            from app.services.register.turnstile_solver import get_turnstile_solver, TurnstileSolverWrapper

            solver = await get_turnstile_solver()
            if solver is None:
                return None

            wrapper = TurnstileSolverWrapper(solver)

            for attempt in range(max_retries):
                result = await wrapper.get_result(task_id)

                if not result:
                    logger.debug(f"Turnstile 任务不存在: {task_id}")
                    return None

                status = result.get("status")

                if status == "processing":
                    await asyncio.sleep(retry_delay)
                    continue

                if status == "ready":
                    token = result.get("token")
                    if token:
                        logger.debug(f"Turnstile 验证成功: {token[:20]}...")
                        return token

                if status == "failed":
                    error = result.get("error", "未知错误")
                    logger.warning(f"Turnstile 验证失败: {error}")
                    return None

                await asyncio.sleep(retry_delay)

        except Exception as e:
            logger.warning(f"获取 Turnstile 结果异常: {e}")
            return None

        logger.warning(f"Turnstile 验证超时: 已重试 {max_retries} 次")
        return None
