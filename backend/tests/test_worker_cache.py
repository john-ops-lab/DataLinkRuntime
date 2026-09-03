"""Focused tests for the bounded, verified v3 version cache."""

from __future__ import annotations

import stat
import threading
from pathlib import Path

import pytest

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
