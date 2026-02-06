"""
Turnstile 验证服务

提供 Cloudflare Turnstile 验证码破解功能。
"""

from typing import Optional


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self):
        pass

    async def create_task(self, site_key: str, page_url: str) -> Optional[str]:
        """创建验证任务，返回 task_id"""
        raise NotImplementedError("待实现")

    async def get_response(self, task_id: str, timeout: int = 60) -> Optional[str]:
        """获取验证结果 token"""
        raise NotImplementedError("待实现")