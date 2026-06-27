from pathlib import Path
from backend import media_index as mi_mod
from backend.media_index import media_index


def _make(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")  # 媒体（同目录）
    (root / "Movie" / "B").mkdir(parents=True)
    (root / "Movie" / "B" / "B.strm").write_text("http://y", encoding="utf-8")  # 无媒体
    return root


def _make_split(tmp_path):
    """STRM 与媒体分离在两个不同根目录（真实 CloudDrive2 布局）。"""
    strm_root = tmp_path / "strm" / "Emby"
    media_root = tmp_path / "media"
    (strm_root / "Movie" / "A").mkdir(parents=True)
    (media_root / "Movie" / "A").mkdir(parents=True)
    (strm_root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (media_root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")  # 媒体在另一根
    return strm_root, media_root


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


def test_refresh_with_split_strm_media_roots(tmp_path, monkeypatch):
    """STRM 与媒体分离：媒体在 lib_media_path 下，不在 STRM 同目录。"""
    _setup_index_file(tmp_path, monkeypatch)
    strm_root, media_root = _make_split(tmp_path)
    # 不传 lib_media_path（回退 STRM 同目录）→ 找不到媒体（STRM 目录无 mp4）
    r1 = media_index.refresh_index("lib1", strm_root, [], [".mp4", ".mkv"])
    assert r1["indexed"] == 0
    assert media_index.get("lib1", "Movie/A/A.strm") is None
    # 传 lib_media_path → 在媒体根对应子目录找到
    r2 = media_index.refresh_index("lib1", strm_root, [], [".mp4", ".mkv"],
                                   lib_media_path=str(media_root))
    assert r2["indexed"] == 1
    assert media_index.get("lib1", "Movie/A/A.strm") == "A.mp4"


def test_refresh_progress_callback(tmp_path, monkeypatch):
    """refresh_index 通过 on_progress 回调上报增量进度（scanned/indexed/missing/total）。"""
    _setup_index_file(tmp_path, monkeypatch)
    root = _make(tmp_path)
    progress = []
    r = media_index.refresh_index(
        "lib1", root, ["trailers"], [".mp4", ".mkv"],
        on_progress=lambda p: progress.append(dict(p)),
    )
    assert r["scanned"] == 2
    # 至少上报了若干次，含 total 与 scanned 字段
    assert progress, "应上报进度"
    assert progress[0]["total"] == 2
    assert progress[-1]["scanned"] == 2

