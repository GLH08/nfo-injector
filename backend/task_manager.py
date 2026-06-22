#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异步任务管理器（强化版）

核心改进：
- 取消时通过 threading.Event 传入 FFprobe 线程，立即 kill 子进程
- 并发数说明：asyncio.Semaphore 控制"同时进行 FFprobe 的文件数"
- 详细错误分类统计（超时 / 403 / 未找到文件 / 已取消）
- 任务可被 PENDING 状态取消（排队未开始时也能取消）
"""

import asyncio
import threading
import uuid
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from datetime import datetime

from backend.config import AppConfig, resolve_media_path, resolve_library, split_lib_path
from backend.nfo_handler import (
    NfoStatus, analyze_nfo, find_nfo_for_strm, inject_mediainfo, inject_mock_mediainfo_to_nfo
)
from backend.ffprobe_runner import probe_with_retry
from backend import file_browser as fb
from backend.file_browser import entries_from_cache, update_file_cache_entry

logger = logging.getLogger("nfo-injector")

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TaskProgress:
    total: int = 0
    processed: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0      # 取消时被跳过的文件数
    # 错误细分
    err_timeout: int = 0
    err_forbidden: int = 0
    err_not_found: int = 0
    err_inject: int = 0
    err_other: int = 0

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.processed / self.total * 100, 1)

    def to_dict(self) -> Dict:
        return {
            "total": self.total,
            "processed": self.processed,
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "percent": self.percent,
            "errors": {
                "timeout": self.err_timeout,
                "forbidden": self.err_forbidden,
                "not_found": self.err_not_found,
                "inject": self.err_inject,
                "other": self.err_other,
            },
        }


@dataclass
class TaskLog:
    timestamp: str
    level: str   # "info" | "success" | "warning" | "error"
    message: str


@dataclass
class Task:
    task_id: str
    status: TaskStatus

    # 任务参数
    relative_path: str
    scope: str           # "file" | "directory" | "recursive"
    force: bool
    filter_status: List[str]
    concurrency: int
    timeout: int
    use_mock: bool = False

    # 进度
    progress: TaskProgress = field(default_factory=TaskProgress)

    # 日志
    logs: List[TaskLog] = field(default_factory=list)

    # 时间
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    # 取消控制
    # _cancel_async：asyncio.Event，用于在 async 层检查
    # _stop_thread：threading.Event，传入同步线程，立即 kill 子进程
    _cancel_async: asyncio.Event = field(default_factory=asyncio.Event)
    _stop_thread: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "relative_path": self.relative_path,
            "scope": self.scope,
            "force": self.force,
            "filter_status": self.filter_status,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "progress": self.progress.to_dict(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_count": len(self.logs),
        }


class TaskManager:
    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._ws_subscribers: Dict[str, Set[asyncio.Queue]] = {}
        # 线程池：max_workers 应 ≥ 最大并发数，留一些余量
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ffprobe")

        # 全局并发控制
        self._active_ffprobe_count = 0
        self._concurrency_cond = None

    async def _acquire_global_slot(self, max_concurrency: int, cancel_event: asyncio.Event) -> bool:
        if self._concurrency_cond is None:
            self._concurrency_cond = asyncio.Condition()

        async with self._concurrency_cond:
            while self._active_ffprobe_count >= max_concurrency:
                if cancel_event.is_set():
                    return False
                try:
                    await asyncio.wait_for(self._concurrency_cond.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            if cancel_event.is_set():
                return False
            self._active_ffprobe_count += 1
            return True

    async def _release_global_slot(self):
        if self._concurrency_cond is None:
            return
        async with self._concurrency_cond:
            self._active_ffprobe_count = max(0, self._active_ffprobe_count - 1)
            self._concurrency_cond.notify_all()

    # ─── 任务生命周期 ────────────────────────────────────────

    def create_task(
        self,
        relative_path: str,
        scope: str,
        force: bool,
        filter_status: List[str],
        concurrency: int,
        timeout: int,
        use_mock: bool = False,
    ) -> Task:
        task_id = str(uuid.uuid4())
        task = Task(
            task_id=task_id,
            status=TaskStatus.PENDING,
            relative_path=relative_path,
            scope=scope,
            force=force,
            filter_status=filter_status,
            concurrency=concurrency,
            timeout=timeout,
            use_mock=use_mock,
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[Dict]:
        return [t.to_dict() for t in sorted(
            self._tasks.values(),
            key=lambda t: t.created_at,
            reverse=True
        )]

    def cancel_task(self, task_id: str) -> bool:
        """
        取消任务。
        - PENDING：直接标记为 CANCELLED
        - RUNNING：设置双层事件，async 层停止调度新文件，thread 层 kill 当前 ffprobe 子进程
        """
        task = self._tasks.get(task_id)
        if not task:
            return False

        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            task.finished_at = datetime.now().isoformat()
            self._emit_status(task)
            return True

        if task.status == TaskStatus.RUNNING:
            # 同时设置两个事件
            task._cancel_async.set()
            task._stop_thread.set()
            self._log(task, "warning", "⏹ 收到取消请求，正在终止当前 FFprobe 子进程…")
            return True

        return False

    def clear_history(self):
        finished = [
            tid for tid, t in self._tasks.items()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED)
        ]
        for tid in finished:
            del self._tasks[tid]

    # ─── WebSocket ───────────────────────────────────────────

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._ws_subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue):
        self._ws_subscribers.get(task_id, set()).discard(queue)

    def _broadcast(self, task_id: str, event: Dict[str, Any]):
        for q in list(self._ws_subscribers.get(task_id, set())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ─── 日志 ────────────────────────────────────────────────

    def _log(self, task: Task, level: str, message: str):
        # 同时写入 Python logging 系统 (也就是 data/nfo-injector.log)
        if level.upper() == "INFO":
            logger.info(f"[{task.task_id[:8]}] {message}")
        elif level.upper() == "ERROR":
            logger.error(f"[{task.task_id[:8]}] {message}")
        elif level.upper() == "WARNING":
            logger.warning(f"[{task.task_id[:8]}] {message}")

        entry = TaskLog(timestamp=datetime.now().isoformat(), level=level, message=message)
        task.logs.append(entry)
        if len(task.logs) > 5000:
            task.logs = task.logs[-4000:]
        self._broadcast(task.task_id, {
            "type": "log",
            "task_id": task.task_id,
            "timestamp": entry.timestamp,
            "level": level,
            "message": message,
        })

    def _emit_progress(self, task: Task):
        self._broadcast(task.task_id, {
            "type": "progress",
            "task_id": task.task_id,
            "progress": task.progress.to_dict(),
        })

    def _emit_status(self, task: Task):
        self._broadcast(task.task_id, {
            "type": "status",
            "task_id": task.task_id,
            "status": task.status,
        })

    # ─── 主任务执行 ──────────────────────────────────────────

    async def run_task(self, task: Task, config: AppConfig):
        """异步执行注入任务"""
        # 如果在排队期间已被取消，直接返回
        if task._cancel_async.is_set() or task.status == TaskStatus.CANCELLED:
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now().isoformat()
        self._emit_status(task)

        try:
            try:
                lib, abs_target = split_lib_path(task.relative_path, config)
            except ValueError as e:
                self._log(task, "error", f"❌ 路径无效: {e}")
                task.status = TaskStatus.FAILED
                return

            self._log(task, "info",
                      f"▶ 任务开始 | 库: {lib.name or lib.id} | 路径: {task.relative_path} | 范围: {task.scope} | "
                      f"并发: {task.concurrency} | 超时: {task.timeout}s")

            # 收集待处理文件：优先复用扫描缓存，命中则跳过 NFO 重读
            recursive = task.scope == "recursive"
            ttl = config.scan_cache_ttl
            subtree_key = task.relative_path  # "<lib_id>" 或 "<lib_id>/..."
            cached_entries = entries_from_cache(subtree_key, ttl) if ttl > 0 else None

            if cached_entries is not None:
                self._log(task, "info", f"复用扫描缓存（{len(cached_entries)} 文件），跳过重扫")
                filter_statuses = {NfoStatus(s) for s in task.filter_status} if task.filter_status else None
                pending = []
                for e in cached_entries:
                    if filter_statuses and e.status not in filter_statuses:
                        if not task.force:
                            continue
                    pending.append((e.strm_path, e.nfo_path, e.detail))
            else:
                self._log(task, "info", "缓存未命中/过期，重新扫描 NFO")
                strm_files = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    fb.get_strm_files_in_path,  # 经模块属性调用以支持测试 spy
                    abs_target,
                    config.exclude_dirs,
                    recursive,
                )
                if task._cancel_async.is_set():
                    raise asyncio.CancelledError()
                if not strm_files:
                    self._log(task, "warning", "未找到任何 STRM 文件")
                    task.status = TaskStatus.COMPLETED
                    return
                self._log(task, "info", f"扫描完成，共 {len(strm_files)} 个 STRM 文件")
                filter_statuses = {NfoStatus(s) for s in task.filter_status} if task.filter_status else None
                pending = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    self._filter_strm_files,
                    strm_files,
                    filter_statuses,
                    task.force,
                )

            if task._cancel_async.is_set():
                raise asyncio.CancelledError()

            skipped_by_filter = (len(strm_files) - len(pending)) if (cached_entries is None) else 0
            if skipped_by_filter > 0:
                self._log(task, "info",
                          f"状态过滤: 跳过 {skipped_by_filter} 个（不符合过滤条件 {task.filter_status}）")

            if not pending:
                self._log(task, "info", "过滤后无待处理文件，任务结束")
                task.status = TaskStatus.COMPLETED
                return

            self._log(task, "info",
                      f"待处理: {len(pending)} 个文件 | "
                      f"全局并发上限: {config.max_concurrency} | "
                      f"FFprobe 超时: {task.timeout}s/文件")
            task.progress.total = len(pending)
            self._emit_progress(task)

            async def process_one(sf, nfo_path, detail):
                # 在获取全局槽位前先检查取消
                if task._cancel_async.is_set():
                    return

                acquired = await self._acquire_global_slot(config.max_concurrency, task._cancel_async)
                if not acquired:
                    return

                try:
                    if task._cancel_async.is_set():
                        return
                    await self._process_strm_file(task, config, sf, nfo_path, detail)
                finally:
                    await self._release_global_slot()

            await asyncio.gather(*[
                process_one(sf, nfo_path, detail)
                for sf, nfo_path, detail in pending
            ])

        except asyncio.CancelledError:
            pass  # 正常取消路径
        except Exception as e:
            import traceback
            self._log(task, "error", f"❌ 任务异常: {e}")
            self._log(task, "error", traceback.format_exc()[:500])
            task.status = TaskStatus.FAILED
        finally:
            if task._cancel_async.is_set() and task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.CANCELLED
                self._log(task, "warning",
                          f"⏹ 任务已取消 | 已处理 {task.progress.processed}/{task.progress.total} | "
                          f"成功 {task.progress.success} | 失败 {task.progress.failed}")
            elif task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.COMPLETED
                self._log(task, "info",
                          f"✅ 任务完成 | 成功 {task.progress.success} | "
                          f"跳过 {task.progress.skipped} | 失败 {task.progress.failed}")
                if task.progress.failed > 0:
                    self._log_error_summary(task)

            task.finished_at = datetime.now().isoformat()
            self._emit_status(task)
            self._emit_progress(task)
            self._broadcast(task.task_id, {"type": "done", "task_id": task.task_id})

    def _log_error_summary(self, task: Task):
        """打印错误汇总"""
        p = task.progress
        summary_parts = []
        if p.err_timeout > 0:     summary_parts.append(f"超时×{p.err_timeout}")
        if p.err_forbidden > 0:   summary_parts.append(f"403×{p.err_forbidden}")
        if p.err_not_found > 0:   summary_parts.append(f"未找到文件×{p.err_not_found}")
        if p.err_inject > 0:      summary_parts.append(f"注入失败×{p.err_inject}")
        if p.err_other > 0:       summary_parts.append(f"其他×{p.err_other}")
        if summary_parts:
            self._log(task, "warning", f"   错误汇总: {' | '.join(summary_parts)}")

    @staticmethod
    def _filter_strm_files(strm_files, filter_statuses, force):
        """在线程中批量检查 NFO 状态，返回 (strm_path, nfo_path, detail) 列表"""
        pending = []
        for sf in strm_files:
            nfo_path = find_nfo_for_strm(sf)
            detail = analyze_nfo(nfo_path)
            if filter_statuses and detail.status not in filter_statuses:
                if not force:
                    continue
            pending.append((sf, nfo_path, detail))
        return pending

    @staticmethod
    def _refresh_cache_after_inject(config: AppConfig, strm_path: Path):
        """注入成功后翻新该文件缓存条目（lib_strm_path 用于算 key）。"""
        lib = resolve_library(strm_path, config)
        if lib:
            try:
                update_file_cache_entry(lib.id, strm_path, Path(lib.strm_path))
            except Exception:
                pass  # 翻新失败不影响任务

    # ─── 单文件处理 ──────────────────────────────────────────

    async def _process_strm_file(self, task, config, strm_path, nfo_path, detail):
        """处理单个 STRM 文件：FFprobe → 注入"""
        if task._cancel_async.is_set():
            task.progress.cancelled += 1
            return

        lib = resolve_library(strm_path, config)
        if lib:
            try:
                rel = f"{lib.id}/{strm_path.relative_to(Path(lib.strm_path)).as_posix()}"
            except ValueError:
                rel = strm_path.name
        else:
            rel = strm_path.name

        self._log(task, "info", f"── {rel} [{detail.status_label}]")

        # 非强制 + HEALTHY → 跳过
        if not task.force and detail.status == NfoStatus.HEALTHY:
            self._log(task, "info", "   跳过（已健康）")
            task.progress.processed += 1
            task.progress.skipped += 1
            self._emit_progress(task)
            return

        # 无 NFO 文件 → 无法注入
        if nfo_path is None:
            self._log(task, "warning", "   跳过（找不到 NFO 文件）")
            task.progress.processed += 1
            task.progress.failed += 1
            task.progress.err_not_found += 1
            self._emit_progress(task)
            return

        media_base = resolve_media_path(strm_path, config)

        def log_cb(msg: str):
            self._log(task, "info", f"   {msg}")

        # ── 虚拟注入模式 ────────────────────────────────────────
        if task.use_mock:
            self._log(task, "warning", "   ⚠️ 启用虚拟注入模式 (跳过 FFprobe)")
            inject_result = inject_mock_mediainfo_to_nfo(nfo_path, force=task.force)
            task.progress.processed += 1
            if inject_result["success"]:
                self._log(task, "success", f"   ✓ {inject_result['message']}")
                task.progress.success += 1
                self._refresh_cache_after_inject(config, strm_path)
            elif inject_result.get("skipped"):
                self._log(task, "info", f"   → {inject_result['message']}")
                task.progress.skipped += 1
            else:
                self._log(task, "error", f"   ✗ {inject_result['message']}")
                task.progress.failed += 1
                task.progress.err_inject += 1
            self._emit_progress(task)
            return

        if media_base is None:
            self._log(task, "error", "   ✗ 该路径不属于任何库")
            task.progress.processed += 1
            task.progress.failed += 1
            task.progress.err_other += 1
            self._emit_progress(task)
            return

        # ── 正常 FFprobe（在线程池中执行）────────────────────────
        loop = asyncio.get_event_loop()
        probe_result = await loop.run_in_executor(
            self._executor,
            lambda: probe_with_retry(
                base_path=media_base,
                extensions=config.guess_extensions,
                timeout=task.timeout,
                max_retries=config.max_retries,
                retry_delay=config.retry_delay,
                forbidden_retry_delay=config.forbidden_retry_delay,
                log_callback=log_cb,
                stop_event=task._stop_thread,
            )
        )

        # 处理 FFprobe 结果
        if probe_result.error_type == "cancelled":
            self._log(task, "warning", "   ⏹ 已取消")
            task.progress.processed += 1
            task.progress.cancelled += 1
            self._emit_progress(task)
            return

        if not probe_result.success:
            error_type = probe_result.error_type or "other"
            self._log(task, "error", f"   ✗ 探测失败 [{error_type}]: {probe_result.error}")
            task.progress.processed += 1
            task.progress.failed += 1
            # 错误分类统计
            if error_type == "timeout":        task.progress.err_timeout += 1
            elif error_type == "forbidden":    task.progress.err_forbidden += 1
            elif error_type == "not_found":    task.progress.err_not_found += 1
            else:                              task.progress.err_other += 1
            self._emit_progress(task)
            return

        # ── 注入 NFO ─────────────────────────────────────────
        inject_result = inject_mediainfo(nfo_path, probe_result.data, force=task.force)

        task.progress.processed += 1

        if inject_result["success"]:
            self._log(task, "success", f"   ✓ {inject_result['message']}")
            task.progress.success += 1
            self._refresh_cache_after_inject(config, strm_path)
        elif inject_result.get("skipped"):
            self._log(task, "info", f"   → {inject_result['message']}")
            task.progress.skipped += 1
        else:
            self._log(task, "error", f"   ✗ {inject_result['message']}")
            task.progress.failed += 1
            task.progress.err_inject += 1

        self._emit_progress(task)


# ─── 全局单例 ────────────────────────────────────────────────
task_manager = TaskManager()
