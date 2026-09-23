from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dlr.worker import cache_lifecycle, cgroup_namespace, sandbox, workspace
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_lifecycle import CacheLifecycleStore, cache_key
from dlr.worker.consumer import ConsumerConfig, V3Consumer
from worker_runtime_support import unit_resource_envelope, unit_sandbox_config


def _promote(cache: VerifiedVersionCache, key: str, identity: dict[str, object]) -> Path:
    staging = cache.staging_path(key)
    staging.mkdir()
    (staging / "payload.txt").write_text("stable-content", encoding="utf-8")
    reservation = cache.reserve(4096)
    return cache.promote(staging, cache.entry_path(key), identity=identity, reservation=reservation)


def _hold_use(
    runtime_root: str,
    ready: Any,
    release: Any,
    completed: bool,
) -> None:
    store = CacheLifecycleStore.for_runtime(Path(runtime_root))
    use = store.begin_use(
        "11-11",
        worker_id=7,
        execution_id=19,
        attempt_id=23,
        fencing_token=3,
    )
    use.__enter__()
    ready.set()
    release.wait(10)
    use.release(cleanup_completed=completed)


def _remove_entry(runtime_root: str, started: Any, result: Any) -> None:
    cache = VerifiedVersionCache(Path(runtime_root) / "version-cache")
    started.set()
    began = time.monotonic()
    try:
        cache.remove_entry(cache.entry_path("11-11"))
    except CacheError as error:
        result.put((error.code, time.monotonic() - began))
    else:
        result.put(("removed", time.monotonic() - began))


def _scan_uses(runtime_root: str, result: Any) -> None:
    try:
        list(CacheLifecycleStore.for_runtime(Path(runtime_root)).iter_use_records())
    except CacheError as error:
        result.put(error.code)
    else:
        result.put("accepted")


def test_legacy_manifest_stays_exact_and_tampering_still_fails(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    identity = {
        "adapter_id": 11,
        "version_id": 11,
        "language": "python",
        "source_sha256": "a" * 64,
    }
    entry = _promote(cache, "11-11", identity)
    manifest_path = entry / ".dlr-cache-manifest.json"
    before = manifest_path.read_bytes()

    assert cache.verify(entry, identity) is True
    assert manifest_path.read_bytes() == before
    assert set(json.loads(before)) == {"bytes", "digest", "files", "identity"}
    sidecar = json.loads(
        (cache.root / ".dlr-cache-lifecycle" / "items" / "11-11.json").read_text(encoding="ascii")
    )
    assert sidecar["identity"] == identity
    assert sidecar["rebuildability"] == "unknown"
    assert sidecar["first_observed_at"] == sidecar["last_used_at"]
    last_used = sidecar["last_used_at"]
    assert cache.verify(entry, identity) is True
    sidecar = json.loads(
        (cache.root / ".dlr-cache-lifecycle" / "items" / "11-11.json").read_text(encoding="ascii")
    )
    assert sidecar["last_used_at"] == last_used

    os.chmod(entry, 0o755)
    os.chmod(entry / "payload.txt", 0o644)
    (entry / "payload.txt").write_text("tampered", encoding="utf-8")
    assert cache.verify(entry, identity) is False


def test_lifecycle_write_failure_keeps_verified_legacy_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    identity = {
        "adapter_id": 1,
        "version_id": 2,
        "language": "python",
        "source_sha256": "b" * 64,
    }
    entry = _promote(cache, "1-2", identity)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise CacheError("cache_lifecycle_write_failed")

    monkeypatch.setattr(CacheLifecycleStore, "observe_verified", fail)
    assert cache.verify(entry, identity) is True


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("last_used_at", None),
        ("last_used_at", "invalid-time"),
        ("last_used_at", 10**1000),
        ("rebuildability", []),
    ],
)
def test_corrupt_lifecycle_never_invalidates_verified_content(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    identity = {
        "adapter_id": 1,
        "version_id": 2,
        "language": "python",
        "source_sha256": "b" * 64,
    }
    entry = _promote(cache, "1-2", identity)
    sidecar_path = cache.root / ".dlr-cache-lifecycle" / "items" / "1-2.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="ascii"))
    sidecar[field] = invalid_value
    sidecar_path.write_text(json.dumps(sidecar), encoding="ascii")
    sidecar_path.chmod(0o600)
    assert cache.verify(entry, identity) is True


def test_json_numeric_limit_in_lifecycle_never_invalidates_content(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    identity = {
        "adapter_id": 1,
        "version_id": 2,
        "language": "python",
        "source_sha256": "b" * 64,
    }
    entry = _promote(cache, "1-2", identity)
    sidecar_path = cache.root / ".dlr-cache-lifecycle" / "items" / "1-2.json"
    sidecar_path.write_text('{"x":' + ("9" * 5000) + "}", encoding="ascii")
    sidecar_path.chmod(0o600)
    assert cache.verify(entry, identity) is True


def test_root_owner_never_follows_registration_id_change(tmp_path: Path) -> None:
    store = CacheLifecycleStore.for_runtime(tmp_path)
    store_id = store.bind_owner(7)
    assert store.bind_owner(7) == store_id
    with pytest.raises(CacheError, match="Version cache operation failed") as error:
        store.bind_owner(8)
    assert error.value.code == "cache_owner_mismatch"

    legacy_root = tmp_path / "legacy"
    legacy_cache = VerifiedVersionCache(legacy_root / "version-cache")
    _promote(
        legacy_cache,
        "3-4",
        {"adapter_id": 3, "version_id": 4, "language": "go", "source_sha256": "c" * 64},
    )
    with pytest.raises(CacheError) as legacy_error:
        CacheLifecycleStore.for_runtime(legacy_root).bind_owner(9)
    assert legacy_error.value.code == "cache_owner_unconfirmed"

    staging_only = tmp_path / "staging-only"
    staging_cache = VerifiedVersionCache(staging_only / "version-cache")
    staging_cache.staging_path("3-4").mkdir()
    with pytest.raises(CacheError) as staging_error:
        CacheLifecycleStore.for_runtime(staging_only).bind_owner(9)
    assert staging_error.value.code == "cache_owner_unconfirmed"


def test_cross_process_lock_and_persistent_use_block_removal(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    cache = VerifiedVersionCache(runtime_root / "version-cache")
    _promote(
        cache,
        "11-11",
        {
            "adapter_id": 11,
            "version_id": 11,
            "language": "python",
            "source_sha256": "d" * 64,
        },
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    started = context.Event()
    result = context.Queue()
    holder = context.Process(target=_hold_use, args=(str(runtime_root), ready, release, False))
    remover = context.Process(target=_remove_entry, args=(str(runtime_root), started, result))
    holder.start()
    assert ready.wait(10)
    remover.start()
    assert started.wait(10)
    time.sleep(0.25)
    assert remover.is_alive(), "remove must wait for the independently held cache key lock"
    release.set()
    holder.join(10)
    remover.join(10)
    assert holder.exitcode == 0
    assert remover.exitcode == 0
    code, waited = result.get(timeout=2)
    assert code == "cache_entry_in_use"
    assert waited >= 0.2
    assert cache.entry_path("11-11").exists()
    assert list(CacheLifecycleStore.for_runtime(runtime_root).iter_use_records())


def test_use_cleanup_requires_explicit_sandbox_completion(tmp_path: Path) -> None:
    store = CacheLifecycleStore.for_runtime(tmp_path)
    key = cache_key(4, 5)
    use = store.begin_use(
        key,
        worker_id=2,
        execution_id=7,
        attempt_id=8,
        fencing_token=9,
    )
    use.__enter__()
    use.release(cleanup_completed=False)
    assert [record["key"] for record in store.iter_use_records()] == [key]

    recovered = store.begin_use(
        key,
        worker_id=2,
        execution_id=7,
        attempt_id=8,
        fencing_token=9,
    )
    recovered.__enter__()
    recovered.release(cleanup_completed=True)
    assert list(store.iter_use_records()) == []


def test_current_attempt_cannot_bypass_an_older_persistent_use(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    identity = {
        "adapter_id": 1,
        "version_id": 2,
        "language": "python",
        "source_sha256": "e" * 64,
    }
    entry = _promote(cache, "1-2", identity)
    store = CacheLifecycleStore(cache.root)
    old = store.begin_use("1-2", worker_id=1, execution_id=1, attempt_id=3, fencing_token=1)
    old.__enter__()
    old.release(cleanup_completed=False)
    current = store.begin_use("1-2", worker_id=1, execution_id=2, attempt_id=6, fencing_token=2)
    current.__enter__()
    try:
        with pytest.raises(CacheError) as error:
            cache.remove_entry(entry)
        assert error.value.code == "cache_entry_in_use"
        assert entry.exists()
    finally:
        current.release(cleanup_completed=True)


def test_malformed_use_record_blocks_removal(tmp_path: Path) -> None:
    cache = VerifiedVersionCache(tmp_path / "version-cache")
    entry = _promote(
        cache,
        "1-2",
        {"adapter_id": 1, "version_id": 2, "language": "go", "source_sha256": "f" * 64},
    )
    store = CacheLifecycleStore(cache.root)
    malformed = {
        "schema": 1,
        "key": None,
        "worker_id": 1,
        "execution_id": 1,
        "attempt_id": 4,
        "fencing_token": 1,
        "started_at": time.time(),
    }
    path = store.use_root / "attempt-4.json"
    path.write_text(json.dumps(malformed), encoding="ascii")
    path.chmod(0o600)
    with pytest.raises(CacheError) as error:
        cache.remove_entry(entry)
    assert error.value.code == "cache_use_invalid"
    assert entry.exists()


def test_lock_file_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    store = CacheLifecycleStore.for_runtime(tmp_path)
    target = tmp_path / "outside"
    target.write_text("unchanged", encoding="utf-8")
    (store.lock_root / "one.lock").symlink_to(target)
    with pytest.raises(CacheError) as error, store.entry_lock("one"):
        pass
    assert error.value.code == "cache_lock_invalid"
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_fifo_use_record_is_rejected_without_blocking(tmp_path: Path) -> None:
    store = CacheLifecycleStore.for_runtime(tmp_path)
    os.mkfifo(store.use_root / "attempt-5.json", mode=0o600)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(target=_scan_uses, args=(str(tmp_path), result))
    process.start()
    process.join(3)
    assert process.exitcode == 0
    assert result.get(timeout=1) == "cache_lifecycle_invalid"


def test_old_and_unknown_journals_are_conservatively_protected(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    for root in (attempt_root, cleanup_root, sandbox_root):
        root.mkdir(parents=True, exist_ok=True)
    (attempt_root / "attempt-8.attempt.json").write_text(
        json.dumps(
            {
                "execution_id": 7,
                "attempt_id": 8,
                "attempt_no": 1,
                "fencing_token": 2,
                "lease_expires_at": "2026-09-22T00:00:00Z",
                "protocol_version": 3,
                "workspace_path": str(runtime_root / "workspaces" / "7"),
                "claim_token": "claim",
                "cleanup_token": "cleanup",
            }
        ),
        encoding="ascii",
    )
    (cleanup_root / "execution-9-attempt-10.cleanup.json").write_text(
        json.dumps(
            {
                "execution_id": 9,
                "attempt_id": 10,
                "protocol_version": 3,
                "workspace_path": str(runtime_root / "workspaces" / "9"),
                "cleanup_token": "cleanup",
            }
        ),
        encoding="ascii",
    )
    (sandbox_root / "sandbox-old.json").write_text("not-json", encoding="ascii")
    for item in runtime_root.rglob("*.json"):
        item.chmod(0o600)

    mapping = {(7, 8): "1-2", (9, 10): "3-4"}
    with cgroup_namespace.lock_roots(runtime_root, [cleanup_root, attempt_root]):
        protection = CacheLifecycleStore.for_runtime(runtime_root).scan_journal_protections(
            attempt_journal_root=attempt_root,
            cleanup_journal_root=cleanup_root,
            sandbox_recovery_root=sandbox_root,
            resolve=lambda execution_id, attempt_id: mapping.get((execution_id, attempt_id)),
        )
    assert protection.protected_keys == frozenset({"1-2", "3-4"})
    assert protection.block_all is True
    assert "sandbox_journal_unknown" in protection.reasons


def test_control_resolver_outage_blocks_then_restart_remaps_old_journal(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    for root in (attempt_root, cleanup_root, sandbox_root):
        root.mkdir(parents=True, exist_ok=True)
    journal = attempt_root / "attempt-23.attempt.json"
    journal.write_text(
        json.dumps(
            {
                "execution_id": 19,
                "attempt_id": 23,
                "attempt_no": 1,
                "fencing_token": 3,
                "lease_expires_at": "2026-09-22T00:00:00Z",
                "protocol_version": 3,
                "workspace_path": str(runtime_root / "workspaces" / "19"),
                "claim_token": "claim",
                "cleanup_token": "cleanup",
            }
        ),
        encoding="ascii",
    )
    journal.chmod(0o600)
    store = CacheLifecycleStore.for_runtime(runtime_root)

    def unavailable(_execution_id: int, _attempt_id: int | None) -> str | None:
        raise ConnectionError("control unavailable")

    disconnected = store.scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=unavailable,
    )
    assert disconnected.block_all is True
    assert disconnected.protected_keys == frozenset()
    assert "attempt_journal_unmapped" in disconnected.reasons

    restarted = store.scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=lambda execution_id, attempt_id: (
            "11-11" if (execution_id, attempt_id) == (19, 23) else None
        ),
    )
    assert restarted.block_all is False
    assert restarted.protected_keys == frozenset({"11-11"})


def test_hidden_and_directory_journals_are_unknown(tmp_path: Path) -> None:
    attempt_root = tmp_path / "attempts"
    cleanup_root = tmp_path / "cleanups"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()
    (attempt_root / ".unfinished.tmp").write_text("{}", encoding="ascii")
    (attempt_root / "attempt-1.attempt.json").mkdir()
    protection = CacheLifecycleStore.for_runtime(tmp_path / "runtime").scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=lambda _execution_id, _attempt_id: None,
    )
    assert protection.block_all is True
    assert "attempt_journal_unknown" in protection.reasons


def test_real_instance_locks_are_metadata_and_remain_held(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()

    with cgroup_namespace.lock_roots(runtime_root, [cleanup_root, attempt_root]):
        before = {
            root: (root / ".dlr-instance.lock").stat() for root in (attempt_root, cleanup_root)
        }
        protection = CacheLifecycleStore.for_runtime(runtime_root).scan_journal_protections(
            attempt_journal_root=attempt_root,
            cleanup_journal_root=cleanup_root,
            sandbox_recovery_root=sandbox_root,
            resolve=lambda _execution_id, _attempt_id: pytest.fail(
                "instance-lock metadata must not reach the journal resolver"
            ),
        )
        with (
            pytest.raises(sandbox.SandboxError) as raised,
            cgroup_namespace.lock_roots(tmp_path / "other-runtime", [attempt_root]),
        ):
            pytest.fail("a second owner must not acquire the recognized lock")
        assert raised.value.code == "sandbox_instance_root_in_use"
        after = {
            root: (root / ".dlr-instance.lock").stat() for root in (attempt_root, cleanup_root)
        }

    assert protection.protected_keys == frozenset()
    assert protection.block_all is False
    assert protection.reasons == ()
    for root in before:
        assert (after[root].st_ino, after[root].st_mode, after[root].st_size) == (
            before[root].st_ino,
            before[root].st_mode,
            0,
        )


def test_real_instance_locks_preserve_excluded_attempt_and_other_journals(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()
    attempt = attempt_root / "attempt-8.attempt.json"
    attempt.write_text(
        json.dumps(
            {
                "execution_id": 7,
                "attempt_id": 8,
                "attempt_no": 1,
                "fencing_token": 2,
                "lease_expires_at": "2026-09-22T00:00:00Z",
                "protocol_version": 3,
                "workspace_path": str(runtime_root / "workspaces" / "7"),
                "claim_token": "claim",
                "cleanup_token": "cleanup",
            }
        ),
        encoding="ascii",
    )
    cleanup = cleanup_root / "execution-9-attempt-10.cleanup.json"
    cleanup.write_text(
        json.dumps(
            {
                "execution_id": 9,
                "attempt_id": 10,
                "protocol_version": 3,
                "workspace_path": str(runtime_root / "workspaces" / "9"),
                "cleanup_token": "cleanup",
            }
        ),
        encoding="ascii",
    )
    attempt.chmod(0o600)
    cleanup.chmod(0o600)

    with cgroup_namespace.lock_roots(runtime_root, [cleanup_root, attempt_root]):
        protection = CacheLifecycleStore.for_runtime(runtime_root).scan_journal_protections(
            attempt_journal_root=attempt_root,
            cleanup_journal_root=cleanup_root,
            sandbox_recovery_root=sandbox_root,
            resolve=lambda execution_id, attempt_id: (
                "3-4" if (execution_id, attempt_id) == (9, 10) else None
            ),
            excluded_attempt=(7, 8, 2),
        )

    assert protection.protected_keys == frozenset({"3-4"})
    assert protection.block_all is False
    assert protection.reasons == ()


@pytest.mark.parametrize(
    "case",
    [
        "sandbox",
        "similar-name",
        "nonempty",
        "wrong-mode",
        "hardlink",
        "symlink",
        "dangling-symlink",
        "directory",
        "fifo",
    ],
)
def test_untrusted_instance_lock_shapes_remain_unknown(tmp_path: Path, case: str) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()
    attempt_root.chmod(0o700)
    cleanup_root.chmod(0o700)
    sandbox_root.chmod(0o700)
    root = sandbox_root if case == "sandbox" else attempt_root
    name = ".dlr-instance.lock.tmp" if case == "similar-name" else ".dlr-instance.lock"
    lock = root / name
    if case in {"symlink", "dangling-symlink"}:
        target = tmp_path / "target"
        if case == "symlink":
            target.write_bytes(b"keep")
            target.chmod(0o600)
        lock.symlink_to(target)
    elif case == "directory":
        lock.mkdir(mode=0o700)
    elif case == "fifo":
        os.mkfifo(lock, 0o600)
    else:
        lock.write_bytes(b"x" if case == "nonempty" else b"")
        lock.chmod(0o640 if case == "wrong-mode" else 0o600)
        if case == "hardlink":
            os.link(lock, tmp_path / "second-link")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    protection = CacheLifecycleStore.for_runtime(runtime_root).scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=lambda _execution_id, _attempt_id: pytest.fail(
            "an untrusted lock-shaped entry must not reach the journal resolver"
        ),
    )

    assert protection.block_all is True
    expected_kind = "sandbox" if case == "sandbox" else "attempt"
    assert f"{expected_kind}_journal_unknown" in protection.reasons
    if case == "symlink":
        assert (tmp_path / "target").read_bytes() == b"keep"


@pytest.mark.parametrize("unsafe", ["root-mode", "wrong-owner"])
def test_instance_lock_requires_private_owned_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True, mode=0o700)
    attempt_root.mkdir(mode=0o700)
    lock = attempt_root / ".dlr-instance.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    store = CacheLifecycleStore.for_runtime(runtime_root)
    if unsafe == "root-mode":
        attempt_root.chmod(0o711)
    else:
        monkeypatch.setattr(cache_lifecycle.os, "geteuid", lambda: os.getuid() + 1)

    protection = store.scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=lambda _execution_id, _attempt_id: pytest.fail(
            "an instance lock in an unsafe root must not reach the resolver"
        ),
    )

    assert protection.block_all is True
    assert "attempt_journal_unknown" in protection.reasons


@pytest.mark.parametrize(
    "race",
    [
        "before-open",
        "before-open-disappear",
        "after-fstat",
        "after-fstat-nonempty",
        "root-replaced",
        "open-error",
        "read-error",
    ],
)
def test_instance_lock_races_and_io_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    root = tmp_path / "attempt-journal"
    root.mkdir(mode=0o700)
    lock = root / ".dlr-instance.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    observed_root = root.lstat()
    observed_file = lock.lstat()
    original_open = os.open
    original_read = os.read
    # Keep the observed inode allocated while simulating replacement. Linux may
    # otherwise immediately reuse it, hiding the intended before-open race.
    observed_descriptor = original_open(lock, os.O_RDONLY)
    replaced = False
    old_root = tmp_path / "old-attempt-journal"

    def replace_lock() -> None:
        nonlocal replaced
        if replaced:
            return
        replaced = True
        lock.unlink()
        lock.write_bytes(b"")
        lock.chmod(0o600)

    def raced_open(path: object, *args: object, **kwargs: object) -> int:
        if path == root and race == "root-replaced":
            descriptor = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
            root.rename(old_root)
            root.mkdir(mode=0o700)
            return descriptor
        if path == ".dlr-instance.lock":
            if race == "open-error":
                raise OSError("synthetic open failure")
            if race == "before-open":
                replace_lock()
            elif race == "before-open-disappear":
                lock.unlink()
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    def raced_read(descriptor: int, size: int) -> bytes:
        if race == "read-error":
            raise OSError("synthetic read failure")
        if race == "after-fstat-nonempty":
            lock.write_bytes(b"x")
        result = original_read(descriptor, size)
        if race == "after-fstat":
            replace_lock()
        return result

    monkeypatch.setattr(cache_lifecycle.os, "open", raced_open)
    monkeypatch.setattr(cache_lifecycle.os, "read", raced_read)

    try:
        assert (
            cache_lifecycle._is_journal_instance_lock(
                root,
                observed_root=observed_root,
                observed_file=observed_file,
            )
            is False
        )
    finally:
        os.close(observed_descriptor)


def test_instance_locks_still_consume_the_journal_scan_budget(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    attempt_root = runtime_root / "attempt-journal"
    cleanup_root = runtime_root / "cleanup-journal"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()

    with cgroup_namespace.lock_roots(runtime_root, [cleanup_root, attempt_root]):
        protection = CacheLifecycleStore.for_runtime(runtime_root).scan_journal_protections(
            attempt_journal_root=attempt_root,
            cleanup_journal_root=cleanup_root,
            sandbox_recovery_root=sandbox_root,
            resolve=lambda _execution_id, _attempt_id: None,
            max_records=1,
        )

    assert protection.block_all is True
    assert protection.reasons == ("journal_scan_truncated",)


def test_oversized_or_wrong_protocol_journal_never_reaches_resolver(tmp_path: Path) -> None:
    attempt_root = tmp_path / "attempts"
    cleanup_root = tmp_path / "cleanups"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()
    journal = attempt_root / "attempt-23.attempt.json"
    journal.write_text(
        json.dumps(
            {
                "execution_id": 19,
                "attempt_id": 23,
                "attempt_no": 1,
                "fencing_token": 1,
                "lease_expires_at": "later",
                "protocol_version": 999,
                "workspace_path": None,
                "claim_token": None,
                "cleanup_token": None,
            }
        )
        + (" " * (2 * 1024 * 1024)),
        encoding="ascii",
    )
    journal.chmod(0o600)
    resolver_called = False

    def resolve(_execution_id: int, _attempt_id: int | None) -> str | None:
        nonlocal resolver_called
        resolver_called = True
        return "11-11"

    protection = CacheLifecycleStore.for_runtime(tmp_path / "runtime").scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=resolve,
    )
    assert protection.block_all is True
    assert resolver_called is False


def test_sandbox_journal_name_and_execution_must_agree(tmp_path: Path) -> None:
    attempt_root = tmp_path / "attempts"
    cleanup_root = tmp_path / "cleanups"
    sandbox_root = cleanup_root / "sandbox-recovery"
    sandbox_root.mkdir(parents=True)
    attempt_root.mkdir()
    name = "attempt-999-11111111111111111111111111111111"
    marker = sandbox_root / f"sandbox-{name}.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": name,
                "execution_id": 19,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(tmp_path / "mount"),
            }
        ),
        encoding="ascii",
    )
    marker.chmod(0o600)
    resolver_called = False

    def resolve(_execution_id: int, _attempt_id: int | None) -> str | None:
        nonlocal resolver_called
        resolver_called = True
        return "11-11"

    protection = CacheLifecycleStore.for_runtime(tmp_path / "runtime").scan_journal_protections(
        attempt_journal_root=attempt_root,
        cleanup_journal_root=cleanup_root,
        sandbox_recovery_root=sandbox_root,
        resolve=resolve,
    )
    assert protection.block_all is True
    assert resolver_called is False
    assert "sandbox_journal_unknown" in protection.reasons


class _UseClient:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def start_attempt(self, _worker_id: int, attempt_id: int, _body: object) -> dict[str, Any]:
        return {
            "decision": "ACK_NOOP",
            "reason": "started",
            "attempt_id": attempt_id,
            "cancel_requested": False,
        }

    def result_attempt(
        self, _worker_id: int, _attempt_id: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.results.append(dict(body))
        return {"decision": "ACK_NOOP"}

    def report_cleanup_receipt(
        self, _worker_id: int, _execution_id: int, *, cleanup_token: str
    ) -> dict[str, Any]:
        assert cleanup_token == "cleanup"
        return {"decision": "ACK_NOOP"}


def _consumer_payload() -> SimpleNamespace:
    values = {
        "adapter_id": 11,
        "version_id": 11,
        "execution_id": 19,
        "attempt_id": 23,
        "fencing_token": 3,
        "claim_token": "claim",
        "cleanup_token": "cleanup",
        "renew_seconds": 100,
        "builtin_package_snapshot": None,
    }
    return SimpleNamespace(**values, model_dump=lambda **_kwargs: dict(values))


@pytest.mark.parametrize(
    ("sandbox_status", "record_remains"), [("completed", False), ("deferred", True)]
)
def test_consumer_keeps_use_until_sandbox_is_confirmed_empty(
    tmp_path: Path, sandbox_status: str, record_remains: bool
) -> None:
    runtime_root = tmp_path / "runtime"
    client = _UseClient()

    def runner(*_args: object, **_kwargs: object) -> dict[str, Any]:
        records = list(CacheLifecycleStore.for_runtime(runtime_root).iter_use_records())
        assert records[0]["key"] == "11-11"
        return {
            "status": "succeeded",
            "workspace_cleanup_status": "completed",
            "cleanup_summary": {"sandbox": {"status": sandbox_status}},
        }

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="test",
            execution_slots=1,
            runtime_root=runtime_root,
            attempt_journal_root=tmp_path / "attempt-journal",
        ),
        client,  # type: ignore[arg-type]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(),
            resource_envelope=unit_resource_envelope(),
        ),
        runner=runner,
    )
    try:
        consumer._run_attempt(_consumer_payload())  # type: ignore[arg-type]
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    records = list(CacheLifecycleStore.for_runtime(runtime_root).iter_use_records())
    assert bool(records) is record_remains


def test_consumer_fails_closed_when_use_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "runtime"
    client = _UseClient()
    runner_called = False

    def fail_use(*_args: object, **_kwargs: object) -> object:
        raise CacheError("cache_use_write_failed")

    def runner(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal runner_called
        runner_called = True
        return {"status": "succeeded"}

    monkeypatch.setattr(CacheLifecycleStore, "begin_use", fail_use)
    attempt_root = tmp_path / "attempt-journal"
    planned = workspace.workspace_path(runtime_root, 19, attempt_id=23)
    workspace.write_attempt_journal(
        attempt_root,
        execution_id=19,
        attempt_id=23,
        attempt_no=1,
        fencing_token=3,
        lease_expires_at="2026-09-22T00:00:00Z",
        workspace=planned,
        claim_token="claim",
        cleanup_token="cleanup",
    )
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="test",
            execution_slots=1,
            runtime_root=runtime_root,
            attempt_journal_root=attempt_root,
        ),
        client,  # type: ignore[arg-type]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(),
            resource_envelope=unit_resource_envelope(),
        ),
        runner=runner,
    )
    try:
        consumer._run_attempt(_consumer_payload())  # type: ignore[arg-type]
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert runner_called is False
    assert client.results[0]["error_code"] == "cache_use_write_failed"
    assert client.results[0]["workspace_cleanup_status"] == "completed"
    assert not workspace.attempt_journal_path(attempt_root, 23).exists()


@pytest.mark.parametrize("fatal", [SystemExit, KeyboardInterrupt])
def test_consumer_fatal_runner_error_preserves_use_and_stops_renewal(
    tmp_path: Path, fatal: type[BaseException]
) -> None:
    runtime_root = tmp_path / "runtime"
    client = _UseClient()
    before = {
        thread.ident for thread in threading.enumerate() if thread.name == "dlr-attempt-renew"
    }

    def runner(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise fatal()

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="test",
            execution_slots=1,
            runtime_root=runtime_root,
            attempt_journal_root=tmp_path / "attempt-journal",
        ),
        client,  # type: ignore[arg-type]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(),
            resource_envelope=unit_resource_envelope(),
        ),
        runner=runner,
    )
    try:
        with pytest.raises(fatal):
            consumer._run_attempt(_consumer_payload())  # type: ignore[arg-type]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            active = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name == "dlr-attempt-renew"
            }
            if active <= before:
                break
            time.sleep(0.01)
        assert active <= before
        assert list(CacheLifecycleStore.for_runtime(runtime_root).iter_use_records())
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
