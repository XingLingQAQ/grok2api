"""
注册任务管理器

管理批量注册任务的启动/停止/进度追踪/结果存储。
"""

import asyncio
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import uuid


@dataclass
class RegisterTaskStats:
    """注册任务统计"""

    total: int = 0
    success: int = 0
    failed: int = 0
    running: bool = False
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@dataclass
class RegisterTaskResult:
    """单条注册结果"""

    email: str
    password: str
    sso_token: Optional[str]
    success: bool
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class TaskManager:
    """注册任务管理器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._task_id: Optional[str] = None
        self._stats = RegisterTaskStats()
        self._results: List[RegisterTaskResult] = []
        self._cancelled = False

    @property
    def task_id(self) -> Optional[str]:
        """当前任务 ID"""
        return self._task_id

    @property
    def stats(self) -> RegisterTaskStats:
        """当前任务统计"""
        return self._stats

    @property
    def results(self) -> List[RegisterTaskResult]:
        """注册结果列表"""
        return self._results

    async def start(self, count: int, concurrent: int = 8) -> str:
        """启动批量注册任务，返回 task_id"""
        raise NotImplementedError("待实现")

    async def stop(self) -> None:
        """停止当前任务"""
        raise NotImplementedError("待实现")

    def clear_results(self) -> None:
        """清空结果列表"""
        self._results.clear()

    def export_results(self, format: str = "json") -> Any:
        """导出结果"""
        raise NotImplementedError("待实现")


# 全局任务管理器实例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """获取全局任务管理器实例"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager