import time as _time
from pathlib import Path
from backend import file_browser as fb
from backend.file_browser import scan_and_cache, clear_scan_cache, counts_from_cache, entries_from_cache, update_file_cache_entry
from backend.nfo_handler import NfoStatus
import backend.nfo_handler as nfoh


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
          <video><codec>h264</codec><width>1920</width><height>1080</height><framerate>24</framerate><duration>3600</duration></video>
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
