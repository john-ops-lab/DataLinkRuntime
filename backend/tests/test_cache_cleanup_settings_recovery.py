"""Unreadable cleanup settings disable automation without interrupting the Worker."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dlr.worker import cache_policy
from dlr.worker.agent import WorkerConfig
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_lifecycle import _read_bounded_json
from dlr.worker.cache_policy import CachePolicy, create_version_cache, effective_cache_policy
from test_cache_automatic_cleanup_settings import _command
from test_issue161_cache_admin import _local_agent
from test_issue161_cache_policy import _manager


def _enabled_base():
    return replace(CachePolicy(), gc_enabled=True, pressure_gc_enabled=True, min_idle_seconds=123)


def _broken_settings(runtime_root: Path, failure: str):
    path = cache_policy._cleanup_settings_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if failure == "directory":
        path.mkdir()
        return path
    if failure == "symlink":
        target = runtime_root / "external-settings.json"
        target.write_text('{"schema":1,"gc_enabled":true,"pressure_gc_enabled":true}')
        target.chmod(0o600)
        path.symlink_to(target)
        return path
    contents = {
        "json": b'{"private":"sentinel-settings-secret",',
        "encoding": b"\xff",
        "oversized": b" " * 1025,
        "shape": b"[]",
        "schema": b'{"schema":2,"gc_enabled":true,"pressure_gc_enabled":true}',
        "permissions": b'{"schema":1,"gc_enabled":true,"pressure_gc_enabled":true}',
    }
    path.write_bytes(contents[failure])
    path.chmod(0o644 if failure == "permissions" else 0o600)
    return path


def _agent(runtime_root, base):
    _, lifecycle, manager = _manager(runtime_root, base)
    agent = _local_agent(lifecycle, manager)
    agent._config = SimpleNamespace(runtime_root=runtime_root, cache_policy=base)
    return agent, manager


def test_missing_settings_keep_base_and_environment_switches(tmp_path, monkeypatch):
    base = _enabled_base()
    assert effective_cache_policy(tmp_path, base) == base
    assert create_version_cache(tmp_path, policy=base).policy == base
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("DLR_CACHE_GC_ENABLED", "true")
    monkeypatch.setenv("DLR_CACHE_PRESSURE_GC_ENABLED", "true")
    config = WorkerConfig()
    assert config.cache_policy.gc_enabled is True
    assert config.cache_policy.pressure_gc_enabled is True


@pytest.mark.parametrize(
    "failure",
    ["json", "encoding", "oversized", "shape", "schema", "permissions", "symlink", "directory"],
)
def test_unreadable_settings_allow_factory_config_and_snapshot(
    tmp_path, monkeypatch, caplog, failure
):
    base = _enabled_base()
    agent, manager = _agent(tmp_path, base)
    path = _broken_settings(tmp_path, failure)
    expected = replace(base, gc_enabled=False, pressure_gc_enabled=False)
    assert effective_cache_policy(tmp_path, base) == expected
    assert create_version_cache(tmp_path, policy=base).policy == expected

    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("DLR_CACHE_GC_ENABLED", "true")
    monkeypatch.setenv("DLR_CACHE_PRESSURE_GC_ENABLED", "true")
    env_base = CachePolicy.from_environment()
    assert WorkerConfig().cache_policy == replace(
        env_base, gc_enabled=False, pressure_gc_enabled=False
    )
    agent._report_cache_snapshot(7)
    assert manager.policy == expected
    assert manager.cache.policy == expected
    assert agent._config.cache_policy == expected
    policy = agent._client.snapshots[-1]["summary"]["policy"]
    assert policy["gc_enabled"] is False and policy["pressure_gc_enabled"] is False
    assert caplog.records
    assert all(record.levelname == "WARNING" for record in caplog.records)
    assert str(path) not in caplog.text
    assert "sentinel-settings-secret" not in caplog.text


def test_settings_io_error_is_degraded_only_at_effective_policy_boundary(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise PermissionError("private-path-and-secret")

    monkeypatch.setattr(cache_policy, "_read_bounded_json", unavailable)
    with pytest.raises(PermissionError):
        cache_policy._cleanup_overrides(tmp_path)
    base = _enabled_base()
    assert effective_cache_policy(tmp_path, base) == replace(
        base, gc_enabled=False, pressure_gc_enabled=False
    )


def test_bad_settings_preserve_original_low_watermark_and_skip_pressure_handler(
    tmp_path, monkeypatch
):
    cache = create_version_cache(tmp_path, policy=_enabled_base())
    _broken_settings(tmp_path, "json")
    original = CacheError("cache_low_watermark")

    def full(*_args, **_kwargs):
        raise original

    def must_not_cleanup(_amount):
        pytest.fail("unreadable settings must disable automatic pressure cleanup")

    monkeypatch.setattr(VerifiedVersionCache, "reserve", full)
    cache_policy.register_pressure_handler(cache.root, must_not_cleanup)
    try:
        with pytest.raises(CacheError) as caught:
            cache.reserve(123)
        assert caught.value is original
    finally:
        cache_policy.register_pressure_handler(cache.root, None)


@pytest.mark.parametrize("failure", ["json", "encoding", "shape", "schema"])
def test_explicit_update_repairs_settings_and_enables_only_requested_switch(tmp_path, failure):
    base = _enabled_base()
    agent, manager = _agent(tmp_path, base)
    path = _broken_settings(tmp_path, failure)
    assert agent._execute_cache_command(7, _command("gc_enabled", True))
    assert agent._client.reports[-1]["status"] == "completed"
    expected = replace(base, pressure_gc_enabled=False)
    assert manager.policy == expected
    assert effective_cache_policy(tmp_path, base) == expected
    assert not path.is_symlink()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {
        "schema": 1,
        "gc_enabled": True,
        "pressure_gc_enabled": False,
    }


@pytest.mark.parametrize("failure", ["permissions", "oversized", "symlink", "directory"])
def test_untrusted_settings_target_cannot_be_repaired_by_administrator_update(tmp_path, failure):
    base = _enabled_base()
    agent, manager = _agent(tmp_path, base)
    path = _broken_settings(tmp_path, failure)
    before = path.lstat()
    contents = path.read_bytes() if not path.is_dir() else None
    assert agent._execute_cache_command(7, _command("gc_enabled", True))
    assert agent._client.reports[-1]["status"] == "failed"
    assert agent._client.reports[-1]["error_code"] == "cache_policy_write_failed"
    assert manager.policy == replace(base, gc_enabled=False, pressure_gc_enabled=False)
    assert path.lstat() == before
    if contents is not None:
        assert path.read_bytes() == contents


def test_failed_repair_does_not_acknowledge_success_or_enable_automation(tmp_path, monkeypatch):
    base = _enabled_base()
    agent, manager = _agent(tmp_path, base)
    path = _broken_settings(tmp_path, "json")
    original = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise CacheError("cache_lifecycle_write_failed")

    monkeypatch.setattr(cache_policy, "_write_json", fail)
    assert agent._execute_cache_command(7, _command("gc_enabled", True))
    assert agent._client.reports[-1]["status"] == "failed"
    assert agent._client.reports[-1]["error_code"] == "cache_policy_write_failed"
    assert path.read_bytes() == original
    assert manager.policy == replace(base, gc_enabled=False, pressure_gc_enabled=False)


def test_other_lifecycle_reads_and_unsafe_paths_remain_strict(tmp_path):
    base = _enabled_base()
    agent, manager = _agent(tmp_path, base)
    _broken_settings(tmp_path, "json")
    owner = manager.lifecycle.owner_path
    owner.write_bytes(b'{"private":"sentinel-owner-secret",')
    with pytest.raises(CacheError):
        _read_bounded_json(owner, max_bytes=1024)
    with pytest.raises(CacheError):
        manager.lifecycle.owner(7)

    unsafe_runtime = tmp_path / "unsafe-runtime"
    unsafe_cache = unsafe_runtime / "version-cache"
    unsafe_cache.mkdir(parents=True)
    external = tmp_path / "external-directory"
    external.mkdir()
    (unsafe_cache / ".dlr-cache-lifecycle").symlink_to(external, target_is_directory=True)
    with pytest.raises(CacheError):
        cache_policy.save_automatic_cleanup(unsafe_runtime, base, {"gc_enabled": True})
    assert list(external.iterdir()) == []
