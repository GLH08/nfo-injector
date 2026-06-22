from backend.config import Library, AppConfig


def test_library_auto_id_and_defaults():
    lib = Library(name="JP主库", strm_path="/apps/s", media_path="/apps/m")
    assert isinstance(lib.id, str) and len(lib.id) == 8
    assert lib.enabled is True
    assert lib.name == "JP主库"


def test_library_keeps_given_id():
    lib = Library(id="ab12cd34", name="X", strm_path="/apps/s", media_path="/apps/m")
    assert lib.id == "ab12cd34"


def test_appconfig_defaults():
    c = AppConfig()
    assert c.libraries == []
    assert c.max_concurrency == 2
    assert c.ffprobe_timeout == 75


def test_old_fields_removed():
    fields = AppConfig.model_fields
    assert "libraries" in fields
    assert "strm_root" not in fields
    assert "media_root" not in fields
    assert "path_mappings" not in fields
