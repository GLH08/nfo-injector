#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NFO 文件解析、状态判断（4档）、MediaInfo 注入
"""

import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


class NfoStatus(str, Enum):
    """NFO MediaInfo 状态四档分级"""
    HEALTHY = "HEALTHY"    # streamdetails 完整，有 video codec + width + height
    PARTIAL = "PARTIAL"    # streamdetails 存在且有子节点，但关键字段不完整
    EMPTY = "EMPTY"        # fileinfo 节点缺失，或存在但为空/自闭合（待注入状态）
    MISSING = "MISSING"    # NFO 物理文件不存在，或 XML 解析失败（无法注入）


STATUS_LABELS = {
    NfoStatus.HEALTHY: "健康",
    NfoStatus.PARTIAL: "不完整",
    NfoStatus.EMPTY: "空白",
    NfoStatus.MISSING: "缺失",
}

STATUS_COLORS = {
    NfoStatus.HEALTHY: "#22c55e",   # green
    NfoStatus.PARTIAL: "#f59e0b",   # amber
    NfoStatus.EMPTY: "#ef4444",     # red
    NfoStatus.MISSING: "#6b7280",   # gray
}


@dataclass
class VideoInfo:
    codec: Optional[str] = None
    micodec: Optional[str] = None
    bitrate: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    aspect: Optional[str] = None
    aspectratio: Optional[str] = None
    framerate: Optional[str] = None
    language: Optional[str] = None
    scantype: Optional[str] = None
    duration: Optional[int] = None
    duration_seconds: Optional[int] = None


@dataclass
class AudioInfo:
    codec: Optional[str] = None
    micodec: Optional[str] = None
    bitrate: Optional[str] = None
    language: Optional[str] = None
    channels: Optional[int] = None
    samplingrate: Optional[str] = None


@dataclass
class SubtitleInfo:
    codec: Optional[str] = None
    language: Optional[str] = None


@dataclass
class StreamDetails:
    video_streams: List[VideoInfo] = field(default_factory=list)
    audio_streams: List[AudioInfo] = field(default_factory=list)
    subtitle_streams: List[SubtitleInfo] = field(default_factory=list)


@dataclass
class NfoDetail:
    nfo_path: Optional[Path]
    status: NfoStatus
    status_label: str
    status_color: str
    stream_details: Optional[StreamDetails]
    raw_xml: Optional[str]          # 原始 XML 文本
    parse_error: Optional[str]      # 解析错误信息（如果有）
    missing_fields: List[str]       # PARTIAL 状态时缺少的字段列表


def _safe_int(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_stream_details(sd_node: ET.Element) -> StreamDetails:
    """解析 <streamdetails> 节点为结构化数据"""
    details = StreamDetails()
    
    for video in sd_node.findall("video"):
        vi = VideoInfo(
            codec=video.findtext("codec"),
            micodec=video.findtext("micodec"),
            bitrate=video.findtext("bitrate"),
            width=_safe_int(video.findtext("width")),
            height=_safe_int(video.findtext("height")),
            aspect=video.findtext("aspect"),
            aspectratio=video.findtext("aspectratio"),
            framerate=video.findtext("framerate"),
            language=video.findtext("language"),
            scantype=video.findtext("scantype"),
            duration=_safe_int(video.findtext("duration")),
            duration_seconds=_safe_int(video.findtext("durationinseconds")),
        )
        details.video_streams.append(vi)
    
    for audio in sd_node.findall("audio"):
        ai = AudioInfo(
            codec=audio.findtext("codec"),
            micodec=audio.findtext("micodec"),
            bitrate=audio.findtext("bitrate"),
            language=audio.findtext("language"),
            channels=_safe_int(audio.findtext("channels")),
            samplingrate=audio.findtext("samplingrate"),
        )
        details.audio_streams.append(ai)
    
    for sub in sd_node.findall("subtitle"):
        si = SubtitleInfo(
            codec=sub.findtext("codec"),
            language=sub.findtext("language"),
        )
        details.subtitle_streams.append(si)
    
    return details


def _check_video_completeness(vi: VideoInfo) -> List[str]:
    """检查视频流信息是否完整，返回缺少的字段列表"""
    missing = []
    if not vi.codec:
        missing.append("codec")
    if not vi.width:
        missing.append("width")
    if not vi.height:
        missing.append("height")
    if not vi.framerate:
        missing.append("framerate")
    if not vi.duration and not vi.duration_seconds:
        missing.append("duration")
    return missing


def analyze_nfo(nfo_path: Optional[Path]) -> NfoDetail:
    """
    分析 NFO 文件，返回状态和结构化信息
    
    状态判断逻辑：
    - nfo_path 为 None 或文件不存在 → MISSING
    - NFO 存在但解析出错 → MISSING（附带 parse_error）
    - NFO 中无 <fileinfo> 节点 → EMPTY（需要注入）
    - <fileinfo> 存在但无 <streamdetails>，或 <streamdetails> 为空 → EMPTY
    - <streamdetails> 有子节点但关键字段缺失 → PARTIAL
    - <streamdetails> 有完整视频流信息 → HEALTHY
    """
    def _make(status, stream_details=None, raw_xml=None, parse_error=None, missing=None):
        return NfoDetail(
            nfo_path=nfo_path,
            status=status,
            status_label=STATUS_LABELS[status],
            status_color=STATUS_COLORS[status],
            stream_details=stream_details,
            raw_xml=raw_xml,
            parse_error=parse_error,
            missing_fields=missing or [],
        )
    
    # 1. 文件不存在
    if nfo_path is None or not nfo_path.exists():
        return _make(NfoStatus.MISSING)
    
    # 2. 读取原始 XML
    try:
        raw_xml = nfo_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return _make(NfoStatus.MISSING, parse_error=f"无法读取文件: {e}")
    
    # 3. 解析 XML
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        return _make(NfoStatus.MISSING, raw_xml=raw_xml, parse_error=f"XML 解析错误: {e}")
    
    # 4. 查找 <fileinfo>
    fileinfo = root.find("fileinfo")
    if fileinfo is None:
        return _make(NfoStatus.EMPTY, raw_xml=raw_xml)
    
    # 5. 查找 <streamdetails>
    sd_node = fileinfo.find("streamdetails")
    if sd_node is None:
        return _make(NfoStatus.EMPTY, raw_xml=raw_xml)
    
    # 6. 判断 <streamdetails> 是否为空（自闭合 or 无子节点）
    children = list(sd_node)
    if len(children) == 0:
        return _make(NfoStatus.EMPTY, raw_xml=raw_xml)
    
    # 7. 解析 streamdetails 内容
    stream_details = _parse_stream_details(sd_node)
    
    # 8. 如果没有视频流 → PARTIAL
    if not stream_details.video_streams:
        return _make(NfoStatus.PARTIAL, stream_details=stream_details, raw_xml=raw_xml,
                     missing=["video stream"])
    
    # 9. 检查视频流关键字段
    missing_fields = _check_video_completeness(stream_details.video_streams[0])
    if missing_fields:
        return _make(NfoStatus.PARTIAL, stream_details=stream_details, raw_xml=raw_xml,
                     missing=missing_fields)
    
    return _make(NfoStatus.HEALTHY, stream_details=stream_details, raw_xml=raw_xml)


def find_nfo_for_strm(strm_path: Path) -> Optional[Path]:
    """
    在 STRM 所在目录寻找对应的 NFO 文件
    匹配逻辑（按优先级）：
    1. 同名 .nfo（最常见：ACHJ-015.strm → ACHJ-015.nfo）
    2. movie.nfo（CN/收集 库）
    3. 目录下任意 .nfo（排除 tvshow.nfo / season.nfo）
    """
    # 1. 精确同名匹配
    exact_nfo = strm_path.with_suffix(".nfo")
    if exact_nfo.exists():
        return exact_nfo
    
    # 2. movie.nfo
    movie_nfo = strm_path.parent / "movie.nfo"
    if movie_nfo.exists():
        return movie_nfo
    
    # 3. 目录扫描兜底
    try:
        excluded = {"tvshow.nfo", "season.nfo"}
        for f in strm_path.parent.iterdir():
            if f.suffix.lower() == ".nfo" and f.name.lower() not in excluded:
                return f
    except PermissionError:
        pass
    
    return None


def inject_mediainfo(nfo_path: Path, probe_data: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """
    将 FFprobe 数据注入到 NFO 文件
    
    Returns:
        dict with keys: success (bool), message (str), status_before, status_after
    """
    # 先分析当前状态
    detail_before = analyze_nfo(nfo_path)
    status_before = detail_before.status
    
    # 非强制模式：已是 HEALTHY 则跳过
    if not force and status_before == NfoStatus.HEALTHY:
        return {
            "success": False,
            "skipped": True,
            "message": f"NFO 已是健康状态，跳过（使用强制覆盖可重新注入）",
            "status_before": status_before,
            "status_after": status_before,
        }
    
    # 如果 NFO 完全不存在，无法注入
    if not nfo_path.exists():
        return {
            "success": False,
            "skipped": False,
            "message": "NFO 文件不存在，无法注入",
            "status_before": status_before,
            "status_after": status_before,
        }
    
    try:
        # 解析现有 NFO
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        
        # 定位或创建 <fileinfo>
        fileinfo = root.find("fileinfo")
        if fileinfo is None:
            fileinfo = ET.SubElement(root, "fileinfo")
        
        # 移除所有旧的 <streamdetails>
        for sd in fileinfo.findall("streamdetails"):
            fileinfo.remove(sd)
        
        streamdetails = ET.SubElement(fileinfo, "streamdetails")
        
        has_video = False
        streams = probe_data.get("streams", [])
        
        for stream in streams:
            codec_type = stream.get("codec_type")
            
            if codec_type == "video":
                v = ET.SubElement(streamdetails, "video")
                _set_text(v, "codec", stream.get("codec_name"))
                _set_text(v, "micodec", stream.get("codec_name"))
                _set_text(v, "bitrate", stream.get("bit_rate"))
                _set_text(v, "width", str(stream["width"]) if "width" in stream else None)
                _set_text(v, "height", str(stream["height"]) if "height" in stream else None)
                _set_text(v, "aspect", stream.get("display_aspect_ratio"))
                _set_text(v, "aspectratio", stream.get("display_aspect_ratio"))
                _set_text(v, "framerate", stream.get("r_frame_rate"))
                _set_text(v, "language", stream.get("tags", {}).get("language", "und"))
                ET.SubElement(v, "scantype").text = "progressive"
                ET.SubElement(v, "default").text = "True"
                ET.SubElement(v, "forced").text = "False"
                
                # 时长：优先用 stream.duration，其次 format.duration
                duration_raw = stream.get("duration") or probe_data.get("format", {}).get("duration")
                if duration_raw:
                    try:
                        dur_sec = int(float(duration_raw))
                        ET.SubElement(v, "duration").text = str(dur_sec)
                        ET.SubElement(v, "durationinseconds").text = str(dur_sec)
                    except (ValueError, TypeError):
                        pass
                
                has_video = True
            
            elif codec_type == "audio":
                a = ET.SubElement(streamdetails, "audio")
                _set_text(a, "codec", stream.get("codec_name"))
                _set_text(a, "micodec", stream.get("codec_name"))
                _set_text(a, "bitrate", stream.get("bit_rate"))
                _set_text(a, "language", stream.get("tags", {}).get("language", "und"))
                _set_text(a, "channels", str(stream["channels"]) if "channels" in stream else None)
                _set_text(a, "samplingrate", stream.get("sample_rate"))
                ET.SubElement(a, "default").text = "True"
                ET.SubElement(a, "forced").text = "False"
            
            elif codec_type == "subtitle":
                s = ET.SubElement(streamdetails, "subtitle")
                _set_text(s, "codec", stream.get("codec_name"))
                _set_text(s, "language", stream.get("tags", {}).get("language", "und"))
        
        if not has_video:
            return {
                "success": False,
                "skipped": False,
                "message": "FFprobe 数据中未找到视频流，未写入 NFO",
                "status_before": status_before,
                "status_after": status_before,
            }
        
        # 保存 XML（UTF-8 + XML 声明）
        _indent_xml(root)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        
        # 验证写入后状态
        detail_after = analyze_nfo(nfo_path)
        
        return {
            "success": True,
            "skipped": False,
            "message": f"注入成功（{status_before} → {detail_after.status}）",
            "status_before": status_before,
            "status_after": detail_after.status,
        }
    
    except ET.ParseError as e:
        return {
            "success": False,
            "skipped": False,
            "message": f"NFO XML 解析失败: {e}",
            "status_before": status_before,
            "status_after": status_before,
        }
    except Exception as e:
        return {
            "success": False,
            "skipped": False,
            "message": f"注入过程异常: {e}",
            "status_before": status_before,
            "status_after": status_before,
        }


def inject_mock_mediainfo_to_nfo(nfo_path: Path, force: bool = False) -> Dict[str, Any]:
    """
    向 NFO 写入虚拟的 MediaInfo 数据（解决死机文件）
    """
    detail = analyze_nfo(nfo_path)
    status_before = detail.status
    
    if not force and status_before == NfoStatus.HEALTHY:
        return {
            "success": True,
            "skipped": True,
            "message": "跳过：已是 HEALTHY 状态（虚拟注入）",
            "status_before": status_before,
            "status_after": status_before,
        }
    
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        fileinfo = root.find("fileinfo")
        if fileinfo is None:
            fileinfo = ET.SubElement(root, "fileinfo")
            
        sd_node = fileinfo.find("streamdetails")
        if sd_node is not None:
            fileinfo.remove(sd_node)
            
        streamdetails = ET.SubElement(fileinfo, "streamdetails")
        
        # 写入虚拟的视频信息（1080p, H264）
        v = ET.SubElement(streamdetails, "video")
        _set_text(v, "codec", "h264")
        _set_text(v, "micodec", "h264")
        _set_text(v, "width", "1920")
        _set_text(v, "height", "1080")
        _set_text(v, "aspect", "16:9")
        _set_text(v, "aspectratio", "16:9")
        _set_text(v, "framerate", "24")
        _set_text(v, "language", "und")
        ET.SubElement(v, "scantype").text = "progressive"
        ET.SubElement(v, "default").text = "True"
        ET.SubElement(v, "forced").text = "False"
        
        # 写入虚拟的音频信息（AAC, 2 Channels）
        a = ET.SubElement(streamdetails, "audio")
        _set_text(a, "codec", "aac")
        _set_text(a, "micodec", "aac")
        _set_text(a, "language", "eng")
        _set_text(a, "channels", "2")
        ET.SubElement(a, "default").text = "True"
        ET.SubElement(a, "forced").text = "False"
        
        _indent_xml(root)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        
        detail_after = analyze_nfo(nfo_path)
        return {
            "success": True,
            "skipped": False,
            "message": f"成功注入虚拟数据（{status_before} → {detail_after.status}）",
            "status_before": status_before,
            "status_after": detail_after.status,
        }
    except Exception as e:
        return {
            "success": False,
            "skipped": False,
            "message": f"写入虚拟数据失败: {e}",
            "status_before": status_before,
            "status_after": status_before,
        }


def _set_text(parent: ET.Element, tag: str, value: Optional[str]):
    """仅在 value 非空时添加子节点"""
    if value is not None:
        ET.SubElement(parent, tag).text = value


def _indent_xml(elem: ET.Element, level: int = 0):
    """为 XML 添加缩进格式（Python 3.8 兼容版）"""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if not level:
        elem.tail = "\n"
