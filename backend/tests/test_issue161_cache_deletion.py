"""Issue #161 crash-safe local cache deletion and recovery."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import dlr.worker.cache_deletion as deletion_module
import dlr.worker.cache_lifecycle as lifecycle_module
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_deletion import CacheDeletionManager, DeletionEligibility
from dlr.worker.cache_lifecycle import CacheLifecycleStore
from dlr.worker.client import ControlUnavailableError


class FakeGuardClient:
    def __init__(self) -> None:
        self.operations: dict[uuid.UUID, dict[str, Any]] = {}
        self.fail_acquire_response = False
        self.fail_finish_response = False
        self.fail_checks = False

    def acquire_cache_guard(
        self,
        worker_id: int,
        *,
        adapter_id: int,
        version_id: int,
        operation_id: uuid.UUID,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        _ = timeout_seconds
        operation = self.operations.setdefault(
            operation_id,
            {
                "worker_id": worker_id,
                "adapter_id": adapter_id,
                "version_id": version_id,
                "operation_id": str(operation_id),
                "generation": 1,
                "phase": "acquired",
            },
        )
        if self.fail_acquire_response:
            self.fail_acquire_response = False
            raise ControlUnavailableError("committed response lost")
        return dict(operation)

    def check_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        _ = timeout_seconds
        if self.fail_checks:
            raise ControlUnavailableError("unavailable")
        operation = self.operations[operation_id]
        assert operation["worker_id"] == worker_id
        return dict(operation)

    def finish_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        *,
        generation: int,
        outcome: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        _ = timeout_seconds
        operation = self.operations[operation_id]
        assert operation["worker_id"] == worker_id
        assert operation["generation"] == generation
        if operation["phase"] == "acquired":
            operation["phase"] = outcome
        assert operation["phase"] == outcome
        if self.fail_finish_response:
            self.fail_finish_response = False
            raise ControlUnavailableError("committed response lost")
        return dict(operation)

    def list_cache_guard_page(
        self,
        worker_id: int,
        *,
        after_version_id: int | None = None,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        _ = timeout_seconds
        active = sorted(
            (
                operation
                for operation in self.operations.values()
                if operation["worker_id"] == worker_id
                and operation["phase"] == "acquired"
                and (after_version_id is None or operation["version_id"] > after_version_id)
            ),
            key=lambda item: item["version_id"],
        )
        page = active[:limit]
        next_after = page[-1]["version_id"] if len(active) > limit else None
        return [dict(item) for item in page], next_after

    def resolve_cache_key_references(
        self,
        worker_id: int,
        items: list[tuple[int, int]],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        _ = timeout_seconds
        return {
            "kind": "cache_keys_v1",
            "worker_id": worker_id,
            "sampled_at": time.time(),
            "complete": True,
            "items": [
                {
                    "adapter_id": adapter_id,
                    "version_id": version_id,
                    "status": "clear",
                    "reasons": [],
                }
                for adapter_id, version_id in items
            ],
        }


def _ready(
    tmp_path: Path,
) -> tuple[VerifiedVersionCache, CacheLifecycleStore, DeletionEligibility]:
    cache = VerifiedVersionCache(
        tmp_path / "version-cache", max_bytes=64 * 1024, low_watermark_bytes=0
    )
    lifecycle = CacheLifecycleStore(cache.root)
    lifecycle.bind_owner(7)
    identity = {"adapter_id": 11, "version_id": 13, "language": "python"}
    staging = cache.staging_path("11-13")
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"x" * 4096)
    nested = staging / "nested"
    nested.mkdir()
    (nested / "second.bin").write_bytes(b"y" * 2048)
    reservation = cache.reserve(8192)
    entry = cache.promote(
        staging,
        cache.entry_path("11-13"),
        identity=identity,
        reservation=reservation,
    )
    manifest = json.loads((entry / ".dlr-cache-manifest.json").read_text(encoding="ascii"))
    facts = lifecycle.lifecycle("11-13")
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=manifest["digest"],
        source_policy="verified_offline",
        evidence_note="test material",
        actor="test",
        valid_until=time.time() + 86_400,
    )
    eligibility = DeletionEligibility(
        adapter_id=11,
        version_id=13,
        identity=identity,
        digest=manifest["digest"],
        last_used_before=float(facts["last_used_at"]),
    )
    return cache, lifecycle, eligibility


def _add_ready(cache: VerifiedVersionCache, adapter_id: int, version_id: int) -> None:
    key = f"{adapter_id}-{version_id}"
    identity = {"adapter_id": adapter_id, "version_id": version_id, "language": "python"}
    staging = cache.staging_path(key)
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"ready")
    cache.promote(
        staging,
        cache.entry_path(key),
        identity=identity,
        reservation=cache.reserve(1024),
    )


def test_partial_trash_is_charged_and_finish_response_loss_recovers(
    tmp_path: Path,
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    before_bytes = cache._committed_bytes()
    result = manager.begin(eligibility, max_nodes=1, max_bytes=8192)
    assert result.status == "in_progress"
    assert not cache.entry_path("11-13").exists()
    assert 0 < cache._committed_bytes() < before_bytes

    client.fail_finish_response = True
    manager.recover_round(max_items=1, max_pages=0, max_nodes=100, max_bytes=8192)
    record_path = lifecycle.deletion_root / f"{result.operation_id}.json"
    record = json.loads(record_path.read_text(encoding="ascii"))
    assert record["phase"] == "receipt_pending"
    assert client.operations[result.operation_id]["phase"] == "completed"

    restarted = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    restarted.recover_round(max_items=1, max_pages=0)
    record = json.loads(record_path.read_text(encoding="ascii"))
    assert record["phase"] == "completed"
    assert cache._committed_bytes() == 0


def test_acquire_commit_without_local_record_is_safely_aborted(tmp_path: Path) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    client.fail_acquire_response = True
    operation_id = uuid.uuid4()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
    )
    with pytest.raises(ControlUnavailableError):
        manager.begin(eligibility, operation_id=operation_id, max_bytes=8192)
    assert cache.entry_path("11-13").exists()
    assert list(lifecycle.deletion_root.iterdir()) == []

    assert manager.recover_round(max_items=1, max_pages=1, page_size=1) == 1
    assert client.operations[operation_id]["phase"] == "aborted"
    assert cache.entry_path("11-13").exists()


def test_three_failures_stop_without_renaming_entry(tmp_path: Path) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    client.fail_checks = True
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    result = manager.begin(eligibility, max_bytes=8192)
    assert result.reason == "cache_control_unavailable"
    manager.recover_round(max_items=1, max_pages=0)
    manager.recover_round(max_items=1, max_pages=0)
    record = json.loads(
        (lifecycle.deletion_root / f"{result.operation_id}.json").read_text(encoding="ascii")
    )
    assert record["attempts"] == 3
    assert record["phase"] == "failed"
    assert cache.entry_path("11-13").exists()
    assert client.operations[result.operation_id]["phase"] == "acquired"


def test_use_or_journal_protection_refuses_before_guard(tmp_path: Path) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: True,
    )
    with pytest.raises(CacheError) as caught:
        manager.begin(eligibility, max_bytes=8192)
    assert caught.value.code == "cache_journal_protected"
    assert client.operations == {}


def test_guard_recovery_crosses_pages_and_wraps_for_new_low_version(
    tmp_path: Path,
) -> None:
    cache = VerifiedVersionCache(
        tmp_path / "version-cache", max_bytes=128 * 1024, low_watermark_bytes=0
    )
    lifecycle = CacheLifecycleStore(cache.root)
    lifecycle.bind_owner(7)
    client = FakeGuardClient()
    for version_id in range(10, 15):
        _add_ready(cache, 11, version_id)
        operation_id = uuid.uuid4()
        client.acquire_cache_guard(
            7,
            adapter_id=11,
            version_id=version_id,
            operation_id=operation_id,
        )
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
    )
    assert manager.recover_round(max_items=2, max_pages=1, page_size=2) == 2
    assert manager.recover_round(max_items=2, max_pages=1, page_size=2) == 2
    assert manager.recover_round(max_items=2, max_pages=1, page_size=2) == 1
    assert all(operation["phase"] == "aborted" for operation in client.operations.values())

    _add_ready(cache, 11, 3)
    low_operation = uuid.uuid4()
    client.acquire_cache_guard(7, adapter_id=11, version_id=3, operation_id=low_operation)
    assert manager.recover_round(max_items=2, max_pages=1, page_size=2) == 1
    assert client.operations[low_operation]["phase"] == "aborted"


def test_rename_failure_keeps_entry_and_recovery_uses_same_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_replace = deletion_module.os.replace

    def fail_entry_rename(source: object, destination: object) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise OSError("injected rename failure")
        original_replace(source, destination)

    monkeypatch.setattr(deletion_module.os, "replace", fail_entry_rename)
    result = manager.begin(eligibility, max_bytes=8192)
    assert result.reason == "cache_delete_failed"
    assert cache.entry_path("11-13").exists()
    assert client.operations[result.operation_id]["phase"] == "acquired"

    monkeypatch.setattr(deletion_module.os, "replace", original_replace)
    manager.recover_round(max_items=1, max_pages=0, max_bytes=8192)
    assert client.operations[result.operation_id]["phase"] == "completed"


def test_directory_fsync_failure_after_rename_is_resumed_from_exact_trash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_sync = deletion_module._sync_directory
    failed = False

    def fail_once(path: Path, *, strict: bool = False) -> None:
        nonlocal failed
        if path == cache.entries and strict and not failed:
            failed = True
            raise CacheError("cache_lifecycle_sync_failed")
        original_sync(path, strict=strict)

    monkeypatch.setattr(deletion_module, "_sync_directory", fail_once)
    result = manager.begin(eligibility, max_bytes=8192)
    assert result.reason == "cache_lifecycle_sync_failed"
    assert not cache.entry_path("11-13").exists()
    assert (cache.entries / f".dlr-trash-{result.operation_id}").exists()
    assert client.operations[result.operation_id]["phase"] == "acquired"

    monkeypatch.setattr(deletion_module, "_sync_directory", original_sync)
    manager.recover_round(max_items=1, max_pages=0, max_bytes=8192)
    assert client.operations[result.operation_id]["phase"] == "completed"


def test_record_fsync_failure_never_starts_destructive_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_sync = lifecycle_module._sync_directory

    def fail_record_sync(path: Path, *, strict: bool = False) -> None:
        if path == lifecycle.deletion_root and strict:
            raise CacheError("cache_lifecycle_sync_failed")
        original_sync(path, strict=strict)

    monkeypatch.setattr(lifecycle_module, "_sync_directory", fail_record_sync)
    with pytest.raises(CacheError) as caught:
        manager.begin(eligibility, max_bytes=8192)
    assert caught.value.code == "cache_lifecycle_sync_failed"
    assert cache.entry_path("11-13").exists()
    assert next(iter(client.operations.values()))["phase"] == "acquired"


def test_crash_after_trash_rmdir_replays_only_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_write = deletion_module._write_record

    def crash_before_receipt(path: Path, value: dict[str, Any]) -> None:
        if value["phase"] == "receipt_pending":
            raise SystemExit("injected crash")
        original_write(path, value)

    monkeypatch.setattr(deletion_module, "_write_record", crash_before_receipt)
    with pytest.raises(SystemExit):
        manager.begin(eligibility, max_bytes=8192)
    operation_id = next(iter(client.operations))
    record_path = lifecycle.deletion_root / f"{operation_id}.json"
    assert json.loads(record_path.read_text(encoding="ascii"))["phase"] == "empty_trash"
    assert not (cache.entries / f".dlr-trash-{operation_id}").exists()
    assert client.operations[operation_id]["phase"] == "acquired"

    monkeypatch.setattr(deletion_module, "_write_record", original_write)
    manager.recover_round(max_items=1, max_pages=0)
    assert client.operations[operation_id]["phase"] == "completed"


def test_new_pin_after_acquire_prevents_recorded_operation_from_renaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_replace = deletion_module.os.replace

    def fail_entry_rename(source: object, destination: object) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise OSError("pause before rename")
        original_replace(source, destination)

    monkeypatch.setattr(deletion_module.os, "replace", fail_entry_rename)
    result = manager.begin(eligibility, max_bytes=8192)
    sidecar_path = lifecycle.lifecycle_root / "11-13.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="ascii"))
    sidecar["pinned"] = True
    sidecar_path.write_text(json.dumps(sidecar), encoding="ascii")
    sidecar_path.chmod(0o600)
    monkeypatch.setattr(deletion_module.os, "replace", original_replace)

    manager.recover_round(max_items=1, max_pages=0, max_bytes=8192)
    record = json.loads(
        (lifecycle.deletion_root / f"{result.operation_id}.json").read_text(encoding="ascii")
    )
    assert record["last_error"] == "cache_pinned"
    assert cache.entry_path("11-13").exists()
    assert client.operations[result.operation_id]["phase"] == "acquired"


def test_candidate_verification_budget_exhaustion_never_acquires_guard(
    tmp_path: Path,
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
    )
    with pytest.raises(CacheError) as caught:
        manager.begin(eligibility, max_scan_nodes=1, max_bytes=8192)
    assert caught.value.code == "cache_scan_budget_exhausted"
    assert cache.entry_path("11-13").exists()
    assert client.operations == {}


def test_corrupt_record_phase_is_reported_without_stopping_unsafely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_replace = deletion_module.os.replace

    def fail_entry_rename(source: object, destination: object) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise OSError("pause before rename")
        original_replace(source, destination)

    monkeypatch.setattr(deletion_module.os, "replace", fail_entry_rename)
    result = manager.begin(eligibility, max_bytes=8192)
    record_path = lifecycle.deletion_root / f"{result.operation_id}.json"
    record = json.loads(record_path.read_text(encoding="ascii"))
    record["phase"] = []
    record_path.write_text(json.dumps(record), encoding="ascii")
    record_path.chmod(0o600)

    with pytest.raises(CacheError) as caught:
        manager.recover_round(max_items=1, max_pages=0)
    assert caught.value.code == "cache_deletion_record_invalid"
    assert cache.entry_path("11-13").exists()
    assert client.operations[result.operation_id]["phase"] == "acquired"


def test_corrupt_recovery_cursor_field_is_reported_stably(tmp_path: Path) -> None:
    cache, lifecycle, _eligibility = _ready(tmp_path)
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        FakeGuardClient(),
        worker_id=7,
        journal_protected=lambda _key: False,
    )
    cursor_path = lifecycle.state_root / "deletion-recovery-cursor.json"
    cursor_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "local_after_operation_id": [],
                "after_version_id": None,
            }
        ),
        encoding="ascii",
    )
    cursor_path.chmod(0o600)

    with pytest.raises(CacheError) as caught:
        manager.recover_round(max_items=1, max_pages=0)
    assert caught.value.code == "cache_deletion_cursor_invalid"


def _recorded_before_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[
    VerifiedVersionCache,
    CacheLifecycleStore,
    FakeGuardClient,
    CacheDeletionManager,
    uuid.UUID,
    Path,
]:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_replace = deletion_module.os.replace

    def crash_before_rename(source: object, destination: object) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise SystemExit("crash before rename")
        original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(deletion_module.os, "replace", crash_before_rename)
        with pytest.raises(SystemExit):
            manager.begin(eligibility, max_bytes=8192)
    operation_id = next(iter(client.operations))
    record_path = lifecycle.deletion_root / f"{operation_id}.json"
    assert json.loads(record_path.read_text(encoding="ascii"))["phase"] == "recorded"
    return cache, lifecycle, client, manager, operation_id, record_path


def test_scan_budget_exhaustion_does_not_consume_failure_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cache, _lifecycle, _client, manager, _operation_id, record_path = _recorded_before_rename(
        monkeypatch, tmp_path
    )
    for _ in range(3):
        manager.recover_round(max_pages=0, max_scan_nodes=1)
    record = json.loads(record_path.read_text(encoding="ascii"))
    assert record["attempts"] == 0
    assert record["phase"] == "recorded"


def test_recorded_recovery_rejects_replaced_trash_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    original_sync = deletion_module._sync_directory

    def crash_after_rename(path: Path, *, strict: bool = False) -> None:
        if path == cache.entries:
            raise SystemExit("crash after rename")
        original_sync(path, strict=strict)

    with monkeypatch.context() as patch:
        patch.setattr(deletion_module, "_sync_directory", crash_after_rename)
        with pytest.raises(SystemExit):
            manager.begin(eligibility, max_bytes=8192)
    operation_id = next(iter(client.operations))
    trash = cache.entries / f".dlr-trash-{operation_id}"
    os.replace(trash, cache.entries / "preserved-original")
    _add_ready(cache, 21, 22)
    os.replace(cache.entry_path("21-22"), trash)
    foreign = trash / "payload.bin"

    manager.recover_round(max_pages=0)
    assert foreign.read_bytes() == b"ready"
    assert client.operations[operation_id]["phase"] == "acquired"


def test_rename_and_accounting_snapshot_have_no_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, _lifecycle, _eligibility = _ready(tmp_path)
    source = cache.entry_path("11-13")
    before = cache._committed_bytes()
    original_rglob = Path.rglob
    moved = False

    def move_before_walk(path: Path, pattern: str) -> Any:
        nonlocal moved
        if path == source and not moved:
            moved = True
            os.replace(source, cache.entries / f".dlr-trash-{uuid.uuid4()}")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", move_before_walk)
    assert cache._committed_bytes() == before


def test_recovery_round_shares_delete_budget_across_records(tmp_path: Path) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )
    manager.begin(eligibility, max_nodes=1, max_bytes=8192)
    _add_ready(cache, 11, 14)
    facts = lifecycle.lifecycle("11-14")
    lifecycle.confirm_rebuildability(
        "11-14",
        identity=facts["identity"],
        digest=facts["digest"],
        source_policy="verified_offline",
        evidence_note="test material",
        actor="test",
        valid_until=time.time() + 86_400,
    )
    manager.begin(
        DeletionEligibility(
            adapter_id=11,
            version_id=14,
            identity=facts["identity"],
            digest=facts["digest"],
            last_used_before=facts["last_used_at"],
        ),
        max_nodes=1,
        max_bytes=8192,
    )
    before = sum(1 for _item in cache.entries.rglob("*"))
    manager.recover_round(max_items=2, max_pages=0, max_nodes=1, max_bytes=8192)
    after = sum(1 for _item in cache.entries.rglob("*"))
    assert before - after <= 1


def test_failed_receipt_retains_exact_resume_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
    )

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise ControlUnavailableError("receipt network failed")

    monkeypatch.setattr(client, "finish_cache_guard", unavailable)
    result = manager.begin(eligibility, max_bytes=8192)
    record_path = lifecycle.deletion_root / f"{result.operation_id}.json"
    manager.recover_round(max_pages=0)
    manager.recover_round(max_pages=0)
    record = json.loads(record_path.read_text(encoding="ascii"))
    assert record["phase"] == "failed"
    assert record["resume_phase"] == "receipt_pending"


def test_orphan_scan_budget_deferral_advances_persisted_cursor(tmp_path: Path) -> None:
    cache, lifecycle, _eligibility = _ready(tmp_path)
    _add_ready(cache, 11, 14)
    client = FakeGuardClient()
    first = uuid.uuid4()
    second = uuid.uuid4()
    client.acquire_cache_guard(7, adapter_id=11, version_id=13, operation_id=first)
    client.acquire_cache_guard(7, adapter_id=11, version_id=14, operation_id=second)
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
    )

    manager.recover_round(max_items=1, max_pages=1, page_size=1, max_scan_nodes=3)
    cursor = json.loads(
        (lifecycle.state_root / "deletion-recovery-cursor.json").read_text(encoding="ascii")
    )
    assert cursor["after_version_id"] == 13
    manager.recover_round(max_items=1, max_pages=1, page_size=1, max_scan_nodes=3)
    assert client.operations[first]["phase"] == "acquired"
    assert client.operations[second]["phase"] == "aborted"


def test_successful_unlink_spends_budget_before_directory_fsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"spent")
    budget = deletion_module._DeleteBudget(
        nodes=1,
        bytes=100,
        deadline=time.monotonic() + 5,
    )

    def fail_sync(_path: Path, *, strict: bool = False) -> None:
        assert strict
        raise CacheError("cache_lifecycle_sync_failed")

    monkeypatch.setattr(deletion_module, "_sync_directory", fail_sync)
    with pytest.raises(CacheError):
        deletion_module._delete_some(payload, budget)
    assert not payload.exists()
    assert budget.removed_nodes == 1
    assert budget.removed_bytes == len(b"spent")


def test_rebuild_proof_expiry_after_guard_prevents_recorded_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache, lifecycle, eligibility = _ready(tmp_path)
    client = FakeGuardClient()
    now = [time.time()]
    manager = CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        retry_seconds=0,
        clock=lambda: now[0],
    )
    original_replace = deletion_module.os.replace

    def fail_entry_rename(source: object, destination: object) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise OSError("pause before rename")
        original_replace(source, destination)

    monkeypatch.setattr(deletion_module.os, "replace", fail_entry_rename)
    result = manager.begin(eligibility, max_bytes=8192)
    now[0] += 86_401
    monkeypatch.setattr(deletion_module.os, "replace", original_replace)

    manager.recover_round(max_items=1, max_pages=0, max_bytes=8192)
    record = json.loads(
        (lifecycle.deletion_root / f"{result.operation_id}.json").read_text(encoding="ascii")
    )
    assert record["last_error"] == "cache_rebuild_unknown"
    assert cache.entry_path("11-13").exists()
