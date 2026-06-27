"""non-faststart mp4 兜底探测测试：moov 在文件尾，头 50MB 无 moov 时走 moov-stitch 兜底。"""
import json
import struct
from unittest.mock import patch

import backend.mediainfo_runner as mr


def _box(btype: bytes, payload: bytes) -> bytes:
    """构造标准 mp4 box：[4字节size][4字节type][payload]，size 含头 8 字节。"""
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def _build_nonfaststart_mp4() -> bytes:
    """构造一个最小的 non-faststart mp4：ftyp + mdat(填充) + moov(含 mvhd)。

    moov 放在文件尾，模拟 115 网盘 non-faststart 文件结构。
    """
    # ftyp（28 字节，模拟真实文件如 FWAY-008 的 ftyp，非默认 32）
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp4")

    # mdat：用一段填充模拟媒体数据；尺寸需使 moov 落在 HEAD_BYTES 尾段外
    # （头段 0..HEAD_BYTES-1 不含 moov，尾段含 moov box 头）
    mdat_body = b"\xAA" * 400
    mdat = _box(b"mdat", mdat_body)

    # moov：mvhd + 一个 trak(Video)
    mvhd = _box(b"mvhd", b"\x00" * 100)
    tkhd = _box(b"tkhd", b"\x00" * 80)
    mdhd = _box(b"mdhd", b"\x00" * 24)
    hdlr_v = _box(b"hdlr", b"\x00" * 24 + b"vide")
    minf_v = _box(b"minf", b"\x00" * 40)
    stbl_v = _box(b"stbl", b"\x00" * 40)
    dinf = _box(b"dinf", b"\x00" * 20)
    stbl_v = _box(b"stbl", dinf + stbl_v)
    minf_v = _box(b"minf", stbl_v + _box(b"vmhd", b"\x00" * 12))
    mdia_v = _box(b"mdia", mdhd + hdlr_v + minf_v)
    trak_v = _box(b"trak", tkhd + mdia_v)
    moov = _box(b"moov", mvhd + trak_v)

    return ftyp + mdat + moov


class _FakeResp:
    def __init__(self, data):
        self._data = data
        self.headers = {"Content-Length": str(len(data)),
                        "Content-Type": "application/octet-stream"}
        if data is not None:
            self.headers["Content-Range"] = f"bytes 0-{len(data)-1}/{len(data)}"
        self.status_code = 206 if data is not None else 404

    def iter_content(self, chunk_size=1024):
        if self._data is None:
            return
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def close(self):
        pass


def _make_requests_mock(full_file: bytes):
    """返回 requests.get/requests.head 的 mock，按 Range 返回对应切片。"""
    import re

    def _parse_range(h):
        m = re.search(r"bytes=(\d+)-(\d+)", h.get("Range", "") or "")
        if not m:
            return None, None
        return int(m.group(1)), int(m.group(2))

    def fake_get(url, headers=None, timeout=None, stream=False, allow_redirects=True):
        headers = headers or {}
        a, b = _parse_range(headers)
        if a is None:
            return _FakeResp(full_file)
        return _FakeResp(full_file[a:b + 1])

    def fake_head(url, timeout=None, allow_redirects=True):
        r = _FakeResp(b"")
        r.headers = {"Content-Length": str(len(full_file)),
                     "Content-Type": "application/octet-stream"}
        r.status_code = 200
        return r

    return fake_get, fake_head


def test_probe_nonfaststart_moov_at_tail(monkeypatch):
    """moov 在文件尾：头 50MB 无 moov → 兜底定位完整 moov box → 解析出 track。"""
    full = _build_nonfaststart_mp4()
    # HEAD_BYTES 调小：头段(0..HEAD-1) 落在 ftyp+mdat 内不含 moov；
    # 尾段(cl-HEAD..cl) 覆盖文件尾的 moov box 头。
    monkeypatch.setattr(mr, "HEAD_BYTES", len(full) - full.index(b"moov") + 4)
    # 头段必须不含 moov（否则会在头段就成功，到不了兜底）
    assert b"moov" not in full[:mr.HEAD_BYTES], "测试构造错误：头段不应含 moov"

    fake_get, fake_head = _make_requests_mock(full)
    monkeypatch.setattr(mr.requests, "get", fake_get)
    monkeypatch.setattr(mr.requests, "head", fake_head)

    # mock _run_mediainfo：校验拼接文件 box 结构合法——ftyp 按其 declared
    # size 结束，紧接 moov box。生产 bug 是固定下 32 字节 ftyp，当真 ftyp≠32
    # 时多/少字节导致 moov 偏移错位（mediainfo truncation）。
    def fake_run(path):
        data = open(path, "rb").read()
        if len(data) < 8:
            return None
        ftyp_size = struct.unpack(">I", data[:4])[0]
        if data[4:8] != b"ftyp" or ftyp_size < 8 or ftyp_size > len(data):
            return None
        # ftyp 之后应紧跟 moov box（type 在 ftyp_size+4）
        moov_type_off = ftyp_size + 4
        if data[moov_type_off:moov_type_off + 4] != b"moov":
            return None
        return json.loads(json.dumps({
            "media": {"track": [
                {"@type": "General", "Duration": "100.0"},
                {"@type": "Video", "Format": "AVC", "Width": "1920",
                 "Height": "1080", "FrameRate": "29.970"},
            ]}}))

    monkeypatch.setattr(mr, "_run_mediainfo", fake_run)

    logs = []
    result = mr.probe("http://x/test.mp4", timeout=30, stop_event=None, log=logs.append)

    assert result.success is True, f"应成功，logs={logs}"
    assert result.data["streams"], f"应有 stream，logs={logs}"
    # 应走了 moov 兜底分支
    assert any("moov" in s for s in logs), f"应记录 moov 兜底，logs={logs}"
