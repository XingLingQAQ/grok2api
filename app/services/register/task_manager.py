"""
注册任务管理器

管理批量注册任务的启动/停止/进度追踪/结果存储。
"""

import asyncio
import csv
import io
import json
from typing import Optional, List, Dict, Any, Callable, AsyncGenerator
from dataclasses import dataclass, field, asdict
from datetime import datetime
import uuid

from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import get_storage
from app.services.register.grok_register import GrokRegister, RegisterResult


@dataclass
class RegisterTaskStats:
    """注册任务统计"""

    total: int = 0
    success: int = 0
    failed: int = 0
    running: bool = False
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RegisterTaskResult:
    """单条注册结果"""

    email: str
    password: str
    sso_token: Optional[str]
    success: bool
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TaskManager:
    """注册任务管理器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._task_id: Optional[str] = None
        self._stats = RegisterTaskStats()
        self._results: List[RegisterTaskResult] = []
        self._cancelled = False
        self._lock = asyncio.Lock()
        self._event_callbacks: List[Callable[[str, Any], None]] = []
        self._registrars: List[GrokRegister] = []
        self._mode: str = "normal"  # normal 或 nsfw
        # 持久化防抖
        self._save_pending = False
        self._flush_task: Optional[asyncio.Task] = None

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

    @property
    def is_running(self) -> bool:
        """任务是否正在运行"""
        return self._stats.running

    async def init(self):
        """从存储加载注册结果"""
        try:
            storage = get_storage()
            items = await storage.load_register_results()
            for item in items:
                self._results.append(RegisterTaskResult(
                    email=item.get("email", ""),
                    password=item.get("password", ""),
                    sso_token=item.get("sso_token"),
                    success=item.get("success", False),
                    error=item.get("error"),
                    created_at=item.get("created_at", ""),
                ))
            logger.info(f"TaskManager: 从存储加载 {len(self._results)} 条注册结果")
        except Exception as e:
            logger.warning(f"TaskManager: 加载注册结果失败: {e}")

    async def _save(self):
        """立即保存到存储"""
        self._save_pending = False
        try:
            data = [r.to_dict() for r in self._results]
            storage = get_storage()
            await storage.save_register_results(data)
        except Exception as e:
            logger.error(f"TaskManager: 保存注册结果失败: {e}")

    def _schedule_save(self):
        """防抖保存（1.0s 延迟合并写入）"""
        self._save_pending = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        """防抖落盘循环"""
        while self._save_pending:
            await asyncio.sleep(1.0)
            if self._save_pending:
                await self._save()

    def add_event_callback(self, callback: Callable[[str, Any], None]) -> None:
        """添加事件回调"""
        self._event_callbacks.append(callback)

    def remove_event_callback(self, callback: Callable[[str, Any], None]) -> None:
        """移除事件回调"""
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _emit_event(self, event: str, data: Any = None) -> None:
        """发送事件到所有回调"""
        for callback in self._event_callbacks:
            try:
                callback(event, data)
            except Exception as e:
                logger.debug(f"事件回调异常: {e}")

    async def start(self, count: int, concurrent: int = 8, mode: str = "normal") -> str:
        """
        启动批量注册任务

        Args:
            count: 注册数量
            concurrent: 并发数
            mode: 注册模式 (normal 或 nsfw)

        Returns:
            task_id
        """
        async with self._lock:
            if self._stats.running:
                raise RuntimeError("已有任务正在运行")

            # 重置状态
            self._cancelled = False
            self._task_id = str(uuid.uuid4())
            self._mode = mode
            self._stats = RegisterTaskStats(
                total=count,
                success=0,
                failed=0,
                running=True,
                start_time=datetime.now().isoformat(),
            )
            self._registrars.clear()

            # 启动后台任务
            self._task = asyncio.create_task(
                self._run_batch(count, concurrent)
            )

            logger.info(f"注册任务启动: {self._task_id}, 数量={count}, 并发={concurrent}, 模式={mode}")
            self._emit_event("task_started", {
                "task_id": self._task_id,
                "total": count,
                "concurrent": concurrent,
                "mode": mode,
            })

            return self._task_id

    async def stop(self) -> None:
        """停止当前任务"""
        async with self._lock:
            if not self._stats.running:
                return

            self._cancelled = True

            # 取消所有注册器
            for registrar in self._registrars:
                registrar.cancel()

            # 取消任务
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

            self._stats.running = False
            self._stats.end_time = datetime.now().isoformat()

            logger.info(f"注册任务已停止: {self._task_id}")
            self._emit_event("task_stopped", {
                "task_id": self._task_id,
                "stats": self._stats.to_dict(),
            })

    async def _run_batch(self, count: int, concurrent: int) -> None:
        """执行批量注册"""
        semaphore = asyncio.Semaphore(concurrent)
        tasks = []

        async def register_one(index: int) -> None:
            async with semaphore:
                if self._cancelled:
                    return

                registrar = GrokRegister(
                    on_progress=lambda event, data: self._on_register_progress(
                        index, event, data
                    )
                )
                self._registrars.append(registrar)

                try:
                    result = await registrar.register_single()
                    await self._handle_result(index, result)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"注册任务 {index} 异常: {e}")
                    await self._handle_result(
                        index,
                        RegisterResult(success=False, error=str(e))
                    )

        try:
            for i in range(count):
                if self._cancelled:
                    break
                tasks.append(asyncio.create_task(register_one(i)))

            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            pass
        finally:
            self._stats.running = False
            self._stats.end_time = datetime.now().isoformat()
            await self._save()
            self._emit_event("task_completed", {
                "task_id": self._task_id,
                "stats": self._stats.to_dict(),
            })
            logger.info(
                f"注册任务完成: {self._task_id}, "
                f"成功={self._stats.success}, 失败={self._stats.failed}"
            )

    def _on_register_progress(self, index: int, event: str, data: Any) -> None:
        """处理单个注册的进度事件"""
        self._emit_event("register_progress", {
            "index": index,
            "event": event,
            "data": data,
        })

    async def _handle_result(self, index: int, result: RegisterResult) -> None:
        """处理注册结果"""
        task_result = RegisterTaskResult(
            email=result.email or "",
            password=result.password or "",
            sso_token=result.sso_token,
            success=result.success,
            error=result.error,
        )
        self._results.append(task_result)

        if result.success:
            self._stats.success += 1
            if result.sso_token:
                # NSFW 模式下先开启 NSFW
                if self._mode == "nsfw":
                    await self._enable_nsfw(result.sso_token)
                # 自动导入 Token
                if get_config("register.auto_import_tokens", True):
                    await self._auto_import_token(result.sso_token)
        else:
            self._stats.failed += 1

        self._emit_event("register_result", {
            "index": index,
            "result": task_result.to_dict(),
            "stats": self._stats.to_dict(),
        })
        self._schedule_save()

    async def _enable_nsfw(self, sso_token: str) -> None:
        """为 Token 开启 NSFW 模式（带回退：代理失败则直连重试）"""
        try:
            from app.services.grok.services.nsfw import NSFWService
            from app.services.proxy import get_effective_proxy

            proxy = get_effective_proxy()
            service = NSFWService(proxy=proxy)
            result = await service.enable(sso_token)

            # 代理失败时回退到直连
            if not result.success and proxy:
                logger.debug(f"NSFW 代理失败，回退直连: {result.error or result.grpc_message}")
                service_direct = NSFWService(proxy=None)
                result = await service_direct.enable(sso_token)

            if result.success:
                logger.debug(f"NSFW 开启成功: {sso_token[:20]}...")
            else:
                logger.warning(f"NSFW 开启失败: {result.error or result.grpc_message}")
        except Exception as e:
            logger.warning(f"NSFW 开启异常: {e}")

    async def _auto_import_token(self, sso_token: str) -> None:
        """自动导入 Token 到 Token 管理"""
        try:
            from app.services.token.manager import get_token_manager
            mgr = await get_token_manager()
            await mgr.add(sso_token, pool_name="ssoBasic")
            logger.debug(f"Token 自动导入成功: {sso_token[:20]}...")
        except Exception as e:
            logger.warning(f"Token 自动导入失败: {e}")

    async def clear_results(self) -> None:
        """清空结果列表"""
        self._results.clear()
        await self._save()
        self._emit_event("results_cleared", None)

    def export_results(self, format: str = "json") -> str:
        """
        导出结果

        Args:
            format: 导出格式 ("json" 或 "csv")

        Returns:
            导出的字符串内容
        """
        if format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["email", "password", "sso_token", "success", "error", "created_at"],
            )
            writer.writeheader()
            for result in self._results:
                writer.writerow(result.to_dict())
            return output.getvalue()
        else:
            return json.dumps(
                [r.to_dict() for r in self._results],
                ensure_ascii=False,
                indent=2,
            )

    def get_status(self) -> Dict[str, Any]:
        """获取当前状态"""
        return {
            "task_id": self._task_id,
            "running": self._stats.running,
            "stats": self._stats.to_dict(),
            "results_count": len(self._results),
        }


# 全局任务管理器实例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """获取全局任务管理器实例"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager