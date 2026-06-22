from pathlib import Path
from backend import file_browser as fb
from backend.file_browser import scan_and_cache, clear_scan_cache
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
