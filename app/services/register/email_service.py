"""
临时邮箱服务

提供创建临时邮箱和获取邮件内容的功能。
"""

from typing import Optional
import httpx

from app.core.config import get_config


class EmailService:
    """临时邮箱服务类"""

    def __init__(self):
        self.api_key: str = get_config("register.mail_api_key", "")
        self.domain: str = get_config("register.mail_domain", "")
        self.api_url: str = get_config("register.mail_api_url", "")

    async def create_email(self) -> Optional[str]:
        """创建临时邮箱地址"""
        raise NotImplementedError("待实现")

    async def fetch_first_email(
        self, email: str, timeout: int = 120
    ) -> Optional[str]:
        """获取指定邮箱的第一封邮件内容"""
        raise NotImplementedError("待实现")