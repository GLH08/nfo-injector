import backend.config as config
from backend.config import AppConfig, Library
from backend import file_browser as fb
from backend.task_manager import task_manager, TaskStatus


def _setup(tmp_path):
    root = tmp_path / "Emby"
    (root / "Movie" / "A").mkdir(parents=True)
    (root / "Movie" / "A" / "A.strm").write_text("http://x", encoding="utf-8")
    (root / "Movie" / "A" / "A.nfo").write_text("<movie></movie>", encoding="utf-8")
    config._config_cache = AppConfig(
        libraries=[Library(id="lib1", name="主库", strm_path=str(root), media_path=str(tmp_path / "media"))],
        scan_cache_ttl=600,
    )
    fb.clear_scan_cache()
    return root


def test_run_task_uses_cache_no_rescan(tmp_path, monkeypatch):
    root = _setup(tmp_path)
    # 预先扫描落缓存
    fb.scan_and_cache(root, "lib1", root, [])
    # spy: 若调 get_strm_files_in_path 说明走了重扫
    calls = {"rescan": 0}
    orig = fb.get_strm_files_in_path
    def spy(*a, **kw):
        calls["rescan"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(fb, "get_strm_files_in_path", spy)

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,  # 虚拟注入，跳过 ffprobe
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    # 命中缓存 → 不应重扫
    assert calls["rescan"] == 0
    assert task.status == TaskStatus.COMPLETED


def test_run_task_falls_back_to_rescan_when_cache_miss(tmp_path, monkeypatch):
    root = _setup(tmp_path)
    # 不预扫 → 缓存未命中 → 应回退重扫
    calls = {"rescan": 0}
    orig = fb.get_strm_files_in_path
    def spy(*a, **kw):
        calls["rescan"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(fb, "get_strm_files_in_path", spy)

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    assert calls["rescan"] >= 1


def test_run_task_refreshes_cache_after_mock_inject(tmp_path):
    root = _setup(tmp_path)
    fb.scan_and_cache(root, "lib1", root, [])
    key = "lib1/Movie/A/A.strm"
    from backend.nfo_handler import NfoStatus
    assert fb._FILE_CACHE[key].status == NfoStatus.EMPTY

    task = task_manager.create_task(
        relative_path="lib1",
        scope="recursive",
        force=False,
        filter_status=["EMPTY"],
        concurrency=2,
        timeout=5,
        use_mock=True,
    )
    import asyncio
    asyncio.run(task_manager.run_task(task, config.get_config()))
    # 注入成功后 _refresh_cache_after_inject 应翻新缓存条目（EMPTY → 注入后状态）。
    # 注：inject_mock_mediainfo_to_nfo 未写 <duration>，故注入后为 PARTIAL 而非 HEALTHY
    # （_check_video_completeness 要求 duration）。本例验证翻新机制生效：状态已从
    # EMPTY 翻为注入后的实际值 PARTIAL。若日后 mock 注入补齐 duration，应同步改回 HEALTHY。
    assert fb._FILE_CACHE[key].status == NfoStatus.PARTIAL
    assert fb._FILE_CACHE[key].detail.status == NfoStatus.PARTIAL
