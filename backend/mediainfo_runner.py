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
