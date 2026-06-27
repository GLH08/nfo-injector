#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件浏览模块
- 遍历 STRM 目录树
- 返回每个条目的 NFO 状态
- 支持目录级别的状态统计聚合
"""

import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from enum import Enum

from backend.nfo_handler import NfoStatus, NfoDetail, analyze_nfo, find_nfo_for_strm


class EntryType(str, Enum):
    DIRECTORY = "directory"
    STRM_FILE = "strm"
    OTHER_FILE = "file"
    LIBRARY = "library"


@dataclass
class StatusCount:
    healthy: int = 0
    partial: int = 0
    empty: int = 0
    missing: int = 0
    total: int = 0

    def add(self, status: NfoStatus):
        self.total += 1
        if status == NfoStatus.HEALTHY:
            self.healthy += 1
        elif status == NfoStatus.PARTIAL:
            self.partial += 1
        elif status == NfoStatus.EMPTY:
            self.empty += 1
        elif status == NfoStatus.MISSING:
            self.missing += 1

    def merge(self, other: "StatusCount"):
        self.healthy += other.healthy
        self.partial += other.partial
        self.empty += other.empty
        self.missing += other.missing
        self.total += other.total

    def to_dict(self) -> Dict:
        return {
            "healthy": self.healthy,
            "partial": self.partial,
            "empty": self.empty,
            "missing": self.missing,
            "total": self.total,
        }

    @property
    def needs_injection(self) -> int:
        """需要注入的文件数（非 HEALTHY）"""
        return self.partial + self.empty + self.missing


@dataclass
class BrowseEntry:
    name: str
    relative_path: str          # "<库id>/<库内相对路径>"
    entry_type: EntryType

    # 仅 STRM 文件有效
    nfo_path: Optional[str] = None
    nfo_status: Optional[NfoStatus] = None
    nfo_status_label: Optional[str] = None
    nfo_status_color: Optional[str] = None
    missing_fields: Optional[List[str]] = None
    indexed: Optional[bool] = None  # 是否在媒体索引里（media_index 有该 STRM 的媒体文件名）

    # 仅目录有效（浅层统计，非递归）
    child_count: int = 0
    status_summary: Optional[Dict] = None  # StatusCount.to_dict()

    has_children: bool = False  # 是否有子目录/STRM 文件


# ─── 扫描缓存（进程内）──────────────────────────────────────
@dataclass
class ScanEntry:
    strm_path: Path
    nfo_path: Optional[Path]
    status: NfoStatus
    detail: NfoDetail

# key = "<lib_id>/<库内 posix 相对路径>"
_FILE_CACHE: Dict[str, ScanEntry] = {}
# key = "<lib_id>" 或 "<lib_id>/<库内 posix 子树路径>"，value = monotonic 时间戳
_SCANNED_SUBTREES: Dict[str, float] = {}
_LOCK = threading.Lock()


def _cache_key(lib_id: str, strm_abs: Path, lib_strm_path: Path) -> str:
    rel = strm_abs.relative_to(lib_strm_path).as_posix()
    return f"{lib_id}/{rel}"


def _subtree_key(lib_id: str, abs_dir: Path, lib_strm_path: Path) -> str:
    if abs_dir == lib_strm_path:
        return lib_id
    rel = abs_dir.relative_to(lib_strm_path).as_posix()
    return f"{lib_id}/{rel}"


def clear_scan_cache() -> None:
    """清空整个扫描缓存（全局手动刷新用）。"""
    with _LOCK:
        _FILE_CACHE.clear()
        _SCANNED_SUBTREES.clear()


def scan_and_cache(
    abs_dir: Path,
    lib_id: str,
    lib_strm_path: Path,
    exclude_dirs: Optional[List[str]] = None,
) -> StatusCount:
    """
    递归扫描 abs_dir 子树，读取每个 .strm 的 NFO 状态，写入 _FILE_CACHE，
    更新 _SCANNED_SUBTREES[subtree_key]，清理该子树下已不存在的旧条目，返回计数。
    """
    if exclude_dirs is None:
        exclude_dirs = ["trailers", "extrafanart"]
    exclude_lower = {d.lower() for d in exclude_dirs}

    counts = StatusCount()
    if not abs_dir.exists():
        return counts

    subtree_key = _subtree_key(lib_id, abs_dir, lib_strm_path)
    subtree_prefix = subtree_key + "/"
    seen_keys: set = set()

    for root, dirs, files in os.walk(abs_dir):
        dirs[:] = [d for d in dirs if d.lower() not in exclude_lower]
        for fname in files:
            if not fname.lower().endswith(".strm"):
                continue
            strm_path = Path(root) / fname
            nfo_path = find_nfo_for_strm(strm_path)
            detail = analyze_nfo(nfo_path)
            counts.add(detail.status)
            key = _cache_key(lib_id, strm_path, lib_strm_path)
            seen_keys.add(key)
            with _LOCK:
                _FILE_CACHE[key] = ScanEntry(
                    strm_path=strm_path,
                    nfo_path=nfo_path,
                    status=detail.status,
                    detail=detail,
                )

    # 清理该子树下本次未触及的旧条目
    with _LOCK:
        stale = [k for k in _FILE_CACHE if k.startswith(subtree_prefix) and k not in seen_keys]
        for k in stale:
            del _FILE_CACHE[k]
        _SCANNED_SUBTREES[subtree_key] = time.monotonic()

    return counts


def _find_ancestor_subtree(subtree_key: str, ttl: float) -> Optional[str]:
    """
    返回覆盖 subtree_key 且在 TTL 内的祖先（或自身）子树 key；无则 None。
    命中条件：a == subtree_key 或 subtree_key.startswith(a + "/")，且未过期。
    选最近（最长匹配）的祖先。
    """
    now = time.monotonic()
    candidates = []
    with _LOCK:
        for a, ts in _SCANNED_SUBTREES.items():
            if a == subtree_key or subtree_key.startswith(a + "/"):
                if now - ts <= ttl:
                    candidates.append(a)
    if not candidates:
        return None
    # 最长匹配 = 最具体的祖先
    return max(candidates, key=len)


def counts_from_cache(subtree_key: str, ttl: float) -> Optional[StatusCount]:
    ancestor = _find_ancestor_subtree(subtree_key, ttl)
    if ancestor is None:
        return None
    counts = StatusCount()
    prefix = subtree_key + "/"
    with _LOCK:
        snapshot = [
            (k, e) for k, e in _FILE_CACHE.items()
            if k == subtree_key or k.startswith(prefix)
        ]
    for k, e in snapshot:
        counts.add(e.status)
    return counts


def entries_from_cache(subtree_key: str, ttl: float) -> Optional[List[ScanEntry]]:
    ancestor = _find_ancestor_subtree(subtree_key, ttl)
    if ancestor is None:
        return None
    prefix = subtree_key + "/"
    with _LOCK:
        snapshot = [
            e for k, e in _FILE_CACHE.items()
            if k == subtree_key or k.startswith(prefix)
        ]
    return snapshot


def file_status_from_cache(cache_key: str, ttl: float) -> Optional[ScanEntry]:
    """若该文件所在子树在 TTL 内被扫过，返回缓存的 ScanEntry，否则 None。

    cache_key 形如 "<lib_id>/<库内相对路径>"（文件级）。避免 Browse 阶段重复读 FUSE NFO。
    """
    if ttl <= 0:
        return None
    if _find_ancestor_subtree(cache_key, ttl) is None:
        return None
    with _LOCK:
        return _FILE_CACHE.get(cache_key)


def update_file_cache_entry(
    lib_id: str,
    strm_abs: Path,
    lib_strm_path: Path,
) -> None:
    """注入成功后重读该单文件 NFO，翻新缓存条目（幂等）。"""
    nfo_path = find_nfo_for_strm(strm_abs)
    detail = analyze_nfo(nfo_path)
    key = _cache_key(lib_id, strm_abs, lib_strm_path)
    with _LOCK:
        _FILE_CACHE[key] = ScanEntry(
            strm_path=strm_abs,
            nfo_path=nfo_path,
            status=detail.status,
            detail=detail,
        )


def browse_directory(
    abs_dir: Path,
    lib_id: str,
    lib_strm_path: Path,
    exclude_dirs: Optional[List[str]] = None,
    ttl: float = 0,
) -> List[BrowseEntry]:
    """
    浏览库内某绝对目录，返回当前层级条目。
    relative_path 一律拼成 "<lib_id>/<库内相对路径>"。

    ttl > 0 时，STRM 文件的 NFO 状态优先取扫描缓存（file_status_from_cache），
    命中则跳过 read FUSE；未命中才 analyze_nfo。目录结构始终 iterdir 实时列出。
    """
    if exclude_dirs is None:
        exclude_dirs = ["trailers", "extrafanart"]
    exclude_lower = {d.lower() for d in exclude_dirs}

    if not abs_dir.exists() or not abs_dir.is_dir():
        return []

    try:
        items = sorted(abs_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return []

    entries: List[BrowseEntry] = []
    for item in items:
        rel_posix = f"{lib_id}/{item.relative_to(lib_strm_path).as_posix()}"

        if item.is_dir():
            if item.name.lower() in exclude_lower:
                continue
            entries.append(BrowseEntry(
                name=item.name,
                relative_path=rel_posix,
                entry_type=EntryType.DIRECTORY,
                has_children=_has_strm_children(item, exclude_lower),
            ))
        elif item.suffix.lower() == ".strm":
            cached = file_status_from_cache(rel_posix, ttl) if ttl > 0 else None
            if cached is not None:
                detail = cached.detail
                nfo_path = cached.nfo_path
            else:
                nfo_path = find_nfo_for_strm(item)
                detail = analyze_nfo(nfo_path)
            entries.append(BrowseEntry(
                name=item.name,
                relative_path=rel_posix,
                entry_type=EntryType.STRM_FILE,
                nfo_path=str(nfo_path) if nfo_path else None,
                nfo_status=detail.status,
                nfo_status_label=detail.status_label,
                nfo_status_color=detail.status_color,
                missing_fields=detail.missing_fields,
            ))
    return entries


def scan_directory_recursive(
    abs_dir: Path,
    exclude_dirs: Optional[List[str]] = None,
) -> StatusCount:
    """递归统计某绝对目录下所有 STRM 的 NFO 状态"""
    if exclude_dirs is None:
        exclude_dirs = ["trailers", "extrafanart"]
    exclude_lower = {d.lower() for d in exclude_dirs}

    counts = StatusCount()
    if not abs_dir.exists():
        return counts

    for root, dirs, files in os.walk(abs_dir):
        dirs[:] = [d for d in dirs if d.lower() not in exclude_lower]
        for fname in files:
            if fname.lower().endswith(".strm"):
                strm_path = Path(root) / fname
                nfo_path = find_nfo_for_strm(strm_path)
                detail = analyze_nfo(nfo_path)
                counts.add(detail.status)
    return counts


def get_strm_files_in_path(
    abs_target: Path,
    exclude_dirs: Optional[List[str]] = None,
    recursive: bool = True,
) -> List[Path]:
    """获取某绝对路径（文件或目录）下的所有 STRM 绝对路径"""
    if exclude_dirs is None:
        exclude_dirs = ["trailers", "extrafanart"]
    exclude_lower = {d.lower() for d in exclude_dirs}

    strm_files: List[Path] = []
    if abs_target.is_file() and abs_target.suffix.lower() == ".strm":
        strm_files.append(abs_target)
    elif abs_target.is_dir():
        if recursive:
            for root, dirs, files in os.walk(abs_target):
                dirs[:] = [d for d in dirs if d.lower() not in exclude_lower]
                for fname in files:
                    if fname.lower().endswith(".strm"):
                        strm_files.append(Path(root) / fname)
        else:
            for item in abs_target.iterdir():
                if item.is_file() and item.suffix.lower() == ".strm":
                    strm_files.append(item)
    return sorted(strm_files)


def _has_strm_children(directory: Path, exclude_lower: set) -> bool:
    """检查目录是否包含 STRM 文件或子目录（浅层检查）"""
    try:
        for item in directory.iterdir():
            if item.is_dir() and item.name.lower() not in exclude_lower:
                return True
            if item.is_file() and item.suffix.lower() == ".strm":
                return True
    except PermissionError:
        pass
    return False
