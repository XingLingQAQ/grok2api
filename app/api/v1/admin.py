from fastapi import APIRouter, Depends, HTTPException, Request, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from app.core.auth import verify_api_key, verify_app_key, get_admin_api_key
from app.core.config import config, get_config
from app.core.batch_tasks import create_task, get_task, expire_task
from app.core.storage import get_storage, LocalStorage, RedisStorage, SQLStorage
import os
from pathlib import Path
import aiofiles
import asyncio
import orjson
from app.core.logger import logger
from typing import Optional


router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "static"


async def render_template(filename: str):
    """渲染指定模板"""
    template_path = TEMPLATE_DIR / filename
    if not template_path.exists():
        return HTMLResponse(f"Template {filename} not found.", status_code=404)

    async with aiofiles.open(template_path, "r", encoding="utf-8") as f:
        content = await f.read()
    return HTMLResponse(content)


def _sse_event(payload: dict) -> str:
    return f"data: {orjson.dumps(payload).decode()}\n\n"


def _verify_stream_api_key(request: Request) -> None:
    api_key = get_admin_api_key()
    if not api_key:
        return
    key = request.query_params.get("api_key")
    if key != api_key:
        raise HTTPException(status_code=401, detail="Invalid authentication token")


@router.get("/api/v1/admin/batch/{task_id}/stream")
async def stream_batch(task_id: str, request: Request):
    _verify_stream_api_key(request)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        queue = task.attach()
        try:
            yield _sse_event({"type": "snapshot", **task.snapshot()})

            final = task.final_event()
            if final:
                yield _sse_event(final)
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield _sse_event(final)
                        return
                    continue

                yield _sse_event(event)
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post(
    "/api/v1/admin/batch/{task_id}/cancel", dependencies=[Depends(verify_api_key)]
)
async def cancel_batch(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.cancel()
    return {"status": "success"}


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page():
    """管理后台登录页"""
    return await render_template("login/login.html")


@router.get("/admin/config", response_class=HTMLResponse, include_in_schema=False)
async def admin_config_page():
    """配置管理页"""
    return await render_template("config/config.html")


@router.get("/admin/token", response_class=HTMLResponse, include_in_schema=False)
async def admin_token_page():
    """Token 管理页"""
    return await render_template("token/token.html")


@router.post("/api/v1/admin/login", dependencies=[Depends(verify_app_key)])
async def admin_login_api():
    """管理后台登录验证（使用 app_key）"""
    return {"status": "success", "api_key": get_admin_api_key()}


@router.get("/api/v1/admin/config", dependencies=[Depends(verify_api_key)])
async def get_config_api():
    """获取当前配置"""
    # 暴露原始配置字典
    return config._config


@router.post("/api/v1/admin/config", dependencies=[Depends(verify_api_key)])
async def update_config_api(data: dict):
    """更新配置"""
    try:
        await config.update(data)
        return {"status": "success", "message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/storage", dependencies=[Depends(verify_api_key)])
async def get_storage_info():
    """获取当前存储模式"""
    storage_type = os.getenv("SERVER_STORAGE_TYPE", "local").lower()
    logger.info(f"Storage type: {storage_type}")
    if not storage_type:
        storage_type = str(get_config("storage.type", "")).lower()
    if not storage_type:
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            storage_type = "local"
        elif isinstance(storage, RedisStorage):
            storage_type = "redis"
        elif isinstance(storage, SQLStorage):
            if storage.dialect in ("mysql", "mariadb"):
                storage_type = "mysql"
            elif storage.dialect in ("postgres", "postgresql", "pgsql"):
                storage_type = "pgsql"
            else:
                storage_type = storage.dialect
    return {"type": storage_type or "local"}


@router.get("/api/v1/admin/tokens", dependencies=[Depends(verify_api_key)])
async def get_tokens_api():
    """获取所有 Token"""
    storage = get_storage()
    tokens = await storage.load_tokens()
    return tokens or {}


@router.post("/api/v1/admin/tokens", dependencies=[Depends(verify_api_key)])
async def update_tokens_api(data: dict):
    """更新 Token 信息"""
    storage = get_storage()
    try:
        from app.services.token.manager import get_token_manager

        async with storage.acquire_lock("tokens_save", timeout=10):
            await storage.save_tokens(data)
            mgr = await get_token_manager()
            await mgr.reload()
        return {"status": "success", "message": "Token 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/tokens/refresh", dependencies=[Depends(verify_api_key)])
async def refresh_tokens_api(data: dict):
    """刷新 Token 状态"""
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    try:
        mgr = await get_token_manager()
        tokens = []
        if "token" in data:
            tokens.append(data["token"])
        if "tokens" in data and isinstance(data["tokens"], list):
            tokens.extend(data["tokens"])

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        # 去重并保持顺序
        unique_tokens = list(dict.fromkeys(tokens))

        # 最大数量限制
        max_tokens = get_config("performance.usage_max_tokens", 1000)
        try:
            max_tokens = int(max_tokens)
        except Exception:
            max_tokens = 1000

        truncated = False
        original_count = len(unique_tokens)
        if len(unique_tokens) > max_tokens:
            unique_tokens = unique_tokens[:max_tokens]
            truncated = True
            logger.warning(
                f"Usage refresh: truncated from {original_count} to {max_tokens} tokens"
            )

        # 批量执行配置
        max_concurrent = get_config("performance.usage_max_concurrent", 25)
        batch_size = get_config("performance.usage_batch_size", 50)

        async def _refresh_one(t):
            return await mgr.sync_usage(
                t, "grok-3", consume_on_fail=False, is_usage=False
            )

        raw_results = await run_in_batches(
            unique_tokens,
            _refresh_one,
            max_concurrent=max_concurrent,
            batch_size=batch_size,
        )

        results = {}
        for token, res in raw_results.items():
            if res.get("ok"):
                results[token] = res.get("data", False)
            else:
                results[token] = False

        response = {"status": "success", "results": results}
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/tokens/refresh/async", dependencies=[Depends(verify_api_key)]
)
async def refresh_tokens_api_async(data: dict):
    """刷新 Token 状态（异步批量 + SSE 进度）"""
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    mgr = await get_token_manager()
    tokens: list[str] = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    unique_tokens = list(dict.fromkeys(tokens))

    max_tokens = get_config("performance.usage_max_tokens", 1000)
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 1000

    truncated = False
    original_count = len(unique_tokens)
    if len(unique_tokens) > max_tokens:
        unique_tokens = unique_tokens[:max_tokens]
        truncated = True
        logger.warning(
            f"Usage refresh: truncated from {original_count} to {max_tokens} tokens"
        )

    max_concurrent = get_config("performance.usage_max_concurrent", 25)
    batch_size = get_config("performance.usage_batch_size", 50)

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _refresh_one(t: str):
                return await mgr.sync_usage(
                    t, "grok-3", consume_on_fail=False, is_usage=False
                )

            async def _on_item(item: str, res: dict):
                task.record(bool(res.get("ok")))

            raw_results = await run_in_batches(
                unique_tokens,
                _refresh_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results: dict[str, bool] = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                if res.get("ok") and res.get("data") is True:
                    ok_count += 1
                    results[token] = True
                else:
                    fail_count += 1
                    results[token] = False

            await mgr._save()

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.post("/api/v1/admin/tokens/nsfw/enable", dependencies=[Depends(verify_api_key)])
async def enable_nsfw_api(data: dict):
    """批量开启 NSFW (Unhinged) 模式"""
    from app.services.grok.services.nsfw import NSFWService
    from app.services.grok.utils.batch import run_in_batches
    from app.services.token.manager import get_token_manager

    try:
        mgr = await get_token_manager()
        from app.services.proxy import get_effective_proxy
        nsfw_service = NSFWService(proxy=get_effective_proxy())

        # 收集 token 列表
        tokens: list[str] = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        # 若未指定，则使用所有 pool 中的 token
        if not tokens:
            for pool_name, pool in mgr.pools.items():
                for info in pool.list():
                    raw = (
                        info.token[4:] if info.token.startswith("sso=") else info.token
                    )
                    tokens.append(raw)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens available")

        # 去重并保持顺序
        unique_tokens = list(dict.fromkeys(tokens))

        # 限制最大数量（超出时截取前 N 个）
        max_tokens = get_config("performance.nsfw_max_tokens", 1000)
        try:
            max_tokens = int(max_tokens)
        except Exception:
            max_tokens = 1000

        truncated = False
        original_count = len(unique_tokens)
        if len(unique_tokens) > max_tokens:
            unique_tokens = unique_tokens[:max_tokens]
            truncated = True
            logger.warning(
                f"NSFW enable: truncated from {original_count} to {max_tokens} tokens"
            )

        # 批量执行配置
        max_concurrent = get_config("performance.nsfw_max_concurrent", 10)
        batch_size = get_config("performance.nsfw_batch_size", 50)

        # 定义 worker
        async def _enable(token: str):
            result = await nsfw_service.enable(token)
            # 成功后添加 nsfw tag
            if result.success:
                await mgr.add_tag(token, "nsfw")
            return {
                "success": result.success,
                "http_status": result.http_status,
                "grpc_status": result.grpc_status,
                "grpc_message": result.grpc_message,
                "error": result.error,
            }

        # 执行批量操作
        raw_results = await run_in_batches(
            unique_tokens, _enable, max_concurrent=max_concurrent, batch_size=batch_size
        )

        # 构造返回结果（mask token）
        results = {}
        ok_count = 0
        fail_count = 0

        for token, res in raw_results.items():
            masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
            if res.get("ok") and res.get("data", {}).get("success"):
                ok_count += 1
                results[masked] = res.get("data", {})
            else:
                fail_count += 1
                results[masked] = res.get("data") or {"error": res.get("error")}

        response = {
            "status": "success",
            "summary": {
                "total": len(unique_tokens),
                "ok": ok_count,
                "fail": fail_count,
            },
            "results": results,
        }

        # 添加截断提示
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enable NSFW failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/tokens/nsfw/enable/async", dependencies=[Depends(verify_api_key)]
)
async def enable_nsfw_api_async(data: dict):
    """批量开启 NSFW (Unhinged) 模式（异步批量 + SSE 进度）"""
    from app.services.grok.services.nsfw import NSFWService
    from app.services.grok.utils.batch import run_in_batches
    from app.services.token.manager import get_token_manager

    mgr = await get_token_manager()
    from app.services.proxy import get_effective_proxy
    nsfw_service = NSFWService(proxy=get_effective_proxy())

    tokens: list[str] = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        for pool_name, pool in mgr.pools.items():
            for info in pool.list():
                raw = info.token[4:] if info.token.startswith("sso=") else info.token
                tokens.append(raw)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens available")

    unique_tokens = list(dict.fromkeys(tokens))

    max_tokens = get_config("performance.nsfw_max_tokens", 1000)
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 1000

    truncated = False
    original_count = len(unique_tokens)
    if len(unique_tokens) > max_tokens:
        unique_tokens = unique_tokens[:max_tokens]
        truncated = True
        logger.warning(
            f"NSFW enable: truncated from {original_count} to {max_tokens} tokens"
        )

    max_concurrent = get_config("performance.nsfw_max_concurrent", 10)
    batch_size = get_config("performance.nsfw_batch_size", 50)

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _enable(token: str):
                result = await nsfw_service.enable(token)
                if result.success:
                    await mgr.add_tag(token, "nsfw")
                return {
                    "success": result.success,
                    "http_status": result.http_status,
                    "grpc_status": result.grpc_status,
                    "grpc_message": result.grpc_message,
                    "error": result.error,
                }

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("ok") and res.get("data", {}).get("success"))
                task.record(ok)

            raw_results = await run_in_batches(
                unique_tokens,
                _enable,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
                if res.get("ok") and res.get("data", {}).get("success"):
                    ok_count += 1
                    results[masked] = res.get("data", {})
                else:
                    fail_count += 1
                    results[masked] = res.get("data") or {"error": res.get("error")}

            await mgr._save()

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/admin/cache", response_class=HTMLResponse, include_in_schema=False)
async def admin_cache_page():
    """缓存管理页"""
    return await render_template("cache/cache.html")


@router.get("/admin/register", response_class=HTMLResponse, include_in_schema=False)
async def admin_register_page():
    """注册管理页"""
    return await render_template("register/register.html")


@router.get("/api/v1/admin/cache", dependencies=[Depends(verify_api_key)])
async def get_cache_stats_api(request: Request):
    """获取缓存统计"""
    from app.services.grok.services.assets import DownloadService, ListService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    try:
        dl_service = DownloadService()
        image_stats = dl_service.get_stats("image")
        video_stats = dl_service.get_stats("video")

        mgr = await get_token_manager()
        pools = mgr.pools
        accounts = []
        for pool_name, pool in pools.items():
            for info in pool.list():
                raw_token = (
                    info.token[4:] if info.token.startswith("sso=") else info.token
                )
                masked = (
                    f"{raw_token[:8]}...{raw_token[-16:]}"
                    if len(raw_token) > 24
                    else raw_token
                )
                accounts.append(
                    {
                        "token": raw_token,
                        "token_masked": masked,
                        "pool": pool_name,
                        "status": info.status,
                        "last_asset_clear_at": info.last_asset_clear_at,
                    }
                )

        scope = request.query_params.get("scope")
        selected_token = request.query_params.get("token")
        tokens_param = request.query_params.get("tokens")
        selected_tokens = []
        if tokens_param:
            selected_tokens = [t.strip() for t in tokens_param.split(",") if t.strip()]

        online_stats = {
            "count": 0,
            "status": "unknown",
            "token": None,
            "last_asset_clear_at": None,
        }
        online_details = []
        account_map = {a["token"]: a for a in accounts}
        max_concurrent = get_config("performance.assets_max_concurrent", 25)
        batch_size = get_config("performance.assets_batch_size", 10)
        try:
            max_concurrent = int(max_concurrent)
        except Exception:
            max_concurrent = 25
        try:
            batch_size = int(batch_size)
        except Exception:
            batch_size = 10
        max_concurrent = max(1, max_concurrent)
        batch_size = max(1, batch_size)

        max_tokens = get_config("performance.assets_max_tokens", 1000)
        try:
            max_tokens = int(max_tokens)
        except Exception:
            max_tokens = 1000

        truncated = False
        original_count = 0

        async def _fetch_assets(token: str):
            list_service = ListService()
            try:
                return await list_service.count(token)
            finally:
                await list_service.close()

        async def _fetch_detail(token: str):
            account = account_map.get(token)
            try:
                count = await _fetch_assets(token)
                return {
                    "detail": {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": count,
                        "status": "ok",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    },
                    "count": count,
                }
            except Exception as e:
                return {
                    "detail": {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    },
                    "count": 0,
                }

        if selected_tokens:
            selected_tokens = list(dict.fromkeys(selected_tokens))
            original_count = len(selected_tokens)
            if len(selected_tokens) > max_tokens:
                selected_tokens = selected_tokens[:max_tokens]
                truncated = True
            total = 0
            raw_results = await run_in_batches(
                selected_tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    data = res.get("data", {})
                    detail = data.get("detail")
                    total += data.get("count", 0)
                else:
                    account = account_map.get(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {res.get('error')}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                if detail:
                    online_details.append(detail)
            online_stats = {
                "count": total,
                "status": "ok" if selected_tokens else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }
            scope = "selected"
        elif scope == "all":
            total = 0
            tokens = list(dict.fromkeys([account["token"] for account in accounts]))
            original_count = len(tokens)
            if len(tokens) > max_tokens:
                tokens = tokens[:max_tokens]
                truncated = True
            raw_results = await run_in_batches(
                tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    data = res.get("data", {})
                    detail = data.get("detail")
                    total += data.get("count", 0)
                else:
                    account = account_map.get(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {res.get('error')}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                if detail:
                    online_details.append(detail)
            online_stats = {
                "count": total,
                "status": "ok" if accounts else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }
        else:
            token = selected_token
            if token:
                try:
                    count = await _fetch_assets(token)
                    match = next((a for a in accounts if a["token"] == token), None)
                    online_stats = {
                        "count": count,
                        "status": "ok",
                        "token": token,
                        "token_masked": match["token_masked"] if match else token,
                        "last_asset_clear_at": match["last_asset_clear_at"]
                        if match
                        else None,
                    }
                except Exception as e:
                    match = next((a for a in accounts if a["token"] == token), None)
                    online_stats = {
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "token": token,
                        "token_masked": match["token_masked"] if match else token,
                        "last_asset_clear_at": match["last_asset_clear_at"]
                        if match
                        else None,
                    }
            else:
                online_stats = {
                    "count": 0,
                    "status": "not_loaded",
                    "token": None,
                    "last_asset_clear_at": None,
                }

        response = {
            "local_image": image_stats,
            "local_video": video_stats,
            "online": online_stats,
            "online_accounts": accounts,
            "online_scope": scope or "none",
            "online_details": online_details,
        }
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/cache/online/load/async", dependencies=[Depends(verify_api_key)]
)
async def load_online_cache_api_async(data: dict):
    """在线资产统计（异步批量 + SSE 进度）"""
    from app.services.grok.services.assets import DownloadService, ListService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    mgr = await get_token_manager()

    # 账号列表
    accounts = []
    for pool_name, pool in mgr.pools.items():
        for info in pool.list():
            raw_token = info.token[4:] if info.token.startswith("sso=") else info.token
            masked = (
                f"{raw_token[:8]}...{raw_token[-16:]}"
                if len(raw_token) > 24
                else raw_token
            )
            accounts.append(
                {
                    "token": raw_token,
                    "token_masked": masked,
                    "pool": pool_name,
                    "status": info.status,
                    "last_asset_clear_at": info.last_asset_clear_at,
                }
            )

    account_map = {a["token"]: a for a in accounts}

    tokens = data.get("tokens")
    scope = data.get("scope")
    selected_tokens: list[str] = []
    if isinstance(tokens, list):
        selected_tokens = [str(t).strip() for t in tokens if str(t).strip()]

    if not selected_tokens and scope == "all":
        selected_tokens = [account["token"] for account in accounts]
        scope = "all"
    elif selected_tokens:
        scope = "selected"
    else:
        raise HTTPException(status_code=400, detail="No tokens provided")

    selected_tokens = list(dict.fromkeys(selected_tokens))

    max_tokens = get_config("performance.assets_max_tokens", 1000)
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 1000

    truncated = False
    original_count = len(selected_tokens)
    if len(selected_tokens) > max_tokens:
        selected_tokens = selected_tokens[:max_tokens]
        truncated = True

    max_concurrent = get_config("performance.assets_max_concurrent", 25)
    batch_size = get_config("performance.assets_batch_size", 10)

    task = create_task(len(selected_tokens))

    async def _run():
        try:
            dl_service = DownloadService()
            image_stats = dl_service.get_stats("image")
            video_stats = dl_service.get_stats("video")

            async def _fetch_detail(token: str):
                account = account_map.get(token)
                list_service = ListService()
                try:
                    count = await list_service.count(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": count,
                        "status": "ok",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                    return {"ok": True, "detail": detail, "count": count}
                except Exception as e:
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                    return {"ok": False, "detail": detail, "count": 0}
                finally:
                    await list_service.close()

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("data", {}).get("ok"))
                task.record(ok)

            raw_results = await run_in_batches(
                selected_tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            online_details = []
            total = 0
            for token, res in raw_results.items():
                data = res.get("data", {})
                detail = data.get("detail")
                if detail:
                    online_details.append(detail)
                total += data.get("count", 0)

            online_stats = {
                "count": total,
                "status": "ok" if selected_tokens else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }

            result = {
                "local_image": image_stats,
                "local_video": video_stats,
                "online": online_stats,
                "online_accounts": accounts,
                "online_scope": scope or "none",
                "online_details": online_details,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(selected_tokens),
    }


@router.post("/api/v1/admin/cache/clear", dependencies=[Depends(verify_api_key)])
async def clear_local_cache_api(data: dict):
    """清理本地缓存"""
    from app.services.grok.services.assets import DownloadService

    cache_type = data.get("type", "image")

    try:
        dl_service = DownloadService()
        result = dl_service.clear(cache_type)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/cache/list", dependencies=[Depends(verify_api_key)])
async def list_local_cache_api(
    cache_type: str = "image",
    type_: str = Query(default=None, alias="type"),
    page: int = 1,
    page_size: int = 1000,
):
    """列出本地缓存文件"""
    from app.services.grok.services.assets import DownloadService

    try:
        if type_:
            cache_type = type_
        dl_service = DownloadService()
        result = dl_service.list_files(cache_type, page, page_size)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/item/delete", dependencies=[Depends(verify_api_key)])
async def delete_local_cache_item_api(data: dict):
    """删除单个本地缓存文件"""
    from app.services.grok.services.assets import DownloadService

    cache_type = data.get("type", "image")
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing file name")
    try:
        dl_service = DownloadService()
        result = dl_service.delete_file(cache_type, name)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/online/clear", dependencies=[Depends(verify_api_key)])
async def clear_online_cache_api(data: dict):
    """清理在线缓存"""
    from app.services.grok.services.assets import DeleteService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    delete_service = None
    try:
        mgr = await get_token_manager()
        tokens = data.get("tokens")
        delete_service = DeleteService()

        if isinstance(tokens, list):
            token_list = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
            if not token_list:
                raise HTTPException(status_code=400, detail="No tokens provided")

            # 去重并保持顺序
            token_list = list(dict.fromkeys(token_list))

            # 最大数量限制
            max_tokens = get_config("performance.assets_max_tokens", 1000)
            try:
                max_tokens = int(max_tokens)
            except Exception:
                max_tokens = 1000
            truncated = False
            original_count = len(token_list)
            if len(token_list) > max_tokens:
                token_list = token_list[:max_tokens]
                truncated = True

            results = {}
            max_concurrent = get_config("performance.assets_max_concurrent", 25)
            batch_size = get_config("performance.assets_batch_size", 10)
            try:
                max_concurrent = int(max_concurrent)
            except Exception:
                max_concurrent = 25
            try:
                batch_size = int(batch_size)
            except Exception:
                batch_size = 10
            max_concurrent = max(1, max_concurrent)
            batch_size = max(1, batch_size)

            async def _clear_one(t: str):
                try:
                    result = await delete_service.delete_all(t)
                    await mgr.mark_asset_clear(t)
                    return {"status": "success", "result": result}
                except Exception as e:
                    return {"status": "error", "error": str(e)}

            raw_results = await run_in_batches(
                token_list,
                _clear_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    results[token] = res.get("data", {})
                else:
                    results[token] = {"status": "error", "error": res.get("error")}

            response = {"status": "success", "results": results}
            if truncated:
                response["warning"] = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            return response

        token = data.get("token") or mgr.get_token()
        if not token:
            raise HTTPException(
                status_code=400, detail="No available token to perform cleanup"
            )

        result = await delete_service.delete_all(token)
        await mgr.mark_asset_clear(token)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if delete_service:
            await delete_service.close()


@router.post(
    "/api/v1/admin/cache/online/clear/async", dependencies=[Depends(verify_api_key)]
)
async def clear_online_cache_api_async(data: dict):
    """清理在线缓存（异步批量 + SSE 进度）"""
    from app.services.grok.services.assets import DeleteService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    mgr = await get_token_manager()
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise HTTPException(status_code=400, detail="No tokens provided")

    token_list = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
    if not token_list:
        raise HTTPException(status_code=400, detail="No tokens provided")

    token_list = list(dict.fromkeys(token_list))

    max_tokens = get_config("performance.assets_max_tokens", 1000)
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 1000
    truncated = False
    original_count = len(token_list)
    if len(token_list) > max_tokens:
        token_list = token_list[:max_tokens]
        truncated = True

    max_concurrent = get_config("performance.assets_max_concurrent", 25)
    batch_size = get_config("performance.assets_batch_size", 10)

    task = create_task(len(token_list))

    async def _run():
        delete_service = DeleteService()
        try:

            async def _clear_one(t: str):
                try:
                    result = await delete_service.delete_all(t)
                    await mgr.mark_asset_clear(t)
                    return {"ok": True, "result": result}
                except Exception as e:
                    return {"ok": False, "error": str(e)}

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("data", {}).get("ok"))
                task.record(ok)

            raw_results = await run_in_batches(
                token_list,
                _clear_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                data = res.get("data", {})
                if data.get("ok"):
                    ok_count += 1
                    results[token] = {"status": "success", "result": data.get("result")}
                else:
                    fail_count += 1
                    results[token] = {"status": "error", "error": data.get("error")}

            result = {
                "status": "success",
                "summary": {
                    "total": len(token_list),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            await delete_service.close()
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(token_list),
    }


# ==================== 注册管理 API ====================


@router.get("/api/v1/admin/register/status", dependencies=[Depends(verify_api_key)])
async def get_register_status():
    """获取注册任务状态"""
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        return {
            "enabled": False,
            "running": False,
            "stats": None,
            "results_count": 0,
        }

    mgr = get_task_manager()
    status = mgr.get_status()
    return {
        "enabled": True,
        **status,
    }


@router.post("/api/v1/admin/register/start", dependencies=[Depends(verify_api_key)])
async def start_register_task(data: dict):
    """启动注册任务"""
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    count = data.get("count", 1)
    concurrent = data.get("concurrent") or get_config("register.register_concurrent", 8)
    mode = data.get("mode", "normal")  # normal 或 nsfw

    if not isinstance(count, int) or count < 1:
        raise HTTPException(status_code=400, detail="count 必须为正整数")
    if not isinstance(concurrent, int) or concurrent < 1:
        raise HTTPException(status_code=400, detail="concurrent 必须为正整数")
    if mode not in ("normal", "nsfw"):
        raise HTTPException(status_code=400, detail="mode 必须为 normal 或 nsfw")

    mgr = get_task_manager()
    try:
        task_id = await mgr.start(count, concurrent, mode=mode)
        return {
            "status": "success",
            "task_id": task_id,
            "count": count,
            "concurrent": concurrent,
            "mode": mode,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/admin/register/stop", dependencies=[Depends(verify_api_key)])
async def stop_register_task():
    """停止注册任务"""
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    mgr = get_task_manager()
    await mgr.stop()
    return {"status": "success", "message": "任务已停止"}


@router.get("/api/v1/admin/register/results", dependencies=[Depends(verify_api_key)])
async def get_register_results(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
):
    """获取注册结果列表"""
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        return {"results": [], "total": 0, "page": page, "page_size": page_size}

    mgr = get_task_manager()
    results = mgr.results
    total = len(results)

    start = (page - 1) * page_size
    end = start + page_size
    page_results = [r.to_dict() for r in results[start:end]]

    return {
        "results": page_results,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/v1/admin/register/export", dependencies=[Depends(verify_api_key)])
async def export_register_results(
    format: str = Query(default="json", regex="^(json|csv)$"),
):
    """导出注册结果"""
    from app.services.register.task_manager import get_task_manager
    from datetime import datetime

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    mgr = get_task_manager()
    content = mgr.export_results(format)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"register_results_{timestamp}.{format}"

    if format == "csv":
        media_type = "text/csv"
    else:
        media_type = "application/json"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/api/v1/admin/register/clear", dependencies=[Depends(verify_api_key)])
async def clear_register_results():
    """清空注册结果"""
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    mgr = get_task_manager()
    mgr.clear_results()
    return {"status": "success", "message": "结果已清空"}


@router.get("/api/v1/admin/register/stream")
async def stream_register_progress(request: Request):
    """SSE 流式推送注册进度"""
    from app.services.register.task_manager import get_task_manager

    _verify_stream_api_key(request)

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    mgr = get_task_manager()

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: str, data):
            try:
                queue.put_nowait({"event": event, "data": data})
            except Exception:
                pass

        mgr.add_event_callback(on_event)
        try:
            # 发送初始状态
            yield _sse_event({
                "type": "snapshot",
                "running": mgr.is_running,
                "stats": mgr.stats.to_dict(),
                "results_count": len(mgr.results),
            })

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse_event(event)

                    # 任务完成时结束流
                    if event.get("event") in ("task_completed", "task_stopped"):
                        return
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    # 检查任务是否已结束
                    if not mgr.is_running:
                        yield _sse_event({"type": "done"})
                        return
        finally:
            mgr.remove_event_callback(on_event)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/v1/admin/register/config", dependencies=[Depends(verify_api_key)])
async def get_register_config():
    """获取注册相关配置"""
    return {
        "enabled": get_config("register.enabled", False),
        "mail_api_key": "***" if get_config("register.mail_api_key", "") else "",
        "mail_domain": get_config("register.mail_domain", ""),
        "mail_api_url": get_config("register.mail_api_url", ""),
        "turnstile_solver_url": get_config("register.turnstile_solver_url", ""),
        "turnstile_solver_threads": get_config("register.turnstile_solver_threads", 5),
        "turnstile_headless": get_config("register.turnstile_headless", True),
        "turnstile_timeout": get_config("register.turnstile_timeout", 60.0),
        "register_concurrent": get_config("register.register_concurrent", 8),
        "auto_import_tokens": get_config("register.auto_import_tokens", True),
    }


# ==================== 自动注册管理 ====================


@router.get("/api/v1/admin/register/auto/status", dependencies=[Depends(verify_api_key)])
async def get_auto_register_status():
    """获取自动注册调度器状态"""
    from app.services.register.auto_scheduler import get_auto_register_scheduler

    scheduler = get_auto_register_scheduler()
    return scheduler.get_status()


@router.post("/api/v1/admin/register/auto/trigger", dependencies=[Depends(verify_api_key)])
async def trigger_auto_register():
    """手动触发一次自动注册"""
    from app.services.register.auto_scheduler import get_auto_register_scheduler
    from app.services.register.task_manager import get_task_manager

    enabled = get_config("register.enabled", False)
    if not enabled:
        raise HTTPException(status_code=400, detail="注册功能未启用")

    mgr = get_task_manager()
    if mgr.is_running:
        raise HTTPException(status_code=400, detail="已有注册任务运行中")

    scheduler = get_auto_register_scheduler()
    task_id = await scheduler.trigger_manual()
    if not task_id:
        raise HTTPException(status_code=400, detail="触发失败")

    return {"status": "success", "task_id": task_id}


# ==================== 代理池管理 ====================


@router.get("/api/v1/admin/proxy/status", dependencies=[Depends(verify_api_key)])
async def get_proxy_status():
    """获取代理池状态"""
    from app.services.proxy import get_proxy_pool

    pool = get_proxy_pool()
    return pool.get_status()


@router.get("/api/v1/admin/proxy/list", dependencies=[Depends(verify_api_key)])
async def get_proxy_list(limit: int = Query(default=100, ge=1, le=1000)):
    """获取存活代理列表"""
    from app.services.proxy import get_proxy_pool

    pool = get_proxy_pool()
    return {
        "proxies": pool.get_alive_proxies(limit),
        "total": pool.alive_count,
    }


@router.post("/api/v1/admin/proxy/fetch", dependencies=[Depends(verify_api_key)])
async def fetch_proxies():
    """从源抓取代理"""
    from app.services.proxy import get_proxy_pool

    pool = get_proxy_pool()
    if pool.is_fetching:
        return {"status": "running", "message": "正在抓取中"}

    asyncio.create_task(pool.fetch_proxies())
    return {"status": "started", "message": "开始抓取代理"}


@router.post("/api/v1/admin/proxy/check", dependencies=[Depends(verify_api_key)])
async def check_proxies(data: dict = None):
    """测活代理"""
    from app.services.proxy import get_proxy_pool

    pool = get_proxy_pool()
    if pool.is_checking:
        return {"status": "running", "message": "正在测活中"}

    max_concurrent = (data or {}).get("concurrent", 100)
    asyncio.create_task(pool.check_proxies(max_concurrent))
    return {"status": "started", "message": "开始测活代理"}


@router.post("/api/v1/admin/proxy/clear", dependencies=[Depends(verify_api_key)])
async def clear_proxies():
    """清空代理池"""
    from app.services.proxy import get_proxy_pool

    pool = get_proxy_pool()
    pool.clear()
    return {"status": "success", "message": "代理池已清空"}


@router.post("/api/v1/admin/proxy/add", dependencies=[Depends(verify_api_key)])
async def add_proxy(data: dict):
    """手动添加代理"""
    from app.services.proxy import get_proxy_pool

    proxy = data.get("proxy", "").strip()
    if not proxy:
        raise HTTPException(status_code=400, detail="proxy 不能为空")

    pool = get_proxy_pool()
    if pool.add_proxy(proxy):
        return {"status": "success", "message": "代理已添加"}
    return {"status": "exists", "message": "代理已存在"}


@router.post("/api/v1/admin/proxy/remove", dependencies=[Depends(verify_api_key)])
async def remove_proxy(data: dict):
    """移除代理"""
    from app.services.proxy import get_proxy_pool

    proxy = data.get("proxy", "").strip()
    if not proxy:
        raise HTTPException(status_code=400, detail="proxy 不能为空")

    pool = get_proxy_pool()
    if pool.remove_proxy(proxy):
        return {"status": "success", "message": "代理已移除"}
    return {"status": "not_found", "message": "代理不存在"}

