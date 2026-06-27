#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mediainfo + HTTP Range 探测引擎：绕开 ffprobe 对 non-faststart mp4 的 FUSE 卡死。"""
import json
import logging
import re
import struct
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


def _locate_moov(tail: bytes, tail_start: int) -> Optional[tuple]:
    """在尾段里扫描 'moov' box type，返回 (moov_box_file_offset, moov_size)。

    moov box 起点含前 4 字节 size 字段，故 file_offset = tail_start + (match_pos - 4)。
    """
    for m in re.finditer(b"moov", tail):
        pos = m.start()
        if pos < 4:
            continue
        size = struct.unpack(">I", tail[pos - 4:pos])[0]
        if size >= 8:
            return (tail_start + pos - 4, size)
    return None


def _get_range(url: str, start: int, end: int, timeout: int) -> Optional[bytes]:
    """下载 [start, end] 字节到内存，返回 bytes 或 None。"""
    try:
        r = requests.get(url, headers={"Range": f"bytes={start}-{end}"},
                         timeout=timeout, stream=True, allow_redirects=True)
        if r.status_code not in (200, 206):
            return None
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                buf.extend(chunk)
        return bytes(buf)
    except Exception as e:
        logger.warning(f"range download fail {url} {start}-{end}: {e}")
        return None


def _get_ftyp(url: str, timeout: int) -> bytes:
    """按 declared size 下载完整 ftyp box。读不到/异常时返回空。"""
    head8 = _get_range(url, 0, 7, timeout)
    if not head8 or len(head8) < 8 or head8[4:8] != b"ftyp":
        return b""
    size = struct.unpack(">I", head8[:4])[0]
    if size < 8 or size > 1024 * 1024:  # ftyp 通常很小，超 1MB 视为异常
        return b""
    return _get_range(url, 0, size - 1, timeout) or b""


def _probe_moov_stitch(url: str, cl: int, timeout: int, log: Callable[[str], None]) -> Optional[dict]:
    """non-faststart mp4 兜底：定位文件尾的 moov box，下载完整 moov + 拼 ftyp 喂 mediainfo。

    返回有 track 的 mi dict 或 None。
    """
    if not cl or cl < HEAD_BYTES:
        return None
    tail_start = cl - HEAD_BYTES
    tail = _get_range(url, tail_start, cl - 1, timeout)
    if not tail:
        return None
    located = _locate_moov(tail, tail_start)
    if not located:
        return None
    moov_off, moov_size = located
    log(f"   头50MB无moov，定位moov box@{moov_off} size={moov_size}，精确下载…")
    moov_end = min(moov_off + moov_size - 1, cl - 1)
    moov_bytes = _get_range(url, moov_off, moov_end, timeout)
    if not moov_bytes:
        return None
    # 拼一个真 ftyp 头（按其 declared size 下载完整 box，不能固定字节数——
    # ftyp 长度因文件而异，多/少字节会让 moov 偏移错位 → mediainfo truncation）
    ftyp_bytes = _get_ftyp(url, timeout)
    stitched = Path(tempfile.mktemp(suffix=".mp4"))
    stitched = Path(tempfile.mktemp(suffix=".mp4"))
    try:
        stitched.write_bytes(ftyp_bytes + moov_bytes)
        mi = _run_mediainfo(stitched)
        if mi and _has_tracks(mi):
            log("   ✓ mediainfo 解析成功(moov-stitch)")
            return mi
        return None
    finally:
        try:
            stitched.unlink(missing_ok=True)
        except Exception:
            pass


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
        # 2. 尾 50MB 兜底（仍可能缺失 moov box 前 4 字节 size，故失败再走 moov-stitch）
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
            # 3. moov-stitch 兜底：non-faststart mp4，moov 在文件尾且整框落在尾段内
            mi = _probe_moov_stitch(url, cl, timeout, log)
            if mi:
                data = _mi_to_ffprobe_dict(json.dumps(mi))
                return ProbeResult(success=True, data=data, tried_path=url, tried_extension=None,
                                   error=None, error_type=None, raw_stderr=None)
        return ProbeResult(success=False, data=None, tried_path=url, tried_extension=None,
                           error="头尾50MB均无有效track", error_type="error", raw_stderr=None)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
