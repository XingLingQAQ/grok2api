"""
代理服务模块
"""

from app.services.proxy.pool import ProxyPool, ProxyInfo, get_proxy_pool

__all__ = ["ProxyPool", "ProxyInfo", "get_proxy_pool"]
