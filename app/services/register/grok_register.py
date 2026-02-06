"""
Grok 账号注册核心逻辑

封装完整的注册流程：创建邮箱→发送验证码→获取验证码→验证→注册→获取 SSO Token
使用 curl_cffi 模拟浏览器指纹绕过 Cloudflare 检测
"""

import asyncio
import random
import re
import string
import struct
from typing import Optional, Callable, Any
from dataclasses import dataclass

from curl_cffi import requests as curl_requests

from app.core.config import get_config
from app.core.logger import logger
from app.services.register.email_service import EmailService
from app.services.register.turnstile_service import TurnstileService


# Grok 注册相关常量
SITE_URL = "https://accounts.x.ai"
DEFAULT_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_STATE_TREE = "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@dataclass
class RegisterResult:
    """注册结果"""

    success: bool
    email: Optional[str] = None
    password: Optional[str] = None
    sso_token: Optional[str] = None
    error: Optional[str] = None


class GrokRegister:
    """Grok 账号注册类"""

    def __init__(
        self,
        on_progress: Optional[Callable[[str, Any], None]] = None,
    ):
        """
        初始化注册器

        Args:
            on_progress: 进度回调函数，签名为 (event: str, data: Any) -> None
        """
        self.on_progress = on_progress
        self._cancelled = False
        self._email_service = EmailService()
        self._turnstile_service = TurnstileService()
        self._action_id: Optional[str] = None
        self._site_key = DEFAULT_SITE_KEY
        self._state_tree = DEFAULT_STATE_TREE

    def cancel(self) -> None:
        """取消当前注册任务"""
        self._cancelled = True

    def _emit(self, event: str, data: Any = None) -> None:
        """发送进度事件"""
        if self.on_progress:
            try:
                self.on_progress(event, data)
            except Exception:
                pass

    @staticmethod
    def _generate_random_name() -> str:
        """生成随机姓名"""
        length = random.randint(4, 6)
        return random.choice(string.ascii_uppercase) + "".join(
            random.choice(string.ascii_lowercase) for _ in range(length - 1)
        )

    @staticmethod
    def _generate_random_password(length: int = 15) -> str:
        """生成随机密码"""
        return "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(length)
        )

    @staticmethod
    def _encode_grpc_message(field_id: int, value: str) -> bytes:
        """编码 gRPC-Web 消息"""
        key = (field_id << 3) | 2
        value_bytes = value.encode("utf-8")
        length = len(value_bytes)
        payload = struct.pack("B", key) + struct.pack("B", length) + value_bytes
        return b"\x00" + struct.pack(">I", len(payload)) + payload

    @staticmethod
    def _encode_grpc_verify_message(email: str, code: str) -> bytes:
        """编码验证码验证的 gRPC-Web 消息"""
        p1 = (
            struct.pack("B", (1 << 3) | 2)
            + struct.pack("B", len(email))
            + email.encode("utf-8")
        )
        p2 = (
            struct.pack("B", (2 << 3) | 2)
            + struct.pack("B", len(code))
            + code.encode("utf-8")
        )
        payload = p1 + p2
        return b"\x00" + struct.pack(">I", len(payload)) + payload

    def _fetch_action_id_sync(self, session: curl_requests.Session) -> Optional[str]:
        """从注册页面获取 action_id（同步版本）"""
        try:
            res = session.get(f"{SITE_URL}/sign-up", timeout=15)
            if res.status_code != 200:
                logger.warning(f"获取注册页面失败: {res.status_code}")
                return None

            html = res.text

            # 提取 site_key
            key_match = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
            if key_match:
                self._site_key = key_match.group(1)

            # 提取 state_tree
            tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
            if tree_match:
                self._state_tree = tree_match.group(1)

            # 提取 JS 文件中的 action_id
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            js_urls = [
                script["src"]
                for script in soup.find_all("script", src=True)
                if "_next/static" in script["src"]
            ]

            for js_url in js_urls:
                if not js_url.startswith("http"):
                    js_url = f"{SITE_URL}{js_url}"
                try:
                    js_res = session.get(js_url, timeout=10)
                    if js_res.status_code == 200:
                        match = re.search(r"7f[a-fA-F0-9]{40}", js_res.text)
                        if match:
                            return match.group(0)
                except Exception:
                    continue

            return None
        except Exception as e:
            logger.warning(f"获取 action_id 失败: {e}")
            return None

    def _send_verification_code_sync(
        self, session: curl_requests.Session, email: str
    ) -> bool:
        """发送验证码到邮箱（同步版本）"""
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
        data = self._encode_grpc_message(1, email)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as e:
            logger.debug(f"发送验证码失败: {e}")
            return False

    def _verify_code_sync(
        self, session: curl_requests.Session, email: str, code: str
    ) -> bool:
        """验证邮箱验证码（同步版本）"""
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
        data = self._encode_grpc_verify_message(email, code)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as e:
            logger.debug(f"验证码验证失败: {e}")
            return False

    def _submit_registration_sync(
        self,
        session: curl_requests.Session,
        email: str,
        password: str,
        verify_code: str,
        turnstile_token: str,
    ) -> Optional[str]:
        """提交注册请求，返回 SSO Token（同步版本）"""
        import json

        headers = {
            "user-agent": USER_AGENT,
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up",
            "next-router-state-tree": self._state_tree,
            "next-action": self._action_id,
        }
        payload = [
            {
                "emailValidationCode": verify_code,
                "createUserAndSessionRequest": {
                    "email": email,
                    "givenName": self._generate_random_name(),
                    "familyName": self._generate_random_name(),
                    "clearTextPassword": password,
                    "tosAcceptedVersion": "$undefined",
                },
                "turnstileToken": turnstile_token,
                "promptOnDuplicateEmail": True,
            }
        ]

        try:
            res = session.post(
                f"{SITE_URL}/sign-up",
                data=json.dumps(payload),
                headers=headers,
                timeout=30,
            )

            if res.status_code != 200:
                logger.debug(f"注册请求失败: {res.status_code}")
                return None

            # 提取 set-cookie URL
            match = re.search(r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', res.text)
            if not match:
                logger.debug("未找到 set-cookie URL")
                return None

            verify_url = match.group(1)
            session.get(verify_url, timeout=15)

            # 获取 SSO Token
            sso = session.cookies.get("sso")
            return sso

        except Exception as e:
            logger.debug(f"提交注册失败: {e}")
            return None

    async def register_single(self) -> RegisterResult:
        """
        执行单次注册流程

        Returns:
            RegisterResult 包含注册结果
        """
        self._cancelled = False
        email = None
        password = self._generate_random_password()

        def _run_sync():
            """在线程中运行同步注册流程"""
            nonlocal email

            with curl_requests.Session(impersonate="chrome120") as session:
                # 预热连接
                try:
                    session.get(SITE_URL, timeout=10)
                except Exception:
                    pass

                # Step 0: 获取 action_id
                if not self._action_id:
                    self._emit("fetching_action_id")
                    self._action_id = self._fetch_action_id_sync(session)
                    if not self._action_id:
                        return RegisterResult(
                            success=False, error="无法获取 action_id"
                        )
                    self._emit("action_id_fetched", self._action_id)

                return session, None  # 返回 session 供后续使用

        try:
            # 在线程中执行初始化
            result = await asyncio.to_thread(_run_sync)
            if isinstance(result, RegisterResult):
                return result

            # 使用新的 session 继续
            def _continue_registration():
                nonlocal email

                with curl_requests.Session(impersonate="chrome120") as session:
                    # 预热
                    try:
                        session.get(SITE_URL, timeout=10)
                    except Exception:
                        pass

                    return session

            # 创建邮箱（异步）
            if self._cancelled:
                return RegisterResult(success=False, error="已取消")

            self._emit("creating_email")
            email_id, email = await self._email_service.create_email()
            if not email:
                return RegisterResult(success=False, error="创建邮箱失败")
            self._emit("email_created", email)

            if self._cancelled:
                return RegisterResult(success=False, email=email, error="已取消")

            # 发送验证码（同步，在线程中）
            def _send_code():
                with curl_requests.Session(impersonate="chrome120") as session:
                    try:
                        session.get(SITE_URL, timeout=10)
                    except Exception:
                        pass
                    return self._send_verification_code_sync(session, email)

            self._emit("sending_code", email)
            if not await asyncio.to_thread(_send_code):
                return RegisterResult(
                    success=False, email=email, error="发送验证码失败"
                )
            self._emit("code_sent", email)

            if self._cancelled:
                return RegisterResult(success=False, email=email, error="已取消")

            # 等待并获取验证码（异步）
            self._emit("waiting_code", email)
            verify_code = None
            for attempt in range(30):
                if self._cancelled:
                    return RegisterResult(
                        success=False, email=email, error="已取消"
                    )

                await asyncio.sleep(1)
                content = await self._email_service.fetch_first_email(email_id)
                if content:
                    match = re.search(r">([A-Z0-9]{3}-[A-Z0-9]{3})<", content)
                    if match:
                        verify_code = match.group(1).replace("-", "")
                        break

            if not verify_code:
                return RegisterResult(
                    success=False, email=email, error="未收到验证码"
                )
            self._emit("code_received", verify_code)

            # 验证验证码（同步，在线程中）
            def _verify():
                with curl_requests.Session(impersonate="chrome120") as session:
                    try:
                        session.get(SITE_URL, timeout=10)
                    except Exception:
                        pass
                    return self._verify_code_sync(session, email, verify_code)

            self._emit("verifying_code", email)
            if not await asyncio.to_thread(_verify):
                return RegisterResult(
                    success=False, email=email, error="验证码无效"
                )
            self._emit("code_verified", email)

            if self._cancelled:
                return RegisterResult(success=False, email=email, error="已取消")

            # 获取 Turnstile Token 并提交注册（最多重试 3 次）
            for captcha_attempt in range(3):
                if self._cancelled:
                    return RegisterResult(
                        success=False, email=email, error="已取消"
                    )

                self._emit("solving_captcha", captcha_attempt + 1)
                task_id = await self._turnstile_service.create_task(
                    SITE_URL, self._site_key
                )
                if not task_id:
                    continue

                turnstile_token = await self._turnstile_service.get_response(
                    task_id
                )
                if not turnstile_token:
                    self._emit("captcha_failed", captcha_attempt + 1)
                    continue

                self._emit("captcha_solved")

                # 提交注册（同步，在线程中）
                def _submit():
                    with curl_requests.Session(impersonate="chrome120") as session:
                        try:
                            session.get(SITE_URL, timeout=10)
                        except Exception:
                            pass
                        return self._submit_registration_sync(
                            session, email, password, verify_code, turnstile_token
                        )

                self._emit("submitting", email)
                sso_token = await asyncio.to_thread(_submit)

                if sso_token:
                    self._emit("success", {"email": email, "sso": sso_token[:20]})
                    return RegisterResult(
                        success=True,
                        email=email,
                        password=password,
                        sso_token=sso_token,
                    )

                self._emit("submit_failed", captcha_attempt + 1)

            return RegisterResult(
                success=False,
                email=email,
                password=password,
                error="注册失败（已重试 3 次）",
            )

        except Exception as e:
            logger.error(f"注册异常: {e}")
            return RegisterResult(
                success=False, email=email, password=password, error=str(e)
            )
