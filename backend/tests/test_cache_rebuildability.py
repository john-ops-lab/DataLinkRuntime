"""Local automatic rebuild facts must remain true at the deletion boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dlr.worker import cache_rebuild, venv
from dlr.worker.builtin_packages import BuiltinMaterials
from dlr.worker.cache import CacheError, CacheScanBudget
from dlr.worker.cache_deletion import DeletionEligibility
from dlr.worker.cache_policy import CachePolicy, managed_source_scope
from test_issue161_cache_policy import _manager


@pytest.fixture
def local_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    paths = {}
    for command in ("uv", "node", "tsc", "java", "javac", "go", "npm", "mvn"):
        path = tmp_path / f"tool-{command}"
        if command == "tsc":
            path = tmp_path / "typescript" / "bin" / "tsc"
            path.parent.mkdir(parents=True)
            (path.parent.parent / "lib").mkdir()
            (path.parent.parent / "lib" / "tsc.js").write_text("compiler")
            (path.parent.parent / "package.json").write_text('{"version":"5.9.3"}')
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
        paths[command] = path
    monkeypatch.setattr(cache_rebuild.shutil, "which", lambda command: str(paths[command]))
    return paths


def _ready(
    tmp_path: Path, *, requirements: str = "", language: str = "python", source: str | None = None
) -> tuple[Any, Any, Any, dict[str, Any], str]:
    cache, store, manager = _manager(
        tmp_path, replace(CachePolicy(), max_bytes=1_048_576, disk_reserve_bytes=0)
    )
    identity = venv._cache_identity(11, 13, language, requirements if source is None else source)
    reservation = cache.reserve(4096)
    staging = cache.staging_path("11-13", reservation.token)
    staging.mkdir()
    (staging / "requirements.txt").write_text(requirements)
    (staging / "payload.bin").write_bytes(b"runtime")
    if language == "python":
        (staging / ".venv" / "bin").mkdir(parents=True)
        (staging / ".venv" / "bin" / "python").symlink_to(Path(sys.executable).resolve())
    entry = cache.promote(
        staging, cache.entry_path("11-13"), identity=identity, reservation=reservation
    )
    manifest = json.loads((entry / ".dlr-cache-manifest.json").read_text())
    store.touch_used("11-13", now=time.time() - 40 * 86_400)
    return cache, store, manager, identity, manifest["digest"]


def _budget() -> CacheScanBudget:
    return CacheScanBudget(2000, 1_000_000, time.monotonic() + 5)


def _reason(store: Any, identity: dict[str, Any], digest: str) -> str | None:
    return store.reclamation_reason(
        "11-13", identity=identity, digest=digest, offline_protection=True, offline_mode=True
    )


def test_legacy_empty_python_revalidates_after_proof_expiry(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path, requirements="# no packages\n\n")
    first = manager.scan(mode="manual")
    assert [candidate.key for candidate in first.candidates] == ["11-13"]
    original = store.lifecycle("11-13")
    assert original["rebuild_proof"]["automatic"] is True
    assert original["last_used_at"] < time.time() - 30 * 86_400
    future = time.time() + 31 * 86_400
    manager.clock = lambda: future
    assert len(manager.scan(mode="periodic").candidates) == 1
    refreshed = store.lifecycle("11-13")
    assert refreshed["rebuild_proof"]["valid_until"] > future
    assert refreshed["last_used_at"] == original["last_used_at"]
    assert _reason(store, identity, digest) is None


@pytest.mark.parametrize(
    ("requirements", "source"),
    [
        ("-r other.txt", None),
        ("-e ./project", None),
        ("--index-url https://example.invalid", None),
        ("requests==1.0", None),
        ("", "\0dlr-builtin-v1:opaque"),
    ],
)
def test_legacy_unknown_or_external_declarations_stay_protected(
    tmp_path: Path, local_tools: dict[str, Path], requirements: str, source: str | None
) -> None:
    _cache, store, manager, identity, digest = _ready(
        tmp_path, requirements=requirements, source=source
    )
    assert manager.scan(mode="manual").candidates == ()
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"


def test_automatic_refresh_never_replaces_manual_proof_or_source_failure(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path)
    now = time.time()
    store.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="managed_online",
        evidence_note="administrator promise",
        actor="admin",
        now=now,
        valid_until=now + 60,
    )
    manual = store.lifecycle("11-13")["rebuild_proof"]
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    manager.scan(mode="manual")
    assert _reason(store, identity, digest) == "cache_rebuild_unavailable"
    assert store.lifecycle("11-13")["rebuild_proof"] == manual
    store.record_source_unavailable(
        "11-13", identity=identity, digest=digest, error_code="dependency_source_unavailable"
    )
    failure = store.lifecycle("11-13")["source_unavailable"]
    store.invalidate_automatic_rebuildability("11-13", identity=identity, digest=digest)
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    assert store.lifecycle("11-13")["rebuild_proof"] == manual
    assert store.lifecycle("11-13")["source_unavailable"] == failure


@pytest.mark.parametrize("language", ["python", "javascript", "typescript", "java", "go"])
def test_receipts_require_unchanged_present_toolchain(
    tmp_path: Path, local_tools: dict[str, Path], language: str
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path, language=language)
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    assert len(manager.scan(mode="manual").candidates) == 1
    tool = local_tools[
        {"python": "uv", "typescript": "tsc", "java": "javac"}.get(
            language, "go" if language == "go" else "node"
        )
    ]
    tool.write_text("#!/bin/sh\nexit 1\n")
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"
    assert manager.scan(mode="manual").candidates == ()
    assert store.lifecycle("11-13")["rebuildability"] == "unknown"


@pytest.mark.parametrize("failure", ["missing", "hash_budget"])
def test_material_recheck_after_preview_blocks_final_guarded_rename(
    tmp_path: Path, local_tools: dict[str, Path], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    cache, store, manager, identity, digest = _ready(tmp_path, requirements="demo==1")
    directory = tmp_path / "materials"
    directory.mkdir()
    content = b"persistent-package"
    material = directory / "demo.whl"
    material.write_bytes(content)
    bundle = BuiltinMaterials(
        {
            "files": [
                {
                    "repository_path": "demo.whl",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
        },
        directory,
        lambda *_args: 0,
        lifecycle="managed_persistent",
    )
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=True, builtin_materials=bundle
    )
    assert len(manager.scan(mode="manual").candidates) == 1
    guard = manager.deletion._guard
    budget = manager._budget()
    calls = 0

    def remove_before_final_check(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = guard(*args, **kwargs)
        calls += 1
        if calls == 2:
            if failure == "missing":
                material.unlink()
            else:
                budget.scan.hash_bytes_remaining = 0
        return result

    monkeypatch.setattr(manager.deletion, "_guard", remove_before_final_check)
    result = manager.deletion.begin(
        DeletionEligibility(11, 13, identity, digest, time.time()), round_budget=budget
    )
    assert calls == 2
    if failure == "missing":
        assert result.reason == "cache_rebuild_unknown"
    else:
        assert result.reason == "cache_scan_budget_exhausted"
        assert result.status == "in_progress"
        record = json.loads((store.deletion_root / f"{result.operation_id}.json").read_text())
        assert record["attempts"] == 0
    assert cache.entry_path("11-13").exists()


def test_scan_revalidation_never_waits_for_a_preparing_key(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    _cache, store, _manager_value, identity, digest = _ready(tmp_path)
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    original = store.lifecycle("11-13")
    ready, release = threading.Event(), threading.Event()

    def hold() -> None:
        with store.entry_lock("11-13"):
            ready.set()
            release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(CacheError) as busy:
            cache_rebuild.revalidate_for_scan(
                store,
                "11-13",
                identity=identity,
                digest=digest,
                budget=_budget(),
                now=time.time() + 2 * 86_400,
            )
        assert busy.value.code == "cache_lock_busy"
        assert time.monotonic() - started < 0.5
        assert store.lifecycle("11-13") == original
    finally:
        release.set()
        thread.join(2)


def test_receipt_binding_corruption_missing_receipt_and_budget_fail_closed(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    cache, store, manager, identity, digest = _ready(tmp_path)
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    path = cache_rebuild._receipt_path(cache.root, "11-13")
    receipt = json.loads(path.read_text())
    receipt["digest"] = "0" * 64
    path.write_text(json.dumps(receipt))
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"
    path.unlink()
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"
    with pytest.raises(CacheError) as exhausted:
        cache_rebuild.revalidate_for_scan(
            store,
            "11-13",
            identity=identity,
            digest=digest,
            budget=CacheScanBudget(1, 0, time.monotonic() + 3),
            now=time.time(),
        )
    assert exhausted.value.code == "cache_scan_budget_exhausted"
    assert cache.entry_path("11-13").exists()


def test_automatic_refresh_preserves_known_key_and_language_source_failure(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path)
    manager.scan(mode="manual")
    store.record_source_unavailable(
        "11-13", identity=identity, digest=digest, error_code="dependency_source_unavailable"
    )
    failure = store.lifecycle("11-13")["source_unavailable"]
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    assert store.lifecycle("11-13")["source_unavailable"] == failure
    assert _reason(store, identity, digest) == "cache_rebuild_unavailable"
    scope = managed_source_scope("python", None)
    store.record_source_scope_unavailable(
        source_scope=scope, error_code="dependency_source_unavailable"
    )
    marker = (
        store.state_root / f"source-unavailable-{hashlib.sha256(scope.encode()).hexdigest()}.json"
    )
    before = marker.read_bytes()
    manager.clock = lambda: time.time() + 60
    manager.scan(mode="manual")
    assert marker.read_bytes() == before
    assert _reason(store, identity, digest) == "cache_rebuild_unavailable"


def test_legacy_python_with_other_or_missing_interpreter_stays_unknown(
    tmp_path: Path, local_tools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path)
    other = tmp_path / "other-python"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o700)
    monkeypatch.setattr(cache_rebuild.sys, "executable", str(other))
    assert manager.scan(mode="manual").candidates == ()
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"
    local_tools["uv"].unlink()
    assert manager.scan(mode="manual").candidates == ()


def test_auto_receipt_keeps_pin_and_active_reference_protection(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path)
    manager.scan(mode="manual")
    store.set_pin(
        "11-13", identity=identity, digest=digest, pinned=True, actor="admin", reason="keep"
    )
    assert manager.scan(mode="manual").retained_reasons == {"cache_pinned": 1}
    store.set_pin(
        "11-13", identity=identity, digest=digest, pinned=False, actor="admin", reason="release"
    )
    manager.deletion.journal_protected = lambda _key: True
    assert manager.scan(mode="manual").candidates == ()


@pytest.mark.parametrize("kind", ["symlink", "oversize", "unknown_field"])
def test_invalid_receipt_never_removes_cache(
    tmp_path: Path, local_tools: dict[str, Path], kind: str
) -> None:
    cache, store, manager, identity, digest = _ready(tmp_path)
    venv.reconcile_local_rebuildability(
        tmp_path, identity=identity, external_dependencies_present=False
    )
    path = cache_rebuild._receipt_path(cache.root, "11-13")
    if kind == "symlink":
        original = path.read_bytes()
        target = tmp_path / "other-receipt.json"
        target.write_bytes(original)
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    elif kind == "oversize":
        path.write_text(" " * (cache_rebuild._RECEIPT_LIMIT + 1))
    else:
        receipt = json.loads(path.read_text())
        receipt["ignore_protection"] = True
        path.write_text(json.dumps(receipt))
    assert _reason(store, identity, digest) == "cache_rebuild_unknown"
    assert manager.scan(mode="manual").candidates == ()
    assert cache.entry_path("11-13").exists()


def test_real_uv_can_rebuild_same_version_after_two_completed_cleanups(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("real uv toolchain is unavailable")
    cache, store, manager = _manager(
        tmp_path, replace(CachePolicy(), max_bytes=1024**3, disk_reserve_bytes=0)
    )
    digests = []
    for cycle in range(3):
        python = venv.prepare_version_venv(tmp_path, 11, 13, "", timeout_seconds=30)
        output = subprocess.run(
            [str(python), "-I", "-c", "print(6 * 7)"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert output.stdout.strip() == "42"
        facts = store.lifecycle("11-13")
        manifest = cache.manifest_identity(cache.entry_path("11-13"))
        assert manifest == (facts["identity"], facts["digest"])
        digests.append(facts["digest"])
        if cycle < 2:
            store.touch_used("11-13", now=time.time() - 2 * 86_400)
            expired = store.lifecycle("11-13")
            proof_now = time.time()
            expired["rebuild_proof"]["confirmed_at"] = proof_now - 2 * 86_400
            expired["rebuild_proof"]["valid_until"] = proof_now - 86_400
            (store.lifecycle_root / "11-13.json").write_text(json.dumps(expired))
            assert (
                len(manager.scan(mode="manual", target_keys=frozenset({"11-13"})).candidates) == 1
            )
            result = manager.run_round(mode="manual", target_keys=frozenset({"11-13"}))
            assert result.deleted == 1
            assert not cache.entry_path("11-13").exists()
            assert not (store.lifecycle_root / "11-13.json").exists()
    assert len(set(digests)) > 1  # uv records the distinct staging path in pyvenv.cfg.


def test_retired_instance_recovers_ack_without_old_lifecycle(
    tmp_path: Path, local_tools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, store, manager, identity, digest = _ready(tmp_path)
    manager.scan(mode="manual")
    client = manager.deletion.client
    finish = client.finish_cache_guard

    def crash_before_ack(*args: Any, **kwargs: Any) -> Any:
        assert not cache.entry_path("11-13").exists()
        assert not (store.lifecycle_root / "11-13.json").exists()
        raise SystemExit("controlled crash before completion acknowledgement")

    monkeypatch.setattr(client, "finish_cache_guard", crash_before_ack)
    with pytest.raises(SystemExit):
        manager.deletion.begin(DeletionEligibility(11, 13, identity, digest, time.time()))
    records = list(store.deletion_root.glob("*.json"))
    assert len(records) == 1
    before = json.loads(records[0].read_text())
    assert before["phase"] == "receipt_pending"
    monkeypatch.setattr(client, "finish_cache_guard", finish)
    manager.deletion.recover_round(max_pages=0)
    after = json.loads(records[0].read_text())
    assert after["phase"] == "completed"
    assert after["attempts"] == 0
    assert not cache.entry_path("11-13").exists()


def test_receipt_rejects_new_manifest_against_historical_sidecar(
    tmp_path: Path, local_tools: dict[str, Path]
) -> None:
    cache, store, _manager_value, identity, _digest = _ready(tmp_path)
    path = store.lifecycle_root / "11-13.json"
    historical = store.lifecycle("11-13")
    historical["digest"] = "0" * 64
    path.write_text(json.dumps(historical))
    with pytest.raises(CacheError) as conflict:
        cache_rebuild.record_rebuild_receipt(
            tmp_path, identity=identity, external_dependencies_present=False
        )
    assert conflict.value.code == "cache_identity_conflict"
    assert not cache_rebuild._receipt_path(cache.root, "11-13").exists()
    assert store.lifecycle("11-13")["digest"] == "0" * 64


@pytest.mark.parametrize("protection", ["pin", "digest", "manual_proof"])
def test_instance_retirement_never_erases_newer_or_conflicting_protection(
    tmp_path: Path, local_tools: dict[str, Path], monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    _cache, store, manager, identity, digest = _ready(tmp_path)
    manager.scan(mode="manual")
    retire = manager.deletion._retire_deleted_instance

    def change_protection(record: dict[str, Any]) -> None:
        if protection == "pin":
            store.set_pin(
                "11-13", identity=identity, digest=digest, pinned=True, actor="admin", reason="keep"
            )
        elif protection == "manual_proof":
            now = time.time()
            store.confirm_rebuildability(
                "11-13",
                identity=identity,
                digest=digest,
                source_policy="verified_offline",
                evidence_note="new administrator promise",
                actor="admin",
                now=now,
                valid_until=now + 60,
            )
        else:
            facts = store.lifecycle("11-13")
            facts["digest"] = "0" * 64
            (store.lifecycle_root / "11-13.json").write_text(json.dumps(facts))
        retire(record)

    monkeypatch.setattr(manager.deletion, "_retire_deleted_instance", change_protection)
    result = manager.deletion.begin(DeletionEligibility(11, 13, identity, digest, time.time()))
    assert result.reason == "cache_identity_conflict"
    assert (store.lifecycle_root / "11-13.json").exists()
    facts = store.lifecycle("11-13")
    if protection == "pin":
        assert facts["pinned"] is True
    elif protection == "manual_proof":
        assert facts["rebuild_proof"]["automatic"] is False
    else:
        assert facts["digest"] == "0" * 64
