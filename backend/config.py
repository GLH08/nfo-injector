#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理模块
支持从环境变量、config.json 文件读取，并通过 API 动态修改
"""

import json
import os
import uuid
from pathlib import Path, PurePath, PurePosixPath
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field

CONFIG_FILE = Path("/app/data/config.json")


class Library(BaseModel):
    """一个媒体库：STRM 目录 ↔ 媒体目录 的独立映射"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8],
                    description="稳定短 id，重命名不变，API/树节点寻址用")
    name: str = Field(default="", description="显示名")
    strm_path: str = Field(default="", description="STRM 目录绝对路径（容器内）")
    media_path: str = Field(default="", description="媒体目录绝对路径（容器内）")
    enabled: bool = Field(default=True, description="是否启用")


class AppConfig(BaseModel):
    """应用全局配置"""
    libraries: List[Library] = Field(default_factory=list, description="媒体库列表")

    # FFprobe 参数（全局统一）
    ffprobe_timeout: int = Field(default=75, ge=10, le=600, description="FFprobe 超时秒数")
    max_concurrency: int = Field(default=2, ge=1, le=8, description="最大并发探测数")
    guess_extensions: List[str] = Field(
        default=[".mp4", ".mkv", ".ts", ".avi", ".iso", ".rmvb", ".flv", ".mpg", ".mpeg"],
        description="猜测的媒体文件扩展名列表（优先级从高到低）"
    )
    max_retries: int = Field(default=3, ge=1, le=10, description="单文件最大重试次数")
    retry_delay: float = Field(default=2.0, ge=0.5, le=30.0, description="超时重试等待秒数")
    forbidden_retry_delay: float = Field(default=5.0, ge=1.0, le=60.0, description="403 重试等待秒数")

    scan_cache_ttl: float = Field(default=600, ge=0, description="扫描缓存有效期（秒），0 表示不缓存")

    exclude_dirs: List[str] = Field(
        default=["trailers", "extrafanart", "behind the scenes", "featurettes"],
        description="扫描时忽略的目录名（不区分大小写）"
    )


_config_cache: Optional[AppConfig] = None


def load_config() -> AppConfig:
    """加载配置：优先 config.json（必要时迁移旧格式），否则从环境变量种子"""
    global _config_cache
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate(data)
            _config_cache = AppConfig(**data)
            return _config_cache
        except Exception as e:
            print(f"[CONFIG] Failed to load config.json: {e}, using defaults")
    _config_cache = _seed_from_env()
    return _config_cache


def save_config(config: AppConfig) -> None:
    """持久化配置到 config.json"""
    global _config_cache
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config.model_dump(), f, indent=2, ensure_ascii=False)
    _config_cache = config


def get_config() -> AppConfig:
    """获取当前配置（带缓存）"""
    global _config_cache
    if _config_cache is None:
        return load_config()
    return _config_cache


def _migrate(data: dict) -> dict:
    """旧格式（strm_root/media_root/path_mappings）→ libraries"""
    if data.get("libraries"):
        return data
    libs = []
    strm_root = data.get("strm_root")
    media_root = data.get("media_root")
    if strm_root and media_root:
        libs.append({
            "id": uuid.uuid4().hex[:8],
            "name": "主库",
            "strm_path": strm_root,
            "media_path": media_root,
            "enabled": True
        })
        for m in data.get("path_mappings", []):
            try:
                libs.append({
                    "id": uuid.uuid4().hex[:8],
                    "name": m.get("description") or m.get("strm_prefix", "映射"),
                    "strm_path": str(PurePosixPath(strm_root) / m["strm_prefix"]),
                    "media_path": str(PurePosixPath(media_root) / m["media_prefix"]),
                    "enabled": True
                })
            except KeyError:
                continue
    data["libraries"] = libs
    for k in ("strm_root", "media_root", "path_mappings"):
        data.pop(k, None)
    return data


def _seed_from_env() -> AppConfig:
    """无 config.json 时，用环境变量建首个库"""
    strm = os.environ.get("STRM_ROOT")
    media = os.environ.get("MEDIA_ROOT")
    libs = []
    if strm and media:
        libs.append(Library(name="主库", strm_path=strm, media_path=media))
    return AppConfig(libraries=libs)


def get_library(config: AppConfig, lib_id: str) -> Optional[Library]:
    return next((l for l in config.libraries if l.id == lib_id), None)


def resolve_library(abs_strm_path: Path, config: AppConfig) -> Optional[Library]:
    """返回 strm_path 为 abs_strm_path 父级（或相等）且最长的那个 enabled 库"""
    best: Optional[Library] = None
    best_len = -1
    for lib in config.libraries:
        if not lib.enabled:
            continue
        lib_root = Path(lib.strm_path)
        try:
            abs_strm_path.relative_to(lib_root)
        except ValueError:
            continue
        n = len(lib_root.parts)
        if n > best_len:
            best_len = n
            best = lib
    return best


def resolve_media_path(abs_strm_path: Path, config: AppConfig) -> Optional[Path]:
    """绝对 STRM 文件路径 → 去后缀的媒体基路径；无匹配库返回 None"""
    lib = resolve_library(abs_strm_path, config)
    if lib is None:
        return None
    rel = abs_strm_path.relative_to(Path(lib.strm_path)).with_suffix("")
    return Path(lib.media_path) / rel


def split_lib_path(path: str, config: AppConfig) -> Tuple[Library, Path]:
    """
    path: "<lib_id>" 或 "<lib_id>/<库内相对路径>"
    返回 (library, abs_strm_path). 未知库或越权（含 ..）抛 ValueError.
    """
    raw = path.strip("/")
    parts = raw.split("/", 1)
    lib_id = parts[0]
    rel = parts[1] if len(parts) > 1 else ""
    lib = get_library(config, lib_id)
    if lib is None:
        raise ValueError(f"未知库 id: {lib_id}")
    if ".." in PurePath(rel).parts:
        raise ValueError(f"非法路径: {path}")
    abs_path = Path(lib.strm_path) / rel if rel else Path(lib.strm_path)
    return lib, abs_path
