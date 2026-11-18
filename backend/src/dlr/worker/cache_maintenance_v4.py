"""Maintenance of a dedicated, identity-bound local version-cache volume."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import threading
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from dlr.worker import workspace_fs_v4
from dlr.worker.cache import CacheError
from dlr.worker.cache_deletion import CacheDeletionManager, DeletionEligibility
from dlr.worker.cache_governance import CachePolicyManager
from dlr.worker.cache_lifecycle import (
    SCHEMA_VERSION,
    CacheLifecycleStore,
    _read_bounded_json,
    _write_json,
)
from dlr.worker.cache_policy import CachePolicy, create_version_cache
from dlr.worker.client import ControlUnavailableError
from dlr.worker.lease_session_v4 import WorkerLeaseError, WorkerLeaseSession

_MAX_KEYS = 8


class CacheMaintenance:
    """Use the existing bounded cache scan and restart-safe physical remover."""

    def __init__(self, root: Path, lease: WorkerLeaseSession, stop: threading.Event) -> None:
        self._root, self._lease, self._stop = root, lease, stop
        self._worker_id = lease._worker_id
        self._command: UUID | None = None
        self._cleanup: dict[str, int] | None = None
        self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._verify()
            self._store = CacheLifecycleStore.for_runtime(root)
            marker = self._store.state_root / "v4-owner.json"
            owners = _read_bounded_json(marker, max_bytes=1024)
            entries = self._store.cache_root / "entries"
            if owners is None and entries.is_dir() and any(entries.iterdir()):
                raise CacheError("cache_owner_unconfirmed")
            store_id = self._store.bind_owner(self._worker_id)
            info = os.fstat(self._fd)
            binding = {
                "schema": SCHEMA_VERSION,
                "cluster_id": str(lease._cluster_id),
                "worker_identity_id": str(lease._identity_id),
                "store_id": store_id,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
            if owners is None:
                _write_json(marker, binding, strict_sync=True)
            elif owners != binding:
                raise CacheError("cache_owner_mismatch")
            policy = replace(
                CachePolicy.from_environment(),
                gc_enabled=False,
                pressure_gc_enabled=False,
                offline_mode=False,
                offline_protection=True,
                max_round_seconds=2,
            )
            cache = create_version_cache(root, policy=policy)
            self._deletion = CacheDeletionManager(
                cache,
                self._store,
                self,
                worker_id=self._worker_id,
                journal_protected=self._journal_protected,
                offline_protection=True,
                bind_observed_identity=True,
            )
            self._manager = CachePolicyManager(root, cache, self._store, self._deletion, policy)
        except BaseException:
            os.close(self._fd)
            raise

    def close(self) -> None:
        os.close(self._fd)

    def _verify(self) -> None:
        actual, named = os.fstat(self._fd), self._root.lstat()
        if (
            self._root != self._root.resolve(strict=True)
            or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
            or actual.st_uid != os.geteuid()
            or stat.S_IMODE(actual.st_mode) != 0o700
            or not workspace_fs_v4._mounts_safe(self._root, self._root / "version-cache" / "probe")
            or self._stop.is_set()
        ):
            raise CacheError("cache_owner_unconfirmed")

    def _journal_protected(self, _key: str) -> bool:
        # The v4 language and material paths use only per-Attempt storage.
        # Cache references and any v3 history are still checked by Control.
        self._verify()
        return False

    def _exchange(self, action: str, body: Mapping[str, object]) -> dict[str, Any] | None:
        self._verify()
        try:
            raw = self._lease._data_exchange(f"cache/{action}", body, self._stop)
            payload = self._lease._body(raw, {"result"})["result"]
            if payload is None or isinstance(payload, dict):
                return payload
            raise WorkerLeaseError("cache ownership unavailable")
        except WorkerLeaseError:
            raise ControlUnavailableError("cache ownership unavailable") from None

    def _require_reply(self, action: str, body: Mapping[str, object]) -> dict[str, Any]:
        payload = self._exchange(action, body)
        if payload is None:
            raise ControlUnavailableError("cache ownership unavailable")
        return payload

    def acquire_cache_guard(
        self,
        worker_id: int,
        *,
        adapter_id: int,
        version_id: int,
        operation_id: UUID,
        cleanup_context: Mapping[str, Any] | None = None,
        observed_identity: Mapping[str, Any] | None = None,
        replacement_context: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if (
            worker_id != self._worker_id
            or observed_identity is None
            or replacement_context is not None
            or (cleanup_context is None and self._command is None)
            or (cleanup_context is not None and cleanup_context != self._cleanup)
        ):
            raise CacheError("cache_owner_unconfirmed")
        return self._require_reply(
            "acquire",
            {
                "command_id": str(self._command) if self._command is not None else None,
                "request": {
                    "adapter_id": adapter_id,
                    "version_id": version_id,
                    "operation_id": str(operation_id),
                    "observed_identity": dict(observed_identity),
                    "cleanup_context": cleanup_context,
                },
            },
        )

    def check_cache_guard(
        self, worker_id: int, operation_id: UUID, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if worker_id != self._worker_id:
            raise CacheError("cache_owner_unconfirmed")
        return self._require_reply("check", {"operation_id": str(operation_id)})

    def finish_cache_guard(
        self,
        worker_id: int,
        operation_id: UUID,
        *,
        generation: int,
        outcome: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if worker_id != self._worker_id:
            raise CacheError("cache_owner_unconfirmed")
        return self._require_reply(
            "release",
            {
                "operation_id": str(operation_id),
                "result": {"generation": generation, "outcome": outcome},
            },
        )

    def list_cache_guard_page(
        self,
        worker_id: int,
        *,
        after_version_id: int | None = None,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        if worker_id != self._worker_id:
            raise CacheError("cache_owner_unconfirmed")
        page = self._require_reply(
            "guards", {"after_version_id": after_version_id, "limit": min(limit, _MAX_KEYS)}
        )
        return page["items"], page["next_after_version_id"]

    def resolve_cache_key_references(
        self, worker_id: int, items: list[tuple[int, int]], *, timeout_seconds: float
    ) -> dict[str, Any]:
        if worker_id != self._worker_id or len(items) > _MAX_KEYS:
            raise CacheError("cache_reference_unavailable")
        return self._require_reply(
            "references",
            {
                "kind": "cache_keys_v1",
                "items": [
                    {"adapter_id": adapter, "version_id": version} for adapter, version in items
                ],
            },
        )

    def _snapshot(self) -> None:
        report = self._manager.management_snapshot(max_items=_MAX_KEYS)
        sample, path = uuid4(), self._store.state_root / "management-snapshot-sequence.json"
        current = _read_bounded_json(path, max_bytes=1024)
        sequence = 1
        if current is not None:
            if (
                set(current) != {"schema", "sequence"}
                or current["schema"] != SCHEMA_VERSION
                or type(current["sequence"]) is not int
                or not 0 < current["sequence"] < 2**63 - 1
            ):
                raise CacheError("cache_lifecycle_invalid")
            sequence = current["sequence"] + 1
        _write_json(path, {"schema": SCHEMA_VERSION, "sequence": sequence}, strict_sync=True)
        failed = report["failed_guard_items"]
        response = self._exchange(
            "snapshot",
            {
                "sample_id": str(sample),
                "sequence": sequence,
                "sampled_at": datetime.fromtimestamp(report["sampled_at"], tz=UTC).isoformat(),
                "state": "complete" if report["complete"] else "incomplete",
                "complete": report["complete"],
                "cursor": report["cursor"],
                "summary": {
                    name: report[name]
                    for name in ("accounting", "categories", "retained_reasons", "policy")
                },
                "items": report["items"],
                "failed_guard_items": failed[:_MAX_KEYS],
                "failed_guard_complete": report["failed_guard_complete"]
                and len(failed) <= _MAX_KEYS,
                "failed_guard_cursor": report["failed_guard_cursor"],
            },
        )
        if response != {"sample_id": str(sample)}:
            raise CacheError("cache_snapshot_unknown")

    def poll(self) -> None:
        """One small command round between heartbeats, keeping exact receipts."""
        self._verify()
        self._snapshot()
        command = self._exchange("claim", {})
        if command is None:
            return
        operation = UUID(command["operation_id"])
        self._command = operation
        try:
            self._command_result(operation, command)
        finally:
            self._command = None

    def cleanup_adapter(self, ticket: Mapping[str, int]) -> bool:
        """Retire only verified cache entries for the exact archived Adapter."""
        self._verify()
        aid, cleanup_id = ticket["adapter_id"], ticket["cleanup_id"]
        context = {"cleanup_id": cleanup_id, "claim_attempt": ticket["claim_attempt"]}
        self._cleanup = context
        try:
            self._deletion.recover_round(max_items=_MAX_KEYS, max_seconds=2)
            if self._deletion.cleanup_state(cleanup_id) != "clear":
                return False
            keys: list[str] = []
            with os.scandir(self._manager.cache.entries) as items:
                for position, entry in enumerate(items):
                    if position >= self._manager.policy.max_scan_entries_per_round:
                        return False
                    if re.fullmatch(r"[1-9][0-9]{0,18}-[1-9][0-9]{0,18}", entry.name) is None:
                        return False
                    if entry.name.startswith(f"{aid}-"):
                        keys.append(entry.name)
            if not keys:
                return True
            policy = self._manager.policy
            for key in keys[:_MAX_KEYS]:
                self._verify()
                identity = self._store.lifecycle(key)
                version_id = int(key.split("-")[1])
                result = self._deletion.begin(
                    DeletionEligibility(
                        adapter_id=aid,
                        version_id=version_id,
                        identity=identity["identity"],
                        digest=identity["digest"],
                        last_used_before=float(identity["last_used_at"]),
                        cleanup_context=context,
                    ),
                    operation_id=uuid5(
                        self._lease._identity_id, f"adapter-cleanup:{cleanup_id}:{key}"
                    ),
                    max_seconds=2,
                    max_nodes=policy.max_scan_nodes_per_round,
                    max_bytes=policy.max_delete_bytes_per_round,
                )
                if result.status != "completed":
                    return False
            # A fresh round observes the original root with no selected files
            # and reads the committed terminal guard receipts before release.
            return False
        finally:
            self._cleanup = None

    def _command_result(self, operation: UUID, command: dict[str, Any]) -> None:
        kind = command["kind"]
        keys = frozenset(
            f"{row['adapter_id']}-{row['version_id']}" for row in command["payload"]["keys"]
        )
        if not keys or len(keys) > _MAX_KEYS or kind not in {"preview", "clean"}:
            raise CacheError("cache_operation_unsupported")
        directory = self._store.state_root / "management-receipts"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{operation}.json"
        previous = _read_bounded_json(path, max_bytes=4096)
        exact = {"claim_epoch": command["claim_epoch"], "request_hash": command["request_hash"]}
        if previous is None:
            if kind == "preview":
                observed = self._manager.scan(mode="manual", target_keys=keys, page_limit=_MAX_KEYS)
                if not observed.complete:
                    return
                result: dict[str, Any] = {
                    "complete": True,
                    "items": list(observed.observations),
                    "cursor": observed.cursor,
                }
            else:
                retained: dict[str, int] = {}
                pending = [
                    uuid5(operation, key)
                    for key in keys
                    if self._deletion.operation_phase(uuid5(operation, key)) != "unknown"
                ]
                if pending:
                    self._deletion.recover_round(max_items=_MAX_KEYS, max_seconds=2)
                remaining = keys - {
                    f"{row['adapter_id']}-{row['version_id']}"
                    for row in (self._deletion.operation_result_fact(key) for key in pending)
                }
                if remaining:
                    changed = self._manager.run_round(
                        mode="manual", target_keys=remaining, operation_namespace=operation
                    )
                    if changed.status not in {"complete", "no_safe_candidates"}:
                        return
                    retained = changed.retained_reasons
                facts = [
                    self._deletion.operation_result_fact(uuid5(operation, key))
                    for key in keys
                    if self._deletion.operation_phase(uuid5(operation, key)) != "unknown"
                ]
                if any(row["phase"] not in {"completed", "aborted"} for row in facts):
                    return
                result = {
                    "deleted": sum(row["phase"] == "completed" for row in facts),
                    "freed_bytes": sum(
                        int(str(row["original_bytes"]))
                        for row in facts
                        if row["phase"] == "completed"
                    ),
                    "child_operations": [
                        {key: value for key, value in row.items() if key != "original_bytes"}
                        for row in facts
                    ],
                    "retained_reasons": retained,
                }
            previous = {**exact, "status": "completed", "result": result}
            _write_json(path, previous, strict_sync=True)
        elif set(previous) != {*exact, "status", "result"} or any(
            previous[key] != value for key, value in exact.items()
        ):
            raise CacheError("cache_operation_receipt_invalid")
        completed = self._exchange("result", {"operation_id": str(operation), "result": previous})
        if completed != {"operation_id": str(operation), "status": "completed"}:
            raise CacheError("cache_operation_unknown")
