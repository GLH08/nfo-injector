# 扫描缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 nfo-injector 增加服务端进程内扫描缓存：首次打开扫全库落缓存，10 分钟内任意层级文件夹徽章走缓存，注入任务复用缓存跳过重扫直接进入 ffprobe，手动刷新（全局 + 文件夹右键，复用现有入口）更新对应子树。

**Architecture:** 在 `backend/file_browser.py` 新增进程内两张表 `_FILE_CACHE`（库相对文件路径 → ScanEntry）与 `_SCANNED_SUBTREES`（库相对子树路径 → monotonic 时间戳），由 `threading.Lock` 保护。`scan_and_cache()` 递归扫描并填充缓存；`counts_from_cache()`/`entries_from_cache()` 按「祖先子树在 TTL 内」判定命中。`/api/scan` 先查缓存命中即返回，未命中才 `scan_and_cache`。`run_task` 注入时 `entries_from_cache` 命中则直接过滤、跳过 NFO 重读。注入成功后单文件重读翻新缓存。新增 `DELETE /api/scan-cache` 供全局刷新清空。

**Tech Stack:** Python 3.12 / FastAPI 0.115.5 / Pydantic v2 (2.10.3) / 原生 JS 前端 / pytest + httpx。

## Global Constraints

- 运行时不新增第三方依赖；测试依赖已在 `requirements-dev.txt`（pytest、httpx）。
- Pydantic v2 写法（`Field(...)`）。
- 缓存判定用 `time.monotonic()`，避免系统时钟跳变。
- 库相对路径作 key：`<lib_id>/<库内 posix 相对路径>`（与 `split_lib_path` 的 path 语义一致）。
- TTL 来自配置 `scan_cache_ttl`（秒），默认 600。
- 测试隔离：每个测试需清空 `file_browser` 的缓存模块级状态（`_FILE_CACHE` / `_SCANNED_SUBTREES`），避免相互污染。
- 运行测试用 `uv` 装的 Python 3.12（见 memory `python-314-test-workaround`），命令：`nfo-injector/.venv-test/Scripts/python.exe -m pytest nfo-injector/tests/ -v`（venv 需先建好）。
- 前端文件 `frontend/app.js`、`frontend/index.html`、`frontend/style.css` 用 2 空格缩进、单引号、`const $ = id => document.getElementById(id)` 约定。
- 每个 Task 结束 commit；commit message 末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。

## File Structure

- `backend/file_browser.py`（修改）：新增 `ScanEntry` dataclass、`_FILE_CACHE`/`_SCANNED_SUBTREES`/`_LOCK` 模块级状态、`_cache_key()`、`scan_and_cache()`、`counts_from_cache()`、`entries_from_cache()`、`clear_scan_cache()`、`refresh_subtree_cache()`、`update_file_cache_entry()`。保留现有 `browse_directory`/`scan_directory_recursive`/`get_strm_files_in_path` 不变（`scan_directory_recursive` 仅在缓存未命中回退路径继续使用，或被 `scan_and_cache` 内部复用）。
- `backend/config.py`（修改）：`AppConfig` 加 `scan_cache_ttl: float = Field(default=600, ge=0)`。
- `backend/main.py`（修改）：`ConfigUpdate` 加 `scan_cache_ttl`；`/api/scan` 改走缓存；新增 `DELETE /api/scan-cache`；`/api/scan-cache/refresh` 不新增（复用 `/api/scan?path=`，右键刷新已有入口）。
- `backend/task_manager.py`（修改）：`run_task` 收集待处理文件阶段改走 `entries_from_cache`，命中跳过重读；`_process_strm_file` 成功注入后调 `update_file_cache_entry` 翻新。
- `frontend/index.html`（修改）：FFprobe 标签页加「扫描缓存有效期(秒)」输入框 `cfgScanCacheTtl`。
- `frontend/app.js`（修改）：`openConfigModal`/`saveConfig` 读写 `cfgScanCacheTtl`；`refreshGlobalStats` 先调 `DELETE /api/scan-cache` 再 scan。
- `tests/test_scan_cache.py`（新建）：缓存核心逻辑测试。
- `tests/test_api.py`（修改）：`/api/scan` 缓存命中与失效测试；`/api/scan-cache` 清空测试。新增 `tests/test_task_inject_cache.py`：注入复用缓存测试。

---

### Task 1: 缓存数据结构与 scan_and_cache

**Files:**
- Modify: `backend/file_browser.py`（末尾追加新结构与函数；不动现有函数）
- Test: `tests/test_scan_cache.py`

**Interfaces:**
- Produces: `ScanEntry` dataclass（字段：`strm_path: Path`, `nfo_path: Optional[Path]`, `status: NfoStatus`, `detail: NfoDetail`）；`scan_and_cache(abs_dir: Path, lib_id: str, lib_strm_path: Path, exclude_dirs: Optional[List[str]] = None) -> StatusCount`；模块级 `_FILE_CACHE: Dict[str, ScanEntry]`、`_SCANNED_SUBTREES: Dict[str, float]`、`_LOCK: threading.Lock`、`clear_scan_cache()`。
- Consumes: `backend.nfo_handler.analyze_nfo`、`find_nfo_for_strm`；现有 `StatusCount`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_scan_cache.py`：

```python
from pathlib import Path
from backend import file_browser as fb
from backend.file_browser import scan_and_cache, counts_from_cache, clear_scan_cache
from backend.nfo_handler import NfoStatus


def _make_lib(tmp_path):
    root = tmp_path / "Emby"
    (root / "TV" / "Show1" / "Season01").mkdir(parents=True)
    (root / "TV" / "Show1" / "Season01" / "ep01.strm").write_text("http://x", encoding="utf-8")
    (root / "TV" / "Show1" / "Season01" / "ep01.nfo").write_text("<movie></movie>", encoding="utf-8")
    (root / "TV" / "Show2").mkdir(parents=True)
    (root / "TV" / "Show2" / "s02.strm").write_text("http://y", encoding="utf-8")  # 无 nfo → MISSING
    return root


def test_scan_and_cache_populates_and_counts(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    counts = scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    assert counts.total == 2
    assert counts.empty == 1     # ep01.nfo 无 fileinfo
    assert counts.missing == 1   # s02 无 nfo
    # 缓存已落盘
    assert "lib1/TV/Show1/Season01/ep01.strm" in fb._FILE_CACHE
    assert fb._FILE_CACHE["lib1/TV/Show1/Season01/ep01.strm"].status == NfoStatus.EMPTY
    # 子树标记
    assert "lib1/TV" in fb._SCANNED_SUBTREES


def test_scan_and_cache_root_uses_lib_id_key(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root, "lib1", root, ["trailers"])
    assert "lib1" in fb._SCANNED_SUBTREES


def test_scan_and_cache_removes_stale_entries(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    assert "lib1/TV/Show1/Season01/ep01.strm" in fb._FILE_CACHE
    # 删除文件后重扫该子树，旧条目应被清理
    (root / "TV" / "Show1" / "Season01" / "ep01.strm").unlink()
    (root / "TV" / "Show1" / "Season01" / "ep01.nfo").unlink()
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    assert "lib1/TV/Show1/Season01/ep01.strm" not in fb._FILE_CACHE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_scan_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'scan_and_cache'`

- [ ] **Step 3: 实现 ScanEntry 与 scan_and_cache**

在 `backend/file_browser.py` 顶部 import 区加 `import threading`、`import time`，并 import `NfoDetail`：

```python
import threading
import time
from backend.nfo_handler import NfoStatus, NfoDetail, analyze_nfo, find_nfo_for_strm
```
（替换原 `from backend.nfo_handler import NfoStatus, NfoDetail, analyze_nfo, find_nfo_for_strm` 那行——若已存在则只补 `threading`/`time`。）

在 `BrowseEntry` 之后、`browse_directory` 之前追加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_scan_cache.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/file_browser.py tests/test_scan_cache.py
git commit -m "feat(nfo-injector): 扫描缓存数据结构与 scan_and_cache"
```

---

### Task 2: 缓存查询 counts_from_cache / entries_from_cache

**Files:**
- Modify: `backend/file_browser.py`（追加两个查询函数）
- Test: `tests/test_scan_cache.py`（追加用例）

**Interfaces:**
- Produces: `counts_from_cache(subtree_key: str, ttl: float) -> Optional[StatusCount]`；`entries_from_cache(subtree_key: str, ttl: float) -> Optional[List[ScanEntry]]`。命中规则：存在一个 `_SCANNED_SUBTREES` 条目 `a`，满足 `a == subtree_key` 或 `subtree_key.startswith(a + "/")`，且 `monotonic - ts <= ttl`。返回值是该子树所有文件的聚合/列表；未命中返回 `None`。
- Consumes: Task 1 的 `_FILE_CACHE`/`_SCANNED_SUBTREES`/`_LOCK`/`scan_and_cache`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scan_cache.py`：

```python
import time as _time
from backend.file_browser import entries_from_cache


def test_counts_from_cache_hit_by_ancestor(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    # 子文件夹命中（祖先 lib1/TV 在 TTL 内）
    c = counts_from_cache("lib1/TV/Show1", ttl=600)
    assert c is not None
    assert c.total == 1  # Show1 下只有 ep01


def test_counts_from_cache_miss_when_no_ancestor_scanned(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV" / "Show1", "lib1", root, ["trailers"])
    # 只扫了 Show1，查 TV → 没有祖先覆盖 → None
    assert counts_from_cache("lib1/TV", ttl=600) is None


def test_counts_from_cache_expired(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    # 手动把时间戳改老
    fb._SCANNED_SUBTREES["lib1/TV"] = _time.monotonic() - 1000
    assert counts_from_cache("lib1/TV", ttl=600) is None


def test_entries_from_cache_returns_list(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    entries = entries_from_cache("lib1/TV", ttl=600)
    assert entries is not None
    assert len(entries) == 2
    paths = {e.strm_path.name for e in entries}
    assert paths == {"ep01.strm", "s02.strm"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_scan_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'entries_from_cache'`

- [ ] **Step 3: 实现查询函数**

在 `backend/file_browser.py` 的 `scan_and_cache` 之后追加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_scan_cache.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/file_browser.py tests/test_scan_cache.py
git commit -m "feat(nfo-injector): 缓存查询 counts_from_cache/entries_from_cache"
```

---

### Task 3: update_file_cache_entry（注入后翻新）

**Files:**
- Modify: `backend/file_browser.py`（追加 `update_file_cache_entry`）
- Test: `tests/test_scan_cache.py`（追加用例）

**Interfaces:**
- Produces: `update_file_cache_entry(lib_id: str, strm_abs: Path, lib_strm_path: Path) -> None`。重新 `analyze_nfo` 该单文件并替换 `_FILE_CACHE` 中对应条目；若 key 不存在则写入（幂等）。供 Task 6 注入成功后调用。
- Consumes: `analyze_nfo`、`find_nfo_for_strm`、`_cache_key`、`_LOCK`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scan_cache.py`：

```python
from backend.file_browser import update_file_cache_entry
import backend.nfo_handler as nfoh


def test_update_file_cache_entry_refreshes_status(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    scan_and_cache(root / "TV", "lib1", root, ["trailers"])
    key = "lib1/TV/Show1/Season01/ep01.strm"
    assert fb._FILE_CACHE[key].status == NfoStatus.EMPTY
    # 模拟注入：把 nfo 写成带完整 streamdetails 的健康 NFO
    healthy_nfo = """<movie>
      <fileinfo>
        <streamdetails>
          <video><codec>h264</codec><width>1920</width><height>1080</height></video>
        </streamdetails>
      </fileinfo>
    </movie>"""
    (root / "TV" / "Show1" / "Season01" / "ep01.nfo").write_text(healthy_nfo, encoding="utf-8")
    update_file_cache_entry("lib1", root / "TV" / "Show1" / "Season01" / "ep01.strm", root)
    assert fb._FILE_CACHE[key].status == NfoStatus.HEALTHY


def test_update_file_cache_entry_idempotent_when_key_absent(tmp_path):
    clear_scan_cache()
    root = _make_lib(tmp_path)
    # 未扫描，key 不存在 → 调用后写入，不报错
    update_file_cache_entry("lib1", root / "TV" / "Show1" / "Season01" / "ep01.strm", root)
    assert "lib1/TV/Show1/Season01/ep01.strm" in fb._FILE_CACHE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_scan_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'update_file_cache_entry'`

- [ ] **Step 3: 实现**

在 `backend/file_browser.py` 的 `entries_from_cache` 之后追加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_scan_cache.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/file_browser.py tests/test_scan_cache.py
git commit -m "feat(nfo-injector): update_file_cache_entry 注入后翻新缓存"
```

---

### Task 4: 配置项 scan_cache_ttl

**Files:**
- Modify: `backend/config.py`（`AppConfig` 加字段）
- Modify: `backend/main.py`（`ConfigUpdate` 加字段）
- Test: `tests/test_config_model.py`（追加用例）

**Interfaces:**
- Produces: `AppConfig.scan_cache_ttl: float = Field(default=600, ge=0)`；`ConfigUpdate.scan_cache_ttl: Optional[float] = None`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_config_model.py`：

```python
def test_scan_cache_ttl_default():
    c = AppConfig()
    assert c.scan_cache_ttl == 600
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_config_model.py::test_scan_cache_ttl_default -v`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'scan_cache_ttl'`

- [ ] **Step 3: 实现配置字段**

在 `backend/config.py` 的 `AppConfig` 中，`forbidden_retry_delay` 之后、`exclude_dirs` 之前加：

```python
    scan_cache_ttl: float = Field(default=600, ge=0, description="扫描缓存有效期（秒），0 表示不缓存")
```

在 `backend/main.py` 的 `ConfigUpdate` 中，`forbidden_retry_delay` 之后、`exclude_dirs` 之前加：

```python
    scan_cache_ttl: Optional[float] = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_config_model.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add backend/config.py backend/main.py tests/test_config_model.py
git commit -m "feat(nfo-injector): 配置项 scan_cache_ttl"
```

---

### Task 5: /api/scan 走缓存 + DELETE /api/scan-cache

**Files:**
- Modify: `backend/main.py`（重写 `/api/scan`，新增 `DELETE /api/scan-cache`）
- Test: `tests/test_api.py`（追加用例）

**Interfaces:**
- Produces: `/api/scan?path=` 先 `counts_from_cache`，命中返回，未命中 `scan_and_cache`；`DELETE /api/scan-cache` 调 `clear_scan_cache()`。
- Consumes: `file_browser.counts_from_cache`、`scan_and_cache`、`clear_scan_cache`、`_subtree_key`；`config.scan_cache_ttl`；`split_lib_path`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_api.py`（文件顶部 import 补 `from backend import file_browser as fb`）：

```python
from backend import file_browser as fb


def test_scan_uses_cache_on_second_call(tmp_path, monkeypatch):
    root = _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    calls = {"n": 0}
    orig = fb.scan_and_cache
    def spy(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(fb, "scan_and_cache", spy)
    client.get("/api/scan?path=lib1")
    client.get("/api/scan?path=lib1/Movie")  # 应命中缓存
    assert calls["n"] == 1  # 只全库扫一次


def test_scan_cache_clear_endpoint(tmp_path):
    _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    client.get("/api/scan?path=lib1")
    assert len(fb._FILE_CACHE) > 0
    r = client.delete("/api/scan-cache")
    assert r.status_code == 200
    assert len(fb._FILE_CACHE) == 0
    assert len(fb._SCANNED_SUBTREES) == 0


def test_scan_subfolder_after_global_scan_hits_cache(tmp_path):
    _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    client.get("/api/scan?path=")  # 全库扫
    # 任意子文件夹走缓存
    r = client.get("/api/scan?path=lib1/Movie")
    assert r.status_code == 200
    assert r.json()["total"] == 1
```

注意：现有 `test_scan_root_sums` 依赖首次 scan 返回正确计数——缓存未命中走 `scan_and_cache` 仍返回正确计数，应继续通过。为避免 `test_scan_root_sums` 被先前测试的缓存污染，在该测试开头加 `fb.clear_scan_cache()`。

修改 `tests/test_api.py` 中 `test_scan_root_sums`：

```python
def test_scan_root_sums(tmp_path):
    _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    r = client.get("/api/scan?path=")
    assert r.status_code == 200
    counts = r.json()
    assert counts["total"] == 1
    assert counts["empty"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_api.py -v`
Expected: FAIL — 新增用例失败（缓存逻辑未实现 / endpoint 不存在）

- [ ] **Step 3: 重写 /api/scan 并新增 DELETE /api/scan-cache**

在 `backend/main.py` 顶部 import 区，确保有：

```python
from backend.file_browser import (
    EntryType, BrowseEntry, browse_directory, scan_directory_recursive,
    scan_and_cache, counts_from_cache, clear_scan_cache, _subtree_key,
)
```
（保留原 `from backend.file_browser import ...` 已有的项，补上 `scan_and_cache`/`counts_from_cache`/`clear_scan_cache`/`_subtree_key`。）

用以下内容替换现有 `@app.get("/api/scan")` 整个函数：

```python
@app.get("/api/scan")
async def scan(path: str = ""):
    """递归统计路径下各状态数量（用于目录徽章）；path 为空时对所有启用库求和。
    命中扫描缓存则直接返回，未命中/过期才递归扫描并填充缓存。"""
    config = get_config()
    ttl = config.scan_cache_ttl
    loop = asyncio.get_event_loop()

    if not path:
        total = StatusCount()
        for lib in config.libraries:
            if not lib.enabled:
                continue
            cached = counts_from_cache(lib.id, ttl) if ttl > 0 else None
            if cached is not None:
                total.merge(cached)
            else:
                c = await loop.run_in_executor(
                    None, scan_and_cache,
                    Path(lib.strm_path), lib.id, Path(lib.strm_path), config.exclude_dirs,
                )
                total.merge(c)
        return {"path": "", **total.to_dict()}

    try:
        lib, abs_dir = split_lib_path(path, config)
    except ValueError as e:
        raise HTTPException(404, str(e))

    subtree_key = path  # path 已是 "<lib_id>" 或 "<lib_id>/..." 形式
    cached = counts_from_cache(subtree_key, ttl) if ttl > 0 else None
    if cached is not None:
        return {"path": path, **cached.to_dict()}
    counts = await loop.run_in_executor(
        None, scan_and_cache,
        abs_dir, lib.id, Path(lib.strm_path), config.exclude_dirs,
    )
    return {"path": path, **counts.to_dict()}


@app.delete("/api/scan-cache")
async def invalidate_scan_cache():
    """全局手动刷新：清空整个扫描缓存。"""
    clear_scan_cache()
    return {"message": "扫描缓存已清空"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_api.py tests/test_scan_cache.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add backend/main.py tests/test_api.py
git commit -m "feat(nfo-injector): /api/scan 走缓存 + DELETE /api/scan-cache"
```

---

### Task 6: 注入任务复用缓存

**Files:**
- Modify: `backend/task_manager.py`（`run_task` 收集阶段；`_process_strm_file` 注入成功后翻新）
- Test: `tests/test_task_inject_cache.py`（新建）

**Interfaces:**
- Produces: `run_task` 用 `entries_from_cache` 命中时跳过 `get_strm_files_in_path`+`_filter_strm_files`，直接从缓存构造 `pending`；`_process_strm_file` 注入成功后调 `update_file_cache_entry`。
- Consumes: `file_browser.entries_from_cache`、`update_file_cache_entry`、`_subtree_key`；`config.scan_cache_ttl`；`resolve_library`（已有）；`Path(lib.strm_path)`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_task_inject_cache.py`：

```python
import backend.config as config
from backend.config import AppConfig, Library
from backend import file_browser as fb
from backend.task_manager import task_manager, TaskStatus


def _setup(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    config._config_cache = AppConfig(
        libraries=[Library(id="lib1", name="主库", strm_path=str(root), media_path=str(tmp_path / "media"))],
        scan_cache_ttl=600,
    )
    fb.clear_scan_cache()
    return root


def test_run_task_uses_cache_no_rescan(tmp_path, monkeypatch):
    root = _setup(tmp_path)
    # 预先扫描落缓存
    fb.scan_and_cache(root, "lib1", root, [])
    # spy: 若调 get_strm_files_in_path 说明走了重扫
    calls = {"rescan": 0}
    orig = fb.get_strm_files_in_path
    def spy(*a, **kw):
        calls["rescan"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(fb, "get_strm_files_in_path", spy)

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,  # 虚拟注入，跳过 ffprobe
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    # 命中缓存 → 不应重扫
    assert calls["rescan"] == 0
    assert task.status == TaskStatus.COMPLETED


def test_run_task_falls_back_to_rescan_when_cache_miss(tmp_path, monkeypatch):
    root = _setup(tmp_path)
    # 不预扫 → 缓存未命中 → 应回退重扫
    calls = {"rescan": 0}
    orig = fb.get_strm_files_in_path
    def spy(*a, **kw):
        calls["rescan"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(fb, "get_strm_files_in_path", spy)

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    assert calls["rescan"] >= 1


def test_run_task_flips_cache_to_healthy_after_mock_inject(tmp_path):
    root = _setup(tmp_path)
    fb.scan_and_cache(root, "lib1", root, [])
    key = "lib1/Movie/A/A.strm"
    from backend.nfo_handler import NfoStatus
    assert fb._FILE_CACHE[key].status == NfoStatus.EMPTY

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    # 虚拟注入写入健康 NFO → 缓存应翻为 HEALTHY
    assert fb._FILE_CACHE[key].status == NfoStatus.HEALTHY
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_task_inject_cache.py -v`
Expected: FAIL — 当前 `run_task` 总是调 `get_strm_files_in_path`，`calls["rescan"]` 不为 0；翻新未实现。

- [ ] **Step 3: 改 run_task 收集阶段**

在 `backend/task_manager.py` 顶部 import 区，把：

```python
from backend.file_browser import get_strm_files_in_path
```

改为：

```python
from backend.file_browser import (
    get_strm_files_in_path, entries_from_cache, update_file_cache_entry,
)
```

在 `run_task` 中，定位到现有的「收集 STRM 文件」与「按状态过滤」两段（从 `recursive = task.scope == "recursive"` 到 `self._log(task, "info", f"待处理: {len(pending)} 个文件 ...")` 之前），用以下内容**替换**它们之间的「收集 + 过滤」逻辑。具体：保留 `self._log(... "▶ 任务开始 ...")` 之后，将原 `strm_files = await ...get_strm_files_in_path...` 直到 `pending = await ..._filter_strm_files...` 的整块替换为：

```python
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
                    get_strm_files_in_path,
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
```

紧接着（保留原有取消检查与 pending 空判断 + 「待处理」日志）。注意原代码中 `if task._cancel_async.is_set(): raise asyncio.CancelledError()` 与 `if not strm_files:` 在缓存命中分支不应执行（缓存命中无 strm_files 变量）。替换后需保证缓存命中分支的 pending 可能为空走原 `if not pending:` 分支。检查替换后下方仍有：

```python
            if task._cancel_async.is_set():
                raise asyncio.CancelledError()

            skipped_by_filter = len(strm_files) - len(pending) if 'strm_files' in dir() else 0
```

为避免 `strm_files` 未定义问题，把原 `skipped_by_filter = len(strm_files) - len(pending)` 改为：

```python
            skipped_by_filter = (len(strm_files) - len(pending)) if (cached_entries is None) else 0
```

（缓存命中分支不算 skipped_by_filter，因为已按 status 过滤；日志可保留原样，`skipped_by_filter=0` 时不打印或打印 0 都可——保持原条件 `if skipped_by_filter > 0` 即可。）

- [ ] **Step 4: _process_strm_file 注入成功后翻新缓存**

在 `_process_strm_file` 中，虚拟注入与正常注入两条「成功」分支后，追加翻新调用。找到虚拟注入分支的：

```python
            if inject_result["success"]:
                self._log(task, "success", f"   ✓ {inject_result['message']}")
                task.progress.success += 1
```

在 `task.progress.success += 1` 之后加：

```python
                self._refresh_cache_after_inject(config, strm_path)
```

同样在正常注入分支的：

```python
        if inject_result["success"]:
            self._log(task, "success", f"   ✓ {inject_result['message']}")
            task.progress.success += 1
```

之后加同样一行：

```python
        self._refresh_cache_after_inject(config, strm_path)
```

注意正常注入分支该行在 `if/elif/else` 之外、`self._emit_progress(task)` 之前。为只对 success 翻新，应放在 `if inject_result["success"]:` 块内：

```python
        if inject_result["success"]:
            self._log(task, "success", f"   ✓ {inject_result['message']}")
            task.progress.success += 1
            self._refresh_cache_after_inject(config, strm_path)
        elif inject_result.get("skipped"):
            ...
```

在 `TaskManager` 类中（`_filter_strm_files` 附近）新增静态方法：

```python
    @staticmethod
    def _refresh_cache_after_inject(config: AppConfig, strm_path: Path):
        """注入成功后翻新该文件缓存条目（lib_strm_path 用于算 key）。"""
        lib = resolve_library(strm_path, config)
        if lib:
            try:
                update_file_cache_entry(lib.id, strm_path, Path(lib.strm_path))
            except Exception:
                pass  # 翻新失败不影响任务
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_task_inject_cache.py -v`
Expected: 3 passed

- [ ] **Step 6: 跑全量后端测试**

Run: `pytest tests/ -v`
Expected: all passed（含原 test_task_inject.py）

- [ ] **Step 7: Commit**

```bash
git add backend/task_manager.py tests/test_task_inject_cache.py
git commit -m "feat(nfo-injector): 注入任务复用扫描缓存跳过重扫"
```

---

### Task 7: 前端配置项 + 全局刷新清缓存

**Files:**
- Modify: `frontend/index.html`（FFprobe 标签页加输入框）
- Modify: `frontend/app.js`（`openConfigModal`/`saveConfig` 读写 `cfgScanCacheTtl`；`refreshGlobalStats` 先 `DELETE /api/scan-cache`）

**Interfaces:**
- Produces: 输入框 `#cfgScanCacheTtl`；`saveConfig` payload 含 `scan_cache_ttl`；全局刷新清服务端缓存。

- [ ] **Step 1: index.html 加输入框**

在 `frontend/index.html` 的「盲猜扩展名」`<div class="form-group">` 之后（`tabFfprobe` 面板内、`</div>` 闭合前）加：

```html
          <div class="form-group">
            <label>扫描缓存有效期(秒)</label>
            <input type="number" id="cfgScanCacheTtl" min="0" max="86400" step="60" />
            <span class="form-hint">目录徽章与注入复用的扫描缓存有效期；0=不缓存。建议 600（10分钟）</span>
          </div>
```

- [ ] **Step 2: app.js 读写配置**

在 `openConfigModal` 中，`$('cfgExtensions').value = ...` 那行之后加：

```javascript
    $('cfgScanCacheTtl').value = config.scan_cache_ttl ?? 600;
```

在 `saveConfig` 的 `payload` 对象中，`guess_extensions` 之后加：

```javascript
    scan_cache_ttl: parseFloat($('cfgScanCacheTtl').value),
```

- [ ] **Step 3: 全局刷新清缓存**

把 `refreshGlobalStats` 改为：

```javascript
async function refreshGlobalStats() {
  try {
    // 先清服务端扫描缓存，强制全库重扫
    await DEL('/api/scan-cache');
    const counts = await GET('/api/scan?path=');
    $('statHealthy').textContent = `${counts.healthy} ✅`;
    $('statPartial').textContent = `${counts.partial} ⚠️`;
    $('statEmpty').textContent = `${counts.empty} 🔴`;
    $('statMissing').textContent = `${counts.missing} ⚫`;
    // 清除目录缓存
    scanCache = {};
  } catch (e) { /* 静默 */ }
}
```

确认 `DEL` 已在 app.js 定义（应已有，用于其他删除请求；若无则用 `fetch` DELETE）。检查方式：`grep -n "function DEL\|const DEL\|async function DEL" frontend/app.js`。

- [ ] **Step 4: 校验前端无引用错误**

Run: `grep -c cfgScanCacheTtl frontend/index.html frontend/app.js`
Expected: index.html ≥1，app.js ≥2（读写各一处，加 saveConfig 一处 = 3）

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/app.js
git commit -m "feat(nfo-injector): 前端扫描缓存有效期配置 + 全局刷新清缓存"
```

---

### Task 8: 端到端手工验证（部署后）

**Files:** 无代码改动

- [ ] **Step 1: 本地全量测试**

Run: `pytest tests/ -v`
Expected: all passed

- [ ] **Step 2: 提交并推送/上传到服务器**

```bash
git log --oneline -8
# 上传 nfo-injector/ 到服务器 /apps/nfo-injector/
```

- [ ] **Step 3: 服务器重建容器**

```bash
cd /apps/nfo-injector
docker compose up -d --build
docker compose logs -f
```

- [ ] **Step 4: 浏览器验证（强制刷新 Ctrl+F5）**

验证清单：
1. 打开页面 → 看日志应有一次全库 `scan_and_cache`（首次打开扫全盘）；顶栏统计与各库徽章出现数字。
2. 展开任意层级子文件夹 → 徽章立即显示数字（走缓存，日志无新 scan）。
3. 右键某文件夹「🔄 刷新目录统计徽章」→ 日志出现该子树 `scan_and_cache`，徽章更新。
4. 右键某文件夹「🔴 注入 [空白] 状态文件」→ 任务日志首行应为「复用扫描缓存（N 文件），跳过重扫」，随后立即进入 ffprobe（`── <文件> [空白]`），**无「扫描完成，共 N 个 STRM 文件」重扫日志**。
5. 配置弹窗 FFprobe 标签页 → 「扫描缓存有效期(秒)」显示 600，可改可保存。
6. 顶栏「刷新统计」→ 日志先 `DELETE /api/scan-cache` 200，再全库重扫。
7. 注入完成后再点同文件夹注入 → 已注入文件被「跳过（已健康）」（缓存已翻新）。

- [ ] **Step 5: 记录结果**

把日志关键行与现象反馈，确认「注入不再全量重扫」。

---

## 自检（Self-Review 结果）

- **Spec 覆盖**：缓存结构（Task 1）、TTL/祖先命中（Task 2）、注入复用（Task 6）、注入后翻新（Task 3+6）、全局刷新清缓存（Task 5+7）、文件夹右键刷新（复用现有 `ctxScan`/`ctxScanNfo`，Task 5 的 `/api/scan?path=` 已 `scan_and_cache` 自动刷新子树，无需新 endpoint）、配置项（Task 4+7）、测试（各 Task 内）。全覆盖。
- **Placeholder**：无 TBD/TODO；每步有实际代码。
- **类型一致**：`scan_and_cache`/`counts_from_cache`/`entries_from_cache`/`update_file_cache_entry`/`clear_scan_cache` 签名在各 Task 一致；`_subtree_key`/`_cache_key` 内部函数命名统一。
- **修正点**：原 spec 提「新增 `POST /api/scan-cache/refresh` 与右键新菜单项」——发现前端已有 `ctxScan`/`ctxScanNfo` 调 `/api/scan?path=<dir>`，缓存改造后自动刷新该子树，故不新增 endpoint/菜单项（YAGNI）。仅保留全局 `DELETE /api/scan-cache`。
