import asyncio
import backend.config as config
from backend.config import AppConfig, Library
from backend import media_index as mi_mod
from backend import mediainfo_runner
from backend import file_browser as fb
from backend.task_manager import task_manager, TaskStatus
from backend.ffprobe_runner import ProbeResult


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.mp4").write_text("x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    monkeypatch.setattr(mi_mod, "_INDEX_FILE", tmp_path / "media_index.json")
    mi_mod.media_index._data = None
    mi_mod.media_index.load()
    config._config_cache = AppConfig(
        libraries=[Library(id="lib1", name="主库", strm_path=str(root),
                           media_path=str(tmp_path / "media"),
                           media_url_root="https://openlist.novaw.de/d/115/Media")],
    )
    fb.clear_scan_cache()
    mi_mod.media_index.refresh_index("lib1", root, [], [".mp4", ".mkv"])
    return root


def test_mediainfo_path_used_when_url_root_set(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    called = {"probe": 0}
    def fake_probe(url, timeout, stop_event, log):
        called["probe"] += 1
        return ProbeResult(success=True, data={"streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "r_frame_rate": "30.000", "duration": "100"}], "format": {"duration": "100"}},
            tried_path=url, tried_extension=None, error=None, error_type=None, raw_stderr=None)
    monkeypatch.setattr(mediainfo_runner, "probe", fake_probe)
    task = task_manager.create_task("lib1", "recursive", False, ["EMPTY"], 2, 5, use_mock=False)
    asyncio.run(task_manager.run_task(task, config.get_config()))
    assert called["probe"] == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.progress.success == 1
