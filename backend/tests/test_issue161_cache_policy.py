from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dlr.worker.cache_deletion import CacheDeletionManager
from dlr.worker.cache_governance import CachePolicyManager
from dlr.worker.cache_lifecycle import CacheLifecycleStore
from dlr.worker.cache_policy import (
    CachePolicy,
    GovernedVersionCache,
    managed_source_scope,
    register_pressure_handler,
)


class _UnusedClient:
    def __init__(self) -> None:
        self.operations: dict[uuid.UUID, dict[str, Any]] = {}

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

    def acquire_cache_guard(
        self,
        worker_id: int,
        *,
        adapter_id: int,
        version_id: int,
        operation_id: uuid.UUID,
        **_kwargs: object,
    ) -> dict[str, Any]:
        operation = {
            "worker_id": worker_id,
            "adapter_id": adapter_id,
            "version_id": version_id,
            "operation_id": str(operation_id),
            "generation": 1,
            "phase": "acquired",
        }
        self.operations[operation_id] = operation
        return dict(operation)

    def check_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        **_kwargs: object,
    ) -> dict[str, Any]:
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
        **_kwargs: object,
    ) -> dict[str, Any]:
        operation = self.operations[operation_id]
        assert operation["worker_id"] == worker_id
        assert operation["generation"] == generation
        operation["phase"] = outcome
        return dict(operation)


def _ready(
    cache: GovernedVersionCache,
    lifecycle: CacheLifecycleStore,
    *,
    adapter_id: int = 11,
    version_id: int = 13,
    payload: bytes = b"runtime",
) -> tuple[dict[str, Any], str]:
    key = f"{adapter_id}-{version_id}"
    identity = {
        "adapter_id": adapter_id,
        "version_id": version_id,
        "language": "python",
        "source_sha256": "a" * 64,
    }
    reservation = cache.reserve(4096)
    staging = cache.staging_path(key, reservation.token)
    staging.mkdir()
    (staging / "payload.bin").write_bytes(payload)
    entry = cache.promote(
        staging, cache.entry_path(key), identity=identity, reservation=reservation
    )
    manifest = json.loads((entry / ".dlr-cache-manifest.json").read_text(encoding="ascii"))
    return identity, str(manifest["digest"])


def _manager(
    tmp_path: Path, policy: CachePolicy
) -> tuple[GovernedVersionCache, CacheLifecycleStore, CachePolicyManager]:
    cache = GovernedVersionCache(tmp_path / "version-cache", policy=policy)
    lifecycle = CacheLifecycleStore(cache.root)
    lifecycle.bind_owner(7)
    deletion = CacheDeletionManager(
        cache,
        lifecycle,
        _UnusedClient(),  # type: ignore[arg-type]
        worker_id=7,
        journal_protected=lambda _key: False,
        offline_protection=policy.offline_protection,
        offline_mode=policy.offline_mode,
    )
    return cache, lifecycle, CachePolicyManager(tmp_path, cache, lifecycle, deletion, policy)


def test_policy_defaults_and_invalid_cross_field_values() -> None:
    policy = CachePolicy.from_environment({})
    assert policy.gc_enabled is False
    assert policy.pressure_gc_enabled is False
    assert policy.max_bytes == 4_294_967_296
    assert policy.shared_cache_mode == "report_only"

    with pytest.raises(ValueError):
        CachePolicy.from_environment({"DLR_CACHE_GC_ENABLED": "sometimes"})
    with pytest.raises(ValueError):
        CachePolicy.from_environment(
            {
                "DLR_CACHE_LOW_WATERMARK_PERCENT": "90",
                "DLR_CACHE_HIGH_WATERMARK_PERCENT": "80",
            }
        )
    with pytest.raises(ValueError):
        CachePolicy.from_environment(
            {"DLR_CACHE_MAX_BYTES": "1024", "DLR_CACHE_DISK_RESERVE_BYTES": "1024"}
        )


def test_preview_missing_control_item_is_retained(tmp_path: Path) -> None:
    cache, lifecycle, manager = _manager(
        tmp_path,
        replace(CachePolicy(), max_bytes=1_048_576, disk_reserve_bytes=0, min_idle_seconds=0),
    )
    identity, digest = _ready(cache, lifecycle)
    now = time.time()
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="local materials",
        actor="admin",
        now=now,
        valid_until=now + 300,
    )

    class MissingClient:
        def resolve_cache_key_references(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            return {
                "kind": "cache_keys_v1",
                "worker_id": 7,
                "sampled_at": now,
                "complete": False,
                "items": [],
            }

    manager.deletion.client = MissingClient()  # type: ignore[assignment]
    report = manager.scan(mode="manual")
    assert report.candidates == ()
    assert report.categories["versions"]["reclaimable_bytes"] == 0
    assert report.retained_reasons == {"cache_reference_unknown": 1}


def test_builtin_proof_requires_declared_persistent_complete_materials(tmp_path: Path) -> None:
    import hashlib

    from dlr.worker import venv
    from dlr.worker.builtin_packages import BuiltinMaterials

    content = b"immutable-wheel"
    snapshot = {
        "files": [
            {
                "repository_path": "demo.whl",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
    }
    directory = tmp_path / "materials"
    directory.mkdir()
    (directory / "demo.whl").write_bytes(content)
    unknown = BuiltinMaterials(snapshot, directory, lambda *_args: 0)
    assert not venv.builtin_rebuildability_verified(unknown, external_dependencies_present=True)
    persistent = BuiltinMaterials(
        snapshot, directory, lambda *_args: 0, lifecycle="managed_persistent"
    )
    assert venv.builtin_rebuildability_verified(persistent, external_dependencies_present=True)
    (directory / "demo.whl").write_bytes(b"changed")
    assert not venv.builtin_rebuildability_verified(persistent, external_dependencies_present=True)
    empty = BuiltinMaterials({"files": []}, tmp_path / "unused", lambda *_args: 0)
    assert venv.builtin_rebuildability_verified(empty, external_dependencies_present=False)
    assert not venv.builtin_rebuildability_verified(empty, external_dependencies_present=True)


def test_observed_material_loss_revokes_only_automatic_proof(tmp_path: Path) -> None:
    cache, lifecycle, _manager_value = _manager(
        tmp_path, replace(CachePolicy(), max_bytes=1_048_576, disk_reserve_bytes=0)
    )
    identity, digest = _ready(cache, lifecycle)
    now = time.time()
    for automatic, expected in ((True, "unknown"), (False, "confirmed")):
        lifecycle.confirm_rebuildability(
            "11-13",
            identity=identity,
            digest=digest,
            source_policy="verified_offline",
            evidence_note="verified material",
            actor="platform" if automatic else "admin",
            now=now,
            valid_until=now + 300,
            automatic=automatic,
        )
        lifecycle.invalidate_automatic_rebuildability("11-13", identity=identity, digest=digest)
        assert lifecycle.lifecycle("11-13")["rebuildability"] == expected


def test_tail_page_is_never_a_complete_capacity_snapshot(tmp_path: Path) -> None:
    cache, lifecycle, manager = _manager(
        tmp_path,
        replace(
            CachePolicy(),
            max_bytes=1_048_576,
            disk_reserve_bytes=0,
            min_idle_seconds=0,
            max_scan_entries_per_round=1,
        ),
    )
    for version in (13, 14):
        identity, digest = _ready(cache, lifecycle, version_id=version)
        now = time.time()
        lifecycle.confirm_rebuildability(
            f"11-{version}",
            identity=identity,
            digest=digest,
            source_policy="verified_offline",
            evidence_note="local materials",
            actor="admin",
            now=now,
            valid_until=now + 300,
        )
    assert manager.snapshot()["complete"] is False
    assert manager.snapshot()["complete"] is False


def test_active_staging_is_reported_without_double_capacity_charge(tmp_path: Path) -> None:
    cache, _lifecycle, manager = _manager(
        tmp_path, replace(CachePolicy(), max_bytes=1_048_576, disk_reserve_bytes=0)
    )
    reservation = cache.reserve(4096)
    staging = cache.staging_path("11-13", reservation.token)
    staging.mkdir()
    (staging / "payload").write_bytes(b"x" * 1000)
    report = manager.scan(mode="pressure")
    authoritative = cache.accounting_snapshot()
    assert report.categories["staging"]["bytes"] == 1000
    assert report.committed_bytes + report.reserved_bytes == (
        authoritative["committed_bytes"] + authoritative["reserved_bytes"]
    )
    assert report.observations == (
        {
            "cache_key": "11-13",
            "kind": "staging",
            "adapter_id": 11,
            "version_id": 13,
            "identity": None,
            "digest": None,
            "bytes": 1000,
            "pinned": False,
            "rebuildability": "unknown",
            "reasons": ["cache_staging_active"],
        },
    )
    manager.cursor_path.write_text(
        json.dumps({"schema": 1, "after_name": "99-99"}), encoding="ascii"
    )
    selected = manager.scan(mode="manual", target_keys=frozenset({"11-13"}))
    assert selected.complete is True
    assert selected.observations[0]["kind"] == "staging"
    assert json.loads(manager.cursor_path.read_text(encoding="ascii"))["after_name"] == "99-99"


def test_pin_expiry_material_drift_offline_and_same_source_failure(tmp_path: Path) -> None:
    policy = replace(CachePolicy(), max_bytes=1_048_576, disk_reserve_bytes=0)
    cache, lifecycle, _ = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle)
    now = time.time()
    scope = managed_source_scope("python", "https://user:secret@example.invalid/simple")
    assert scope == managed_source_scope("python", "https://other@example.invalid/simple?token=x")

    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now,
        )
        == "cache_rebuild_unknown"
    )

    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="managed_online",
        source_scope=scope,
        evidence_note="managed source receipt",
        actor="admin:7",
        now=now,
        valid_until=now + 300,
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now + 1,
        )
        is None
    )
    lifecycle.record_source_scope_unavailable(
        source_scope=managed_source_scope("python", None),
        error_code="dependency_source_unavailable",
        now=now + 2,
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now + 3,
        )
        == "cache_rebuild_unavailable"
    )
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="managed_online",
        source_scope=scope,
        evidence_note="source rechecked",
        actor="admin:7",
        now=now + 4,
        valid_until=now + 604,
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=True,
            now=now + 5,
        )
        == "cache_rebuild_unavailable"
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now + 605,
        )
        == "cache_rebuild_unknown"
    )

    lifecycle.record_source_scope_unavailable(
        source_scope=scope, error_code="dependency_source_unavailable", now=now + 6
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now + 7,
        )
        == "cache_rebuild_unavailable"
    )
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="managed_online",
        source_scope=scope,
        evidence_note="source rechecked",
        actor="admin:7",
        now=now + 8,
        valid_until=now + 608,
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=True,
            offline_mode=False,
            now=now + 9,
        )
        is None
    )
    lifecycle.set_pin(
        "11-13",
        identity=identity,
        digest=digest,
        pinned=True,
        actor="admin:7",
        reason="retain",
        now=now + 6,
    )
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity=identity,
            digest=digest,
            offline_protection=False,
            offline_mode=False,
            now=now + 7,
        )
        == "cache_pinned"
    )
    stored = lifecycle.lifecycle("11-13")
    assert "secret" not in json.dumps(stored)
    assert (
        lifecycle.reclamation_reason(
            "11-13",
            identity={**identity, "source_sha256": "b" * 64},
            digest=digest,
            offline_protection=False,
            offline_mode=False,
            now=now + 7,
        )
        == "cache_identity_conflict"
    )


def test_classified_scan_is_report_only_for_shared_and_retains_unknown(tmp_path: Path) -> None:
    policy = replace(
        CachePolicy(),
        max_bytes=1_048_576,
        disk_reserve_bytes=0,
        min_idle_seconds=0,
        idle_ttl_seconds=60,
        max_scan_entries_per_round=20,
    )
    cache, lifecycle, manager = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle)
    now = time.time()
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="local wheelhouse",
        actor="admin",
        now=now,
        valid_until=now + 3600,
    )
    shared = tmp_path / "shared-cache" / "uv"
    shared.mkdir(parents=True)
    (shared / "download.whl").write_bytes(b"shared")
    unknown = cache.entries / ".legacy-unknown"
    unknown.mkdir()
    (unknown / "data").write_bytes(b"unknown")
    stale = cache.entries / ".11-14.staging-expired"
    stale.mkdir()
    (stale / "partial").write_bytes(b"partial")
    old = now - policy.staging_ttl_seconds - 1
    stale.touch()
    import os

    os.utime(stale, (old, old))

    report = manager.scan(mode="manual")
    assert [item.key for item in report.candidates] == ["11-13"]
    assert report.categories["shared"]["bytes"] == len(b"shared")
    assert report.categories["shared"]["reclaimable_bytes"] == 0
    assert report.categories["shared"]["reasons"] == {"shared_cache_not_supported": 1}
    assert report.categories["unknown"]["reasons"] == {"cache_ownership_unknown": 1}
    assert report.categories["staging"]["reclaimable_bytes"] == len(b"partial")
    assert report.inactive_staging == ((stale, "11-14", "expired"),)
    snapshot = manager.snapshot()
    assert snapshot["policy"]["shared_cache_mode"] == "report_only"
    assert "active_reservation_tokens" not in snapshot["accounting"]
    assert str(tmp_path) not in json.dumps(snapshot)


def test_pressure_reservation_runs_one_cleanup_and_one_retry(tmp_path: Path) -> None:
    policy = replace(CachePolicy(), max_bytes=8192, disk_reserve_bytes=0, pressure_gc_enabled=True)
    cache = GovernedVersionCache(tmp_path / "version-cache", policy=policy)
    first = cache.reserve(7000)
    calls: list[int] = []

    def cleanup(amount: int) -> bool:
        calls.append(amount)
        first.release()
        return True

    register_pressure_handler(cache.root, cleanup)
    try:
        second = cache.reserve(4096)
        assert calls == [4096]
        second.release()
    finally:
        register_pressure_handler(cache.root, None)


def test_pressure_reservation_reclaims_below_low_watermark_until_request_fits(
    tmp_path: Path,
) -> None:
    policy = replace(
        CachePolicy(),
        max_bytes=8192,
        disk_reserve_bytes=0,
        min_idle_seconds=0,
        pressure_gc_enabled=True,
    )
    assert policy.low_watermark_percent == 70
    assert policy.high_watermark_percent == 85
    cache, lifecycle, manager = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle, payload=b"x" * 2048)
    now = time.time()
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="local materials",
        actor="admin",
        now=now,
        valid_until=now + 300,
    )
    before = cache.accounting_snapshot()
    requested = 7000
    low_target = policy.max_bytes * policy.low_watermark_percent // 100
    assert before["committed_bytes"] < low_target
    assert policy.max_bytes - before["committed_bytes"] < requested

    register_pressure_handler(cache.root, manager.pressure_cleanup)
    try:
        reservation = cache.reserve(requested)
        assert not cache.entry_path("11-13").exists()
        assert reservation.amount == requested
        reservation.release()
    finally:
        register_pressure_handler(cache.root, None)


def test_background_pressure_reclaims_below_low_watermark_until_disk_reserve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = replace(
        CachePolicy(),
        max_bytes=8192,
        disk_reserve_bytes=4000,
        min_idle_seconds=0,
        pressure_gc_enabled=True,
    )
    assert policy.low_watermark_percent == 70
    assert policy.high_watermark_percent == 85
    cache, lifecycle, manager = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle, payload=b"x" * 2048)
    now = time.time()
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="local materials",
        actor="admin",
        now=now,
        valid_until=now + 300,
    )
    before = cache.accounting_snapshot()
    low_target = policy.max_bytes * policy.low_watermark_percent // 100
    assert before["committed_bytes"] < low_target
    disk_free = 3000
    assert disk_free < policy.disk_reserve_bytes
    monkeypatch.setattr(
        "dlr.worker.cache_governance.shutil.disk_usage",
        lambda _path: type("DiskUsage", (), {"free": disk_free})(),
    )

    result = manager.run_round(mode="pressure")

    assert result.deleted == 1
    assert result.freed_bytes >= policy.disk_reserve_bytes - disk_free
    assert not cache.entry_path("11-13").exists()


def test_standard_compose_passes_policy_environment() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    for name in (
        "DLR_CACHE_GC_ENABLED",
        "DLR_CACHE_PRESSURE_GC_ENABLED",
        "DLR_CACHE_MAX_BYTES",
        "DLR_CACHE_MAX_SCAN_DEPTH",
        "DLR_CACHE_SHARED_CACHE_MODE",
    ):
        assert f"{name}: ${{{name}" in compose


def test_scan_cursor_is_stable_and_deep_tree_is_retained(tmp_path: Path) -> None:
    policy = replace(
        CachePolicy(),
        max_bytes=1_048_576,
        disk_reserve_bytes=0,
        max_scan_entries_per_round=2,
        max_scan_nodes_per_round=20,
        max_scan_depth=2,
    )
    cache, _lifecycle, manager = _manager(tmp_path, policy)
    for name in (".unknown-c", ".unknown-a", ".unknown-b"):
        path = cache.entries / name
        path.mkdir()
        (path / "payload").write_bytes(b"x")

    first = manager.scan(mode="manual")
    assert first.complete is False
    assert first.cursor == ".unknown-b"
    second = manager.scan(mode="manual")
    assert second.cursor is None
    assert second.categories["unknown"]["entries"] == 1

    deep = cache.entries / ".unknown-deep"
    (deep / "one" / "two").mkdir(parents=True)
    (deep / "one" / "two" / "payload").write_bytes(b"x")
    manager.scan(mode="manual")
    report = manager.scan(mode="manual")
    assert report.complete is False
    assert report.retained_reasons["cache_scan_budget_exhausted"] == 1


def test_round_to_busy_key_does_not_wait(tmp_path: Path) -> None:
    import threading

    policy = replace(
        CachePolicy(),
        max_bytes=1_048_576,
        disk_reserve_bytes=0,
        min_idle_seconds=0,
        max_round_seconds=1,
    )
    cache, lifecycle, manager = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle)
    now = time.time()
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="local materials",
        actor="admin",
        now=now,
        valid_until=now + 300,
    )
    entered = threading.Event()
    release = threading.Event()

    def hold_key() -> None:
        with lifecycle.entry_lock("11-13"):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold_key)
    thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    result = manager.run_round(mode="manual")
    elapsed = time.monotonic() - started
    release.set()
    thread.join()

    assert elapsed < 0.5
    assert result.deleted == 0
    assert cache.entry_path("11-13").exists()


def test_single_factory_applies_same_policy_to_five_language_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dlr.worker import venv

    monkeypatch.setenv("DLR_CACHE_MAX_BYTES", str(512 * 1024 * 1024))
    monkeypatch.setenv("DLR_CACHE_DISK_RESERVE_BYTES", "0")
    observed: list[int] = []
    for version_id, language in enumerate(
        ("python", "javascript", "typescript", "java", "go"), start=1
    ):
        cache, _target, build = venv._begin_version_build(
            tmp_path,
            11,
            version_id,
            identity={
                "adapter_id": 11,
                "version_id": version_id,
                "language": language,
                "source_sha256": f"{version_id:064x}",
            },
        )
        assert build is not None
        observed.append(cache.max_bytes)
        build.abort()
    assert observed == [512 * 1024 * 1024] * 5
