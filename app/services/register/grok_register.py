"""
Grok 账号注册核心逻辑

封装完整的注册流程：创建邮箱→发送验证码→获取验证码→验证→注册→获取 SSO Token
"""

from typing import Optional, Callable, Any
from dataclasses import dataclass


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

    def cancel(self) -> None:
        """取消当前注册任务"""
        self._cancelled = True

    async def register_single(self) -> RegisterResult:
        """执行单次注册流程"""
        raise NotImplementedError("待实现")