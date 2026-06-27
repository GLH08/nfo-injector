# 扫描缓存设计（消除注入前的冗余全量重扫）

> 日期：2026-06-22
> 状态：设计稿，待用户 review

## 1. 背景与问题

nfo-injector 的 STRM/NFO 目录在服务器本地（约 9000 个影视文件），真实媒体在网盘（115/Google Drive/OneDrive），经 CloudDrive2 FUSE 挂载。FFprobe 探测网盘媒体受严格限速，是固有瓶颈。

用户实际流程：展开目录树 → 看到文件夹徽章数字（红/绿状态计数）→ 右键注入。

当前问题：
- 徽章扫描（`scan_directory_recursive`）已递归遍历整棵子树并读每个 NFO，但只保留聚合计数，**丢弃每文件详情**。
- 注入任务（`run_task`）发起后，**重新 `os.walk` 整棵子树 + 重新读每个 NFO** 来过滤出待处理文件，才进入 ffprobe 阶段。
- 于是「扫过一遍→注入又扫一遍」的冗余发生在本地磁盘上，9000 文件量级带来可感的启动延迟。ffprobe 阶段本身（网盘限速）是固有成本，不在本设计范围内。

目标：扫一次，缓存每文件状态；注入直接消费缓存，跳过重扫，立即进入 ffprobe。

## 2. 目录层级与展示语义（用户确认）

以默认库 `/apps/moviepilot/115strm/Emby` 为例，层级为：

```
Emby（库根）
└─ TV
   ├─ 综艺
   │  ├─ 奔跑吧
   │  │  ├─ Season01
   │  │  │  ├─ ep01.strm / ep01.nfo
   │  │  │  └─ ...
   │  │  └─ Season02
   │  └─ 哈哈哈哈哈
   ├─ 国产剧
   └─ 日韩剧
```

展示语义（用户明确要求）：
- **文件夹徽章 = 该文件夹整棵子树的递归计数**（含所有子文件夹）。当前 `scan` 已是递归，语义不变。
- **首次打开页面 → 扫描全库并缓存**（现有 `init` 已调 `/api/scan?path=` 触发全库递归扫描，只需让它落缓存）。
- **10 分钟内点任意层级文件夹 → 直接从缓存出数据，不重扫。**
- **手动刷新某层级 → 重扫该层级及以下，替换缓存中该子树的条目。**

## 3. 缓存结构

新增模块级状态（`backend/file_browser.py`），进程内、非持久：

```python
@dataclass
class ScanEntry:
    strm_path: Path            # 容器内绝对路径
    nfo_path: Optional[Path]
    status: NfoStatus
    detail: NfoDetail          # 复用 analyze_nfo 结果，含 missing_fields 等

# key = 库相对文件路径，如 "lib1/TV/综艺/奔跑吧/Season01/ep01.strm"
_FILE_CACHE: Dict[str, ScanEntry] = {}

# key = 库相对子树路径（目录或库根），如 "lib1"、"lib1/TV"、"lib1/TV/综艺"
# value = 该子树上次被完整递归扫描的 monotonic 时间戳
_SCANNED_SUBTREES: Dict[str, float] = {}

_LOCK = threading.Lock()       # 保护上面两个 dict 的读写
```

内存估算：9000 × ScanEntry（两个 Path + 枚举 + 小对象）≈ 几 MB，可忽略。

## 4. 核心操作

### 4.1 扫描并填充缓存（scan-and-cache）

新增函数 `scan_and_cache(abs_dir, lib_id, lib_strm_path, exclude_dirs) -> StatusCount`：
- 递归 `os.walk`，对每个 `.strm`：
  - `find_nfo_for_strm` + `analyze_nfo`（与现有 `scan_directory_recursive` 完全相同的 I/O，但**保留** detail）。
  - 计算 `file_key = f"{lib_id}/{strm.relative_to(lib_strm_path).as_posix()}"`。
  - 写入 `_FILE_CACHE[file_key] = ScanEntry(...)`。
- 对该子树下**已不存在**的旧缓存条目：扫描后，删除 `_FILE_CACHE` 中所有以 `subtree_prefix/` 开头但本次未触及的 key（处理文件被删除的情况）。
- 记录 `_SCANNED_SUBTREES[subtree_key] = time.monotonic()`。
- 返回 `StatusCount`（与现状一致）。

`subtree_key` = `f"{lib_id}/{abs_dir.relative_to(lib_strm_path).as_posix()}"`（库根时为 `lib_id`）。

### 4.2 从缓存查询（cache-lookup）

新增函数 `counts_from_cache(subtree_key) -> Optional[StatusCount]`：
- 检查是否存在一个 `_SCANNED_SUBTREES` 条目，其 key 等于 `subtree_key` 或是它的祖先（即 `subtree_key.startswith(ancestor + "/")` 或 `ancestor == subtree_key`），且时间戳在 TTL 内。
- 若有 → 遍历 `_FILE_CACHE`，筛出所有以 `subtree_key + "/"` 开头（或 `subtree_key` 本身）的文件 → 聚合 `StatusCount` 返回。
- 若无 → 返回 `None`（缓存未命中/过期）。

新增函数 `entries_from_cache(subtree_key) -> Optional[List[ScanEntry]]`：同上判定，返回筛选出的文件列表（供注入用）。

### 4.3 /api/scan（徽章 / 全局统计）改造

```python
@app.get("/api/scan")
async def scan(path: str = ""):
    config = get_config()
    ttl = config.scan_cache_ttl
    if not path:
        # 全库：逐库 cache-lookup；任一库未命中/过期则 scan-and-cache 该库
        total = StatusCount()
        for lib in config.libraries:
            if not lib.enabled:
                continue
            lib_key = lib.id
            cached = counts_from_cache(lib_key, ttl)
            if cached is None:
                c = await run_in_executor(scan_and_cache, lib.strm_path, lib.id, ...)
            else:
                c = cached
            total.merge(c)
        return total.to_dict()
    # 单路径
    lib, abs_dir = split_lib_path(path, config)
    subtree_key = path  # 已是 "lib_id/..." 形式
    cached = counts_from_cache(subtree_key, ttl)
    if cached is not None:
        return cached.to_dict()
    c = await run_in_executor(scan_and_cache, abs_dir, lib.id, lib.strm_path, ...)
    return c.to_dict()
```

行为：
- 首次打开（`path=""`）→ 每个库 `counts_from_cache` 未命中 → `scan_and_cache` 全库 → 缓存落盘。**这就是「首次打开扫全盘」。**
- 10 分钟内点任意文件夹 → `counts_from_cache` 命中（祖先子树在 TTL 内）→ 立即返回，不扫。
- 过期后点某文件夹 → 该子树无有效覆盖 → `scan_and_cache` 该子树 → 返回。

### 4.4 /api/inject（注入任务）改造 `run_task`

替换现有「`get_strm_files_in_path` + `_filter_strm_files`」两步：

```python
# 收集待处理文件
cached = entries_from_cache(subtree_key, ttl)
if cached is None:
    # 缓存未命中/过期 → 退回扫描（与现状等价，保证正确性）
    strm_files = await run_in_executor(get_strm_files_in_path, abs_target, exclude, recursive)
    pending = await run_in_executor(_filter_strm_files, strm_files, filter_statuses, force)
    self._log(task, "info", "缓存未命中/过期，重新扫描 NFO")
else:
    # 命中 → 直接从缓存过滤，不读 NFO
    pending = [(e.strm_path, e.nfo_path, e.detail) for e in cached
               if (not filter_statuses or e.status in filter_statuses)]
    if not force:
        pending = [p for p in pending if p[2].status != NfoStatus.HEALTHY]
    self._log(task, "info", f"复用扫描缓存（{len(cached)} 文件），跳过重扫")
```

注意：`_process_strm_file` 已对每个文件做存在性/状态兜底（`nfo_path is None` 检查、HEALTHY 跳过），即使缓存条目略陈旧也安全。

### 4.5 手动刷新（两个入口）

**全局刷新**（现有 `btnRefreshRoot` / `btnScanAll` → `refreshGlobalStats`）：
- 现状前端 `refreshGlobalStats` 会清前端 `scanCache` 并调 `/api/scan?path=`。
- 后端改造：新增 `DELETE /api/scan-cache`（清空 `_FILE_CACHE` + `_SCANNED_SUBTREES`），前端 `refreshGlobalStats` 先调它再调 `/api/scan?path=` 重新全库扫描填充。

**文件夹右键刷新**：
- 右键菜单新增「🔄 刷新此目录状态」项（仅 directory 类型）。
- 点击 → `POST /api/scan-cache/refresh?path=<dir>` → 后端对该子树 `scan_and_cache`（重扫并替换该子树条目）→ 返回新计数 → 前端更新该文件夹徽章 + 清前端 `scanCache[dir]`。
- 语义：刷新某层级 = 更新缓存中该层级及以下。等价于强制该子树 `scan_and_cache`，`_SCANNED_SUBTREES[subtree_key]` 更新为 now。

### 4.6 注入写入后的缓存自愈

`_process_strm_file` 成功注入某文件后，将其在 `_FILE_CACHE` 中的条目翻新：调用 `analyze_nfo(nfo_path)` 重读该单文件 NFO（注入后 NFO 已在本地，单文件读取极快），用结果替换 `_FILE_CACHE[file_key].detail/status`。这样立即重注入同一文件会被 HEALTHY 跳过，不重复 ffprobe。

失败/取消的文件**不**改缓存状态（保持原状，下次仍会尝试）。

## 5. TTL 与配置

- 新增 `AppConfig.scan_cache_ttl: float = 600`（10 分钟，单位秒）。
- `ConfigUpdate` 加 `scan_cache_ttl: Optional[float] = None`。
- FFprobe 参数标签页加输入框「扫描缓存有效期(秒)」。
- 判定用 `time.monotonic()`，避免系统时钟跳变影响。

## 6. 并发与锁

- `_FILE_CACHE` / `_SCANNED_SUBTREES` 由 `_LOCK`（`threading.Lock`）保护读写。
- `scan_and_cache` 在 executor 线程中跑，持锁写入；`counts_from_cache`/`entries_from_cache` 持锁读取并**返回快照副本**（list/dict 拷贝），释放锁后再聚合，避免长时间持锁。
- 多个并发 scan 请求可能重复扫同一子树（无防重入锁），可接受：最坏情况是首次打开时多个徽章并发各扫一次，结果一致。若需优化，可加「子树级 in-flight 标记」去重，初版不做（YAGNI）。

## 7. 边界与正确性

- **Emby 重新刮削覆盖 NFO**：10 分钟 TTL 内，被覆盖的文件状态可能陈旧。用户可手动刷新该层级纠正。注入时 `_process_strm_file` 的兜底（HEALTHY 跳过、nfo 存在性检查）保证不会错误注入。这是用户已知并接受的权衡（用户明确：重复扫描必要，但应可手动触发）。
- **缓存条目文件被删除**：`scan_and_cache` 重扫某子树时清理该子树下不再存在的旧 key。TTL 内未重扫的子树可能残留少量已删文件条目，影响徽章计数略偏多；手动刷新或 TTL 过期后自愈。可接受。
- **force=True 注入**：仍用缓存文件列表（跳过重扫），但不过滤状态（force 覆盖所有）。符合 force 语义。
- **单文件注入 `/api/inject-file`**：不在本设计范围，保持现状（单文件直接 probe）。

## 8. 测试（`tests/test_scan_cache.py`）

1. `scan_and_cache` 填充 `_FILE_CACHE` + `_SCANNED_SUBTREES`；`counts_from_cache` 命中返回正确计数。
2. `counts_from_cache` 对子文件夹路径命中（祖先子树在 TTL 内）。
3. TTL 过期后 `counts_from_cache` 返回 None；`/api/scan` 触发重扫。
4. `/api/scan?path=` 全库扫描后，任意子文件夹徽章走缓存（mock `analyze_nfo` 计数验证不重复调用）。
5. `run_task` 注入：缓存命中时不调 `analyze_nfo`（mock 计数验证），直接进入 ffprobe 阶段；缓存未命中时退回重扫。
6. 注入成功后该文件缓存状态翻 HEALTHY；再次注入同文件被跳过。
7. `DELETE /api/scan-cache` 清空缓存；`POST /api/scan-cache/refresh?path=D` 重扫 D 子树并替换条目。
8. force=True 用缓存文件列表但不过滤状态。

## 9. 不做（YAGNI）

- 不持久化缓存到磁盘（进程重启重新扫，9000 文件本地扫描可接受）。
- 不做子树级 in-flight 去重（并发重复扫描可接受）。
- 不让浅层 `browse` 喂缓存（徽章 scan 已覆盖递归子树，browse 浅层无额外收益）。
- 不改 ffprobe 限速/并发策略（固有瓶颈，另行配置）。
