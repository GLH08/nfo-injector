from backend.config import AppConfig, Library
import backend.config as config


def test_resolve_library():
    cfg = AppConfig(libraries=[
        Library(id="lib1", name="TestLib", strm_path="/apps/strm", media_path="/apps/media"),
    ])

    from pathlib import Path
    from backend.config import resolve_library, resolve_media_path

    assert resolve_library(Path("/apps/strm/test.strm"), cfg) is not None
    assert resolve_library(Path("/other/path/test.strm"), cfg) is None

    media = resolve_media_path(Path("/apps/strm/test.strm"), cfg)
    assert media == Path("/apps/media/test")


def test_split_lib_path():
    cfg = AppConfig(libraries=[
        Library(id="lib1", name="TestLib", strm_path="/apps/strm", media_path="/apps/media"),
    ])

    from backend.config import split_lib_path

    lib, abs_path = split_lib_path("lib1/test.strm", cfg)
    assert lib.id == "lib1"
    assert abs_path.exists() is False
