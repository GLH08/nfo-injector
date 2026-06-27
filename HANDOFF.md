# nfo-injector 项目接续说明

> 本文档为 Claude Code 新会话提供项目上下文。nfo-injector 原是 `D:\Files\Code\emby-ffprobe` 仓库的子目录，
> 已用 `git filter-branch --subdirectory-filter` 抽出为独立仓库（保留 24 个提交历史）。
> 移到 `D:\Files\Code\nfo-injector` 后，在 Claude Code 开新会话时，先读本文件 + `docs/superpowers/` 下的设计文档。

## 这是什么项目

`nfo-injector`：为 Emby STRM 媒体库精确注入 MediaInfo（FFprobe/mediainfo 探测结果）到 NFO 文件。
FastAPI(Python 3.12) + Pydantic v2 后端 + 原生 JS 前端，Docker 部署，端口 18880。
STRM/NFO 目录在服务器本地（约 9000 影视文件），真实媒体在网盘（115/Google Drive/OneDrive），
经 CloudDrive2 FUSE 挂载本地。

## 已完成的演进（按时间）

### 1. 多库自由映射重构（2026-06-21）
从「单一 STRM 根 + 单一媒体根」重构为「库列表」模型：每个库 `{id, name, strm_path, media_path, enabled, media_url_root}`，
可自由映射 STRM 目录 ↔ 媒体目录，新增库纯 Web UI 操作，无需改 docker-compose（容器一次性挂载 `/apps:/apps` + `privileged: true`）。
- 设计：`docs/superpowers/specs/2026-06-21-multi-library-mapping-design.md`
- 计划：`docs/superpowers/plans/2026-06-21-multi-library-mapping.md`

### 2. 扫描缓存（2026-06-22）
服务端进程内缓存每文件 NFO 状态，消除注入前的冗余全量重扫：
首次打开扫全库落缓存，10 分钟 TTL（`scan_cache_ttl` 可配）内任意层级文件夹徽章走缓存；
注入任务复用缓存跳过 NFO 重读直接进入探测；注入成功后单文件翻新缓存。
- 设计：`docs/superpowers/specs/2026-06-22-scan-cache-design.md`
- 计划：`docs/superpowers/plans/2026-06-22-scan-cache.md`

### 3. mediainfo HTTP 探测引擎（2026-06-26）
**核心突破**：ffprobe 读 FUSE 大文件（non-faststart mp4，moov 在文件尾）会卡在 D 状态（page cache 阻塞 `folio_wait_bit_common`），
75s timeout 全失败。改用 **mediainfo + OpenList HTTP Range 下载 50MB 头**：mediainfo 对 partial mp4 容错好，8-19s 拿结果。
- `media_index.py`：STRM→媒体文件名持久化索引（`data/media_index.json`，手动右键刷新），探测时零本地 FUSE I/O
- `openlist_resolver.py`：STRM 路径 + 文件名 → OpenList HTTP URL（中文 percent-encode）
- `mediainfo_runner.py`：下载 50MB 头（失败兜底下尾 50MB）+ `mediainfo --Output=JSON` + 转 ffprobe 风格 dict
- `task_manager.py`：按 `Library.media_url_root` 分流（有值→mediainfo HTTP；空→ffprobe 本地 FUSE fallback）
- Dockerfile 装 mediainfo，requirements 加 requests
- 设计：`docs/superpowers/specs/2026-06-26-mediainfo-http-probe-design.md`
- 计划：`docs/superpowers/plans/2026-06-26-mediainfo-http-probe.md`

## 当前状态（2026-06-27）

代码全部完成，**59 个测试通过**（`cd nfo-injector && .venv-test/Scripts/python.exe -m pytest tests/ -v`）。
已部署到服务器（oracle-20260330，容器 nfo-injector，:18880），已验证：
- FUSE 透传成功（`privileged: true`，容器内可见 `/apps/clouddrive2/CloudDrive/...`）
- `media_url_root` 已存入配置（`https://openlist.novaw.de/d/115/Media`）
- 注入任务**已走 mediainfo 分流**（日志报「媒体索引未找到…」而非 ffprobe 的「未找到对应媒体文件」）

## 待完成 / 待验证（按优先级）

### P0：媒体索引匹配 STRM/媒体分离根（已修，待部署验证）
**根因**：`media_index._match_media` 原在 STRM 同目录找媒体，但 STRM 和媒体分离在两个根
（`strm_path` 在 `/apps/moviepilot/115strm/Emby`，`media_path` 在 `/apps/clouddrive2/CloudDrive/115open/Media`），
STRM 目录无 .mp4 → 索引为空 → 注入报「媒体索引未找到该 STRM 的媒体文件名」。
**已修**（提交 `6b0a40c`）：`refresh_index` 新增 `lib_media_path` 参数，去 `media_root/<STRM相对目录>` 找媒体；
main.py 的 `/api/media-index/refresh` 传 `lib.media_path`。
**待做**：上传到服务器重建 → 右键目录刷新媒体索引 → 验证 `data/media_index.json` 有内容 → 注入应走 mediainfo HTTP 成功。

### P1：媒体索引刷新大目录会 502（未修，已知限制）
`POST /api/media-index/refresh` 同步阻塞，大目录（几千文件）卡几十秒、nginx 502。
当前刷小子目录（如 `Meta/JP`）可行，整库不行。
**修法**：把刷新改成后台任务（像注入任务那样，BackgroundTask + 进度 WebSocket）。

### P2：进页面不再每次全库重扫（已修）
`init` 不再 await `refreshGlobalStats`（fire-and-forget），`refreshGlobalStats(force)` 默认用缓存，
仅手动「刷新统计」/「全部扫描」按钮 force=true 清缓存重扫（提交 `9d36d04`）。

### P3：nginx 502（首次注入，未修）
浏览器首次 POST /api/inject 偶发 502（第二次成功）。根因 nginx 反代超时配置（默认 60s）+ uvicorn 单 worker。
直连 `/api/inject` 7.5ms 返回，端点健康。修法：nginx 加 `proxy_read_timeout 300s` 等，或 uvicorn `--workers 2`。
诊断报告：原 emby-ffprobe 仓库根的 `nfo-injector-502-diagnosis-20260625.md`（未随历史抽出）。

## 关键技术约定

- **库相对路径作 key**：`<lib_id>/<库内 posix 相对路径>`（与 `split_lib_path` 的 path 语义一致）。
- **探测引擎分流**：`lib.media_url_root` 非空 → mediainfo HTTP；空 → ffprobe（保留兼容）。
- **缓存两套独立**：scan_cache（NFO 状态，内存+10min TTL）vs media_index（媒体文件名，磁盘持久化，手动刷新）。
- **HEALTHY 5 字段**：codec/width/height/framerate/duration（见 `nfo_handler._check_video_completeness`）。
- **ProbeResult** 是 pydantic BaseModel（`ffprobe_runner.py` 定义），mediainfo_runner 复用同一类型。
- **mediainfo JSON → ffprobe dict 转换**：`mediainfo_runner._mi_to_ffprobe_dict`，codec 映射 AVC→h264 等。
- URL 已被 openlist_resolver percent-encode，mediainfo_runner 直接传 requests 不再编码（避免双重编码）。

## 测试环境（Python 3.14 无法构建 pydantic-core）

本机系统 Python 3.14，装 pydantic 会失败。用 uv 装 Python 3.12 跑测试：
```
uv python install 3.12
uv venv --python 3.12 nfo-injector/.venv-test
uv pip install --python nfo-injector/.venv-test/Scripts/python.exe -r nfo-injector/requirements.txt -r nfo-injector/requirements-dev.txt
nfo-injector/.venv-test/Scripts/python.exe -m pytest nfo-injector/tests/ -v
```
`.venv-test/` 已被 `.gitignore` 忽略。

## 部署

```bash
cd /apps/nfo-injector
docker compose up -d --build
# 验证
docker exec nfo-injector sh -c "which mediainfo && mediainfo --version | head -1"
curl -s http://localhost:18880/api/config | python3 -m json.tool | grep media_url_root
```
- 库配置行填 `media_url_root`（如 `https://openlist.novaw.de/d/115/Media`）
- 右键目录「📁 刷新媒体文件名索引」建 media_index
- 注入日志应见「尝试探测(HTTP): https://openlist...」

## Memory 位置（重要）

本项目历史 memory 在原 emby-ffprobe 项目路径下：
`C:\Users\Gong\.claude\projects\D--Files-Code-emby-ffprobe\memory\`

包含：
- `MEMORY.md` — 索引
- `nfo-injector-multi-library-refactor.md` — 多库重构状态与约定
- `python-314-test-workaround.md` — Python 3.14 测试 workaround

**移到 `D:\Files\Code\nfo-injector` 后**，Claude Code 会按新路径建 memory 目录：
`C:\Users\Gong\.claude\projects\D--Files-Code-nfo-injector\memory\`
建议手动把上述 3 个 memory 文件复制过去，新会话能自动加载项目上下文。

## 原仓库

原 `D:\Files\Code\emby-ffprobe` 仓库保留不动，含完整 27 提交历史（含 docs 纯文档提交）、
根目录独立脚本（`strm_mediainfo_universal_injector.py`、`sync_cn_metadata.py`、`parse_trees.py`）、
诊断报告（`nfo-injector-502-diagnosis-20260625.md`、`nfo-injector-ffprobe-diagnosis-20260625.md`）、
白皮书（`EMBY_STRM_PLAYBACK_RESOLUTION.md`）。
