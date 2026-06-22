from fastapi.testclient import TestClient
import backend.config as config
from backend.config import AppConfig, Library
from backend.main import app
from backend import file_browser as fb


def _setup(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    config._config_cache = AppConfig(libraries=[
        Library(id="lib1", name="主库", strm_path=str(root), media_path=str(tmp_path / "media")),
    ])
    return root


def test_browse_root_lists_libraries(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["entry_type"] == "library"
    assert entries[0]["relative_path"] == "lib1"
    assert entries[0]["name"] == "主库"


def test_browse_within_library(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=lib1/Movie/A")
    names = [e["name"] for e in r.json()["entries"]]
    assert "A.strm" in names


def test_scan_root_sums(tmp_path):
    _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    r = client.get("/api/scan?path=")
    assert r.status_code == 200
    counts = r.json()
    assert counts["total"] == 1
    assert counts["empty"] == 1


def test_browse_unknown_lib_404(tmp_path):
    _setup(tmp_path)
    client = TestClient(app)
    r = client.get("/api/browse?path=nope/x")
    assert r.status_code == 404


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


def test_scan_force_bypasses_cache(tmp_path):
    """force=true 必须绕过缓存重扫，反映磁盘当前状态而非过期缓存。"""
    root = _setup(tmp_path)
    fb.clear_scan_cache()
    client = TestClient(app)
    # 首次扫描落缓存：empty=1
    r1 = client.get("/api/scan?path=lib1")
    assert r1.json()["empty"] == 1
    # 修改磁盘：删除 NFO → 状态由 EMPTY 变 MISSING
    (root / "Movie" / "A" / "A.nfo").unlink()
    # 不带 force：命中过期缓存，仍返回 empty=1
    r2 = client.get("/api/scan?path=lib1")
    assert r2.json()["empty"] == 1
    # 带 force=true：绕过缓存重扫，反映磁盘新状态 missing=1
    r3 = client.get("/api/scan?path=lib1&force=true")
    counts = r3.json()
    assert counts["missing"] == 1
    assert counts["empty"] == 0
