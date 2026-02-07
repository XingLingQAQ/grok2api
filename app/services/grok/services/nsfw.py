"""
NSFW (Unhinged) 模式服务

使用 gRPC-Web 协议开启账号的 NSFW 功能。
"""

from dataclasses import dataclass
from typing import Optional

from curl_cffi.requests import AsyncSession

from app.core.config import get_config
from app.core.logger import logger
from app.services.grok.protocols.grpc_web import (
    encode_grpc_web_payload,
    parse_grpc_web_response,
    get_grpc_status,
)


NSFW_API = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
BROWSER = "chrome136"
TIMEOUT = 30


@dataclass
class NSFWResult:
    """NSFW 操作结果"""

    success: bool
    http_status: int
    grpc_status: Optional[int] = None
    grpc_message: Optional[str] = None
    error: Optional[str] = None


class NSFWService:
    """NSFW 模式服务"""

    def __init__(self, proxy: str = None):
        if proxy is None:
            proxy = get_config("grok.base_proxy_url", "")
        self.proxy = proxy

    def _build_headers(self, token: str) -> dict:
        """构造 gRPC-Web 请求头"""
        token = token[4:] if token.startswith("sso=") else token
        cf = get_config("grok.cf_clearance", "")
        cookie = f"sso={token}; sso-rw={token}"
        if cf:
            cookie += f"; cf_clearance={cf}"

        return {
            "accept": "*/*",
            "content-type": "application/grpc-web+proto",
            "origin": "https://grok.com",
            "referer": "https://grok.com/",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "cookie": cookie,
        }

    @staticmethod
    def _build_payload() -> bytes:
        """构造请求 payload"""
        name = b"always_show_nsfw_content"
        inner = b"\x0a" + bytes([len(name)]) + name
        protobuf = b"\x0a\x02\x10\x01\x12" + bytes([len(inner)]) + inner
        return encode_grpc_web_payload(protobuf)

    async def _do_request(self, headers: dict, payload: bytes, proxy: str) -> NSFWResult:
        """执行单次 gRPC-Web 请求"""
        try:
            async with AsyncSession(impersonate=BROWSER) as session:
                response = await session.post(
                    NSFW_API,
                    data=payload,
                    headers=headers,
                    timeout=TIMEOUT,
                    proxy=proxy,
                )

                if response.status_code != 200:
                    return NSFWResult(
                        success=False,
                        http_status=response.status_code,
                        error=f"HTTP {response.status_code}",
                    )

                content_type = response.headers.get("content-type")
                _, trailers = parse_grpc_web_response(
                    response.content, content_type=content_type
                )

                grpc_status = get_grpc_status(trailers)
                success = grpc_status.code == -1 or grpc_status.ok

                return NSFWResult(
                    success=success,
                    http_status=response.status_code,
                    grpc_status=grpc_status.code,
                    grpc_message=grpc_status.message or None,
                )

        except Exception as e:
            return NSFWResult(success=False, http_status=0, error=str(e)[:200])

    async def enable(self, token: str) -> NSFWResult:
        """为单个 token 开启 NSFW 模式（代理失败自动直连重试）"""
        headers = self._build_headers(token)
        payload = self._build_payload()
        proxy_arg = self.proxy if self.proxy else ""

        result = await self._do_request(headers, payload, proxy_arg)

        # 代理失败时回退直连
        if not result.success and proxy_arg:
            logger.warning(f"NSFW 代理失败({proxy_arg}): {result.error}, 回退直连")
            result = await self._do_request(headers, payload, "")

        if not result.success:
            logger.error(f"NSFW enable failed: {result.error or result.grpc_message}")

        return result


__all__ = ["NSFWService", "NSFWResult"]
