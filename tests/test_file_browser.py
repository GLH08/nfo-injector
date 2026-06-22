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
