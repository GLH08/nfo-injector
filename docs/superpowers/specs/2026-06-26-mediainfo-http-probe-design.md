# mediainfo HTTP 探测引擎 设计

> 日期：2026-06-26
> 状态：设计稿

## 1. 背景与问题

nfo-injector 注入任务用 ffprobe 探测网盘媒体（CloudDrive2 FUSE 挂载的 115 文件）。实测：non-faststart mp4（moov 在文件尾）时，ffprobe 要 seek 整个大文件（2-5GB），卡在 FUSE page cache 阻塞（D 状态 `folio_wait_bit_common`），75s timeout 超时失败。9000 文件大量 non-faststart → 注入几乎全失败。

Hermes 实测发现：**mediainfo 对 partial mp4 容错极好**——只下载文件头 50MB（HTTP Range），mediainfo 就能从 moov box 解析出 codec/分辨率/时长，8-19s 拿到结果。OpenList 的 HTTP `/d/` 路径支持 Range，绕开 FUSE。

## 2. 目标

新增 mediainfo + HTTP Range 探测引擎，与现有 ffprobe 并存（方案 A）。库配置决定走哪条路。媒体文件名通过手动刷新的磁盘缓存获取（扩展名方式 iii：本地 FUSE 列举，但只用于建索引，探测时不再现场列举）。

## 3. 架构

```
① media_index.py（新）    媒体文件名索引：STRM库相对路径 → 媒体文件名(.mp4/.mkv)
                          持久化 data/media_index.json，手动刷新（右键目录）批量更新
                          批量按库/目录遍历，本地 FUSE 列举同目录媒体文件

② openlist_resolver.py（新） STRM库相对路径 → OpenList URL
                             用①的文件名 + 配置 media_url_root + 路径映射规则

③ mediainfo_runner.py（新） OpenList URL → ffprobe风格 ProbeResult
                             下载50MB头 → mediainfo解析 → 转格式
                             返回 ProbeResult（与 ffprobe_runner.ProbeResult 同接口）

④ task_manager.py（改）     探测引擎选择：库配了 media_url_root → mediainfo_runner
                                                否则 → ffprobe（保留兼容）
```

数据流（注入时，库配了 media_url_root）：
```
strm_path → resolve_library 找库
  → media_index.get(strm_lib_relative) → "ABF-259.mp4"（磁盘缓存，零FUSE IO）
  → openlist_resolver.resolve(strm_lib_relative, "ABF-259.mp4", config) → URL
  → mediainfo_runner.probe(url, timeout, stop_event) → ProbeResult(data=ffprobe dict)
  → inject_mediainfo(nfo_path, probe_result.data, force)  ← 复用现有注入
```

## 4. 配置（AppConfig 新增字段）

```python
class Library(BaseModel):
    ...  # 现有 id/name/strm_path/media_path/enabled
    media_url_root: str = ""   # OpenList HTTP 根，如 https://openlist.novaw.de/d/115/Media
                               # 空则走 ffprobe（fallback）
```

路径映射规则：`url = media_url_root + "/" + strm_lib_relative(去.strm后缀) + 扩展名`
其中 `strm_lib_relative` = STRM 相对库 strm_path 的路径（POSIX，去 `.strm` 后缀），扩展名来自 media_index。

例：`media_url_root=https://openlist.novaw.de/d/115/Media`，STRM 相对 `Meta/JP/NO-ZH/ABF-259/ABF-259`，media_index 给 `.mp4` →
`https://openlist.novaw.de/d/115/Media/Meta/JP/NO-ZH/ABF-259/ABF-259.mp4`

## 5. 媒体文件名索引（media_index.py）

### 结构
```python
# data/media_index.json —— 持久化，重启不丢，无 TTL（手动刷新才更新）
{
  "<lib_id>": {
    "Meta/JP/NO-ZH/ABF-259/ABF-259.strm": "ABF-259.mp4",
    ...
  }
}
```
key = STRM 库相对路径（含 `.strm`，POSIX），value = 同目录匹配到的媒体文件名（含扩展名）。

### 匹配规则（复用 ffprobe_runner.run_ffprobe_sync 的逻辑）
对每个 STRM，在同目录找媒体文件：
1. 按配置 guess_extensions 逐个试 `<stem>.<ext>` 是否存在
2. 否则列目录，单文件直接用；多文件按名称归一化匹配（复用现有 `norm` 逻辑）
3. 都没有 → 该 STRM 不记入索引（探测时 not_found）

### 刷新接口
- `refresh_index(lib_id, lib_strm_path, exclude_dirs, subdir_relative="")`：遍历该库（或子目录）所有 STRM，重建该 key 下的索引条目。批量 `os.walk`，对每个 STRM 调用上述匹配。返回 `{scanned, indexed, missing}` 统计。
- `get(lib_id, strm_lib_relative) -> Optional[str]`：查文件名，未命中返回 None。
- `load()` / `save()`：读写 `data/media_index.json`，模块级单例 + 锁。

### API
- `POST /api/media-index/refresh?path=<lib_id 或 lib_id/subdir>`：手动刷新，后台遍历，返回任务 id 或同步结果（小目录同步，大目录后台）。右键目录触发。
- `GET /api/media-index?path=<lib_id>`：查看某库索引条目数（用于 UI 显示）。

### 前端
- 目录树右键菜单新增「📁 刷新媒体文件名索引」项（仅 directory/library）。
- 点击 → POST refresh → toast「已刷新 N 个媒体文件名」。

## 6. mediainfo_runner.py

```python
@dataclass
class ProbeResult:  # 与 ffprobe_runner.ProbeResult 同结构
    success: bool
    data: Optional[Dict]   # ffprobe 风格 dict
    tried_path: Optional[str]
    tried_extension: Optional[str]
    error: Optional[str]
    error_type: Optional[str]   # timeout/not_found/error/cancelled
    raw_stderr: Optional[str]

def probe(url: str, timeout: int, stop_event, log) -> ProbeResult:
    # 1. 用 requests 下载头 50MB（Range: bytes=0-52428799），stream，max-time=timeout
    #    失败（超时/网络）→ error_type=timeout 或 error
    # 2. 写临时文件
    # 3. subprocess: mediainfo --Output=JSON tmpfile
    #    失败/无 track → error_type=error
    # 4. 转换 mediainfo JSON → ffprobe 风格 dict（见 §7）
    # 5. 删临时文件
```

### 失败兜底（Hermes 经验）
- 50MB 头无 moov → 下载尾 50MB（Range: cl-50MB 到 cl-1）再试。
- 头尾都无 → error_type=error（标记人工处理）。
- 需要先 HEAD 拿 Content-Length 才能下尾。

## 7. mediainfo JSON → ffprobe dict 转换

mediainfo JSON:
```json
{"media":{"track":[{"@type":"General","Duration":"7262.030",...},
                   {"@type":"Video","Format":"AVC","Width":"1280","Height":"720","FrameRate":"59.940",...},
                   {"@type":"Audio","Format":"AAC","Channels":"2","SamplingRate":"48000","Language":"Japanese",...}]}}
```

转 ffprobe 风格（喂给现有 inject_mediainfo）:
```python
{
  "streams": [
    {"codec_type":"video","codec_name":"h264","width":1280,"height":720,
     "display_aspect_ratio":"1.778","r_frame_rate":"59.940",
     "duration":"7262.030","tags":{"language":"und"}},
    {"codec_type":"audio","codec_name":"aac","channels":2,"sample_rate":"48000",
     "tags":{"language":"jpn"}},
  ],
  "format":{"duration":"7262.030"}
}
```

codec 映射（Hermes 经验）：AVC→h264, HEVC→hevc, AAC→aac, AC-3→ac3, E-AC-3→eac3, DTS→dts, MP3→mp3。
language：mediainfo "Japanese"→"jpn"，"Chinese"→"chi"，"English"→"eng"，空→"und"。
保证 HEALTHY 5 字段（codec/width/height/framerate/duration）尽量齐。

## 8. task_manager.py 改动

`_process_strm_file` 探测段（line ~500-545）：
```python
lib = resolve_library(strm_path, config)
if lib and lib.media_url_root:
    # mediainfo 路径
    strm_rel = strm_path 相对 lib.strm_path 的 POSIX 路径
    media_name = media_index.get(lib.id, strm_rel)
    if not media_name:
        → not_found（提示先刷新媒体索引）
    url = openlist_resolver.resolve(lib.media_url_root, strm_rel, media_name)
    probe_result = await run_in_executor(mediainfo_runner.probe, url, timeout, stop_event, log)
else:
    # ffprobe 路径（现有，fallback）
    media_base = resolve_media_path(strm_path, config)
    probe_result = await run_in_executor(probe_with_retry, base_path=media_base, ...)
```
之后 `inject_mediainfo(nfo_path, probe_result.data, force)` 不变。

## 9. Dockerfile

`apt-get install -y --no-install-recommends ffmpeg` → 加 `mediainfo`：
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mediainfo && ...
```
requirements.txt 加 `requests`（用于 HTTP Range 下载；现有无 HTTP 库）。

## 10. 边界与不做

- ffprobe 保留，库未配 media_url_root 时走原路径（本地 FUSE 或其他场景）。
- media_index 不自动刷新，仅手动（右键目录）。
- mediainfo_runner 不处理本地文件路径，只处理 HTTP URL。
- 不改 scan-cache（NFO 状态缓存与媒体索引是两套独立缓存）。
- 不改前端配置模态框的库行结构（media_url_root 加到 Library 模型，前端库配置行加一个输入框）。

## 11. 测试

- `test_media_index.py`：刷新填索引、get 命中/未命中、持久化（save/load）、批量按子目录。
- `test_openlist_resolver.py`：路径拼接（含中文、嵌套）、media_url_root 为空返回 None。
- `test_mediainfo_runner.py`：mediainfo JSON → ffprobe dict 转换（codec/language 映射、HEALTHY 5 字段齐）；mock subprocess。
- `test_task_inject.py` 追加：库配 media_url_root 时走 mediainfo_runner（mock），未配走 ffprobe。
- `test_config_model.py` 追加：Library.media_url_root 默认空。

## 12. 不做（YAGNI）

- 不做 OpenList API 目录列举（用本地 FUSE 列举建索引，已够）。
- 不做多 media_url_root（每库一个）。
- 不做索引自动 TTL（手动刷新）。
- 不做 mediainfo 装好检测的优雅降级（Dockerfile 装了就有）。
