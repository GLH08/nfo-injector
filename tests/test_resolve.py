from pathlib import Path
import pytest
from backend.config import (
    AppConfig, Library, get_library, resolve_library, resolve_media_path, split_lib_path,
)


def _cfg():
    return AppConfig(libraries=[
        Library(id="jp", name="JP", strm_path="/apps/strm/Emby",
                media_path="/apps/cd2/115/Media"),
        Library(id="cn", name="CN", strm_path="/apps/strm/Emby/中转/CN",
                media_path="/apps/cd2/115/Media/Meta/CN"),
        Library(id="gd", name="GD", strm_path="/apps/strm/gd",
                media_path="/apps/cd2/Gdrive/Media", enabled=False),
    ])


def test_get_library():
    cfg = _cfg()
    assert get_library(cfg, "cn").name == "CN"
    assert get_library(cfg, "nope") is None


def test_resolve_longest_match():
    p = Path("/apps/strm/Emby/中转/CN/X/X.strm")
    assert resolve_library(p, _cfg()).id == "cn"


def test_resolve_parent_lib():
    p = Path("/apps/strm/Emby/Meta/JP/A/A.strm")
    assert resolve_library(p, _cfg()).id == "jp"


def test_resolve_skips_disabled():
    p = Path("/apps/strm/gd/movie/m.strm")
    assert resolve_library(p, _cfg()) is None


def test_resolve_media_path():
    p = Path("/apps/strm/Emby/Meta/JP/A/A.strm")
    assert resolve_media_path(p, _cfg()) == Path("/apps/cd2/115/Media/Meta/JP/A/A")


def test_resolve_media_none_when_no_lib():
    assert resolve_media_path(Path("/other/x.strm"), _cfg()) is None


def test_split_ok():
    lib, abs_path = split_lib_path("jp/Meta/JP/A/A.strm", _cfg())
    assert lib.id == "jp"
    assert abs_path == Path("/apps/strm/Emby/Meta/JP/A/A.strm")


def test_split_root():
    lib, abs_path = split_lib_path("jp", _cfg())
    assert abs_path == Path("/apps/strm/Emby")


def test_split_unknown_lib():
    with pytest.raises(ValueError):
        split_lib_path("nope/x", _cfg())


def test_split_escape_rejected():
    with pytest.raises(ValueError):
        split_lib_path("jp/../../etc/passwd", _cfg())
