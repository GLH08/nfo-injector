#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体文件名索引：STRM 库相对路径 → 同目录媒体文件名。持久化，手动刷新。"""
import json
import re
import threading
from pathlib import Path
from typing import Dict, Optional, List
import logging

from backend.file_browser import get_strm_files_in_path

logger = logging.getLogger("nfo-injector")
_INDEX_FILE = Path("/app/data/media_index.json")


def _norm(s: str) -> str:
    return re.sub(r'\W+', '', s).lower()


class MediaIndex:
    def __init__(self):
        self._data: Dict[str, Dict[str, str]] = {}  # lib_id -> {strm_rel: media_name}
        self._lock = threading.Lock()
        self._loaded = False

    def load(self):
        if self._loaded and self._data is not None:
            return
        with self._lock:
            self._data = {}
            if _INDEX_FILE.exists():
                try:
                    self._data = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"media_index load fail: {e}")
                    self._data = {}
            self._loaded = True

    def save(self):
        with self._lock:
            _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
            _INDEX_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, lib_id: str, strm_lib_relative: str) -> Optional[str]:
        self.load()
        with self._lock:
            return self._data.get(lib_id, {}).get(strm_lib_relative)

    def refresh_index(self, lib_id: str, lib_strm_path: Path,
                      exclude_dirs: List[str], guess_extensions: List[str],
                      subdir_relative: str = "",
                      lib_media_path: Optional[str] = None) -> Dict[str, int]:
        self.load()
        abs_base = Path(lib_strm_path)
        if subdir_relative:
            abs_base = abs_base / subdir_relative
        strm_files = get_strm_files_in_path(abs_base, exclude_dirs, recursive=True)
        lib_strm = Path(lib_strm_path)
        media_root = Path(lib_media_path) if lib_media_path else None
        indexed = {}
        scanned = 0
        missing = 0
        for sf in strm_files:
            scanned += 1
            rel = sf.relative_to(lib_strm).as_posix()
            # 媒体目录：若库配了 media_path，媒体在 media_root/rel所在目录；
            # 否则回退到 STRM 同目录
            if media_root is not None:
                rel_dir = sf.relative_to(lib_strm).parent.as_posix()
                media_dir = media_root / rel_dir if rel_dir else media_root
            else:
                media_dir = sf.parent
            name = self._match_media(sf.stem, media_dir, guess_extensions)
            if name:
                indexed[rel] = name
            else:
                missing += 1
        with self._lock:
            # 替换该 lib（或该子树）的条目
            if subdir_relative:
                prefix = subdir_relative + "/"
                old = {k: v for k, v in self._data.get(lib_id, {}).items()
                       if not k.startswith(prefix)}
                old.update(indexed)
                self._data[lib_id] = old
            else:
                self._data[lib_id] = indexed
        self.save()
        return {"scanned": scanned, "indexed": len(indexed), "missing": missing}

    @staticmethod
    def _match_media(stem: str, media_dir: Path, guess_extensions: List[str]) -> Optional[str]:
        # 1. 按扩展名猜
        for ext in guess_extensions:
            p = media_dir / f"{stem}{ext}"
            if p.exists():
                return p.name
        # 2. 列目录
        try:
            cands = [f for f in media_dir.iterdir()
                     if f.is_file() and f.suffix.lower() in guess_extensions]
        except Exception:
            return None
        if len(cands) == 1:
            return cands[0].name
        if len(cands) > 1:
            n_base = _norm(stem)
            for c in cands:
                if n_base and _norm(c.stem).startswith(n_base):
                    return c.name
        return None


media_index = MediaIndex()
