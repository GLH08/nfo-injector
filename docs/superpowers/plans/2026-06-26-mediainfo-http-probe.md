# mediainfo HTTP 探测引擎 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 新增 mediainfo + OpenList HTTP Range 探测引擎，绕开 ffprobe 对 non-faststart mp4 的 FUSE 卡死；媒体文件名通过手动刷新的磁盘缓存获取。

**Architecture:** 四组件——`media_index.py`（STRM→媒体文件名持久化索引）、`openlist_resolver.py`（拼 URL）、`mediainfo_runner.py`（下载50MB头+mediainfo解析+转ffprobe dict，返回 ProbeResult）、`task_manager.py` 改动（按库 media_url_root 选引擎）。ffprobe 保留为 fallback。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic v2 / mediainfo(CLI) / requests / Docker。

## Global Constraints

- 运行时新增依赖：`requests`（加到 requirements.txt）。mediainfo 通过 Dockerfile apt 装。
- Pydantic v2。`ProbeResult` 用 pydantic BaseModel（与 ffprobe_runner 一致）。
- 库相对路径作 key（`<lib_id>/...` POSIX）。
- media_index 持久化 `data/media_index.json`，无 TTL，手动刷新。
- 测试用 uv Python 3.12 venv（`.venv-test`），`cd nfo-injector && .venv-test/Scripts/python.exe -m pytest tests/ -v`。
- 前端 2 空格缩进、单引号、`const $ = id => document.getElementById(id)`。
- 每 Task 结束 commit，body 末尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 现有 ProbeResult 字段：success/data/tried_path/tried_extension/error/error_type/raw_stderr。
- HEALTHY 5 字段：codec/width/height/framerate/duration（见 nfo_handler._check_video_completeness）。

## File Structure

- `backend/media_index.py`（新）：`MediaIndex` 类 + 模块单例 `media_index`。
- `backend/openlist_resolver.py`（新）：`resolve(media_url_root, strm_lib_relative, media_filename) -> str`。
- `backend/mediainfo_runner.py`（新）：`ProbeResult`（或 import ffprobe_runner 的）、`probe(url, timeout, stop_event, log) -> ProbeResult`、`_mi_to_ffprobe_dict(mi_json) -> dict`。
- `backend/config.py`（改）：`Library` 加 `media_url_root: str = ""`。
- `backend/task_manager.py`（改）：`_process_strm_file` 探测段分流。
- `backend/main.py`（改）：`POST /api/media-index/refresh?path=`、`GET /api/media-index?path=`。
- `frontend/index.html`（改）：右键菜单加「刷新媒体文件名索引」；库配置行加 media_url_root 输入框。
- `frontend/app.js`（改）：右键刷新处理；库行读写 media_url_root。
- `Dockerfile`（改）：apt 加 mediainfo。
- `requirements.txt`（改）：加 requests。

---

### Task 1: media_index.py 媒体文件名索引

**Files:** Create `backend/media_index.py`, `tests/test_media_index.py`.

**Interfaces:**
- Produces: `MediaIndex` 类（`refresh_index(lib_id, lib_strm_path, exclude_dirs, guess_extensions, subdir_relative="") -> dict{scanned,indexed,missing}`、`get(lib_id, strm_lib_relative) -> Optional[str]`、`load()`/`save()`），模块单例 `media_index`。持久化 `Path("/app/data/media_index.json")`（测试用 monkeypatch 改路径）。`_INDEX_FILE` 模块级变量。
- Consumes: `backend.file_browser.get_strm_files_in_path`（遍历 STRM）、`backend.config.Library` 的 guess_extensions（从 config 传）。

匹配规则（复用 ffprobe_runner.run_ffprobe_sync）：
1. 按 guess_extensions 逐个试 `<stem>.<ext>` 是否存在（Path.exists）
2. 否则列父目录，单文件直接用；多文件按 `norm`（`re.sub(r'\W+','',s).lower()`）stem 前缀匹配
3. 都没 → 不记入

- [ ] **Step 1: 写失败测试** `tests/test_media_index.py`:

```python
from pathlib import Path
from backend import media_index as mi_mod
from backend.media_index import media_index


def _make(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")  # 媒体
    (root / "Movie" / "B").mkdir(parents=True)
    (root / "Movie" / "B" / "B.strm").write_text("http://y", encoding="utf-8")  # 无媒体
    return root


def _setup_index_file(tmp_path, monkeypatch):
    f = tmp_path / "media_index.json"
    monkeypatch.setattr(mi_mod, "_INDEX_FILE", f)
    media_index._data = None
    media_index.load()


def test_refresh_indexes_filenames(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    r = media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"])
    assert r["scanned"] == 2
    assert r["indexed"] == 1
    assert r["missing"] == 1
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"
    assert media_index.get("lib1", "Movie/B/B.strm") is None


def test_refresh_subdir_only(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"], subdir_relative="Movie/A")
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"


def test_persistence_save_load(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"])
    # 模拟重启
    media_index._data = None
    media_index.load()
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"
    assert (tmp_path / "media_index.json").exists()
```

- [ ] **Step 2: 运行确认失败** `pytest tests/test_media_index.py -v` → ImportError
- [ ] **Step 3: 实现** `backend/media_index.py`:

```python
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
        if self._loaded:
            return
        with self._lock:
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
                      subdir_relative: str = "") -> Dict[str, int]:
        self.load()
        abs_base = Path(lib_strm_path)
        if subdir_relative:
            abs_base = abs_base / subdir_relative
        strm_files = get_strm_files_in_path(abs_base, exclude_dirs, recursive=True)
        lib_strm = Path(lib_strm_path)
        indexed = {}
        scanned = 0
        missing = 0
        for sf in strm_files:
            scanned += 1
            rel = sf.relative_to(lib_strm).as_posix()
            name = self._match_media(sf, guess_extensions)
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
    def _match_media(strm_path: Path, guess_extensions: List[str]) -> Optional[str]:
        parent = strm_path.parent
        stem = strm_path.stem
        # 1. 按扩展名猜
        for ext in guess_extensions:
            p = parent / f"{stem}{ext}"
            if p.exists():
                return p.name
        # 2. 列目录
        try:
            cands = [f for f in parent.iterdir()
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
```

- [ ] **Step 4: 运行确认通过** → 3 passed
- [ ] **Step 5: Commit** `feat(nfo-injector): 媒体文件名索引 media_index`

---

### Task 2: openlist_resolver.py

**Files:** Create `backend/openlist_resolver.py`, `tests/test_openlist_resolver.py`.

**Interfaces:** `resolve(media_url_root: str, strm_lib_relative: str, media_filename: str) -> str`。
- `strm_lib_relative` = STRM 相对库根的 POSIX 路径，**含 .strm 后缀**（与 media_index key 一致）。
- 去 `.strm` 后缀取目录部分，拼 `media_url_root + "/" + 去后缀的相对目录 + media_filename`。
- `media_url_root` 为空返回 ""（调用方判走 ffprobe）。

例：`media_url_root=https://openlist.novaw.de/d/115/Media`，`strm_lib_relative=Meta/JP/NO-ZH/ABF-259/ABF-259.strm`，`media_filename=ABF-259.mp4` →
`https://openlist.novaw.de/d/115/Media/Meta/JP/NO-ZH/ABF-259/ABF-259.mp4`

- [ ] **Step 1: 写失败测试**:

```python
from backend.openlist_resolver import resolve


def test_basic():
    url = resolve("https://openlist.novaw.de/d/115/Media",
                  "Meta/JP/NO-ZH/ABF-259/ABF-259.strm", "ABF-259.mp4")
    assert url == "https://openlist.novaw.de/d/115/Media/Meta/JP/NO-ZH/ABF-259/ABF-259.mp4"


def test_chinese():
    url = resolve("https://openlist.novaw.de/d/115/Media",
                  "中转/CN/收集/某片/某片.strm", "某片.mp4")
    assert url == "https://openlist.novaw.de/d/115/Media/中转/CN/收集/某片/某片.mp4"


def test_empty_root():
    assert resolve("", "x/y.strm", "y.mp4") == ""


def test_trailing_slash_root():
    url = resolve("https://openlist.novaw.de/d/115/Media/",
                  "Meta/A.strm", "A.mp4")
    assert url == "https://openlist.novaw.de/d/115/Media/Meta/A.mp4"
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** `backend/openlist_resolver.py`:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STRM 库相对路径 + 媒体文件名 → OpenList HTTP URL。"""
from urllib.parse import quote


def resolve(media_url_root: str, strm_lib_relative: str, media_filename: str) -> str:
    if not media_url_root or not strm_lib_relative or not media_filename:
        return ""
    # 去 .strm 后缀，取目录部分
    rel = strm_lib_relative
    if rel.endswith(".strm"):
        rel = rel[:-5]
    # rel = "Meta/JP/.../ABF-259"，目录 = "Meta/JP/.../ABF-259"（含文件名 stem 作为目录）
    # 实际：媒体文件与 STRM 同目录，文件名 stem 相同
    # 所以 URL = root + "/" + rel(去后缀的完整相对路径，但末段是 stem) ... 
    # 不对：rel 去后缀后是 "Meta/JP/NO-ZH/ABF-259/ABF-259"，媒体是 "ABF-259.mp4" 在同目录
    # 即 url = root + "/" + dirname(rel) + "/" + media_filename
    parts = rel.rsplit("/", 1)
    dir_rel = parts[0] if len(parts) > 1 else ""
    base = media_url_root.rstrip("/")
    if dir_rel:
        path = f"{base}/{dir_rel}/{media_filename}"
    else:
        path = f"{base}/{media_filename}"
    # 对路径部分做百分号编码（保留 /）
    head, _, tail = path.partition("://")
    encoded = quote(tail, safe="/:?=&%")
    return f"{head}://{encoded}"
```

- [ ] **Step 4: 确认通过** → 4 passed
- [ ] **Step 5: Commit** `feat(nfo-injector): openlist_resolver URL 拼接`

---

### Task 3: mediainfo_runner.py + 转换

**Files:** Create `backend/mediainfo_runner.py`, `tests/test_mediainfo_runner.py`.

**Interfaces:**
- `probe(url: str, timeout: int, stop_event, log: Callable) -> ProbeResult`（import ffprobe_runner.ProbeResult）。
- `_mi_to_ffprobe_dict(mi_json: str) -> dict`（纯函数，可单测）。
- 下载头 50MB（requests Range），失败兜底下尾 50MB（先 HEAD 拿 Content-Length）。

- [ ] **Step 1: 写失败测试**（重点测转换函数 + mock）:

```python
import json
from backend.mediainfo_runner import _mi_to_ffprobe_dict


MI_JSON = json.dumps({"media": {"track": [
    {"@type": "General", "Duration": "7262.030"},
    {"@type": "Video", "Format": "AVC", "Width": "1280", "Height": "720",
     "FrameRate": "59.940", "DisplayAspectRatio": "1.778"},
    {"@type": "Audio", "Format": "AAC", "Channels": "2", "SamplingRate": "48000",
     "Language": "Japanese"},
]}})


def test_conversion_video():
    d = _mi_to_ffprobe_dict(MI_JSON)
    v = [s for s in d["streams"] if s["codec_type"] == "video"][0]
    assert v["codec_name"] == "h264"
    assert v["width"] == 1280
    assert v["height"] == 720
    assert v["r_frame_rate"] == "59.940"
    assert v["display_aspect_ratio"] == "1.778"
    assert d["format"]["duration"] == "7262.030"


def test_conversion_audio():
    d = _mi_to_ffprobe_dict(MI_JSON)
    a = [s for s in d["streams"] if s["codec_type"] == "audio"][0]
    assert a["codec_name"] == "aac"
    assert a["channels"] == 2
    assert a["sample_rate"] == "48000"
    assert a["tags"]["language"] == "jpn"


def test_conversion_empty():
    d = _mi_to_ffprobe_dict('{"media":{"track":[]}}')
    assert d == {"streams": [], "format": {}}
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** `backend/mediainfo_runner.py`:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mediainfo + HTTP Range 探测引擎：绕开 ffprobe 对 non-faststart mp4 的 FUSE 卡死。"""
import json
import logging
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

import requests

from backend.ffprobe_runner import ProbeResult

logger = logging.getLogger("nfo-injector")

HEAD_BYTES = 50 * 1024 * 1024  # 50MB

_CODEC_MAP = {"AVC": "h264", "HEVC": "hevc", "MPEG-4 Visual": "divx", "AV1": "av1", "VP9": "vp9"}
_AUDIO_MAP = {"AAC": "aac", "AC-3": "ac3", "E-AC-3": "eac3", "DTS": "dts", "FLAC": "flac", "MPEG Audio": "mp3", "Opus": "opus"}
_LANG_MAP = {"Japanese": "jpn", "Chinese": "chi", "English": "eng", "Mandarin": "chi", "Cantonese": "yue"}


def _mi_to_ffprobe_dict(mi_json: str) -> dict:
    try:
        d = json.loads(mi_json)
    except Exception:
        return {"streams": [], "format": {}}
    tracks = d.get("media", {}).get("track", [])
    streams = []
    duration = ""
    for t in tracks:
        ttype = t.get("@type")
        if ttype == "General":
            duration = t.get("Duration", "") or ""
        elif ttype == "Video":
            codec = _CODEC_MAP.get(t.get("Format", ""), (t.get("Format", "") or "").lower())
            streams.append({
                "codec_type": "video",
                "codec_name": codec,
                "width": int(t["Width"]) if t.get("Width", "").isdigit() else None,
                "height": int(t["Height"]) if t.get("Height", "").isdigit() else None,
                "display_aspect_ratio": t.get("DisplayAspectRatio"),
                "r_frame_rate": t.get("FrameRate"),
                "duration": t.get("Duration") or duration,
                "tags": {"language": "und"},
            })
        elif ttype == "Audio":
            codec = _AUDIO_MAP.get(t.get("Format", ""), (t.get("Format", "") or "").lower())
            lang = _LANG_MAP.get(t.get("Language", ""), "und")
            ch = t.get("Channels", "")
            streams.append({
                "codec_type": "audio",
                "codec_name": codec,
                "channels": int(ch) if str(ch).isdigit() else None,
                "sample_rate": t.get("SamplingRate"),
                "tags": {"language": lang},
            })
    # 补 video duration
    for s in streams:
        if s["codec_type"] == "video" and not s.get("duration"):
            s["duration"] = duration
    return {"streams": streams, "format": {"duration": duration} if duration else {}}


def _download_range(url: str, start: int, end: int, timeout: int, dest: Path) -> bool:
    try:
        headers = {"Range": f"bytes={start}-{end}"}
        r = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
        if r.status_code not in (200, 206):
            return False
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        logger.warning(f"download fail {url}: {e}")
        return False


def _content_length(url: str, timeout: int = 20) -> Optional[int]:
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        cl = r.headers.get("Content-Length")
        return int(cl) if cl else None
    except Exception:
        return None


def _run_mediainfo(path: Path) -> Optional[dict]:
    try:
        r = subprocess.run(["mediainfo", "--Output=JSON", str(path)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception as e:
        logger.warning(f"mediainfo fail: {e}")
        return None


def _has_tracks(mi: dict) -> bool:
    tracks = (mi or {}).get("media", {}).get("track", [])
    return any(t.get("@type") in ("Video", "Audio") for t in tracks)


def probe(url: str, timeout: int, stop_event: Optional[threading.Event],
          log: Callable[[str], None]) -> ProbeResult:
    if stop_event and stop_event.is_set():
        return ProbeResult(success=False, data=None, tried_path=url, tried_extension=None,
                            error="cancelled", error_type="cancelled", raw_stderr=None)
    log(f"  尝试探测(HTTP): {url[:80]}...")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # 1. 头 50MB
        if not _download_range(url, 0, HEAD_BYTES - 1, timeout, tmp_path):
            return ProbeResult(success=False, data=None, tried_path=url, tried_extension=None,
                               error="下载失败", error_type="timeout", raw_stderr=None)
        mi = _run_mediainfo(tmp_path)
        if mi and _has_tracks(mi):
            data = _mi_to_ffprobe_dict(json.dumps(mi))
            log("   ✓ mediainfo 解析成功(头50MB)")
            return ProbeResult(success=True, data=data, tried_path=url, tried_extension=None,
                               error=None, error_type=None, raw_stderr=None)
        # 2. 尾 50MB 兜底
        cl = _content_length(url)
        if cl and cl > HEAD_BYTES:
            log("   头50MB无moov，尝试尾50MB...")
            start = cl - HEAD_BYTES
            if _download_range(url, start, cl - 1, timeout, tmp_path):
                mi = _run_mediainfo(tmp_path)
                if mi and _has_tracks(mi):
                    data = _mi_to_ffprobe_dict(json.dumps(mi))
                    log("   ✓ mediainfo 解析成功(尾50MB)")
                    return ProbeResult(success=True, data=data, tried_path=url, tried_extension=None,
                                       error=None, error_type=None, raw_stderr=None)
        return ProbeResult(success=False, data=None, tried_path=url, tried_extension=None,
                           error="头尾50MB均无有效track", error_type="error", raw_stderr=None)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
```

- [ ] **Step 4: 确认通过** → 3 passed（转换测试；probe 的网络部分靠 Task 8 端到端验证）
- [ ] **Step 5: Commit** `feat(nfo-injector): mediainfo_runner HTTP Range 探测引擎`

---

### Task 4: Library.media_url_root 配置

**Files:** `backend/config.py`, `tests/test_config_model.py`.

- [ ] **Step 1: 写失败测试**（追加 test_config_model.py）:

```python
def test_library_media_url_root_default_empty():
    from backend.config import Library
    l = Library(id="x", name="n", strm_path="/s", media_path="/m")
    assert l.media_url_root == ""
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** config.py Library 加（在 enabled 后）:

```python
    media_url_root: str = Field(default="", description="OpenList HTTP 根（如 https://openlist.novaw.de/d/115/Media）；空则走 ffprobe")
```

- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** `feat(nfo-injector): Library.media_url_root 配置`

---

### Task 5: task_manager 探测引擎分流

**Files:** `backend/task_manager.py`, `tests/test_task_inject_cache.py`（追加）或新 `tests/test_task_mediainfo.py`.

**关键**：`_process_strm_file` 探测段。现有 line ~500-545 用 `resolve_media_path` + `probe_with_retry`。改为：lib 有 `media_url_root` 走 mediainfo_runner，否则原 ffprobe。

- [ ] **Step 1: 写失败测试** `tests/test_task_mediainfo.py`:

```python
import asyncio
import backend.config as config
from backend.config import AppConfig, Library
from backend import media_index as mi_mod
from backend import mediainfo_runner
from backend.task_manager import task_manager, TaskStatus
from backend.ffprobe_runner import ProbeResult


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    monkeypatch.setattr(mi_mod, "_INDEX_FILE", tmp_path / "media_index.json")
    mi_mod.media_index._data = None
    mi_mod.media_index.load()
    config._config_cache = AppConfig(
        libraries=[Library(id="lib1", name="主库", strm_path=str(root),
                           media_path=str(tmp_path / "media"),
                           media_url_root="https://openlist.novaw.de/d/115/Media")],
    )
    mi_mod.media_index.refresh_index("lib1", root, [], [".mp4", ".mkv"])
    return root


def test_mediainfo_path_used_when_url_root_set(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    called = {"probe": 0}
    def fake_probe(url, timeout, stop_event, log):
        called["probe"] += 1
        return ProbeResult(success=True, data={"streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "r_frame_rate": "30.000", "duration": "100"}], "format": {"duration": "100"}},
            tried_path=url, tried_extension=None, error=None, error_type=None, raw_stderr=None)
    monkeypatch.setattr(mediainfo_runner, "probe", fake_probe)
    task = task_manager.create_task("lib1", "recursive", False, ["EMPTY"], 2, 5, use_mock=False)
    asyncio.run(task_manager.run_task(task, config.get_config()))
    assert called["probe"] == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.progress.success == 1
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 改 task_manager.py**:

import 区加：
```python
from backend import mediainfo_runner
from backend.media_index import media_index
from backend.openlist_resolver import resolve as resolve_openlist_url
```

`_process_strm_file` 探测段，把现有 `media_base = resolve_media_path(...)` 起到 `probe_result = await ...probe_with_retry(...)` 整段，替换为：

```python
        lib = resolve_library(strm_path, config)
        use_mediainfo = bool(lib and lib.media_url_root)

        if use_mediainfo:
            try:
                strm_rel = strm_path.relative_to(Path(lib.strm_path)).as_posix()
            except ValueError:
                strm_rel = strm_path.name
            media_name = media_index.get(lib.id, strm_rel)
            if not media_name:
                self._log(task, "error", "   ✗ 媒体索引未找到该 STRM 的媒体文件名（请右键目录刷新媒体文件名索引）")
                task.progress.processed += 1
                task.progress.failed += 1
                task.progress.err_not_found += 1
                self._emit_progress(task)
                return
            url = resolve_openlist_url(lib.media_url_root, strm_rel, media_name)
            loop = asyncio.get_event_loop()
            probe_result = await loop.run_in_executor(
                self._executor,
                lambda: mediainfo_runner.probe(url, task.timeout, task._stop_thread, log_cb)
            )
        else:
            media_base = resolve_media_path(strm_path, config)
            if media_base is None:
                self._log(task, "error", "   ✗ 该路径不属于任何库")
                task.progress.processed += 1
                task.progress.failed += 1
                task.progress.err_other += 1
                self._emit_progress(task)
                return
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
```

注意保留后面的 `if probe_result.error_type == "cancelled":` 等处理不变。

- [ ] **Step 4: 跑新测试 + 全量** `pytest tests/ -v`
- [ ] **Step 5: Commit** `feat(nfo-injector): task_manager 按 media_url_root 分流探测引擎`

---

### Task 6: API + 前端

**Files:** `backend/main.py`（`POST /api/media-index/refresh`、`GET /api/media-index`）、`frontend/index.html`（右键菜单项 + 库行 media_url_root 输入框）、`frontend/app.js`（右键刷新处理 + 库行读写）。

- [ ] **Step 1: main.py 加 API**:

```python
from backend.media_index import media_index

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
        lib.id, Path(lib.strm_path), config.exclude_dirs, config.guess_extensions, subdir
    )
    return result

@app.get("/api/media-index")
async def get_media_index(path: str = ""):
    config = get_config()
    if not path:
        return {lib.id: len(media_index._data.get(lib.id, {})) for lib in config.libraries}
    parts = path.split("/", 1)
    lib_id = parts[0]
    return {"count": len(media_index._data.get(lib_id, {}))}
```

- [ ] **Step 2: index.html 右键菜单**（在 ctxScan 后加）:

```html
    <div class="ctx-item" id="ctxRefreshMediaIndex">📁 刷新媒体文件名索引</div>
```

库配置行（app.js addLibraryRow 的 innerHTML）加一个 input（class library-urlroot）。

- [ ] **Step 3: app.js**:
  - init 绑定 `$('ctxRefreshMediaIndex').addEventListener('click', refreshMediaIndex)`
  - `refreshMediaIndex()`：POST `/api/media-index/refresh?path=${currentDirCtx}` → toast
  - `addLibraryRow` 加 `<input class="library-urlroot" placeholder="OpenList URL根(可选)" value="...">`，collectLibraries 读 `row.querySelector('.library-urlroot').value.trim()`
  - `renderLibrariesList` 传 `l.media_url_root`

- [ ] **Step 4: 跑全量测试** `pytest tests/ -v`；`node --check frontend/app.js`
- [ ] **Step 5: Commit** `feat(nfo-injector): 媒体索引刷新 API + 前端右键菜单`

---

### Task 7: Dockerfile + requirements

**Files:** `Dockerfile`, `requirements.txt`.

- [ ] **Step 1: Dockerfile** apt 行加 mediainfo:

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg mediainfo && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: requirements.txt** 加:

```
requests==2.32.3
```

- [ ] **Step 3: 装到 venv 测** `uv pip install --python .venv-test/Scripts/python.exe requests`；`pytest tests/ -v`
- [ ] **Step 4: Commit** `feat(nfo-injector): Dockerfile 装 mediainfo + requirements 加 requests`

---

### Task 8: 端到端验证（部署后用户驱动）

- [ ] 部署、配置某库 media_url_root、右键刷新媒体索引、注入验证。

---

## 自检

- Spec 覆盖：media_index(Task1)、openlist_resolver(Task2)、mediainfo_runner+转换(Task3)、config(Task4)、task分流(Task5)、API+前端(Task6)、Dockerfile(Task7)。
- openlist_resolver 的 dir_rel 计算已修正（rel.rsplit("/",1) 取目录）。
- mediainfo_runner 复用 ffprobe_runner.ProbeResult（import），不重复定义。
- task_manager 分流：lib.media_url_root 为空走 ffprobe（现有测试 test_task_inject_cache 仍通过）。
