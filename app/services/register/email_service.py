"""
临时邮箱服务

提供创建临时邮箱和获取邮件内容的功能。
使用 mail.xingling.one API。
"""

import random
import string
from typing import Optional, Tuple

import httpx

from app.core.config import get_config
from app.core.logger import logger


class EmailService:
    """临时邮箱服务类"""

    def __init__(self):
        self.api_key: str = get_config("register.mail_api_key", "")
        self.domain: str = get_config("register.mail_domain", "")
        self.api_url: str = get_config(
            "register.mail_api_url", "https://mail.xingling.one"
        )
        self._timeout = 15.0

    def _generate_random_name(self) -> str:
        """生成随机邮箱名称"""
        letters1 = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(4, 6))
        )
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(0, 5))
        )
        return letters1 + numbers + letters2

    async def _fetch_default_domain(self) -> str:
        """从 API 获取默认邮箱域名"""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                res = await client.get(
                    f"{self.api_url}/api/config",
                    headers={"X-API-Key": self.api_key},
                )
                if res.status_code == 200:
                    domains = res.json().get("emailDomains", "")
                    return domains.split(",")[0] if domains else "moemail.app"
        except Exception as e:
            logger.warning(f"获取默认邮箱域名失败: {e}")
        return "moemail.app"

    async def create_email(self) -> Tuple[Optional[str], Optional[str]]:
        """
        创建临时邮箱

        Returns:
            (email_id, email_address) 或 (None, None) 如果失败
        """
        if not self.api_key:
            logger.error("邮箱服务未配置 API Key")
            return None, None

        # 如果未配置域名，尝试获取默认域名
        domain = self.domain
        if not domain:
            domain = await self._fetch_default_domain()

        url = f"{self.api_url}/api/emails/generate"
        random_name = self._generate_random_name()

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                res = await client.post(
                    url,
                    json={
                        "name": random_name,
                        "domain": domain,
                        "expiryTime": 3600000,  # 1 小时
                    },
                    headers={
                        "X-API-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                )
                if res.status_code == 200:
                    data = res.json()
                    email_id = data.get("id")
                    email_addr = data.get("email")
                    logger.debug(f"创建邮箱成功: {email_addr}")
                    return email_id, email_addr
                else:
                    logger.warning(
                        f"创建邮箱失败: {res.status_code} - {res.text[:200]}"
                    )
                    return None, None
        except httpx.TimeoutException:
            logger.warning(f"创建邮箱超时: {url}")
            return None, None
        except Exception as e:
            logger.warning(f"创建邮箱异常: {e}")
            return None, None

    async def fetch_first_email(self, email_id: str) -> Optional[str]:
        """
        获取指定邮箱的第一封邮件内容

        Args:
            email_id: 邮箱的唯一标识符

        Returns:
            邮件 HTML 内容，或 None 如果没有邮件
        """
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # 获取邮件列表
                res = await client.get(
                    f"{self.api_url}/api/emails/{email_id}",
                    headers={"X-API-Key": self.api_key},
                )

                if res.status_code != 200:
                    return None

                data = res.json()
                messages = data.get("messages", [])
                if not messages:
                    return None

                # 获取第一封邮件内容
                message_id = messages[0]["id"]
                return await self._fetch_message_content(client, email_id, message_id)
        except Exception as e:
            logger.debug(f"获取邮件失败: {e}")
            return None

    async def _fetch_message_content(
        self, client: httpx.AsyncClient, email_id: str, message_id: str
    ) -> Optional[str]:
        """获取单封邮件内容"""
        try:
            res = await client.get(
                f"{self.api_url}/api/emails/{email_id}/{message_id}",
                headers={"X-API-Key": self.api_key},
            )
            if res.status_code == 200:
                msg = res.json().get("message", {})
                return msg.get("html") or msg.get("content")
            return None
        except Exception:
            return None