# 多库自由映射 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## 执行状态（2026-06-22 更新）

- **Task 1–9 代码已全部写入磁盘**（config / file_browser / main / task_manager / index.html / app.js / docker-compose / .env.example）。
- **Task 之外补丁：** `frontend/style.css` 增加 `.library-rule` 网格（5 列）与 `.library-enabled` 样式，修复库配置行布局。
- **测试已实际运行并通过：** 用 `uv` 安装的 Python 3.12 建临时 venv 跑 `pytest tests/` → **27 passed**。
  - 期间修复 3 处问题：
    1. `task_manager.py:251` 语法错误 `discard queue` → `discard(queue)`；
    2. `config.py` `_migrate` 用 `Path` 拼接在 Windows 上把 `/` 变 `\` → 改用 `PurePosixPath` 保留容器 POSIX 路径；
    3. `test_file_browser.py` 期望 `"empty"`（小写）→ 实际枚举值为 `"EMPTY"`，修正测试期望。
- **未执行：** Task 10 端到端 Docker 手工验证、各 Task 的 git commit 步骤（用户未要求提交）。
- **环境备注：** 系统 Python 3.14 无法构建 `pydantic-core`；本机用 `uv python install 3.12` + `uv venv` 跑测试。生产容器为 Linux，`pathlib.Path` 行为正常，迁移逻辑的 `PurePosixPath` 修复对容器无副作用。



**Goal:** 把 nfo-injector 从「单一 STRM 根 + 单一媒体根」重构为「库列表」模型，使每个库可自由指定任意 STRM 目录 ↔ 任意网盘媒体目录，且新增库只需在 Web UI 操作。

**Architecture:** 容器一次性挂载 `/apps:/apps`（host 路径 == 容器路径）。配置改为 `libraries: List[Library]`，每个库自带 `strm_path`/`media_path` 绝对路径。所有 API 的 `path` 参数语义改为 `<库id>/<库内相对路径>`，后端用 `split_lib_path` 拆解并做越权校验，用 `resolve_library`（最长前缀匹配）/`resolve_media_path` 完成 strm→media 解析。FFprobe 探测逻辑与任务流程保持不变。

**Tech Stack:** Python 3.12 / FastAPI 0.115.5 / Pydantic v2 (2.10.3) / 原生 JS 前端 / Docker。测试用 pytest + httpx（仅开发依赖）。

## Global Constraints

- 运行时不新增依赖；测试依赖单列 `requirements-dev.txt`（`pytest`、`httpx`）。
- Pydantic v2 写法（`model_dump` / `model_fields` / `Field(default_factory=...)`）。
- 库 id 由 `uuid.uuid4().hex[:8]` 生成，**与库名解耦**（重命名不改 id）。
- API path 方案统一为 `<库id>` 或 `<库id>/<库内相对路径>`；根浏览（path 为空）返回库列表。
- 越权校验：库内相对路径不得含 `..` 段。
- FFprobe 参数全局统一，**不做**每库覆盖。
- 配置持久化到容器内 `/app/data/config.json`（宿主机 `nfo-injector/data/config.json`），逻辑不变。
- 删除旧字段 `strm_root` / `media_root` / `path_mappings` 与 `PathMapping` 类。
- **本项目当前不是 git 仓库**：各任务末尾的 `git commit` 步骤为可选检查点——若要启用，先在项目根 `git init`；否则把"Commit"当作"完成本任务"的标记跳过。
- 测试从 `nfo-injector/` 目录运行：`python -m pytest tests/ -v`。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `nfo-injector/requirements-dev.txt` | 新增：测试依赖 |
| `nfo-injector/tests/conftest.py` | 新增：让 `import backend` 在任意 cwd 可用 |
| `nfo-injector/tests/test_*.py` | 新增：单元/接口测试 |
| `nfo-injector/backend/config.py` | `Library` 模型、`AppConfig` 改字段、加载/迁移/种子、`resolve_library`/`resolve_media_path`/`get_library`/`split_lib_path` |
| `nfo-injector/backend/file_browser.py` | `EntryType.LIBRARY`；浏览/扫描/收集函数改为按绝对子目录工作、相对路径带库 id 前缀 |
| `nfo-injector/backend/main.py` | browse 根返回库列表、各端点走库寻址、全局 scan 求和、config API 适配 |
| `nfo-injector/backend/task_manager.py` | 任务收集与单文件处理改走库解析 |
| `nfo-injector/frontend/index.html` | 配置弹窗：删「路径配置/路径映射」，加「媒体库」tab |
| `nfo-injector/frontend/app.js` | 树根渲染库节点、库配置增删 UI、path 带库 id |
| `nfo-injector/docker-compose.yml` | 挂载 `/apps:/apps`、透传环境变量 |
| `nfo-injector/.env.example` | 注释说明 `STRM_ROOT/MEDIA_ROOT` 仅用于首次建库 |

---

## Task 1: 测试基建 + 配置数据模型

**Files:**
- Create: `nfo-injector/requirements-dev.txt`
- Create: `nfo-injector/tests/conftest.py`
- Create: `nfo-injector/tests/test_config_model.py`
- Modify: `nfo-injector/backend/config.py`（顶部 import、`Library` 类、`AppConfig` 类、删除 `PathMapping`）

**Interfaces:**
- Produces:
  - `Library(BaseModel)`：字段 `id: str`（默认 `uuid4().hex[:8]`）、`name: str=""`、`strm_path: str=""`、`media_path: str=""`、`enabled: bool=True`
  - `AppConfig(BaseModel)`：字段 `libraries: List[Library]=[]` + 原全局 FFprobe 字段；**不含** `strm_root/media_root/path_mappings`

- [ ] **Step 1: 写测试依赖文件**

Create `nfo-injector/requirements-dev.txt`:
```
pytest==8.3.4
httpx==0.28.1
```

- [ ] **Step 2: 写 conftest.py（保证 backend 可导入）**

Create `nfo-injector/tests/conftest.py`:
```python
import sys
from pathlib import Path

# 让 `import backend.xxx` 在任意工作目录下都可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 3: 安装测试依赖**

Run（在 `nfo-injector/` 下）: `python -m pip install -r requirements-dev.txt`
Expected: 成功安装 pytest、httpx（及 starlette TestClient 所需）。

- [ ] **Step 4: 写失败测试**

Create `nfo-injector/tests/test_config_model.py`:
```python
from backend.config import Library, AppConfig


def test_library_auto_id_and_defaults():
    lib = Library(name="JP主库", strm_path="/apps/s", media_path="/apps/m")
    assert isinstance(lib.id, str) and len(lib.id) == 8
    assert lib.enabled is True
    assert lib.name == "JP主库"


def test_library_keeps_given_id():
    lib = Library(id="ab12cd34", name="X", strm_path="/apps/s", media_path="/apps/m")
    assert lib.id == "ab12cd34"


def test_appconfig_defaults():
    c = AppConfig()
    assert c.libraries == []
    assert c.max_concurrency == 2
    assert c.ffprobe_timeout == 75


def test_old_fields_removed():
    fields = AppConfig.model_fields
    assert "libraries" in fields
    assert "strm_root" not in fields
    assert "media_root" not in fields
    assert "path_mappings" not in fields
```

- [ ] **Step 5: 运行测试确认失败**

Run: `python -m pytest tests/test_config_model.py -v`
Expected: FAIL（`Library` 不存在 / `AppConfig` 仍含旧字段）。

- [ ] **Step 6: 实现模型**

In `nfo-injector/backend/config.py`，把文件顶部到 `AppConfig` 定义结束的部分改为：
```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理模块
支持从环境变量、config.json 文件读取，并通过 API 动态修改
"""

import json
import os
import uuid
from pathlib import Path, PurePath
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

    exclude_dirs: List[str] = Field(
        default=["trailers", "extrafanart", "behind the scenes", "featurettes"],
        description="扫描时忽略的目录名（不区分大小写）"
    )
```

> 注意：保留文件中 `_config_cache`、`load_config`、`save_config`、`get_config`、`resolve_media_path` 等后续定义，Task 2/3 会改它们。本步只替换"顶部到 AppConfig 结束"这一段，并删除原 `PathMapping` 类。

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/test_config_model.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 8: Commit（如使用 git）**

```bash
git add nfo-injector/requirements-dev.txt nfo-injector/tests/ nfo-injector/backend/config.py
git commit -m "feat(config): Library model and libraries-based AppConfig"
```

---

## Task 2: 配置加载 / 迁移 / 首次种子

**Files:**
- Modify: `nfo-injector/backend/config.py`（`load_config` + 新增 `_migrate`、`_seed_from_env`）
- Create: `nfo-injector/tests/test_config_load.py`

**Interfaces:**
- Consumes: `Library`, `AppConfig`, 模块级 `CONFIG_FILE`, `_config_cache`（Task 1）
- Produces:
  - `load_config() -> AppConfig`：config.json 存在→（必要时迁移旧格式）解析；不存在→从环境变量种子
  - `_migrate(data: dict) -> dict`：旧格式（`strm_root/media_root/path_mappings`）转 `libraries`
  - `_seed_from_env() -> AppConfig`：用 `STRM_ROOT/MEDIA_ROOT` 建单个"主库"

- [ ] **Step 1: 写失败测试**

Create `nfo-injector/tests/test_config_load.py`:
```python
import json
import backend.config as config


def test_seed_from_env_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.json")
    monkeypatch.setenv("STRM_ROOT", "/apps/moviepilot/115strm/Emby")
    monkeypatch.setenv("MEDIA_ROOT", "/apps/clouddrive2/CloudDrive/115open/Media")
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 1
    assert c.libraries[0].name == "主库"
    assert c.libraries[0].strm_path == "/apps/moviepilot/115strm/Emby"
    assert c.libraries[0].media_path == "/apps/clouddrive2/CloudDrive/115open/Media"


def test_migrate_old_format(tmp_path, monkeypatch):
    old = {
        "strm_root": "/apps/s",
        "media_root": "/apps/m",
        "path_mappings": [
            {"strm_prefix": "中转/CN", "media_prefix": "Meta/CN", "description": "CN库"}
        ],
        "max_concurrency": 3,
    }
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cf)
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 2
    assert c.libraries[0].strm_path == "/apps/s"
    assert c.libraries[0].media_path == "/apps/m"
    assert c.libraries[1].name == "CN库"
    assert c.libraries[1].strm_path == "/apps/s/中转/CN"
    assert c.libraries[1].media_path == "/apps/m/Meta/CN"
    assert c.max_concurrency == 3


def test_new_format_passthrough(tmp_path, monkeypatch):
    new = {"libraries": [
        {"id": "ab12cd34", "name": "X", "strm_path": "/apps/s",
         "media_path": "/apps/m", "enabled": True}
    ]}
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cf)
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 1
    assert c.libraries[0].id == "ab12cd34"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config_load.py -v`
Expected: FAIL（旧 `load_config` 不认识 libraries / 不迁移）。

- [ ] **Step 3: 实现加载/迁移/种子**

In `nfo-injector/backend/config.py`，把现有 `load_config` 函数整体替换为以下三个函数（`_config_cache` 声明保持在它们之前）：
```python
def _migrate(data: dict) -> dict:
    """旧格式（strm_root/media_root/path_mappings）→ libraries"""
    if data.get("libraries"):
        return data
    libs = []
    strm_root = data.get("strm_root")
    media_root = data.get("media_root")
    if strm_root and media_root:
        libs.append({
            "id": uuid.uuid4().hex[:8], "name": "主库",
            "strm_path": strm_root, "media_path": media_root, "enabled": True,
        })
        for m in data.get("path_mappings", []):
            try:
                libs.append({
                    "id": uuid.uuid4().hex[:8],
                    "name": m.get("description") or m.get("strm_prefix", "映射"),
                    "strm_path": str(Path(strm_root) / m["strm_prefix"]),
                    "media_path": str(Path(media_root) / m["media_prefix"]),
                    "enabled": True,
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_config_load.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: Commit（如使用 git）**

```bash
git add nfo-injector/backend/config.py nfo-injector/tests/test_config_load.py
git commit -m "feat(config): load with old-format migration and env seeding"
```

---

## Task 3: 路径解析（resolve_library / resolve_media_path / split_lib_path）

**Files:**
- Modify: `nfo-injector/backend/config.py`（替换旧 `resolve_media_path`，新增 `get_library`、`resolve_library`、`split_lib_path`）
- Create: `nfo-injector/tests/test_resolve.py`

**Interfaces:**
- Consumes: `Library`, `AppConfig`（Task 1）
- Produces:
  - `get_library(config: AppConfig, lib_id: str) -> Optional[Library]`
  - `resolve_library(abs_strm_path: Path, config: AppConfig) -> Optional[Library]`（最长前缀匹配，跳过 disabled）
  - `resolve_media_path(abs_strm_path: Path, config: AppConfig) -> Optional[Path]`（返回去后缀的媒体基路径；无库返回 None）
  - `split_lib_path(path: str, config: AppConfig) -> Tuple[Library, Path]`（拆 `<id>/<rel>`，越权/未知库抛 `ValueError`）

- [ ] **Step 1: 写失败测试**

Create `nfo-injector/tests/test_resolve.py`:
```python
from pathlib import Path
import pytest
from backend.config import (
    AppConfig, Library, get_library, resolve_library, resolve_media_path, split_lib_path,
)


def _cfg():
    return AppConfig(libraries=[
        Library(id="jp", name="JP", strm_path="/apps/strm/Emby",
                media_path="/apps/cd2/115/Media"),
        Library(id="cn", name="CN", strm_path="/apps/strm/Emby/中转/CN",
                media_path="/apps/cd2/115/Media/Meta/CN"),
        Library(id="gd", name="GD", strm_path="/apps/strm/gd",
                media_path="/apps/cd2/Gdrive/Media", enabled=False),
    ])


def test_get_library():
    cfg = _cfg()
    assert get_library(cfg, "cn").name == "CN"
    assert get_library(cfg, "nope") is None


def test_resolve_longest_match():
    p = Path("/apps/strm/Emby/中转/CN/X/X.strm")
    assert resolve_library(p, _cfg()).id == "cn"


def test_resolve_parent_lib():
    p = Path("/apps/strm/Emby/Meta/JP/A/A.strm")
    assert resolve_library(p, _cfg()).id == "jp"


def test_resolve_skips_disabled():
    p = Path("/apps/strm/gd/movie/m.strm")
    assert resolve_library(p, _cfg()) is None


def test_resolve_media_path():
    p = Path("/apps/strm/Emby/Meta/JP/A/A.strm")
    assert resolve_media_path(p, _cfg()) == Path("/apps/cd2/115/Media/Meta/JP/A/A")


def test_resolve_media_none_when_no_lib():
    assert resolve_media_path(Path("/other/x.strm"), _cfg()) is None


def test_split_ok():
    lib, abs_path = split_lib_path("jp/Meta/JP/A/A.strm", _cfg())
    assert lib.id == "jp"
    assert abs_path == Path("/apps/strm/Emby/Meta/JP/A/A.strm")


def test_split_root():
    lib, abs_path = split_lib_path("jp", _cfg())
    assert abs_path == Path("/apps/strm/Emby")


def test_split_unknown_lib():
    with pytest.raises(ValueError):
        split_lib_path("nope/x", _cfg())


def test_split_escape_rejected():
    with pytest.raises(ValueError):
        split_lib_path("jp/../../etc/passwd", _cfg())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_resolve.py -v`
Expected: FAIL（新函数不存在 / 旧 `resolve_media_path` 签名不符）。

- [ ] **Step 3: 实现解析函数**

In `nfo-injector/backend/config.py`，**删除**旧的 `resolve_media_path` 整个函数，在文件末尾追加：
```python
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
    返回 (library, abs_strm_path)。未知库或越权（含 '..'）抛 ValueError。
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_resolve.py -v`
Expected: PASS（10 passed）。

- [ ] **Step 5: Commit（如使用 git）**

```bash
git add nfo-injector/backend/config.py nfo-injector/tests/test_resolve.py
git commit -m "feat(config): library-aware path resolution and split_lib_path"
```

---

## Task 4: file_browser 改造（库 id 前缀 + 新签名）

**Files:**
- Modify: `nfo-injector/backend/file_browser.py`
- Create: `nfo-injector/tests/test_file_browser.py`

**Interfaces:**
- Consumes: `NfoStatus, analyze_nfo, find_nfo_for_strm`（nfo_handler，不变）
- Produces:
  - `EntryType.LIBRARY = "library"`
  - `browse_directory(abs_dir: Path, lib_id: str, lib_strm_path: Path, exclude_dirs=None) -> List[BrowseEntry]`（条目 `relative_path` 形如 `<lib_id>/<库内相对>`）
  - `scan_directory_recursive(abs_dir: Path, exclude_dirs=None) -> StatusCount`
  - `get_strm_files_in_path(abs_target: Path, exclude_dirs=None, recursive=True) -> List[Path]`（返回绝对路径）

- [ ] **Step 1: 写失败测试**

Create `nfo-injector/tests/test_file_browser.py`:
```python
from backend.file_browser import (
    browse_directory, scan_directory_recursive, get_strm_files_in_path, EntryType,
)


def _make_lib(tmp_path):
    root = tmp_path / "lib"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    (root / "Movie" / "B").mkdir(parents=True)
    (root / "Movie" / "B" / "B.strm").write_text("http://y", encoding="utf-8")
    return root


def test_browse_prefixes_lib_id(tmp_path):
    root = _make_lib(tmp_path)
    entries = browse_directory(root / "Movie" / "A", "lib1", root, ["trailers"])
    strm = [e for e in entries if e.entry_type == EntryType.STRM_FILE][0]
    assert strm.relative_path == "lib1/Movie/A/A.strm"
    assert strm.nfo_status == "EMPTY"


def test_browse_dir_entry(tmp_path):
    root = _make_lib(tmp_path)
    entries = browse_directory(root, "lib1", root, ["trailers"])
    d = [e for e in entries if e.entry_type == EntryType.DIRECTORY][0]
    assert d.name == "Movie"
    assert d.relative_path == "lib1/Movie"
    assert d.has_children is True


def test_scan_counts(tmp_path):
    root = _make_lib(tmp_path)
    counts = scan_directory_recursive(root, ["trailers"])
    assert counts.total == 2
    assert counts.empty == 1     # A.nfo 存在但无 fileinfo
    assert counts.missing == 1   # B 无 nfo


def test_get_strm_files(tmp_path):
    root = _make_lib(tmp_path)
    files = get_strm_files_in_path(root, ["trailers"], True)
    assert len(files) == 2
    assert all(f.suffix == ".strm" for f in files)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_file_browser.py -v`
Expected: FAIL（旧签名要求 `strm_root, relative_path`）。

- [ ] **Step 3: 加 LIBRARY 类型**

In `nfo-injector/backend/file_browser.py`，把 `EntryType` 改为：
```python
class EntryType(str, Enum):
    DIRECTORY = "directory"
    STRM_FILE = "strm"
    OTHER_FILE = "file"
    LIBRARY = "library"
```

- [ ] **Step 4: 重写 browse_directory**

替换 `browse_directory` 整个函数：
```python
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
```

- [ ] **Step 5: 重写 scan_directory_recursive 与 get_strm_files_in_path**

替换这两个函数：
```python
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
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_file_browser.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 7: Commit（如使用 git）**

```bash
git add nfo-injector/backend/file_browser.py nfo-injector/tests/test_file_browser.py
git commit -m "feat(browser): library-id-prefixed paths and absolute-dir signatures"
```

---

## Task 5: main.py 端点适配库寻址

**Files:**
- Modify: `nfo-injector/backend/main.py`
- Create: `nfo-injector/tests/test_api.py`

**Interfaces:**
- Consumes: `split_lib_path, resolve_media_path, resolve_library`（config）、`browse_directory, scan_directory_recursive, StatusCount, EntryType`（file_browser）
- Produces: HTTP 行为——`/api/browse?path=` 返回库列表；`/api/browse?path=<id>/...` 浏览库内；`/api/scan?path=` 求和；未知库/越权 → 404

- [ ] **Step 1: 写失败测试**

Create `nfo-injector/tests/test_api.py`:
```python
from fastapi.testclient import TestClient
import backend.config as config
from backend.config import AppConfig, Library
from backend.main import app


def _setup(tmp_path):
    root = tmp_path / "Emby"
    (root / "A").mkdir(parents=True)
    (root / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    config._config_cache = AppConfig(libraries=[
        Library(id="lib1", name="主库", strm_path=str(root), media_path=str(tmp_path / "media")),
    ])
    return root


def test_browse_root_lists_libraries(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["entry_type"] == "library"
    assert entries[0]["relative_path"] == "lib1"
    assert entries[0]["name"] == "主库"


def test_browse_within_library(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=lib1/A")
    names = [e["name"] for e in r.json()["entries"]]
    assert "A.strm" in names


def test_scan_root_sums(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/scan?path=")
    body = r.json()
    assert body["total"] == 1
    assert body["empty"] == 1


def test_browse_unknown_lib_404(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=nope/x")
    assert r.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL（根浏览未返回库列表 / 旧 import 报错）。

- [ ] **Step 3: 改 import 与 ConfigUpdate**

In `nfo-injector/backend/main.py`：

把 config 的 import 改为：
```python
from backend.config import (
    AppConfig, Library, get_config, load_config, save_config,
    resolve_media_path, resolve_library, split_lib_path,
)
```
把 file_browser 的 import 改为：
```python
from backend.file_browser import (
    browse_directory, scan_directory_recursive, get_strm_files_in_path,
    EntryType, StatusCount,
)
```
把 `ConfigUpdate` 类替换为：
```python
class ConfigUpdate(BaseModel):
    libraries: Optional[List[Library]] = None
    ffprobe_timeout: Optional[int] = None
    max_concurrency: Optional[int] = None
    guess_extensions: Optional[List[str]] = None
    max_retries: Optional[int] = None
    retry_delay: Optional[float] = None
    forbidden_retry_delay: Optional[float] = None
    exclude_dirs: Optional[List[str]] = None
```

- [ ] **Step 4: 重写 /api/browse**

替换 `browse` 函数：
```python
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
        None, browse_directory, abs_dir, lib.id, Path(lib.strm_path), config.exclude_dirs,
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
```

- [ ] **Step 5: 重写 /api/scan**

替换 `scan` 函数：
```python
@app.get("/api/scan")
async def scan(path: str = ""):
    """递归统计路径下各状态数量（用于目录徽章）；path 为空时对所有启用库求和"""
    config = get_config()
    loop = asyncio.get_event_loop()

    if not path:
        total = StatusCount()
        for lib in config.libraries:
            if not lib.enabled:
                continue
            c = await loop.run_in_executor(
                None, scan_directory_recursive, Path(lib.strm_path), config.exclude_dirs,
            )
            total.merge(c)
        return {"path": "", **total.to_dict()}

    try:
        lib, abs_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))
    counts = await loop.run_in_executor(
        None, scan_directory_recursive, abs_dir, config.exclude_dirs,
    )
    return {"path": path, **counts.to_dict()}
```

- [ ] **Step 6: 重写 /api/issues**

替换 `find_issues` 函数：
```python
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
                    nfo_path = find_nfo_for_strm(item)
                    detail = analyze_nfo(nfo_path)
                    if detail.status != NfoStatus.HEALTHY:
                        rel = f"{lib_id}/{item.relative_to(lib_strm_path).as_posix()}"
                        issues.append({
                            "path": rel,
                            "status": detail.status,
                            "status_label": detail.status_label,
                            "status_color": detail.status_color,
                        })
        except Exception:
            pass

    if target_dir.is_dir():
        await asyncio.get_event_loop().run_in_executor(None, _find_issues_recursive, target_dir)

    return {"path": path, "issues": issues}
```

- [ ] **Step 7: 重写 /api/nfo、/api/ffprobe、/api/inject-file 的路径解析**

替换 `get_nfo` 中定位 strm 的两行：
```python
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    if not strm_path.exists():
        raise HTTPException(404, f"STRM 文件不存在: {path}")
```
（删除原 `strm_root = Path(config.strm_root)` / `strm_path = strm_root / path`。）

替换 `probe_only` 中定位与媒体解析：
```python
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    media_base = resolve_media_path(strm_path, config)
    if media_base is None:
        raise HTTPException(404, "该路径不属于任何库")
```

替换 `inject_single_file` 中定位：
```python
    config = get_config()
    try:
        lib, strm_path = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    if not strm_path.exists():
        raise HTTPException(404, f"STRM 文件不存在: {path}")
```

> `/api/inject`（POST）无需改：它把 `req.path`（已是 `<id>/<rel>`）交给 task_manager，由 Task 6 解析。

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 9: 跑全量后端测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（Task 1-5 累计）。

- [ ] **Step 10: Commit（如使用 git）**

```bash
git add nfo-injector/backend/main.py nfo-injector/tests/test_api.py
git commit -m "feat(api): library-based browse/scan/issues/nfo/ffprobe addressing"
```

---

## Task 6: task_manager 走库解析

**Files:**
- Modify: `nfo-injector/backend/task_manager.py`
- Create: `nfo-injector/tests/test_task_inject.py`

**Interfaces:**
- Consumes: `split_lib_path, resolve_media_path, resolve_library`（config）、`get_strm_files_in_path`（file_browser，新签名）
- Produces: 注入任务从 `<库id>/<相对>` 正确解析、收集文件、探测、注入；路径无效时任务 FAILED

- [ ] **Step 1: 写失败测试**

Create `nfo-injector/tests/test_task_inject.py`:
```python
import time
from fastapi.testclient import TestClient
import backend.config as config
from backend.config import AppConfig, Library
from backend.main import app


def test_inject_resolves_library_path(tmp_path):
    # A.strm + 空 A.nfo；media 目录不存在 → 探测必然 not_found（不需要 ffprobe 二进制）
    root = tmp_path / "Emby"
    (root / "A").mkdir(parents=True)
    (root / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    config._config_cache = AppConfig(libraries=[
        Library(id="lib1", name="主库", strm_path=str(root), media_path=str(tmp_path / "media")),
    ])

    client = TestClient(app)
    r = client.post("/api/inject", json={
        "path": "lib1/A/A.strm", "scope": "file", "force": False,
        "filter_status": [], "concurrency": 1, "timeout": 10,
    })
    task_id = r.json()["task_id"]

    t = None
    for _ in range(100):
        t = client.get(f"/api/task/{task_id}").json()
        if t["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.1)

    assert t["progress"]["total"] == 1
    assert t["progress"]["failed"] == 1
    assert t["progress"]["errors"]["not_found"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_task_inject.py -v`
Expected: FAIL（task_manager 仍引用 `config.strm_root`，AttributeError → 任务异常或 total≠1）。

- [ ] **Step 3: 改 import**

In `nfo-injector/backend/task_manager.py`，把 config / file_browser 的 import 改为：
```python
from backend.config import AppConfig, resolve_media_path, resolve_library, split_lib_path
from backend.nfo_handler import (
    NfoStatus, analyze_nfo, find_nfo_for_strm, inject_mediainfo, inject_mock_mediainfo_to_nfo
)
from backend.ffprobe_runner import probe_with_retry
from backend.file_browser import get_strm_files_in_path
```

- [ ] **Step 4: 改 run_task 的文件收集**

In `run_task`，把收集 STRM 的那段（原 `strm_root = Path(config.strm_root)` 到 `get_strm_files_in_path(... strm_root, task.relative_path ...)`）替换为：
```python
        try:
            lib, abs_target = split_lib_path(task.relative_path, config)
        except ValueError as e:
            self._log(task, "error", f"❌ 路径无效: {e}")
            task.status = TaskStatus.FAILED
            task.finished_at = datetime.now().isoformat()
            self._emit_status(task)
            return

        try:
            self._log(task, "info",
                      f"▶ 任务开始 | 库: {lib.name or lib.id} | 路径: {task.relative_path} | "
                      f"范围: {task.scope} | 并发: {task.concurrency} | 超时: {task.timeout}s")

            recursive = task.scope == "recursive"
            strm_files = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                get_strm_files_in_path,
                abs_target,
                config.exclude_dirs,
                recursive,
            )
```
> 即：在原 `try:` 之前先做 `split_lib_path`，并删除原先 `strm_root = Path(config.strm_root)` 行；`get_strm_files_in_path` 改为新签名 `(abs_target, config.exclude_dirs, recursive)`。原 `try` 块内第一条 `self._log(... 任务开始 ...)` 用上面带"库:"的版本替换。

- [ ] **Step 5: 改 _process_strm_file 的相对路径与媒体解析**

In `_process_strm_file`，把开头的：
```python
        strm_root = Path(config.strm_root)
        rel = strm_path.relative_to(strm_root).as_posix()
```
替换为：
```python
        lib = resolve_library(strm_path, config)
        rel = strm_path.relative_to(Path(lib.strm_path)).as_posix() if lib else strm_path.name
```
并在 `media_base = resolve_media_path(strm_path, config)` 之后加 None 处理：
```python
        media_base = resolve_media_path(strm_path, config)
        if media_base is None:
            self._log(task, "error", "   ✗ 该路径不属于任何库")
            task.progress.processed += 1
            task.progress.failed += 1
            task.progress.err_other += 1
            self._emit_progress(task)
            return
```
> 注意：该 None 处理要放在「虚拟注入模式」分支**之前**？不——虚拟注入不需要 media_base。把 None 检查放在 `media_base = ...` 这行紧随其后、且在「正常 FFprobe」分支之前即可；虚拟注入分支在它前面已 return，不受影响。保持现有语句顺序：`media_base` 赋值原本就在 `log_cb` 定义之后、虚拟注入分支之前——在该赋值后立即插入 None 检查。

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_task_inject.py -v`
Expected: PASS（1 passed）。

- [ ] **Step 7: 跑全量后端测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS。

- [ ] **Step 8: Commit（如使用 git）**

```bash
git add nfo-injector/backend/task_manager.py nfo-injector/tests/test_task_inject.py
git commit -m "feat(tasks): resolve injection targets via library model"
```

---

## Task 7: 前端配置弹窗「媒体库」tab（index.html）

**Files:**
- Modify: `nfo-injector/frontend/index.html`

**Interfaces:**
- Produces: 配置弹窗含 `data-tab="libraries"` 的「媒体库」tab 与 `#tabLibraries` 面板、`#librariesList` 容器、`#btnAddLibrary` 按钮；删除「路径配置」「路径映射」tab 与面板。FFprobe tab 保留。

- [ ] **Step 1: 替换配置 tabs 头**

In `nfo-injector/frontend/index.html`，把 `.config-tabs` 块替换为：
```html
        <div class="config-tabs">
          <button class="config-tab active" data-tab="libraries">媒体库</button>
          <button class="config-tab" data-tab="ffprobe">FFprobe 参数</button>
        </div>
```

- [ ] **Step 2: 替换「路径配置」面板为「媒体库」面板**

把 `<div class="config-panel active" id="tabPaths">...</div>` 整块替换为：
```html
        <!-- 媒体库 -->
        <div class="config-panel active" id="tabLibraries">
          <p class="form-hint" style="margin-bottom:12px">
            每个库 = 一对「STRM 目录 ↔ 媒体目录」（容器内绝对路径，通常以 /apps 开头）。
            新增库只需加一行，无需改 docker-compose。
          </p>
          <div id="librariesList"></div>
          <button class="btn btn-secondary" id="btnAddLibrary" style="margin-top:8px">
            + 添加库
          </button>
        </div>
```

- [ ] **Step 3: 删除「FFprobe 参数」面板的 active、删除「路径映射」面板**

把 `<div class="config-panel" id="tabFfprobe">` 保持（其本就无 active）。
删除整块 `<div class="config-panel" id="tabMappings">...</div>`。

- [ ] **Step 4: 手工验证（结构）**

Run: `python -c "import pathlib,re;h=pathlib.open if False else open; s=open(r'nfo-injector/frontend/index.html',encoding='utf-8').read(); print('tabLibraries' in s, 'librariesList' in s, 'tabMappings' not in s, 'tabPaths' not in s)"`
Expected: `True True True True`

- [ ] **Step 5: Commit（如使用 git）**

```bash
git add nfo-injector/frontend/index.html
git commit -m "feat(ui): replace path-config/mappings tabs with libraries tab"
```

---

## Task 8: 前端 app.js（树根库节点 + 库配置 UI）

**Files:**
- Modify: `nfo-injector/frontend/app.js`

**Interfaces:**
- Consumes: `/api/browse?path=`（根返回 `entry_type:"library"`）、`/api/config`（含 `libraries`）
- Produces: 树根渲染库（📚）；配置弹窗读写 `libraries`；保存提交 `libraries` 数组

- [ ] **Step 1: 树节点支持 library 类型**

In `nfo-injector/frontend/app.js` 的 `renderTreeLevel`，把
```javascript
    if (entry.entry_type === 'directory') {
```
改为
```javascript
    if (entry.entry_type === 'directory' || entry.entry_type === 'library') {
```
并在该分支内，把图标行
```javascript
      icon.textContent = '📁';
```
改为
```javascript
      icon.textContent = entry.entry_type === 'library' ? '📚' : '📁';
```
同分支内、点击展开里把展开后的图标
```javascript
          icon.textContent = '📂';
```
改为
```javascript
          icon.textContent = entry.entry_type === 'library' ? '📚' : '📂';
```
以及折叠回退
```javascript
          icon.textContent = '📁';
```
改为
```javascript
          icon.textContent = entry.entry_type === 'library' ? '📚' : '📁';
```

> 库节点的 `relative_path` 是 `<库id>`，展开时 `loadTreeChildren` 调 `/api/browse?path=<库id>`，与目录逻辑一致，无需额外改动。

- [ ] **Step 2: 配置弹窗读取改为库列表**

替换 `openConfigModal` 函数：
```javascript
async function openConfigModal() {
  try {
    config = await GET('/api/config');
    renderLibrariesList(config.libraries || []);
    $('cfgTimeout').value = config.ffprobe_timeout || 75;
    $('cfgConcurrency').value = config.max_concurrency || 2;
    $('cfgMaxRetries').value = config.max_retries || 3;
    $('cfgRetryDelay').value = config.retry_delay || 2;
    $('cfgForbiddenDelay').value = config.forbidden_retry_delay || 5;
    $('cfgExtensions').value = (config.guess_extensions || []).join(',');
    $('configModal').style.display = 'flex';
  } catch (e) {
    showToast('加载配置失败: ' + e.message, 'error');
  }
}
```

- [ ] **Step 3: 库列表渲染/增删/收集**

替换 `renderMappingsList` / `addMappingRule` / `addMappingRow` / `collectMappings` 四个函数为：
```javascript
function renderLibrariesList(libraries) {
  const list = $('librariesList');
  list.innerHTML = '';
  libraries.forEach(l => addLibraryRow(list, l));
}

function addLibraryRule() {
  addLibraryRow($('librariesList'), { id: '', name: '', strm_path: '', media_path: '', enabled: true });
}

function addLibraryRow(container, lib) {
  const row = document.createElement('div');
  row.className = 'mapping-rule library-rule';
  row.dataset.id = lib.id || '';
  row.innerHTML = `
    <input type="text" placeholder="库名称" value="${escapeHtml(lib.name || '')}" class="lib-name" />
    <input type="text" placeholder="STRM 目录 (/apps/...)" value="${escapeHtml(lib.strm_path || '')}" class="lib-strm" />
    <input type="text" placeholder="媒体目录 (/apps/...)" value="${escapeHtml(lib.media_path || '')}" class="lib-media" />
    <label class="lib-enabled"><input type="checkbox" ${lib.enabled !== false ? 'checked' : ''} /> 启用</label>
    <button class="mapping-remove" title="删除">✕</button>
  `;
  row.querySelector('.mapping-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function collectLibraries() {
  return Array.from(document.querySelectorAll('.library-rule')).map(row => {
    const lib = {
      name: row.querySelector('.lib-name').value.trim(),
      strm_path: row.querySelector('.lib-strm').value.trim(),
      media_path: row.querySelector('.lib-media').value.trim(),
      enabled: row.querySelector('.lib-enabled input').checked,
    };
    if (row.dataset.id) lib.id = row.dataset.id;
    return lib;
  }).filter(l => l.strm_path && l.media_path);
}
```

- [ ] **Step 4: 保存配置改提交 libraries**

替换 `saveConfig` 函数：
```javascript
async function saveConfig() {
  const payload = {
    libraries: collectLibraries(),
    ffprobe_timeout: parseInt($('cfgTimeout').value),
    max_concurrency: parseInt($('cfgConcurrency').value),
    max_retries: parseInt($('cfgMaxRetries').value),
    retry_delay: parseFloat($('cfgRetryDelay').value),
    forbidden_retry_delay: parseFloat($('cfgForbiddenDelay').value),
    guess_extensions: $('cfgExtensions').value.split(',').map(s => s.trim()).filter(Boolean),
  };
  try {
    config = await PUT('/api/config', payload);
    $('configModal').style.display = 'none';
    showToast('配置已保存', 'success', 2500);
    treeData = {};
    scanCache = {};
    await loadTreeRoot();
    await refreshGlobalStats();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}
```
> 注意：`exclude_dirs` 原由「路径配置」tab 提供，现该 tab 已删。本次不在 UI 暴露 `exclude_dirs`（沿用后端默认值，PUT 不传该字段即保持不变）。

- [ ] **Step 5: 改按钮事件绑定**

In `init()`，把
```javascript
  $('btnAddMapping').addEventListener('click', addMappingRule);
```
改为
```javascript
  $('btnAddLibrary').addEventListener('click', addLibraryRule);
```
（`switchConfigTab` 通用，无需改；它按 `data-tab` 拼 `tab<Cap>`，`libraries`→`tabLibraries`、`ffprobe`→`tabFfprobe` 均匹配。）

- [ ] **Step 6: 手工验证**

启动后端（见 Task 9 完成后），或本地直接核对：
Run: `python -c "s=open(r'nfo-injector/frontend/app.js',encoding='utf-8').read(); print(all(k in s for k in['renderLibrariesList','collectLibraries','btnAddLibrary',\"'library'\"]) and 'collectMappings' not in s)"`
Expected: `True`

- [ ] **Step 7: Commit（如使用 git）**

```bash
git add nfo-injector/frontend/app.js
git commit -m "feat(ui): render library tree nodes and library config CRUD"
```

---

## Task 9: docker-compose 与 .env.example

**Files:**
- Modify: `nfo-injector/docker-compose.yml`
- Modify: `nfo-injector/.env.example`

**Interfaces:**
- Produces: 容器挂载 `/apps:/apps`；环境透传 `${STRM_ROOT}/${MEDIA_ROOT}`（仅供首次建库）

- [ ] **Step 1: 改 docker-compose.yml**

把 `volumes:` 与 `environment:` 两段替换为：
```yaml
    volumes:
      # 一次性挂载 /apps：覆盖所有 STRM 目录与所有网盘媒体源（host 路径 == 容器路径）
      - /apps:/apps
      # 持久化配置和数据
      - ./data:/app/data
    environment:
      # 仅用于「首次无 config.json」时自动生成第一个库
      - STRM_ROOT=${STRM_ROOT}
      - MEDIA_ROOT=${MEDIA_ROOT}
      - TZ=Asia/Shanghai
```

- [ ] **Step 2: 改 .env.example**

替换为：
```
# NFO MediaInfo 注入管理器 — 环境变量配置
#
# 复制为 .env，在 nfo-injector/ 下运行： docker compose up -d
#
# 容器固定挂载宿主机 /apps -> /apps（host 路径 == 容器路径）。
# 请确保你所有的 STRM 目录与网盘媒体目录都在 /apps 之下。
#
# 下面两项仅用于「首次启动、尚无 data/config.json」时自动生成第一个库；
# 之后所有库的增删改都在 Web UI「配置 → 媒体库」里完成。

STRM_ROOT=/apps/moviepilot/115strm/Emby
MEDIA_ROOT=/apps/clouddrive2/CloudDrive/115open/Media
```

- [ ] **Step 3: 校验 compose 语法**

Run（在 `nfo-injector/`，需本机有 docker）: `docker compose config`
Expected: 输出解析后的配置，含 `/apps:/apps` 挂载；无报错。
（若本机无 docker，跳过，留待 Task 10 部署时验证。）

- [ ] **Step 4: Commit（如使用 git）**

```bash
git add nfo-injector/docker-compose.yml nfo-injector/.env.example
git commit -m "feat(deploy): mount /apps once; env only seeds first library"
```

---

## Task 10: 端到端手工验证

**Files:** 无（部署验证）

- [ ] **Step 1: 构建并启动**

Run（在 `nfo-injector/`）: `docker compose up -d --build`
Expected: 容器 `nfo-injector` 运行中。

- [ ] **Step 2: 验证首次自动建库**

打开 `http://<host>:18880`。
Expected: 左侧树顶层出现一个 📚「主库」节点（来自 .env 的 STRM_ROOT/MEDIA_ROOT）。

- [ ] **Step 3: 验证浏览与探测**

展开「主库」→ 进入任一含 .strm 的目录 → 选中一个 STRM → 点「仅探测」。
Expected: 右侧出现 FFprobe JSON（媒体存在时）或明确错误（不存在时）。

- [ ] **Step 4: 验证注入**

对一个 `空白/EMPTY` 文件点「注入」。
Expected: 任务面板出现任务、实时日志滚动、完成后该文件状态变 `健康/HEALTHY`，顶栏统计刷新。

- [ ] **Step 5: 验证新增第二个库**

「配置 → 媒体库 → + 添加库」，填名称 + 另一个网盘的 STRM/媒体目录（如 Gdrive）→ 保存。
Expected: 树根出现第二个 📚 库；可展开、浏览、注入；`data/config.json` 含两个库。

- [ ] **Step 6: 验证全局统计为求和**

点顶栏「刷新统计」。
Expected: 四档数字为所有启用库之和。

- [ ] **Step 7: 标记完成**

记录验证结果。若全部符合，本计划完成。

---

## 自检记录（写计划时已核对）

- **Spec 覆盖**：§4.1→Task9；§4.2→Task1；§4.3→Task2；§4.4→Task3；§4.5→Task5；§4.6→Task4；§4.7→Task6；§4.8→Task7+8；§5 边界（最长匹配/越权/无库/disabled）→Task3+5 测试；§6 测试→各任务；§7 文件清单→全覆盖。
- **占位符**：无 TBD/TODO；每个代码步骤含完整代码。
- **类型一致**：`split_lib_path`/`resolve_library`/`resolve_media_path`/`get_strm_files_in_path`/`browse_directory`/`scan_directory_recursive` 在定义任务与调用任务（main/task_manager）签名一致；`EntryType.LIBRARY` 字符串值 `"library"` 前后端一致（后端返回、前端判断）。
