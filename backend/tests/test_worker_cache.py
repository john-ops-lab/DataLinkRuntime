"""Focused tests for the bounded, verified v3 version cache."""

from __future__ import annotations

import stat
import threading
from pathlib import Path

import pytest

import dlr.worker.cache as cache_module
from dlr.worker.cache import CacheError, VerifiedVersionCache


def _promote(cache: VerifiedVersionCache, key: str, content: bytes) -> Path:
    reservation = cache.reserve(max(1024, len(content) + 512))
    staging = cache.staging_path(key, reservation.token)
    staging.mkdir(mode=0o700)
    (staging / "runtime.bin").write_bytes(content)
    return cache.promote(
        staging,
        cache.entry_path(key),
        identity={"language": "test", "version": key},
        reservation=reservation,
    )


def test_promoted_entry_is_verified_read_only_and_tamper_detected(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=16 * 1024, low_watermark_bytes=0)
    identity = {"language": "test", "version": "one"}
    entry = _promote(cache, "one", b"runtime")

    assert cache.verify(entry, identity)
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o711
    assert stat.S_IMODE(cache.entries.stat().st_mode) == 0o711
    assert stat.S_IMODE(entry.stat().st_mode) == 0o555
    assert stat.S_IMODE((entry / "runtime.bin").stat().st_mode) & 0o222 == 0
    (entry / "runtime.bin").chmod(0o600)
    (entry / "runtime.bin").write_bytes(b"tampered")
    assert not cache.verify(entry, identity)


def test_reservations_are_bounded_across_concurrent_misses(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=4096, low_watermark_bytes=0)
    barrier = threading.Barrier(3)
    reservations = []
    failures = []

    def reserve() -> None:
        barrier.wait()
        try:
            reservations.append(cache.reserve(3000))
        except CacheError as error:
            failures.append(error.code)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(reservations) == 1
    assert failures == ["cache_low_watermark"]
    assert sum(int(item["amount"]) for item in cache._state().values()) == 3000
    reservations[0].release()
    assert sum(int(item["amount"]) for item in cache._state().values()) == 0


def test_failed_read_only_staging_can_be_removed_without_broad_cleanup(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=16 * 1024, low_watermark_bytes=0)
    reservation = cache.reserve(1024)
    staging = cache.staging_path("failed", reservation.token)
    staging.mkdir(mode=0o700)
    (staging / "nested").mkdir()
    (staging / "nested" / "partial").write_bytes(b"partial")
    (staging / "nested").chmod(0o500)
    staging.chmod(0o500)

    cache.remove_staging(staging)
    reservation.release()
    assert not staging.exists()
    assert list(cache.entries.iterdir()) == []


def test_invalid_cache_budget_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CacheError) as error:
        VerifiedVersionCache(tmp_path / "cache", max_bytes=1, low_watermark_bytes=1)
    assert error.value.code == "cache_budget_invalid"


def test_cache_rejects_leaf_symlinks_for_verify_and_cleanup(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=16 * 1024, low_watermark_bytes=0)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "sentinel").write_text("keep", encoding="ascii")

    entry_link = cache.entries / "entry-link"
    staging_link = cache.entries / ".staging-link"
    entry_link.symlink_to(target, target_is_directory=True)
    staging_link.symlink_to(target, target_is_directory=True)

    assert cache.verify(entry_link, {"language": "test", "version": "link"}) is False
    with pytest.raises(CacheError) as entry_error:
        cache.remove_entry(entry_link)
    assert entry_error.value.code == "cache_path_invalid"
    with pytest.raises(CacheError) as staging_error:
        cache.remove_staging(staging_link)
    assert staging_error.value.code == "cache_path_invalid"
    assert (target / "sentinel").read_text(encoding="ascii") == "keep"


def test_promoted_entry_verifies_normal_runtime_symlinks_without_following(
    tmp_path: Path,
) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=16 * 1024, low_watermark_bytes=0)
    staging = cache.staging_path("linked", "token")
    staging.mkdir(mode=0o700)
    (staging / "runtime.bin").write_bytes(b"runtime")
    (staging / "runtime-link").symlink_to("runtime.bin")
    reservation = cache.reserve(1024)

    entry = cache.promote(
        staging,
        cache.entry_path("linked"),
        identity={"language": "test", "version": "linked"},
        reservation=reservation,
    )

    assert cache.verify(entry, {"language": "test", "version": "linked"})
    assert stat.S_IMODE(entry.stat().st_mode) == 0o555
    assert (entry / "runtime-link").is_symlink()
    assert (entry / "runtime-link").readlink() == Path("runtime.bin")


def test_tmpfs_build_rejects_over_budget_before_persistent_promotion(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=4096, low_watermark_bytes=0)
    source = tmp_path / "attempt-tmpfs" / "version-build"
    source.mkdir(mode=0o700, parents=True)
    (source / "runtime.bin").write_bytes(b"x" * 33)
    reservation = cache.reserve(32)

    with pytest.raises(CacheError) as error:
        cache.promote_from_tmpfs(
            source,
            cache.entry_path("bounded"),
            identity={"language": "test", "version": "bounded"},
            reservation=reservation,
        )

    assert error.value.code == "cache_reservation_insufficient"
    assert not cache.entry_path("bounded").exists()
    assert list(cache.entries.iterdir()) == []
    assert (source / "runtime.bin").read_bytes() == b"x" * 33
    (source / "runtime.bin").unlink()
    source.rmdir()
    source.parent.rmdir()


def test_tmpfs_promotion_is_atomic_and_read_only(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=4096, low_watermark_bytes=0)
    source = tmp_path / "attempt-tmpfs" / "version-build"
    source.mkdir(mode=0o700, parents=True)
    (source / "runtime.bin").write_bytes(b"runtime")
    (source / "runtime-link").symlink_to("runtime.bin")
    reservation = cache.reserve(1024)

    entry = cache.promote_from_tmpfs(
        source,
        cache.entry_path("atomic"),
        identity={"language": "test", "version": "atomic"},
        reservation=reservation,
    )

    assert cache.verify(entry, {"language": "test", "version": "atomic"})
    assert stat.S_IMODE(entry.stat().st_mode) == 0o555
    assert (entry / "runtime.bin").stat().st_mode & 0o222 == 0
    assert (entry / "runtime-link").is_symlink()
    assert not any(child.name.startswith(".atomic.staging-") for child in cache.entries.iterdir())


def test_tmpfs_failed_promotion_preserves_primary_error_when_cleanup_fails_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=4096, low_watermark_bytes=0)
    source = tmp_path / "attempt-tmpfs" / "version-build"
    source.mkdir(mode=0o700, parents=True)
    (source / "runtime.bin").write_bytes(b"runtime")
    reservation = cache.reserve(1024)
    staging = cache.staging_path("cleanup-primary", reservation.token)
    original_copy = cache_module._copy_tree_bounded
    original_remove = cache.remove_staging
    cleanup_calls = 0

    def fail_copy(*args: object, **kwargs: object) -> int:
        raise CacheError("cache_reservation_insufficient")

    def fail_cleanup_once(path: Path) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise CacheError("cache_staging_cleanup_failed")
        original_remove(path)

    monkeypatch.setattr(cache_module, "_copy_tree_bounded", fail_copy)
    monkeypatch.setattr(cache, "remove_staging", fail_cleanup_once)
    try:
        with pytest.raises(CacheError) as error:
            cache.promote_from_tmpfs(
                source,
                cache.entry_path("cleanup-primary"),
                identity={"language": "test", "version": "cleanup-primary"},
                reservation=reservation,
            )

        assert error.value.code == "cache_reservation_insufficient"
        assert cleanup_calls == 1
        assert staging.is_dir()
        assert cache._state() == {}
        cache.remove_staging(staging)
        assert cleanup_calls == 2
        assert not staging.exists()
    finally:
        monkeypatch.setattr(cache_module, "_copy_tree_bounded", original_copy)


def test_live_reservation_renewal_preserves_global_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = VerifiedVersionCache(tmp_path / "cache", max_bytes=4096, low_watermark_bytes=0)
    now = 1000.0
    monkeypatch.setattr(cache_module.time, "time", lambda: now)
    reservation = cache.reserve(3000, ttl_seconds=1)

    now = 1000.5
    reservation.renew()
    now = 1001.4
    with pytest.raises(CacheError) as error:
        cache.reserve(1097)

    assert error.value.code == "cache_low_watermark"
    assert sum(int(item["amount"]) for item in cache._state().values()) == 3000
    reservation.release()
    assert cache._state() == {}
