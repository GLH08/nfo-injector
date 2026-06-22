#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件浏览模块
- 遍历 STRM 目录树
- 返回每个条目的 NFO 状态
- 支持目录级别的状态统计聚合
"""

import os
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

    # 仅目录有效（浅层统计，非递归）
    child_count: int = 0
    status_summary: Optional[Dict] = None  # StatusCount.to_dict()

    has_children: bool = False  # 是否有子目录/STRM 文件


def browse_directory(
    abs_dir: Path,
    lib_id: str,
    lib_strm_path: Path,
    exclude_dirs: Optional[List[str]] = None,
) -> List[BrowseEntry]:
    """
    浏览库内某绝对目录，返回当前层级条目。
    relative_path 一律拼成 "<lib_id>/<库内相对路径>"。
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
