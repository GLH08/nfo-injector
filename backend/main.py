#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI 主应用
- REST API
- WebSocket 实时日志
- 静态文件服务（前端）
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional

# ─── 日志配置 ──────────────────────────────────────────────────
# 确保 data 目录存在，设置文件日志
data_dir = Path("data")
data_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(data_dir / "nfo-injector.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("nfo-injector")


from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import (
    AppConfig, Library, get_config, load_config, save_config,
    resolve_media_path, resolve_library, split_lib_path,
)
from backend.nfo_handler import (
    NfoStatus, analyze_nfo, find_nfo_for_strm,
    STATUS_LABELS, STATUS_COLORS
)
from backend.ffprobe_runner import run_ffprobe_sync
from backend.file_browser import (
    browse_directory, EntryType, StatusCount,
)
from backend import file_browser
from backend.task_manager import task_manager, TaskStatus
from backend.media_index import media_index

# ─── 应用初始化 ────────────────────────────────────────────────
app = FastAPI(
    title="NFO MediaInfo 注入管理器",
    description="为 Emby STRM 库精确注入 MediaInfo 信息",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 请求/响应模型 ─────────────────────────────────────────────

class InjectRequest(BaseModel):
    path: str = Field(description="<库id>/<库内相对路径>（文件或目录）")
    scope: str = Field(default="recursive", description="'file' | 'directory' | 'recursive'")
    force: bool = Field(default=False, description="是否强制覆盖已有 MediaInfo")
    filter_status: List[str] = Field(
        default=["EMPTY", "MISSING", "PARTIAL"],
        description="只处理这些状态的文件，空列表表示全部"
    )
    concurrency: Optional[int] = Field(default=None, description="并发数（覆盖全局配置）")
    timeout: Optional[int] = Field(default=None, description="FFprobe 超时（覆盖全局配置）")
    use_mock: bool = Field(default=False, description="是否使用虚拟数据填充（跳过FFprobe探测）")


class ConfigUpdate(BaseModel):
    libraries: Optional[List[Library]] = None
    ffprobe_timeout: Optional[int] = None
    max_concurrency: Optional[int] = None
    guess_extensions: Optional[List[str]] = None
    max_retries: Optional[int] = None
    retry_delay: Optional[float] = None
    forbidden_retry_delay: Optional[float] = None
    scan_cache_ttl: Optional[float] = None
    exclude_dirs: Optional[List[str]] = None


# ─── 配置 API ──────────────────────────────────────────────────

@app.get("/api/config")
async def get_config_api():
    config = get_config()
    return config.model_dump()


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    config = get_config()
    data = config.model_dump()

    for key, value in update.model_dump(exclude_none=True).items():
        data[key] = value

    new_config = AppConfig(**data)
    save_config(new_config)
    return new_config.model_dump()


# ─── 文件浏览 API ──────────────────────────────────────────────

@app.get("/api/browse")
async def browse(path: str = ""):
    config = get_config()

    # 根：返回库列表
    if not path:
        result = []
        for lib in config.libraries:
            if not lib.enabled:
                continue
            result.append({
                "name": lib.name or lib.id,
                "relative_path": lib.id,
                "entry_type": EntryType.LIBRARY,
                "has_children": True,
            })
        return {"path": "", "entries": result}

    # 库内
    try:
        lib, abs_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    entries = await asyncio.get_event_loop().run_in_executor(
        None,
        browse_directory,
        abs_dir,
        lib.id,
        Path(lib.strm_path),
        config.exclude_dirs,
    )

    result = []
    for e in entries:
        d = {
            "name": e.name,
            "relative_path": e.relative_path,
            "entry_type": e.entry_type,
            "has_children": e.has_children,
        }
        if e.entry_type == EntryType.STRM_FILE:
            d.update({
                "nfo_path": e.nfo_path,
                "nfo_status": e.nfo_status,
                "nfo_status_label": e.nfo_status_label,
                "nfo_status_color": e.nfo_status_color,
                "missing_fields": e.missing_fields,
            })
        result.append(d)

    return {"path": path, "entries": result}


@app.get("/api/scan")
async def scan(path: str = "", force: bool = False):
    """递归统计路径下各状态数量（用于目录徽章）；path 为空时对所有启用库求和。
    命中扫描缓存则直接返回，未命中/过期才递归扫描并填充缓存。
    force=True 时绕过缓存查询，强制重扫并刷新缓存（手动刷新用）。"""
    config = get_config()
    ttl = config.scan_cache_ttl
    loop = asyncio.get_event_loop()

    if not path:
        total = StatusCount()
        for lib in config.libraries:
            if not lib.enabled:
                continue
            cached = file_browser.counts_from_cache(lib.id, ttl) if (ttl > 0 and not force) else None
            if cached is not None:
                total.merge(cached)
            else:
                c = await loop.run_in_executor(
                    None, file_browser.scan_and_cache,
                    Path(lib.strm_path), lib.id, Path(lib.strm_path), config.exclude_dirs,
                )
                total.merge(c)
        return {"path": "", **total.to_dict()}

    try:
        lib, abs_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    subtree_key = path  # path 已是 "<lib_id>" 或 "<lib_id>/..." 形式
    cached = file_browser.counts_from_cache(subtree_key, ttl) if (ttl > 0 and not force) else None
    if cached is not None:
        return {"path": path, **cached.to_dict()}
    counts = await loop.run_in_executor(
        None, file_browser.scan_and_cache,
        abs_dir, lib.id, Path(lib.strm_path), config.exclude_dirs,
    )
    return {"path": path, **counts.to_dict()}


@app.delete("/api/scan-cache")
async def invalidate_scan_cache():
    """全局手动刷新：清空整个扫描缓存。"""
    file_browser.clear_scan_cache()
    return {"message": "扫描缓存已清空"}


# ─── 媒体索引 API ─────────────────────────────────────────────

@app.post("/api/media-index/refresh")
async def refresh_media_index(path: str = ""):
    config = get_config()
    if not path:
        raise HTTPException(400, "需要 path")
    try:
        lib, abs_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))
    # subdir_relative = path 去掉 lib_id/ 前缀
    parts = path.split("/", 1)
    subdir = parts[1] if len(parts) > 1 else ""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, media_index.refresh_index,
        lib.id, Path(lib.strm_path), config.exclude_dirs, config.guess_extensions, subdir,
        lib.media_path or None
    )
    return result


@app.get("/api/media-index")
async def get_media_index(path: str = ""):
    config = get_config()
    media_index.load()
    if not path:
        return {lib.id: len(media_index._data.get(lib.id, {})) for lib in config.libraries}
    parts = path.split("/", 1)
    lib_id = parts[0]
    return {"count": len(media_index._data.get(lib_id, {}))}


@app.get("/api/issues")
async def find_issues(path: str = ""):
    """查找指定库内目录下所有问题文件（非 HEALTHY）"""
    config = get_config()
    try:
        lib, target_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    lib_strm_path = Path(lib.strm_path)
    lib_id = lib.id
    issues = []

    def _find_issues_recursive(current_path: Path):
        try:
            for item in current_path.iterdir():
                if item.is_dir():
                    if config.exclude_dirs and item.name.lower() in [d.lower() for d in config.exclude_dirs]:
                        continue
                    _find_issues_recursive(item)
                elif item.suffix.lower() == ".strm":
                    rel_path = f"{lib_id}/{item.relative_to(lib_strm_path).as_posix()}"
                    nfo_path = find_nfo_for_strm(item)
                    detail = analyze_nfo(nfo_path)
                    if detail.status != NfoStatus.HEALTHY:
                        issues.append({
                            "path": rel_path,
                            "status": detail.status,
                            "status_label": detail.status_label,
                            "status_color": detail.status_color,
                        })
        except Exception:
            pass

    if target_dir.is_dir():
        await asyncio.get_event_loop().run_in_executor(
            None, _find_issues_recursive, target_dir
        )

    return {"path": path, "issues": issues}


# ─── NFO 详情 API ─────────────────────────────────────────────

@app.get("/api/nfo")
async def get_nfo(path: str):
    """
    获取指定 STRM 文件对应 NFO 的详细信息
    path: "<库id>/<库内相对路径>"
    """
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    if not strm_path.exists():
        raise HTTPException(404, f"STRM 文件不存在: {path}")

    nfo_path = find_nfo_for_strm(strm_path)
    detail = analyze_nfo(nfo_path)

    # 序列化 stream_details
    sd_data = None
    if detail.stream_details:
        sd = detail.stream_details
        sd_data = {
            "video": [vars(v) for v in sd.video_streams],
            "audio": [vars(a) for a in sd.audio_streams],
            "subtitle": [vars(s) for s in sd.subtitle_streams],
        }

    return {
        "strm_path": path,
        "nfo_path": str(nfo_path) if nfo_path else None,
        "status": detail.status,
        "status_label": detail.status_label,
        "status_color": detail.status_color,
        "missing_fields": detail.missing_fields,
        "stream_details": sd_data,
        "raw_xml": detail.raw_xml,
        "parse_error": detail.parse_error,
    }


@app.get("/api/ffprobe")
async def probe_only(path: str):
    """
    仅运行 FFprobe，不写入 NFO
    path: "<库id>/<库内相对路径>"
    """
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    media_base = resolve_media_path(strm_path, config)
    if media_base is None:
        raise HTTPException(404, "该路径不属于任何库")

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        run_ffprobe_sync,
        media_base,
        config.guess_extensions,
        config.ffprobe_timeout,
        None,
    )

    return {
        "success": result.success,
        "tried_path": result.tried_path,
        "tried_extension": result.tried_extension,
        "error": result.error,
        "error_type": result.error_type,
        "data": result.data,
    }


# ─── 注入任务 API ─────────────────────────────────────────────

@app.post("/api/inject")
async def create_inject_task(req: InjectRequest, background_tasks: BackgroundTasks):
    config = get_config()

    task = task_manager.create_task(
        relative_path=req.path,
        scope=req.scope,
        force=req.force,
        filter_status=req.filter_status,
        concurrency=req.concurrency or config.max_concurrency,
        timeout=req.timeout or config.ffprobe_timeout,
        use_mock=req.use_mock,
    )

    background_tasks.add_task(task_manager.run_task, task, config)

    return {"task_id": task.task_id, "status": task.status}


@app.get("/api/tasks")
async def list_tasks():
    from datetime import datetime
    return {
        "tasks": task_manager.list_tasks(),
        "server_time": datetime.now().isoformat()
    }


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")

    return {
        **task.to_dict(),
        "logs": [
            {"timestamp": l.timestamp, "level": l.level, "message": l.message}
            for l in task.logs[-200:]
        ],
    }


@app.delete("/api/task/{task_id}")
async def cancel_task(task_id: str):
    success = task_manager.cancel_task(task_id)
    if not success:
        raise HTTPException(400, "任务不存在或已结束，无法取消")
    return {"message": "取消信号已发送"}


@app.delete("/api/tasks/history")
async def clear_task_history():
    task_manager.clear_history()
    return {"message": "历史任务已清空"}


# ─── 单文件注入 API（快捷）────────────────────────────────────

@app.post("/api/inject-file")
async def inject_single_file(
    path: str,
    force: bool = False,
    background_tasks: BackgroundTasks = None,
):
    """快捷接口：对单个 STRM 文件注入"""
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    if not strm_path.exists():
        raise HTTPException(404, f"STRM 文件不存在: {path}")

    task = task_manager.create_task(
        relative_path=path,
        scope="file",
        force=force,
        filter_status=[],
        concurrency=1,
        timeout=config.ffprobe_timeout,
    )

    background_tasks.add_task(task_manager.run_task, task, config)
    return {"task_id": task.task_id, "status": task.status}


# ─── WebSocket ────────────────────────────────────────────────

@app.websocket("/ws/logs/{task_id}")
async def websocket_logs(websocket: WebSocket, task_id: str):
    await websocket.accept()

    task = task_manager.get_task(task_id)
    if not task:
        await websocket.send_json({"type": "error", "message": f"Task {task_id} not found"})
        await websocket.close()
        return

    queue = task_manager.subscribe(task_id)

    # 先推送历史日志
    for log in task.logs:
        await websocket.send_json({
            "type": "log",
            "task_id": task_id,
            "timestamp": log.timestamp,
            "level": log.level,
            "message": log.message,
        })

    # 如果任务已结束，推送状态后关闭
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        await websocket.send_json({"type": "done", "task_id": task_id, "status": task.status})
        task_manager.unsubscribe(task_id, queue)
        await websocket.close()
        return

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event)
                if event.get("type") == "done":
                    break
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        task_manager.unsubscribe(task_id, queue)


# ─── 状态标签 API ─────────────────────────────────────────────

@app.get("/api/status-labels")
async def get_status_labels():
    return {
        status.value: {
            "label": STATUS_LABELS[status],
            "color": STATUS_COLORS[status],
        }
        for status in NfoStatus
    }


# ─── 静态文件（前端）────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ─── 启动入口（直接运行时）──────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    load_config()
    uvicorn.run("backend.main:app", host="0.0.0.0", port=18880, reload=False)
