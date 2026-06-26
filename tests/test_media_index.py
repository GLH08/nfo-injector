from pathlib import Path
from backend import media_index as mi_mod
from backend.media_index import media_index


def _make(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")  # 媒体
    (root / "Movie" / "B").mkdir(parents=True)
    (root / "Movie" / "B" / "B.strm").write_text("http://y", encoding="utf-8")  # 无媒体
    return root


def _setup_index_file(tmp_path, monkeypatch):
    f = tmp_path / "media_index.json"
    monkeypatch.setattr(mi_mod, "_INDEX_FILE", f)
    media_index._data = None
    media_index.load()


def test_refresh_indexes_filenames(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    r = media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"])
    assert r["scanned"] == 2
    assert r["indexed"] == 1
    assert r["missing"] == 1
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"
    assert media_index.get("lib1", "Movie/B/B.strm") is None


def test_refresh_subdir_only(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"], subdir_relative="Movie/A")
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"


def test_persistence_save_load(tmp_path, monkeypatch):
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    media_index.refresh_index("lib1", root, ["trailers"], [".mp4", ".mkv"])
    # 模拟重启
    media_index._data = None
    media_index.load()
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"
    assert (tmp_path / "media_index.json").exists()
