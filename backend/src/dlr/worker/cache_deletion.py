"""Crash-safe deletion of one verified Worker version-cache entry."""

from __future__ import annotations

import errno
import os
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dlr.worker.cache import CacheError, CacheScanBudget, VerifiedVersionCache
from dlr.worker.cache_lifecycle import (
    SCHEMA_VERSION,
    CacheLifecycleStore,
    _read_bounded_json,
    _safe_key,
    _sync_directory,
    _write_json,
    cache_key,
    current_thread_owns_all_uses,
)
from dlr.worker.client import ClientError, ControlUnavailableError

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "store_id",
        "operation_id",
        "worker_id",
        "adapter_id",
        "version_id",
        "key",
        "generation",
        "identity",
        "digest",
        "root_device",
        "root_inode",
        "original_bytes",
        "last_used_before",
        "phase",
        "attempts",
        "next_retry_at",
        "last_error",
        "resume_phase",
        "cleanup_context",
        "observed_identity",
        "operation_kind",
        "replacement_context",
        "new_staging_name",
        "new_identity",
        "new_digest",
        "new_bytes",
        "new_root_device",
        "new_root_inode",
        "created_at",
        "updated_at",
    }
)
_TERMINAL_PHASES = frozenset({"completed", "aborted"})
_LOCAL_MAX_BYTES = 1024 * 1024


class GuardClient(Protocol):
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
    ) -> dict[str, Any]: ...

    def check_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def finish_cache_guard(
        self,
        worker_id: int,
        operation_id: uuid.UUID,
        *,
        generation: int,
        outcome: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def list_cache_guard_page(
        self,
        worker_id: int,
        *,
        after_version_id: int | None = None,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]: ...


@dataclass(frozen=True)
class DeletionEligibility:
    adapter_id: int
    version_id: int
    identity: Mapping[str, Any]
    digest: str
    last_used_before: float
    cleanup_context: Mapping[str, Any] | None = None
    observed_identity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DeletionResult:
    operation_id: uuid.UUID
    status: str
    freed_bytes: int = 0
    reason: str | None = None


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _operation_fact(
    value: Mapping[str, Any],
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    operation_id: uuid.UUID,
    generation: int | None = None,
    phases: frozenset[str] = frozenset({"acquired"}),
) -> int:
    try:
        response_operation = uuid.UUID(str(value["operation_id"]))
        response_generation = value["generation"]
    except (KeyError, TypeError, ValueError) as error:
        raise CacheError("cache_guard_invalid") from error
    if (
        value.get("worker_id") != worker_id
        or value.get("adapter_id") != adapter_id
        or value.get("version_id") != version_id
        or response_operation != operation_id
        or not _positive(response_generation)
        or (generation is not None and response_generation != generation)
        or value.get("phase") not in phases
    ):
        raise CacheError("cache_guard_invalid")
    return int(response_generation)


def _record_path(store: CacheLifecycleStore, operation_id: uuid.UUID) -> Path:
    return store.deletion_root / f"{operation_id}.json"


def _trash_path(store: CacheLifecycleStore, operation_id: uuid.UUID) -> Path:
    # macOS does not permit moving a read-only directory across parents because
    # that mutates its internal parent link. A hidden direct child keeps the
    # rename atomic without changing immutable entry permissions first.
    return store.cache_root / "entries" / f".dlr-trash-{operation_id}"


def _root_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as error:
        raise CacheError("cache_cleanup_unknown") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CacheError("cache_cleanup_unknown")
    return info.st_dev, info.st_ino


def _assert_root_identity(path: Path, record: Mapping[str, Any]) -> None:
    if _root_identity(path) != (record["root_device"], record["root_inode"]):
        raise CacheError("cache_cleanup_unknown")


def _read_record(path: Path) -> dict[str, Any] | None:
    value = _read_bounded_json(path, max_bytes=_LOCAL_MAX_BYTES)
    if value is None:
        return None
    if set(value) != _RECORD_FIELDS or value.get("schema") != SCHEMA_VERSION:
        raise CacheError("cache_deletion_record_invalid")
    try:
        operation_id = uuid.UUID(str(value["operation_id"]))
        key = _safe_key(value["key"])
        created_at = float(value["created_at"])
        updated_at = float(value["updated_at"])
        next_retry_at = float(value["next_retry_at"])
        last_used_before = float(value["last_used_before"])
    except (CacheError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise CacheError("cache_deletion_record_invalid") from error
    if (
        path.name != f"{operation_id}.json"
        or any(
            not _positive(value[field])
            for field in ("worker_id", "adapter_id", "version_id", "generation")
        )
        or key != cache_key(value["adapter_id"], value["version_id"])
        or not isinstance(value["store_id"], str)
        or not value["store_id"]
        or not isinstance(value["identity"], dict)
        or not isinstance(value["digest"], str)
        or len(value["digest"]) != 64
        or any(character not in "0123456789abcdef" for character in value["digest"])
        or not isinstance(value["original_bytes"], int)
        or isinstance(value["original_bytes"], bool)
        or value["original_bytes"] < 0
        or any(
            not isinstance(value[field], int) or isinstance(value[field], bool) or value[field] < 0
            for field in ("root_device", "root_inode")
        )
        or not isinstance(value["phase"], str)
        or value["phase"]
        not in {
            "recorded",
            "trashed",
            "empty_trash",
            "receipt_pending",
            "completed",
            "aborted",
            "failed",
            "replacement_prepared",
            "replacement_old_trashed",
        }
        or not isinstance(value["attempts"], int)
        or isinstance(value["attempts"], bool)
        or not 0 <= value["attempts"] <= 3
        or not all(
            number >= 0 and number < float("inf")
            for number in (created_at, updated_at, next_retry_at)
        )
        or not (last_used_before >= 0 and last_used_before < float("inf"))
        or (value["last_error"] is not None and not isinstance(value["last_error"], str))
        or (
            value["resume_phase"] is not None
            and (
                not isinstance(value["resume_phase"], str)
                or value["resume_phase"]
                not in {
                    "recorded",
                    "trashed",
                    "empty_trash",
                    "receipt_pending",
                    "replacement_prepared",
                    "replacement_old_trashed",
                }
            )
        )
        or (value["cleanup_context"] is not None and not isinstance(value["cleanup_context"], dict))
        or (
            value["observed_identity"] is not None
            and not isinstance(value["observed_identity"], dict)
        )
        or not isinstance(value["operation_kind"], str)
        or value["operation_kind"] not in {"gc", "cleanup", "replacement"}
        or (value["operation_kind"] == "cleanup") != isinstance(value["cleanup_context"], dict)
        or (value["operation_kind"] == "replacement" and value["observed_identity"] is not None)
        or (
            value["replacement_context"] is not None
            and not isinstance(value["replacement_context"], dict)
        )
        or (
            value["operation_kind"] == "replacement"
            and (
                not isinstance(value["new_staging_name"], str)
                or not value["new_staging_name"].startswith(".")
                or value["new_staging_name"] in {".", ".."}
                or Path(value["new_staging_name"]).name != value["new_staging_name"]
                or not isinstance(value["new_identity"], dict)
                or value["new_identity"].get("adapter_id") != value["adapter_id"]
                or value["new_identity"].get("version_id") != value["version_id"]
                or not isinstance(value["new_digest"], str)
                or len(value["new_digest"]) != 64
                or any(character not in "0123456789abcdef" for character in value["new_digest"])
                or any(
                    not isinstance(value[field], int)
                    or isinstance(value[field], bool)
                    or value[field] < 0
                    for field in ("new_bytes", "new_root_device", "new_root_inode")
                )
            )
        )
        or (
            value["operation_kind"] != "replacement"
            and any(
                value[field] is not None
                for field in (
                    "replacement_context",
                    "new_staging_name",
                    "new_identity",
                    "new_digest",
                    "new_bytes",
                    "new_root_device",
                    "new_root_inode",
                )
            )
        )
    ):
        raise CacheError("cache_deletion_record_invalid")
    return value


def _write_record(path: Path, value: Mapping[str, Any]) -> None:
    _write_json(path, value, strict_sync=True)


def _local_record_page(root: Path, *, after: str | None, limit: int, deadline: float) -> list[Path]:
    """Read one bounded directory page without materializing all local records."""

    def scan(*, start_after: str | None, stop_at: str | None = None) -> list[Path]:
        page: list[Path] = []
        seen = start_after is None
        try:
            with os.scandir(root) as iterator:
                for item in iterator:
                    if time.monotonic() >= deadline:
                        break
                    if stop_at is not None and item.name == stop_at:
                        break
                    if not seen:
                        if item.name == start_after:
                            seen = True
                        continue
                    if item.name.endswith(".json"):
                        page.append(Path(item.path))
                        if len(page) >= limit:
                            break
        except OSError as error:
            raise CacheError("cache_deletion_record_unavailable") from error
        return page

    after_name = f"{after}.json" if after is not None and not after.endswith(".json") else after
    page = scan(start_after=after_name)
    if not page and after is not None and time.monotonic() < deadline:
        page = scan(start_after=None, stop_at=after_name)
        cursor_path = root / str(after_name)
        if not page and cursor_path.exists():
            page = [cursor_path]
    return page


@dataclass
class _DeleteBudget:
    nodes: int
    bytes: int
    deadline: float
    removed_nodes: int = 0
    removed_bytes: int = 0


@dataclass
class _RoundBudget:
    deletion: _DeleteBudget
    scan: CacheScanBudget


def _delete_some(path: Path, budget: _DeleteBudget, *, depth: int = 0) -> bool:
    if time.monotonic() >= budget.deadline or budget.removed_nodes >= budget.nodes:
        return False
    if depth > 64:
        raise CacheError("cache_delete_path_invalid")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
        size = info.st_size if stat.S_ISREG(info.st_mode) else 0
        if budget.removed_bytes + size > budget.bytes:
            return False
        path.unlink()
        budget.removed_nodes += 1
        budget.removed_bytes += size
        _sync_directory(path.parent, strict=True)
        return True
    if not stat.S_ISDIR(info.st_mode):
        raise CacheError("cache_delete_path_invalid")
    if stat.S_IMODE(info.st_mode) & 0o300 != 0o300:
        os.chmod(path, stat.S_IMODE(info.st_mode) | 0o300, follow_symlinks=False)
    with os.scandir(path) as iterator:
        for item in iterator:
            if time.monotonic() >= budget.deadline or budget.removed_nodes >= budget.nodes:
                return False
            _delete_some(Path(item.path), budget, depth=depth + 1)
    if time.monotonic() >= budget.deadline or budget.removed_nodes >= budget.nodes:
        return False
    try:
        _sync_directory(path, strict=True)
        path.rmdir()
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return False
        raise
    budget.removed_nodes += 1
    _sync_directory(path.parent, strict=True)
    return True


def _delete_contents(path: Path, budget: _DeleteBudget) -> bool:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CacheError("cache_delete_path_invalid")
    if stat.S_IMODE(info.st_mode) & 0o300 != 0o300:
        os.chmod(path, stat.S_IMODE(info.st_mode) | 0o300, follow_symlinks=False)
    with os.scandir(path) as iterator:
        for item in iterator:
            if time.monotonic() >= budget.deadline or budget.removed_nodes >= budget.nodes:
                return False
            _delete_some(Path(item.path), budget, depth=1)
    return not any(path.iterdir())


class CacheDeletionManager:
    """One-key deletion state machine shared by commands and recovery."""

    def __init__(
        self,
        cache: VerifiedVersionCache,
        lifecycle: CacheLifecycleStore,
        client: GuardClient,
        *,
        worker_id: int,
        journal_protected: Callable[[str], bool],
        replacement_owner: tuple[int, int, int, int] | None = None,
        replacement_journal_roots: tuple[Path, Path] | None = None,
        replacement_resolve: Callable[[int, int | None], str | None] | None = None,
        retry_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache = cache
        self.lifecycle = lifecycle
        self.client = client
        self.worker_id = worker_id
        self.journal_protected = journal_protected
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.replacement_owner = replacement_owner
        self.replacement_journal_roots = replacement_journal_roots
        self.replacement_resolve = replacement_resolve

    def make_round_budget(
        self,
        *,
        max_nodes: int,
        max_bytes: int,
        max_scan_nodes: int,
        max_hash_bytes: int,
        deadline: float,
    ) -> _RoundBudget:
        return _RoundBudget(
            deletion=_DeleteBudget(
                nodes=max(1, max_nodes), bytes=max(1, max_bytes), deadline=deadline
            ),
            scan=CacheScanBudget(
                nodes_remaining=max_scan_nodes,
                hash_bytes_remaining=max_hash_bytes,
                deadline=deadline,
            ),
        )

    def observed_identity_bounded(
        self, path: Path, *, budget: _RoundBudget
    ) -> tuple[dict[str, Any], str] | None:
        return self.cache.observed_identity_bounded(path, budget=budget.scan)

    def _local_safe(self, key: str) -> None:
        records = self.lifecycle.use_records_for_key(key)
        if self.replacement_owner is None:
            if records:
                raise CacheError("cache_entry_in_use")
            if self.journal_protected(key):
                raise CacheError("cache_journal_protected")
            return
        worker_id, execution_id, attempt_id, fencing_token = self.replacement_owner
        expected = {
            "worker_id": worker_id,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
        }
        if any(
            any(record[field] != value for field, value in expected.items()) for record in records
        ):
            raise CacheError("cache_entry_in_use")
        if not current_thread_owns_all_uses(self.lifecycle, records):
            raise CacheError("cache_entry_in_use")
        if self.replacement_journal_roots is None or self.replacement_resolve is None:
            raise CacheError("cache_journal_protected")
        protection = self.lifecycle.scan_journal_protections(
            attempt_journal_root=self.replacement_journal_roots[0],
            cleanup_journal_root=self.replacement_journal_roots[1],
            resolve=self.replacement_resolve,
            excluded_attempt=(execution_id, attempt_id, fencing_token),
        )
        if protection.block_all or key in protection.protected_keys:
            raise CacheError("cache_journal_protected")

    def _guard(self, record: Mapping[str, Any], *, timeout_seconds: float | None = None) -> str:
        operation_id = uuid.UUID(str(record["operation_id"]))
        value = self.client.check_cache_guard(
            self.worker_id, operation_id, timeout_seconds=timeout_seconds
        )
        allowed = {"acquired"}
        if record["phase"] == "receipt_pending":
            allowed.add("completed")
        _operation_fact(
            value,
            worker_id=self.worker_id,
            adapter_id=int(record["adapter_id"]),
            version_id=int(record["version_id"]),
            operation_id=operation_id,
            generation=int(record["generation"]),
            phases=frozenset(allowed),
        )
        if record.get("cleanup_context") is not None and (
            value.get("cleanup_id") != record["cleanup_context"].get("cleanup_id")
            or value.get("cleanup_claim_attempt") != record["cleanup_context"].get("claim_attempt")
            or value.get("observed_identity") != record.get("observed_identity")
        ):
            raise CacheError("cache_guard_invalid")
        if record.get("replacement_context") is not None and (
            value.get("operation_kind") != "replacement"
            or value.get("replacement_context") != record["replacement_context"]
        ):
            raise CacheError("cache_guard_invalid")
        return str(value["phase"])

    def _active_replacement_guard(
        self,
        record: Mapping[str, Any],
        replacement_context: Mapping[str, Any],
        *,
        timeout_seconds: float | None,
    ) -> None:
        operation_id = uuid.UUID(str(record["operation_id"]))
        value = self.client.acquire_cache_guard(
            self.worker_id,
            adapter_id=int(record["adapter_id"]),
            version_id=int(record["version_id"]),
            operation_id=operation_id,
            replacement_context=replacement_context,
            timeout_seconds=timeout_seconds,
        )
        _operation_fact(
            value,
            worker_id=self.worker_id,
            adapter_id=int(record["adapter_id"]),
            version_id=int(record["version_id"]),
            operation_id=operation_id,
            generation=int(record["generation"]),
        )
        binding = {key: value for key, value in replacement_context.items() if key != "claim_token"}
        if (
            value.get("operation_kind") != "replacement"
            or value.get("replacement_context") != binding
        ):
            raise CacheError("cache_guard_invalid")

    def begin(
        self,
        eligibility: DeletionEligibility,
        *,
        operation_id: uuid.UUID | None = None,
        max_nodes: int = 100_000,
        max_bytes: int = 256 * 1024 * 1024,
        max_scan_nodes: int = 100_000,
        max_hash_bytes: int = 256 * 1024 * 1024,
        max_seconds: float = 10.0,
        round_budget: _RoundBudget | None = None,
    ) -> DeletionResult:
        key = cache_key(eligibility.adapter_id, eligibility.version_id)
        operation = operation_id or uuid.uuid4()
        deadline = time.monotonic() + max(0.001, max_seconds)
        budget = round_budget or self.make_round_budget(
            max_nodes=max_nodes,
            max_bytes=max_bytes,
            max_scan_nodes=max_scan_nodes,
            max_hash_bytes=max_hash_bytes,
            deadline=deadline,
        )
        with self.lifecycle.entry_lock(key, blocking=False):
            owner = self.lifecycle.owner(self.worker_id)
            facts = self.lifecycle.lifecycle(key)
            if (
                facts["identity"] != dict(eligibility.identity)
                or facts["digest"] != eligibility.digest
                or facts["pinned"] is not False
                or facts["rebuildability"] != "confirmed"
                or float(facts["last_used_at"]) > eligibility.last_used_before
            ):
                raise CacheError("cache_not_eligible")
            entry = self.cache.entry_path(key)
            verified, original_bytes = self.cache.verify_for_deletion_bounded(
                entry,
                eligibility.identity,
                eligibility.digest,
                max_nodes=max_scan_nodes,
                max_hash_bytes=max_hash_bytes,
                deadline=deadline,
                budget=budget.scan,
            )
            if not verified:
                raise CacheError("cache_identity_unverified")
            self._local_safe(key)
            if original_bytes > max_bytes:
                raise CacheError("cache_budget_exhausted")
            root_device, root_inode = _root_identity(entry)
            if eligibility.cleanup_context is None and eligibility.observed_identity is None:
                response = self.client.acquire_cache_guard(
                    self.worker_id,
                    adapter_id=eligibility.adapter_id,
                    version_id=eligibility.version_id,
                    operation_id=operation,
                )
            else:
                response = self.client.acquire_cache_guard(
                    self.worker_id,
                    adapter_id=eligibility.adapter_id,
                    version_id=eligibility.version_id,
                    operation_id=operation,
                    cleanup_context=eligibility.cleanup_context,
                    observed_identity=eligibility.observed_identity,
                )
            generation = _operation_fact(
                response,
                worker_id=self.worker_id,
                adapter_id=eligibility.adapter_id,
                version_id=eligibility.version_id,
                operation_id=operation,
            )
            now = self.clock()
            record: dict[str, Any] = {
                "schema": SCHEMA_VERSION,
                "store_id": owner["store_id"],
                "operation_id": str(operation),
                "worker_id": self.worker_id,
                "adapter_id": eligibility.adapter_id,
                "version_id": eligibility.version_id,
                "key": key,
                "generation": generation,
                "identity": dict(eligibility.identity),
                "digest": eligibility.digest,
                "root_device": root_device,
                "root_inode": root_inode,
                "original_bytes": original_bytes,
                "last_used_before": eligibility.last_used_before,
                "phase": "recorded",
                "attempts": 0,
                "next_retry_at": 0.0,
                "last_error": None,
                "resume_phase": None,
                "cleanup_context": (
                    dict(eligibility.cleanup_context)
                    if eligibility.cleanup_context is not None
                    else None
                ),
                "observed_identity": (
                    dict(eligibility.observed_identity)
                    if eligibility.observed_identity is not None
                    else None
                ),
                "operation_kind": ("cleanup" if eligibility.cleanup_context is not None else "gc"),
                "replacement_context": None,
                "new_staging_name": None,
                "new_identity": None,
                "new_digest": None,
                "new_bytes": None,
                "new_root_device": None,
                "new_root_inode": None,
                "created_at": now,
                "updated_at": now,
            }
            path = _record_path(self.lifecycle, operation)
            _write_record(path, record)
            return self._advance_locked(
                path,
                record,
                budget=budget,
            )

    def begin_replacement(
        self,
        *,
        adapter_id: int,
        version_id: int,
        old_identity: Mapping[str, Any],
        old_digest: str,
        new_identity: Mapping[str, Any],
        new_digest: str,
        new_bytes: int,
        prepared_staging: Path,
        replacement_context: Mapping[str, Any],
        operation_id: uuid.UUID | None = None,
        max_seconds: float = 10.0,
    ) -> DeletionResult:
        """Publish one prepared ready tree while retaining the old root until verified."""

        key = cache_key(adapter_id, version_id)
        operation = operation_id or uuid.uuid4()
        deadline = time.monotonic() + max(0.001, max_seconds)
        budget = _RoundBudget(
            deletion=_DeleteBudget(nodes=100_000, bytes=256 * 1024 * 1024, deadline=deadline),
            scan=CacheScanBudget(
                nodes_remaining=100_000,
                hash_bytes_remaining=256 * 1024 * 1024,
                deadline=deadline,
            ),
        )
        with self.lifecycle.entry_lock(key, blocking=False):
            owner = self.lifecycle.owner(self.worker_id)
            source = self.cache.entry_path(key)
            try:
                facts = self.lifecycle.lifecycle(key)
            except CacheError as error:
                if error.code != "cache_lifecycle_unknown" or self.cache.manifest_identity(
                    source
                ) != (dict(old_identity), old_digest):
                    raise
                facts = self.lifecycle.initialize_legacy_bound(
                    key, identity=old_identity, digest=old_digest
                )
            if (
                facts["identity"] != dict(old_identity)
                or facts["digest"] != old_digest
                or facts["pinned"] is not False
                or old_identity.get("language") != new_identity.get("language")
            ):
                raise CacheError("cache_not_eligible")
            old_device, old_inode = _root_identity(source)
            new_device, new_inode = _root_identity(prepared_staging)
            verified, checked_bytes = self.cache.verify_replacement_staging(
                prepared_staging, new_identity, new_digest, budget=budget.scan
            )
            if not verified or checked_bytes != new_bytes:
                raise CacheError("cache_identity_unverified")
            self._local_safe(key)
            response = self.client.acquire_cache_guard(
                self.worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                operation_id=operation,
                replacement_context=replacement_context,
            )
            generation = _operation_fact(
                response,
                worker_id=self.worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                operation_id=operation,
            )
            binding = response.get("replacement_context")
            if not isinstance(binding, dict):
                raise CacheError("cache_guard_invalid")
            now = self.clock()
            record: dict[str, Any] = {
                "schema": SCHEMA_VERSION,
                "store_id": owner["store_id"],
                "operation_id": str(operation),
                "worker_id": self.worker_id,
                "adapter_id": adapter_id,
                "version_id": version_id,
                "key": key,
                "generation": generation,
                "identity": dict(old_identity),
                "digest": old_digest,
                "root_device": old_device,
                "root_inode": old_inode,
                "original_bytes": 0,
                "last_used_before": float(facts["last_used_at"]),
                "phase": "replacement_prepared",
                "attempts": 0,
                "next_retry_at": 0.0,
                "last_error": None,
                "resume_phase": None,
                "cleanup_context": None,
                "observed_identity": None,
                "operation_kind": "replacement",
                "replacement_context": binding,
                "new_staging_name": prepared_staging.name,
                "new_identity": dict(new_identity),
                "new_digest": new_digest,
                "new_bytes": new_bytes,
                "new_root_device": new_device,
                "new_root_inode": new_inode,
                "created_at": now,
                "updated_at": now,
            }
            path = _record_path(self.lifecycle, operation)
            _write_record(path, record)
            return self._advance_locked(
                path,
                record,
                budget=budget,
                active_replacement_context=replacement_context,
            )

    def _advance_locked(
        self,
        path: Path,
        record: dict[str, Any],
        *,
        budget: _RoundBudget,
        active_replacement_context: Mapping[str, Any] | None = None,
    ) -> DeletionResult:
        operation_id = uuid.UUID(record["operation_id"])
        key = str(record["key"])
        deadline = budget.deletion.deadline
        if record["phase"] in _TERMINAL_PHASES:
            return DeletionResult(operation_id, str(record["phase"]), int(record["original_bytes"]))
        if record["phase"] == "failed" or float(record["next_retry_at"]) > self.clock():
            return DeletionResult(operation_id, str(record["phase"]), reason=record["last_error"])
        try:
            owner = self.lifecycle.owner(self.worker_id)
            if owner["store_id"] != record["store_id"]:
                raise CacheError("cache_owner_mismatch")
            self._local_safe(key)
            guard_phase = self._guard(
                record, timeout_seconds=max(0.001, deadline - time.monotonic())
            )
            if guard_phase == "completed" and record["phase"] == "receipt_pending":
                record["phase"] = "completed"
                record["updated_at"] = self.clock()
                _write_record(path, record)
                return DeletionResult(operation_id, "completed", int(record["original_bytes"]))
            source = self.cache.entry_path(key)
            trash = _trash_path(self.lifecycle, operation_id)
            if record["phase"] in {"replacement_prepared", "replacement_old_trashed"}:
                staging = self.cache.entries / str(record["new_staging_name"])
                source_exists = source.exists()
                trash_exists = trash.exists()
                staging_exists = staging.exists()
                if record["phase"] == "replacement_prepared":
                    if source_exists and not trash_exists and staging_exists:
                        _assert_root_identity(source, record)
                        _assert_root_identity(
                            staging,
                            {
                                "root_device": record["new_root_device"],
                                "root_inode": record["new_root_inode"],
                            },
                        )
                        verified, _new_bytes = self.cache.verify_replacement_staging(
                            staging,
                            record["new_identity"],
                            str(record["new_digest"]),
                            budget=budget.scan,
                        )
                        if not verified:
                            raise CacheError("cache_identity_unverified")
                        self._local_safe(key)
                        self._guard(
                            record,
                            timeout_seconds=max(0.001, deadline - time.monotonic()),
                        )
                        if active_replacement_context is not None:
                            self._active_replacement_guard(
                                record,
                                active_replacement_context,
                                timeout_seconds=max(0.001, deadline - time.monotonic()),
                            )
                        with self.cache.accounting_lock():
                            os.replace(source, trash)
                            _sync_directory(self.cache.entries, strict=True)
                        record["phase"] = "replacement_old_trashed"
                        record["updated_at"] = self.clock()
                        _write_record(path, record)
                    elif not source_exists and trash_exists and staging_exists:
                        _assert_root_identity(trash, record)
                        record["phase"] = "replacement_old_trashed"
                        record["updated_at"] = self.clock()
                        _write_record(path, record)
                    elif source_exists and trash_exists and not staging_exists:
                        _assert_root_identity(trash, record)
                        _assert_root_identity(
                            source,
                            {
                                "root_device": record["new_root_device"],
                                "root_inode": record["new_root_inode"],
                            },
                        )
                        verified, _new_bytes = self.cache.verify_replacement_target(
                            source,
                            record["new_identity"],
                            str(record["new_digest"]),
                            budget=budget.scan,
                        )
                        if not verified:
                            raise CacheError("cache_identity_unverified")
                        record["phase"] = "replacement_old_trashed"
                    else:
                        raise CacheError("cache_cleanup_unknown")
                if record["phase"] == "replacement_old_trashed":
                    _assert_root_identity(trash, record)
                    if staging.exists() and not source.exists():
                        _assert_root_identity(
                            staging,
                            {
                                "root_device": record["new_root_device"],
                                "root_inode": record["new_root_inode"],
                            },
                        )
                        verified, new_bytes = self.cache.verify_replacement_staging(
                            staging,
                            record["new_identity"],
                            str(record["new_digest"]),
                            budget=budget.scan,
                        )
                        if not verified or new_bytes != record["new_bytes"]:
                            raise CacheError("cache_identity_unverified")
                        self._local_safe(key)
                        self._guard(
                            record,
                            timeout_seconds=max(0.001, deadline - time.monotonic()),
                        )
                        if active_replacement_context is not None:
                            self._active_replacement_guard(
                                record,
                                active_replacement_context,
                                timeout_seconds=max(0.001, deadline - time.monotonic()),
                            )
                        self.cache.publish_replacement_staging(
                            staging,
                            source,
                            identity=record["new_identity"],
                            digest=str(record["new_digest"]),
                            bytes_used=int(record["new_bytes"]),
                            observe_lifecycle=False,
                        )
                    elif not staging.exists() and source.exists():
                        _assert_root_identity(
                            source,
                            {
                                "root_device": record["new_root_device"],
                                "root_inode": record["new_root_inode"],
                            },
                        )
                        verified, new_bytes = self.cache.verify_replacement_target(
                            source,
                            record["new_identity"],
                            str(record["new_digest"]),
                            budget=budget.scan,
                        )
                        if not verified or new_bytes != record["new_bytes"]:
                            raise CacheError("cache_identity_unverified")
                    else:
                        raise CacheError("cache_cleanup_unknown")
                    self.lifecycle.replace_verified(
                        key,
                        old_identity=record["identity"],
                        old_digest=str(record["digest"]),
                        new_identity=record["new_identity"],
                        new_digest=str(record["new_digest"]),
                        bytes_used=int(record["new_bytes"]),
                    )
                    record["phase"] = "trashed"
                    record["updated_at"] = self.clock()
                    _write_record(path, record)
            if record["phase"] == "recorded":
                source_exists = source.exists()
                trash_exists = trash.exists()
                if source_exists == trash_exists:
                    raise CacheError("cache_cleanup_unknown")
                if source_exists:
                    _assert_root_identity(source, record)
                    lifecycle = self.lifecycle.lifecycle(key)
                    if (
                        lifecycle["identity"] != record["identity"]
                        or lifecycle["digest"] != record["digest"]
                        or lifecycle["pinned"] is not False
                        or lifecycle["rebuildability"] != "confirmed"
                        or float(lifecycle["last_used_at"]) > float(record["last_used_before"])
                    ):
                        raise CacheError("cache_not_eligible")
                    verified, _bytes = self.cache.verify_for_deletion_bounded(
                        source,
                        record["identity"],
                        str(record["digest"]),
                        max_nodes=budget.scan.nodes_remaining,
                        max_hash_bytes=budget.scan.hash_bytes_remaining,
                        deadline=deadline,
                        budget=budget.scan,
                    )
                    if not verified:
                        raise CacheError("cache_identity_unverified")
                    self._local_safe(key)
                    self._guard(record, timeout_seconds=max(0.001, deadline - time.monotonic()))
                    with self.cache.accounting_lock():
                        os.replace(source, trash)
                        _sync_directory(self.cache.entries, strict=True)
                else:
                    _assert_root_identity(trash, record)
                record["phase"] = "trashed"
                record["updated_at"] = self.clock()
                _write_record(path, record)
            if record["phase"] == "trashed":
                if not trash.exists():
                    raise CacheError("cache_cleanup_unknown")
                _assert_root_identity(trash, record)
                self._local_safe(key)
                self._guard(record, timeout_seconds=max(0.001, deadline - time.monotonic()))
                empty = _delete_contents(trash, budget.deletion)
                _sync_directory(self.cache.entries, strict=True)
                if not empty:
                    return DeletionResult(operation_id, "in_progress")
                record["phase"] = "empty_trash"
                record["updated_at"] = self.clock()
                _write_record(path, record)
            if record["phase"] == "empty_trash":
                self._local_safe(key)
                self._guard(record, timeout_seconds=max(0.001, deadline - time.monotonic()))
                if trash.exists():
                    _assert_root_identity(trash, record)
                    try:
                        trash.rmdir()
                    except OSError as error:
                        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                            record["phase"] = "trashed"
                            record["updated_at"] = self.clock()
                            _write_record(path, record)
                            return DeletionResult(operation_id, "in_progress")
                        raise
                _sync_directory(self.cache.entries, strict=True)
                record["phase"] = "receipt_pending"
                record["updated_at"] = self.clock()
                _write_record(path, record)
            if record["phase"] == "receipt_pending":
                response = self.client.finish_cache_guard(
                    self.worker_id,
                    operation_id,
                    generation=int(record["generation"]),
                    outcome="completed",
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
                _operation_fact(
                    response,
                    worker_id=self.worker_id,
                    adapter_id=int(record["adapter_id"]),
                    version_id=int(record["version_id"]),
                    operation_id=operation_id,
                    generation=int(record["generation"]),
                    phases=frozenset({"completed"}),
                )
                record["phase"] = "completed"
                record["updated_at"] = self.clock()
                _write_record(path, record)
                return DeletionResult(operation_id, "completed", int(record["original_bytes"]))
            raise CacheError("cache_deletion_record_invalid")
        except CacheError as error:
            if error.code in {"cache_scan_budget_exhausted", "cache_budget_exhausted"}:
                return DeletionResult(operation_id, "in_progress", reason=error.code)
            record["attempts"] = min(3, int(record["attempts"]) + 1)
            record["last_error"] = error.code
            record["next_retry_at"] = self.clock() + self.retry_seconds
            record["updated_at"] = self.clock()
            if record["attempts"] >= 3:
                record["resume_phase"] = record["phase"]
                record["phase"] = "failed"
            _write_record(path, record)
            return DeletionResult(operation_id, str(record["phase"]), reason=error.code)
        except OSError:
            record["attempts"] = min(3, int(record["attempts"]) + 1)
            record["last_error"] = "cache_delete_failed"
            record["next_retry_at"] = self.clock() + self.retry_seconds
            record["updated_at"] = self.clock()
            if record["attempts"] >= 3:
                record["resume_phase"] = record["phase"]
                record["phase"] = "failed"
            _write_record(path, record)
            return DeletionResult(operation_id, str(record["phase"]), reason="cache_delete_failed")
        except (ClientError, ControlUnavailableError) as error:
            record["attempts"] = min(3, int(record["attempts"]) + 1)
            record["last_error"] = getattr(error, "code", "cache_control_unavailable")
            record["next_retry_at"] = self.clock() + self.retry_seconds
            record["updated_at"] = self.clock()
            if record["attempts"] >= 3:
                record["resume_phase"] = record["phase"]
                record["phase"] = "failed"
            _write_record(path, record)
            return DeletionResult(
                operation_id, str(record["phase"]), reason=str(record["last_error"])
            )

    def _abort_orphan(
        self,
        guard: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        scan_budget: CacheScanBudget,
    ) -> None:
        deadline = time.monotonic() + max(0.001, timeout_seconds or 10.0)
        operation_id = uuid.UUID(str(guard["operation_id"]))
        adapter_id = int(guard["adapter_id"])
        version_id = int(guard["version_id"])
        generation = _operation_fact(
            guard,
            worker_id=self.worker_id,
            adapter_id=adapter_id,
            version_id=version_id,
            operation_id=operation_id,
        )
        operation = self.client.check_cache_guard(
            self.worker_id, operation_id, timeout_seconds=timeout_seconds
        )
        if operation.get("operation_kind", "gc") == "replacement":
            self._abort_replacement_orphan(
                operation,
                operation_id=operation_id,
                adapter_id=adapter_id,
                version_id=version_id,
                generation=generation,
                timeout_seconds=timeout_seconds,
            )
            return
        key = cache_key(adapter_id, version_id)
        with self.lifecycle.entry_lock(key, blocking=False):
            self.lifecycle.owner(self.worker_id)
            self._local_safe(key)
            source = self.cache.entry_path(key)
            trash = _trash_path(self.lifecycle, operation_id)
            try:
                source_info = source.lstat()
            except OSError as error:
                raise CacheError("cache_cleanup_unknown") from error
            if (
                not stat.S_ISDIR(source_info.st_mode)
                or stat.S_ISLNK(source_info.st_mode)
                or trash.exists()
            ):
                raise CacheError("cache_cleanup_unknown")
            lifecycle = self.lifecycle.lifecycle(key)
            verified, _bytes = self.cache.verify_for_deletion_bounded(
                source,
                lifecycle["identity"],
                str(lifecycle["digest"]),
                max_nodes=scan_budget.nodes_remaining,
                max_hash_bytes=scan_budget.hash_bytes_remaining,
                deadline=deadline,
                budget=scan_budget,
            )
            if not verified:
                raise CacheError("cache_cleanup_unknown")
            response = self.client.finish_cache_guard(
                self.worker_id,
                operation_id,
                generation=generation,
                outcome="aborted",
                timeout_seconds=max(0.001, deadline - time.monotonic()),
            )
            _operation_fact(
                response,
                worker_id=self.worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                operation_id=operation_id,
                generation=generation,
                phases=frozenset({"aborted"}),
            )

    def _abort_replacement_orphan(
        self,
        operation: Mapping[str, Any],
        *,
        operation_id: uuid.UUID,
        adapter_id: int,
        version_id: int,
        generation: int,
        timeout_seconds: float | None,
    ) -> None:
        """Abort an acquire-response loss only while the original root is untouched."""

        context = operation.get("replacement_context")
        old = context.get("old_identity") if isinstance(context, dict) else None
        if not isinstance(old, dict):
            raise CacheError("cache_cleanup_unknown")
        key = cache_key(adapter_id, version_id)
        with self.lifecycle.entry_lock(key, blocking=False):
            owner = self.lifecycle.owner(self.worker_id)
            if owner["store_id"] != old.get("store_id"):
                raise CacheError("cache_cleanup_unknown")
            self._local_safe(key)
            source = self.cache.entry_path(key)
            trash = _trash_path(self.lifecycle, operation_id)
            if trash.exists():
                raise CacheError("cache_cleanup_unknown")
            manifest = self.cache.manifest_identity(source)
            lifecycle = self.lifecycle.lifecycle(key)
            expected_identity = {
                "adapter_id": adapter_id,
                "version_id": version_id,
                "language": old.get("language"),
                "source_sha256": old.get("source_sha256"),
            }
            if (
                manifest != (expected_identity, old.get("digest"))
                or lifecycle["identity"] != expected_identity
                or lifecycle["digest"] != old.get("digest")
            ):
                raise CacheError("cache_cleanup_unknown")
            response = self.client.finish_cache_guard(
                self.worker_id,
                operation_id,
                generation=generation,
                outcome="aborted",
                timeout_seconds=timeout_seconds,
            )
            _operation_fact(
                response,
                worker_id=self.worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                operation_id=operation_id,
                generation=generation,
                phases=frozenset({"aborted"}),
            )

    def recover_round(
        self,
        *,
        max_items: int = 20,
        max_pages: int = 2,
        page_size: int = 20,
        max_nodes: int = 100_000,
        max_bytes: int = 256 * 1024 * 1024,
        max_scan_nodes: int = 100_000,
        max_hash_bytes: int = 256 * 1024 * 1024,
        max_seconds: float = 10.0,
    ) -> int:
        deadline = time.monotonic() + max_seconds
        round_budget = _RoundBudget(
            deletion=_DeleteBudget(
                nodes=max(1, max_nodes), bytes=max(1, max_bytes), deadline=deadline
            ),
            scan=CacheScanBudget(
                nodes_remaining=max_scan_nodes,
                hash_bytes_remaining=max_hash_bytes,
                deadline=deadline,
            ),
        )
        processed = 0
        cursor_path = self.lifecycle.state_root / "deletion-recovery-cursor.json"
        cursor_value = _read_bounded_json(cursor_path, max_bytes=4096)
        after: int | None = None
        local_after: str | None = None
        if cursor_value is not None:
            if (
                set(cursor_value) != {"schema", "local_after_operation_id", "after_version_id"}
                or cursor_value.get("schema") != SCHEMA_VERSION
            ):
                raise CacheError("cache_deletion_cursor_invalid")
            candidate = cursor_value.get("after_version_id")
            local_candidate = cursor_value.get("local_after_operation_id")
            if candidate is not None and not _positive(candidate):
                raise CacheError("cache_deletion_cursor_invalid")
            if local_candidate is not None:
                try:
                    uuid.UUID(str(local_candidate))
                except (TypeError, ValueError) as error:
                    raise CacheError("cache_deletion_cursor_invalid") from error
            after = candidate
            local_after = str(local_candidate) if local_candidate is not None else None
        pending_paths = _local_record_page(
            self.lifecycle.deletion_root,
            after=local_after,
            limit=max_items,
            deadline=deadline,
        )
        local_inspected = 0
        for path in pending_paths:
            if local_inspected >= max_items or time.monotonic() >= deadline:
                break
            local_inspected += 1
            local_after = path.stem
            record = _read_record(path)
            if record is None:
                continue
            if record["phase"] in _TERMINAL_PHASES:
                path.unlink()
                _sync_directory(self.lifecycle.deletion_root, strict=True)
                continue
            if record["phase"] == "failed" or float(record["next_retry_at"]) > self.clock():
                continue
            try:
                with self.lifecycle.entry_lock(str(record["key"]), blocking=False):
                    self._advance_locked(
                        path,
                        record,
                        budget=round_budget,
                    )
                    processed += 1
            except CacheError as error:
                if error.code != "cache_lock_busy":
                    raise
        for _ in range(max_pages):
            if processed >= max_items or time.monotonic() >= deadline:
                break
            guards, next_after = self.client.list_cache_guard_page(
                self.worker_id,
                after_version_id=after,
                limit=min(100, page_size, max_items - processed),
                timeout_seconds=max(0.001, deadline - time.monotonic()),
            )
            consumed_page = True
            page_after = after
            budget_deferred = False
            for guard in guards:
                if processed >= max_items or time.monotonic() >= deadline:
                    consumed_page = False
                    break
                page_after = int(guard["version_id"])
                operation_id = uuid.UUID(str(guard.get("operation_id")))
                if _record_path(self.lifecycle, operation_id).exists():
                    continue
                try:
                    self._abort_orphan(
                        guard,
                        timeout_seconds=max(0.001, deadline - time.monotonic()),
                        scan_budget=round_budget.scan,
                    )
                except CacheError as error:
                    if error.code == "cache_scan_budget_exhausted":
                        consumed_page = False
                        budget_deferred = True
                    elif error.code not in {
                        "cache_lock_busy",
                        "cache_cleanup_unknown",
                        "cache_entry_in_use",
                        "cache_journal_protected",
                    }:
                        raise
                processed += 1
                if budget_deferred:
                    break
            after = next_after if consumed_page else page_after
            if not consumed_page or next_after is None:
                break
        _write_json(
            cursor_path,
            {
                "schema": SCHEMA_VERSION,
                "local_after_operation_id": local_after,
                "after_version_id": after,
            },
            strict_sync=True,
        )
        return processed

    def cleanup_pending(self, cleanup_id: int, *, max_records: int = 1024) -> bool:
        """Return fail-closed while this cleanup still has a local operation."""

        return self.cleanup_state(cleanup_id, max_records=max_records) != "clear"

    def cleanup_state(self, cleanup_id: int, *, max_records: int = 1024) -> str:
        """Summarize local operations for one Adapter cleanup without paths."""

        inspected = 0
        pending = False
        try:
            with os.scandir(self.lifecycle.deletion_root) as entries:
                for item in entries:
                    if not item.name.endswith(".json"):
                        continue
                    inspected += 1
                    if inspected > max_records:
                        return "unknown"
                    record = _read_record(Path(item.path))
                    context = None if record is None else record.get("cleanup_context")
                    if (
                        isinstance(context, dict)
                        and context.get("cleanup_id") == cleanup_id
                        and record is not None
                        and record["phase"] not in _TERMINAL_PHASES
                    ):
                        if record["phase"] == "failed":
                            return "failed"
                        pending = True
        except OSError as error:
            raise CacheError("cache_deletion_record_unavailable") from error
        return "pending" if pending else "clear"
