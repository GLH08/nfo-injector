from backend.file_browser import (
    browse_directory, scan_directory_recursive, get_strm_files_in_path, EntryType,
)


def _make_lib(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    (root / "Movie" / "B").mkdir(parents=True)
    (root / "Movie" / "B" / "B.strm").write_text("http://y", encoding="utf-8")
    return root


def test_browse_prefixes_lib_id(tmp_path):
    root = _make_lib(tmp_path)
    entries = browse_directory(root / "Movie" / "A", "lib1", root, ["trailers"])
    strm = [e for e in entries if e.entry_type == EntryType.STRM_FILE][0]
    assert strm.relative_path == "lib1/Movie/A/A.strm"
    assert strm.nfo_status == "EMPTY"


def test_browse_dir_entry(tmp_path):
    root = _make_lib(tmp_path)
    entries = browse_directory(root, "lib1", root, ["trailers"])
    d = [e for e in entries if e.entry_type == EntryType.DIRECTORY][0]
    assert d.name == "Movie"
    assert d.relative_path == "lib1/Movie"
    assert d.has_children is True


def test_scan_counts(tmp_path):
    root = _make_lib(tmp_path)
    counts = scan_directory_recursive(root, ["trailers"])
    assert counts.total == 2
    assert counts.empty == 1     # A.nfo 存在但无 fileinfo
    assert counts.missing == 1   # B 无 nfo


def test_get_strm_files(tmp_path):
    root = _make_lib(tmp_path)
    files = get_strm_files_in_path(root, ["trailers"], True)
    assert len(files) == 2
    assert all(f.suffix == ".strm" for f in files)


def test_browse_uses_scan_cache_skips_reread(tmp_path, monkeypatch):
    """子树已扫描落缓存后，browse 同目录不应再读 NFO（analyze_nfo 零调用）。"""
    from backend import file_browser as fb
    from backend.file_browser import scan_and_cache, clear_scan_cache
    import backend.nfo_handler as nfoh

    clear_scan_cache()
    root = _make_lib(tmp_path)
    # 先扫描落缓存
    scan_and_cache(root / "Movie", "lib1", root, ["trailers"])

    # 监控 analyze_nfo：browse 命中缓存时不应调用它
    calls = {"n": 0}
    orig = nfoh.analyze_nfo

    def spy(nfo_path):
        calls["n"] += 1
        return orig(nfo_path)

    monkeypatch.setattr(fb, "analyze_nfo", spy)
    monkeypatch.setattr(nfoh, "analyze_nfo", spy)

    entries = browse_directory(root / "Movie" / "A", "lib1", root, ["trailers"], ttl=600)
    strm = [e for e in entries if e.entry_type == EntryType.STRM_FILE][0]
    assert strm.nfo_status == "EMPTY"
    assert calls["n"] == 0, f"browse 不应再读 NFO，但 analyze_nfo 被调用了 {calls['n']} 次"


def test_scan_and_cache_counts_indexed(tmp_path, monkeypatch):
    """scan_and_cache 顺带数索引覆盖度：A 已索引、B 未索引。"""
    from backend.file_browser import scan_and_cache, clear_scan_cache
    from backend import media_index as mi

    clear_scan_cache()
    root = _make_lib(tmp_path)

    # mock media_index.get：只有 A.strm 命中
    def fake_get(lib_id, rel):
        return "A.mp4" if rel.endswith("A.strm") else None
    monkeypatch.setattr(mi.media_index, "get", fake_get)

    counts = scan_and_cache(root / "Movie", "lib1", root, ["trailers"])
    assert counts.total == 2
    assert counts.indexed == 1
    assert counts.unindexed == 1


def test_counts_from_cache_includes_indexed(tmp_path, monkeypatch):
    """缓存命中后聚合统计应含索引覆盖度。"""
    from backend.file_browser import scan_and_cache, clear_scan_cache, counts_from_cache
    from backend import media_index as mi

    clear_scan_cache()
    root = _make_lib(tmp_path)

    def fake_get(lib_id, rel):
        return "A.mp4" if rel.endswith("A.strm") else None
    monkeypatch.setattr(mi.media_index, "get", fake_get)

    # 先扫描落缓存
    scan_and_cache(root / "Movie", "lib1", root, ["trailers"])
    # 缓存聚合（不传 lib_id 时无索引统计）
    c_no_lib = counts_from_cache("lib1/Movie", 600)
    assert c_no_lib.indexed == 0 and c_no_lib.unindexed == 0
    # 传 lib_id 时含索引统计
    c_with_lib = counts_from_cache("lib1/Movie", 600, "lib1")
    assert c_with_lib.indexed == 1
    assert c_with_lib.unindexed == 1

