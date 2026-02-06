"""
Turnstile Solver 集成模块

直接使用原始 api_solver.py 中的 TurnstileAPIServer 类，
不做任何修改，仅提供 FastAPI 集成接口。
"""

import asyncio
from typing import Optional, Dict, Any

from app.core.config import get_config
from app.core.logger import logger


# 全局 Solver 实例
_solver = None
_solver_lock = asyncio.Lock()


async def get_turnstile_solver():
    """获取全局 Turnstile Solver 实例"""
    global _solver
    return _solver


async def init_turnstile_solver() -> None:
    """初始化全局 Turnstile Solver（使用原始 TurnstileAPIServer）"""
    global _solver

    async with _solver_lock:
        if _solver is not None:
            return

        pool_size = int(get_config("register.turnstile_solver_threads", 5))
        headless = get_config("register.turnstile_headless", True)

        # 导入原始的 TurnstileAPIServer
        from app.services.register.api_solver import TurnstileAPIServer

        # 创建实例（使用原始参数）
        _solver = TurnstileAPIServer(
            headless=headless,
            useragent=None,  # 使用随机配置
            debug=True,
            browser_type="chromium",
            thread=pool_size,
            proxy_support=False,
            use_random_config=True,
            browser_name=None,
            browser_version=None,
        )

        # 初始化浏览器池（调用原始的 _initialize_browser）
        from app.services.register.db_results import init_db
        await init_db()
        await _solver._initialize_browser()

        logger.info(f"Turnstile Solver 初始化完成，共 {pool_size} 个浏览器实例")


async def close_turnstile_solver() -> None:
    """关闭全局 Turnstile Solver"""
    global _solver

    async with _solver_lock:
        if _solver is None:
            return

        # 关闭所有浏览器
        while not _solver.browser_pool.empty():
            try:
                index, browser, config = await asyncio.wait_for(
                    _solver.browser_pool.get(), timeout=1.0
                )
                try:
                    await browser.close()
                except Exception:
                    pass
            except asyncio.TimeoutError:
                break

        _solver = None
        logger.info("Turnstile Solver 已关闭")


class TurnstileSolverWrapper:
    """Turnstile Solver 包装器，提供简化的接口"""

    def __init__(self, solver):
        self._solver = solver

    @property
    def is_initialized(self) -> bool:
        return self._solver is not None and not self._solver.browser_pool.empty()

    async def create_task(self, site_url: str, site_key: str) -> Optional[str]:
        """创建验证任务"""
        if self._solver is None:
            return None

        import uuid
        import time
        from app.services.register.db_results import save_result

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": site_url,
            "sitekey": site_key,
        })

        # 启动解决任务
        asyncio.create_task(
            self._solver._solve_turnstile(
                task_id=task_id,
                url=site_url,
                sitekey=site_key,
                action=None,
                cdata=None,
            )
        )

        return task_id

    async def get_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务结果"""
        from app.services.register.db_results import load_result

        result = await load_result(task_id)
        if not result:
            return None

        # 转换为统一格式
        if result == "CAPTCHA_NOT_READY" or (isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"):
            return {"status": "processing"}

        if isinstance(result, dict):
            if result.get("value") == "CAPTCHA_FAIL":
                return {"status": "failed", "error": "验证码解决失败"}
            if result.get("value") and result.get("value") != "CAPTCHA_FAIL":
                return {"status": "ready", "token": result["value"]}

        return {"status": "failed", "error": "未知错误"}
