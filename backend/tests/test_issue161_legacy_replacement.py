"""Issue #161 legacy cleanup and Attempt-bound cache replacement."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dlr.worker.cache_deletion as deletion_module
from dlr.worker import goenv, javaenv, nodeenv, venv
from dlr.worker.agent import Agent
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_deletion import CacheDeletionManager
from dlr.worker.cache_lifecycle import CacheLifecycleStore
from dlr.worker.cache_policy import CachePolicy
from dlr.worker.cache_replacement import ReplacementAuthority, activate


class ReplacementGuardClient:
    def __init__(self) -> None:
        self.operations: dict[uuid.UUID, dict[str, Any]] = {}
        self.fail_acquire_response = False

    def acquire_cache_guard(
        self,
        worker_id: int,
        *,
        adapter_id: int,
        version_id: int,
        operation_id: uuid.UUID,
        cleanup_context: Mapping[str, Any] | None = None,
        observed_identity: Mapping[str, Any] | None = None,
        replacement_context: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds, cleanup_context, observed_identity
        binding = (
            {key: value for key, value in replacement_context.items() if key != "claim_token"}
            if replacement_context is not None
            else None
        )
        operation = self.operations.setdefault(
            operation_id,
            {
                "worker_id": worker_id,
                "adapter_id": adapter_id,
                "version_id": version_id,
                "operation_id": str(operation_id),
                "generation": 1,
                "phase": "acquired",
                "operation_kind": "replacement",
                "replacement_context": binding,
            },
        )
        if self.fail_acquire_response:
            self.fail_acquire_response = False
            from dlr.worker.client import ControlUnavailableError

            raise ControlUnavailableError("committed response lost")
        return dict(operation)

    def check_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds
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
        del timeout_seconds
        operation = self.operations[operation_id]
        assert operation["worker_id"] == worker_id
        assert operation["generation"] == generation
        if operation["phase"] == "acquired":
            operation["phase"] = outcome
        assert operation["phase"] == outcome
        return dict(operation)

    def list_cache_guard_page(
        self,
        worker_id: int,
        *,
        after_version_id: int | None = None,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        del timeout_seconds
        active = sorted(
            (
                value
                for value in self.operations.values()
                if value["worker_id"] == worker_id
                and value["phase"] == "acquired"
                and (after_version_id is None or value["version_id"] > after_version_id)
            ),
            key=lambda value: value["version_id"],
        )
        page = active[:limit]
        return [dict(value) for value in page], page[-1]["version_id"] if len(
            active
        ) > limit else None


def _replacement_fixture(
    tmp_path: Path,
) -> tuple[
    VerifiedVersionCache,
    CacheLifecycleStore,
    ReplacementGuardClient,
    Path,
    dict[str, Any],
    str,
    dict[str, Any],
    str,
    int,
]:
    cache = VerifiedVersionCache(
        tmp_path / "version-cache", max_bytes=1024 * 1024, low_watermark_bytes=0
    )
    lifecycle = CacheLifecycleStore(cache.root)
    lifecycle.bind_owner(7)
    old_identity = {
        "adapter_id": 11,
        "version_id": 13,
        "language": "python",
        "source_sha256": "a" * 64,
    }
    old_staging = cache.staging_path("11-13", "old")
    old_staging.mkdir()
    (old_staging / "payload.bin").write_bytes(b"old")
    target = cache.promote(
        old_staging,
        cache.entry_path("11-13"),
        identity=old_identity,
        reservation=cache.reserve(4096),
    )
    old_manifest = json.loads((target / ".dlr-cache-manifest.json").read_text(encoding="ascii"))
    new_identity = {**old_identity, "source_sha256": "b" * 64}
    new_staging = cache.staging_path("11-13", "new")
    new_staging.mkdir()
    (new_staging / "payload.bin").write_bytes(b"new")
    reservation = cache.reserve(4096)
    prepared, new_digest, new_bytes = cache.prepare_replacement_staging(
        new_staging,
        target,
        identity=new_identity,
        reservation=reservation,
        source_is_tmpfs=False,
    )
    reservation.release()
    return (
        cache,
        lifecycle,
        ReplacementGuardClient(),
        prepared,
        old_identity,
        old_manifest["digest"],
        new_identity,
        new_digest,
        new_bytes,
    )


def _manager(
    tmp_path: Path,
    cache: VerifiedVersionCache,
    lifecycle: CacheLifecycleStore,
    client: ReplacementGuardClient,
    *,
    active_attempt: bool,
) -> CacheDeletionManager:
    attempt_root = tmp_path / "attempt-journal"
    cleanup_root = tmp_path / "cleanup-journal"
    attempt_root.mkdir(exist_ok=True)
    cleanup_root.mkdir(exist_ok=True)
    return CacheDeletionManager(
        cache,
        lifecycle,
        client,
        worker_id=7,
        journal_protected=lambda _key: False,
        replacement_owner=(7, 17, 19, 23) if active_attempt else None,
        replacement_journal_roots=(attempt_root, cleanup_root) if active_attempt else None,
        replacement_resolve=(lambda _execution, _attempt: "11-13") if active_attempt else None,
        retry_seconds=0,
    )


def _context(lifecycle: CacheLifecycleStore, old_digest: str) -> dict[str, Any]:
    owner = lifecycle.owner(7)
    return {
        "execution_id": 17,
        "attempt_id": 19,
        "fencing_token": 23,
        "claim_token": "claim",
        "old_identity": {
            "store_id": owner["store_id"],
            "language": "python",
            "source_sha256": "a" * 64,
            "digest": old_digest,
        },
        "target_language": "python",
        "target_source_sha256": "b" * 64,
    }


def test_replacement_switches_only_with_exact_current_use(tmp_path: Path) -> None:
    cache, lifecycle, client, prepared, old_identity, old_digest, new_identity, digest, size = (
        _replacement_fixture(tmp_path)
    )
    manager = _manager(tmp_path, cache, lifecycle, client, active_attempt=True)
    with lifecycle.begin_use(
        "11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23
    ):
        result = manager.begin_replacement(
            adapter_id=11,
            version_id=13,
            old_identity=old_identity,
            old_digest=old_digest,
            new_identity=new_identity,
            new_digest=digest,
            new_bytes=size,
            prepared_staging=prepared,
            replacement_context=_context(lifecycle, old_digest),
        )
    assert result.status == "completed"
    assert cache.verify_replacement_target(cache.entry_path("11-13"), new_identity, digest)[0]
    facts = lifecycle.lifecycle("11-13")
    assert facts["identity"] == new_identity
    assert facts["digest"] == digest


def test_replacement_recovery_after_publish_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, lifecycle, client, prepared, old_identity, old_digest, new_identity, digest, size = (
        _replacement_fixture(tmp_path)
    )
    manager = _manager(tmp_path, cache, lifecycle, client, active_attempt=True)
    original = lifecycle.replace_verified

    def crash_after_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise SystemExit("crash")

    monkeypatch.setattr(lifecycle, "replace_verified", crash_after_publish)
    use = lifecycle.begin_use(
        "11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23
    )
    use.__enter__()
    try:
        with pytest.raises(SystemExit):
            manager.begin_replacement(
                adapter_id=11,
                version_id=13,
                old_identity=old_identity,
                old_digest=old_digest,
                new_identity=new_identity,
                new_digest=digest,
                new_bytes=size,
                prepared_staging=prepared,
                replacement_context=_context(lifecycle, old_digest),
            )
    finally:
        use.release(cleanup_completed=True)
    monkeypatch.setattr(lifecycle, "replace_verified", original)
    restarted = _manager(tmp_path, cache, lifecycle, client, active_attempt=False)
    assert restarted.recover_round(max_items=2, max_pages=0) == 1
    assert cache.verify_replacement_target(cache.entry_path("11-13"), new_identity, digest)[0]
    assert lifecycle.lifecycle("11-13")["identity"] == new_identity


def test_replacement_recovery_after_old_rename_preserves_original_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, lifecycle, client, prepared, old_identity, old_digest, new_identity, digest, size = (
        _replacement_fixture(tmp_path)
    )
    manager = _manager(tmp_path, cache, lifecycle, client, active_attempt=True)
    original_sync = deletion_module._sync_directory
    crashed = False

    def crash_after_old_rename(path: Path, *, strict: bool = False) -> None:
        nonlocal crashed
        if path == cache.entries and strict and not crashed:
            crashed = True
            raise SystemExit("crash")
        original_sync(path, strict=strict)

    monkeypatch.setattr(deletion_module, "_sync_directory", crash_after_old_rename)
    use = lifecycle.begin_use(
        "11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23
    )
    use.__enter__()
    try:
        with pytest.raises(SystemExit):
            manager.begin_replacement(
                adapter_id=11,
                version_id=13,
                old_identity=old_identity,
                old_digest=old_digest,
                new_identity=new_identity,
                new_digest=digest,
                new_bytes=size,
                prepared_staging=prepared,
                replacement_context=_context(lifecycle, old_digest),
            )
    finally:
        use.release(cleanup_completed=True)
    monkeypatch.setattr(deletion_module, "_sync_directory", original_sync)
    restarted = _manager(tmp_path, cache, lifecycle, client, active_attempt=False)
    assert restarted.recover_round(max_items=2, max_pages=0) == 1
    assert cache.verify_replacement_target(cache.entry_path("11-13"), new_identity, digest)[0]
    operation = next(iter(client.operations.values()))
    assert operation["generation"] == 1
    assert operation["phase"] == "completed"


def test_replacement_acquire_response_loss_uses_dedicated_safe_abort(tmp_path: Path) -> None:
    cache, lifecycle, client, prepared, old_identity, old_digest, new_identity, digest, size = (
        _replacement_fixture(tmp_path)
    )
    client.fail_acquire_response = True
    operation_id = uuid.uuid4()
    manager = _manager(tmp_path, cache, lifecycle, client, active_attempt=True)
    use = lifecycle.begin_use(
        "11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23
    )
    use.__enter__()
    try:
        from dlr.worker.client import ControlUnavailableError

        with pytest.raises(ControlUnavailableError):
            manager.begin_replacement(
                adapter_id=11,
                version_id=13,
                old_identity=old_identity,
                old_digest=old_digest,
                new_identity=new_identity,
                new_digest=digest,
                new_bytes=size,
                prepared_staging=prepared,
                replacement_context=_context(lifecycle, old_digest),
                operation_id=operation_id,
            )
    finally:
        use.release(cleanup_completed=True)
    cache.remove_staging(prepared)
    restarted = _manager(tmp_path, cache, lifecycle, client, active_attempt=False)
    assert restarted.recover_round(max_items=1, max_pages=1) == 1
    assert client.operations[operation_id]["phase"] == "aborted"
    assert cache.manifest_identity(cache.entry_path("11-13")) == (old_identity, old_digest)


def test_other_use_blocks_replacement_without_touching_old_root(tmp_path: Path) -> None:
    cache, lifecycle, client, prepared, old_identity, old_digest, new_identity, digest, size = (
        _replacement_fixture(tmp_path)
    )
    manager = _manager(tmp_path, cache, lifecycle, client, active_attempt=True)
    other = lifecycle.begin_use(
        "11-13", worker_id=7, execution_id=99, attempt_id=100, fencing_token=101
    )
    with other, pytest.raises(CacheError) as error:
        manager.begin_replacement(
            adapter_id=11,
            version_id=13,
            old_identity=old_identity,
            old_digest=old_digest,
            new_identity=new_identity,
            new_digest=digest,
            new_bytes=size,
            prepared_staging=prepared,
            replacement_context=_context(lifecycle, old_digest),
        )
    assert error.value.code == "cache_entry_in_use"
    assert cache.manifest_identity(cache.entry_path("11-13")) == (old_identity, old_digest)


def test_cleanup_scan_budget_continues_same_claim_before_retained_result(tmp_path: Path) -> None:
    entries = tmp_path / "version-cache" / "entries"
    entries.mkdir(parents=True)
    for version_id in range(1, 102):
        (entries / f"11-{version_id}").mkdir()

    class Client:
        def __init__(self) -> None:
            self.results: list[dict[str, Any]] = []

        def report_cleanup(self, _worker_id: int, _cleanup_id: int, **result: Any) -> None:
            self.results.append(result)

    class Cache:
        def __init__(self) -> None:
            self.entries = entries

        def observed_identity(self, _path: Path) -> None:
            return None

    class Lifecycle:
        def owner(self, _worker_id: int) -> dict[str, str]:
            return {"store_id": "store"}

    class Manager:
        cache = Cache()
        lifecycle = Lifecycle()

        def make_round_budget(self, **_kwargs: Any) -> object:
            return object()

        def observed_identity_bounded(self, _path: Path, *, budget: object) -> None:
            del budget
            return None

        def cleanup_state(self, _cleanup_id: int) -> str:
            return "clear"

    client = Client()
    config = SimpleNamespace(
        runtime_root=tmp_path,
        workspace_cleanup_interval_seconds=0.01,
        cache_policy=CachePolicy(max_scan_entries_per_round=100),
    )
    agent = Agent(config, client)  # type: ignore[arg-type]
    agent._cache_deletion_manager = Manager()  # type: ignore[assignment]
    task = {"cleanup_id": 3, "adapter_id": 11, "claim_attempt": 2}
    assert agent._execute_cleanup_task(7, task) is False
    assert client.results == []
    assert agent._execute_cleanup_task(7, task) is True
    assert client.results == [
        {
            "success": False,
            "claim_attempt": 2,
            "error_code": "cache_cleanup_retained",
        }
    ]


@pytest.mark.parametrize("language", ["python", "javascript", "typescript", "go", "java"])
def test_all_language_prepare_paths_request_guarded_replacement_for_unavailable_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    calls: list[bool] = []
    target = venv.version_dir(tmp_path, 11, 13)

    def fake_begin(*_args: Any, force_replacement: bool = False, **_kwargs: Any) -> Any:
        calls.append(force_replacement)
        if force_replacement:
            raise CacheError("replacement_requested")
        return object(), target, None

    monkeypatch.setattr(venv, "_begin_version_build", fake_begin)
    if language == "python":

        def prepare() -> Any:
            return venv.prepare_version_venv(tmp_path, 11, 13, "", timeout_seconds=1)

    elif language in {"javascript", "typescript"}:
        if language == "typescript":
            package = tmp_path / "toolchain" / "typescript"
            compiler = package / "bin" / "tsc"
            compiler.parent.mkdir(parents=True)
            compiler.write_text("", encoding="ascii")
            (package / "package.json").write_text('{"version":"5.8.3"}', encoding="ascii")
            node = tmp_path / "toolchain" / "node"
            node.write_text("", encoding="ascii")
            monkeypatch.setattr(
                nodeenv.shutil,
                "which",
                lambda command: str(compiler if command == "tsc" else node),
            )

        def prepare() -> Any:
            return nodeenv.prepare_version_node(
                tmp_path,
                11,
                13,
                "export function handle() {}",
                "",
                timeout_seconds=1,
                registry_url=None,
                language=language,
            )

    elif language == "go":
        compiler = tmp_path / "go"
        compiler.write_text("", encoding="ascii")
        monkeypatch.setattr(goenv.shutil, "which", lambda _command: str(compiler))

        def prepare() -> Any:
            return goenv.prepare_version_go(
                tmp_path,
                11,
                13,
                "package main",
                "",
                timeout_seconds=1,
                proxy_url=None,
            )

    else:

        def prepare() -> Any:
            return javaenv.prepare_version_java(
                tmp_path,
                11,
                13,
                "public class Adapter {}",
                "",
                timeout_seconds=1,
                repository_url=None,
            )

    with pytest.raises(venv.DependencyPreparationError):
        prepare()
    assert calls == [False, True]


def test_finish_keeps_staging_owned_by_durable_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, store, client, prepared, old, old_digest, new, _digest, _size = _replacement_fixture(
        tmp_path
    )
    cache.remove_staging(prepared)
    _, _target, build = venv._begin_version_build(
        tmp_path, 11, 13, identity=new, reservation_bytes=4096
    )
    assert build is not None
    (build.staging / "payload.bin").write_bytes(b"new")
    journals = tmp_path / "attempt-journal"
    cleanup = tmp_path / "cleanup-journal"
    journals.mkdir()
    cleanup.mkdir()
    client.resolve_cache_reference = lambda *_args: "11-13"  # type: ignore[method-assign]
    authority = ReplacementAuthority(client, 7, _context(store, old_digest), journals, cleanup)
    original_sync = deletion_module._sync_directory

    def fail_after_old_rename(path: Path, **kwargs: Any) -> None:
        if path == cache.entries:
            raise OSError("injected post-rename fsync failure")
        original_sync(path, **kwargs)

    with (
        store.begin_use("11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23),
        activate(authority),
        monkeypatch.context() as patch,
    ):
        patch.setattr(deletion_module, "_sync_directory", fail_after_old_rename)
        with pytest.raises(CacheError):
            build.finish(new)
    build.abort()
    records = list(store.deletion_root.glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="ascii"))
    assert (cache.entries / record["new_staging_name"]).exists()


def test_replacement_recovery_charges_shared_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, store, client, prepared, old, old_digest, new, digest, size = _replacement_fixture(
        tmp_path
    )
    manager = _manager(tmp_path, cache, store, client, active_attempt=True)
    original_replace = deletion_module.os.replace

    def stop_before_old_rename(source: Path, target: Path) -> None:
        if Path(source) == cache.entry_path("11-13"):
            raise SystemExit("before old rename")
        original_replace(source, target)

    use = store.begin_use("11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23)
    use.__enter__()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(deletion_module.os, "replace", stop_before_old_rename)
            with pytest.raises(SystemExit):
                manager.begin_replacement(
                    adapter_id=11,
                    version_id=13,
                    old_identity=old,
                    old_digest=old_digest,
                    new_identity=new,
                    new_digest=digest,
                    new_bytes=size,
                    prepared_staging=prepared,
                    replacement_context=_context(store, old_digest),
                )
    finally:
        use.release(cleanup_completed=True)
    restarted = _manager(tmp_path, cache, store, client, active_attempt=False)
    restarted.recover_round(max_items=1, max_pages=0, max_scan_nodes=1, max_hash_bytes=1)
    assert (cache.entry_path("11-13") / "payload.bin").read_bytes() == b"old"


def test_corrupt_manifest_bound_legacy_entry_without_sidecar_can_repair(
    tmp_path: Path,
) -> None:
    cache, store, client, prepared, _old, old_digest, new, _digest, _size = _replacement_fixture(
        tmp_path
    )
    cache.remove_staging(prepared)
    target = cache.entry_path("11-13")
    payload = target / "payload.bin"
    payload.chmod(0o600)
    payload.write_bytes(b"corrupt")
    payload.chmod(0o444)
    (store.lifecycle_root / "11-13.json").unlink()
    _, _, build = venv._begin_version_build(tmp_path, 11, 13, identity=new, reservation_bytes=4096)
    assert build is not None
    (build.staging / "payload.bin").write_bytes(b"new")
    journals = tmp_path / "attempt-journal"
    cleanup = tmp_path / "cleanup-journal"
    journals.mkdir()
    cleanup.mkdir()
    client.resolve_cache_reference = lambda *_args: "11-13"  # type: ignore[method-assign]
    authority = ReplacementAuthority(client, 7, _context(store, old_digest), journals, cleanup)
    with (
        store.begin_use("11-13", worker_id=7, execution_id=17, attempt_id=19, fencing_token=23),
        activate(authority),
    ):
        build.finish(new)
    assert (target / "payload.bin").read_bytes() == b"new"


def test_cleanup_retains_unknown_name_owned_by_adapter(tmp_path: Path) -> None:
    cache, store, client, prepared, _old, _old_digest, _new, _digest, _size = _replacement_fixture(
        tmp_path
    )
    cache.remove_staging(prepared)
    cache.entry_path("11-13").replace(cache.entries / "unknown-name")
    replies: list[dict[str, Any]] = []
    client.report_cleanup = (  # type: ignore[attr-defined]
        lambda *_args, **kwargs: replies.append(kwargs)
    )
    config = SimpleNamespace(runtime_root=tmp_path, workspace_cleanup_interval_seconds=0.01)
    agent = Agent(config, client)  # type: ignore[arg-type]
    agent._cache_deletion_manager = _manager(tmp_path, cache, store, client, active_attempt=False)
    assert agent._execute_cleanup_task(7, {"cleanup_id": 3, "adapter_id": 11, "claim_attempt": 1})
    assert replies[-1]["success"] is False


def test_cleanup_oversize_candidate_does_not_starve_later_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, store, client, prepared, old, _old_digest, _new, _digest, _size = _replacement_fixture(
        tmp_path
    )
    cache.remove_staging(prepared)
    small = cache.staging_path("11-14", "small")
    small.mkdir()
    (small / "payload.bin").write_bytes(b"x")
    cache.promote(
        small,
        cache.entry_path("11-14"),
        identity={**old, "version_id": 14},
        reservation=cache.reserve(4096),
    )
    replies: list[dict[str, Any]] = []
    client.report_cleanup = (  # type: ignore[attr-defined]
        lambda *_args, **kwargs: replies.append(kwargs)
    )
    agent = Agent(  # type: ignore[arg-type]
        SimpleNamespace(runtime_root=tmp_path, workspace_cleanup_interval_seconds=0.01),
        client,
    )
    manager = _manager(tmp_path, cache, store, client, active_attempt=False)
    agent._cache_deletion_manager = manager

    class OrderedScan:
        def __init__(self) -> None:
            self.items = iter([cache.entry_path("11-13"), cache.entry_path("11-14")])

        def __next__(self) -> SimpleNamespace:
            path = next(self.items)
            return SimpleNamespace(path=path, name=path.name)

        def close(self) -> None:
            return None

    agent._cleanup_entries = OrderedScan()  # type: ignore[assignment]
    original_budget = manager.make_round_budget

    def tiny_budget(**kwargs: Any) -> object:
        kwargs["max_hash_bytes"] = 2
        return original_budget(**kwargs)

    monkeypatch.setattr(manager, "make_round_budget", tiny_budget)
    original_observe = manager.observed_identity_bounded
    observed: list[str] = []

    def track(path: Path, **kwargs: Any) -> Any:
        observed.append(path.name)
        return original_observe(path, **kwargs)

    monkeypatch.setattr(manager, "observed_identity_bounded", track)
    for _ in range(3):
        if agent._execute_cleanup_task(7, {"cleanup_id": 3, "adapter_id": 11, "claim_attempt": 1}):
            break
    assert "11-14" in observed
    assert (cache.entry_path("11-13") / "payload.bin").read_bytes() == b"old"
    assert replies[-1]["success"] is False
