import json
import backend.config as config


def test_seed_from_env_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope.json")
    monkeypatch.setenv("STRM_ROOT", "/apps/moviepilot/115strm/Emby")
    monkeypatch.setenv("MEDIA_ROOT", "/apps/clouddrive2/CloudDrive/115open/Media")
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 1
    assert c.libraries[0].name == "主库"
    assert c.libraries[0].strm_path == "/apps/moviepilot/115strm/Emby"
    assert c.libraries[0].media_path == "/apps/clouddrive2/CloudDrive/115open/Media"


def test_migrate_old_format(tmp_path, monkeypatch):
    old = {
        "strm_root": "/apps/s",
        "media_root": "/apps/m",
        "path_mappings": [
            {"strm_prefix": "中转/CN", "media_prefix": "Meta/CN", "description": "CN库"}
        ],
        "max_concurrency": 3,
    }
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cf)
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 2
    assert c.libraries[0].strm_path == "/apps/s"
    assert c.libraries[0].media_path == "/apps/m"
    assert c.libraries[1].name == "CN库"
    assert c.libraries[1].strm_path == "/apps/s/中转/CN"
    assert c.libraries[1].media_path == "/apps/m/Meta/CN"
    assert c.max_concurrency == 3


def test_new_format_passthrough(tmp_path, monkeypatch):
    new = {"libraries": [
        {"id": "ab12cd34", "name": "X", "strm_path": "/apps/s",
         "media_path": "/apps/m", "enabled": True}
    ]}
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cf)
    config._config_cache = None

    c = config.load_config()
    assert len(c.libraries) == 1
    assert c.libraries[0].id == "ab12cd34"
