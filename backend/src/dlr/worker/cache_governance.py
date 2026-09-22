"""Bounded policy selection and classified Worker cache reporting."""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import uuid
from bisect import insort
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dlr.worker.cache import CacheError, CacheScanBudget
from dlr.worker.cache_deletion import (
    CacheDeletionManager,
    DeletionEligibility,
    _RoundBudget,
)
from dlr.worker.cache_lifecycle import (
    SCHEMA_VERSION,
    CacheLifecycleStore,
    _read_bounded_json,
    _write_json,
)
from dlr.worker.cache_policy import CachePolicy, GovernedVersionCache
from dlr.worker.client import ClientError, ControlUnavailableError

_READY_KEY = re.compile(r"^(?P<adapter>[1-9][0-9]*)-(?P<version>[1-9][0-9]*)$")
_STAGING_KEY = re.compile(
    r"^\.(?P<key>[1-9][0-9]*-[1-9][0-9]*)\.staging-(?P<token>[A-Za-z0-9._-]+)$"
)


@dataclass
class CacheClassStats:
    entries: int = 0
    bytes: int = 0
    reclaimable_bytes: int = 0
    reasons: Counter[str] = field(default_factory=Counter)

    def public(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "bytes": self.bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "reasons": dict(sorted(self.reasons.items())),
        }


@dataclass(frozen=True)
class _Candidate:
    key: str
    adapter_id: int
    version_id: int
    identity: dict[str, Any]
    digest: str
    last_used_at: float
    physical_bytes: int


@dataclass(frozen=True)
class CacheScanResult:
    complete: bool
    cursor: str | None
    categories: dict[str, dict[str, Any]]
    candidates: tuple[_Candidate, ...]
    inactive_staging: tuple[tuple[Path, str, str], ...]
    retained_reasons: dict[str, int]
    committed_bytes: int
    reserved_bytes: int
    observations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CacheRoundResult:
    status: str
    scanned: int
    candidates: int
    deleted: int
    freed_bytes: int
    cursor: str | None
    retained_reasons: dict[str, int]
    child_operations: tuple[dict[str, object], ...] = ()


def _bounded_tree_size(path: Path, *, budget: CacheScanBudget) -> int:
    try:
        root_info = path.lstat()
    except OSError as error:
        raise CacheError("cache_entry_invalid") from error
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise CacheError("cache_entry_invalid")
    total = 0
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= budget.max_depth:
            raise CacheError("cache_scan_budget_exhausted")
        with os.scandir(current) as iterator:
            for item in iterator:
                budget.consume_node()
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append((Path(item.path), depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                elif not stat.S_ISLNK(info.st_mode):
                    raise CacheError("cache_entry_invalid")
    return total


class CachePolicyManager:
    """One Worker-local policy engine; every deletion still uses A primitives."""

    def __init__(
        self,
        runtime_root: Path,
        cache: GovernedVersionCache,
        lifecycle: CacheLifecycleStore,
        deletion: CacheDeletionManager,
        policy: CachePolicy,
        *,
        clock: Any = time.time,
        monotonic: Any = time.monotonic,
        shared_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.cache = cache
        self.lifecycle = lifecycle
        self.deletion = deletion
        self.policy = policy
        self.clock = clock
        self.monotonic = monotonic
        self.shared_roots = shared_roots or tuple(
            self.runtime_root / "shared-cache" / name for name in ("uv", "npm", "maven", "go")
        )
        self.cursor_path = lifecycle.state_root / "policy-scan-cursor.json"

    def _budget(self) -> _RoundBudget:
        deadline = self.monotonic() + self.policy.max_round_seconds
        return self.deletion.make_round_budget(
            max_nodes=self.policy.max_scan_nodes_per_round,
            max_bytes=self.policy.max_delete_bytes_per_round,
            max_scan_nodes=self.policy.max_scan_nodes_per_round,
            max_hash_bytes=self.policy.max_scan_hash_bytes_per_round,
            max_scan_depth=self.policy.max_scan_depth,
            deadline=deadline,
        )

    def _cursor(self) -> str | None:
        value = _read_bounded_json(self.cursor_path, max_bytes=4096)
        if value is None:
            return None
        if set(value) != {"schema", "after_name"} or value.get("schema") != SCHEMA_VERSION:
            raise CacheError("cache_policy_cursor_invalid")
        after = value.get("after_name")
        if after is not None and (not isinstance(after, str) or len(after) > 255):
            raise CacheError("cache_policy_cursor_invalid")
        return after

    def _control_clear_keys(
        self, keys: set[str], *, deadline: float
    ) -> tuple[set[str], dict[str, str]]:
        if not keys:
            return set(), {}
        requested: list[tuple[tuple[int, int], str]] = []
        for key in keys:
            adapter, version = key.split("-", 1)
            requested.append(((int(adapter), int(version)), key))
        requested.sort()
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            return set(), {key: "cache_reference_unknown" for _pair, key in requested}
        try:
            body = self.deletion.client.resolve_cache_key_references(
                self.deletion.worker_id,
                [pair for pair, _key in requested],
                timeout_seconds=remaining,
            )
        except (AttributeError, ClientError, ControlUnavailableError, OSError, TimeoutError):
            return set(), {key: "cache_reference_unknown" for _pair, key in requested}
        if (
            body.get("kind") != "cache_keys_v1"
            or body.get("worker_id") != self.deletion.worker_id
            or not isinstance(body.get("items"), list)
        ):
            return set(), {key: "cache_reference_unknown" for _pair, key in requested}
        expected = {pair: key for pair, key in requested}
        clear: set[str] = set()
        reasons: dict[str, str] = {}
        seen: set[tuple[int, int]] = set()
        for item in body["items"]:
            if not isinstance(item, dict):
                continue
            adapter_id = item.get("adapter_id")
            version_id = item.get("version_id")
            if (
                not isinstance(adapter_id, int)
                or isinstance(adapter_id, bool)
                or not isinstance(version_id, int)
                or isinstance(version_id, bool)
            ):
                return set(), {key: "cache_reference_unknown" for key in keys}
            pair = (adapter_id, version_id)
            if pair not in expected or pair in seen:
                return set(), {key: "cache_reference_unknown" for key in keys}
            seen.add(pair)
            key = expected[pair]
            status = item.get("status")
            item_reasons = item.get("reasons")
            if status == "clear" and item_reasons == []:
                clear.add(key)
            elif status == "protected":
                reasons[key] = "cache_reference_active"
            else:
                reasons[key] = "cache_reference_unknown"
        for pair, key in expected.items():
            if pair not in seen:
                reasons[key] = "cache_reference_unknown"
                clear.discard(key)
        return clear, reasons

    def _page(
        self, *, after: str | None, budget: CacheScanBudget, limit: int | None = None
    ) -> tuple[list[Path], bool]:
        names: list[str] = []
        effective_limit = (
            self.policy.max_scan_entries_per_round
            if limit is None
            else min(limit, self.policy.max_scan_entries_per_round, 200)
        )
        try:
            with os.scandir(self.cache.entries) as iterator:
                for item in iterator:
                    if self.monotonic() >= budget.deadline:
                        page_names = names[:effective_limit]
                        return [self.cache.entries / name for name in page_names], False
                    if after is not None and item.name <= after:
                        continue
                    try:
                        budget.consume_node()
                    except CacheError as error:
                        if error.code != "cache_scan_budget_exhausted":
                            raise
                        page_names = names[:effective_limit]
                        return [self.cache.entries / name for name in page_names], False
                    insort(names, item.name)
                    if len(names) > effective_limit + 1:
                        names.pop()
        except OSError as error:
            raise CacheError("cache_scan_failed") from error
        if not names and after is not None:
            return self._page(after=None, budget=budget, limit=effective_limit)
        page_names = names[:effective_limit]
        complete = len(names) <= effective_limit
        return [self.cache.entries / name for name in page_names], complete

    def _selected_paths(
        self, target_keys: frozenset[str], *, budget: CacheScanBudget
    ) -> list[Path]:
        """Resolve an explicit bounded key set without inheriting the periodic cursor."""

        paths: list[Path] = []
        for key in sorted(target_keys):
            ready = self.cache.entries / key
            try:
                ready.lstat()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise CacheError("cache_scan_failed") from error
            else:
                paths.append(ready)
        try:
            with os.scandir(self.cache.entries) as iterator:
                for item in iterator:
                    budget.consume_node()
                    match = _STAGING_KEY.fullmatch(item.name)
                    if match is not None and match.group("key") in target_keys:
                        paths.append(self.cache.entries / item.name)
        except OSError as error:
            raise CacheError("cache_scan_failed") from error
        paths.sort(key=lambda path: path.name)
        return paths

    def scan(
        self,
        *,
        mode: Literal["periodic", "pressure", "manual"],
        budget: _RoundBudget | None = None,
        target_keys: frozenset[str] | None = None,
        page_limit: int | None = None,
    ) -> CacheScanResult:
        budget = budget or self._budget()
        after = None if target_keys is not None else self._cursor()
        try:
            if target_keys is None:
                paths, page_complete = self._page(after=after, budget=budget.scan, limit=page_limit)
            else:
                paths = self._selected_paths(target_keys, budget=budget.scan)
                page_complete = True
        except CacheError as error:
            if error.code != "cache_scan_budget_exhausted":
                raise
            paths, page_complete = [], False
        # A tail page is complete for cursor progress, but it is not a
        # complete occupancy snapshot when this round started after a cursor.
        complete = page_complete and after is None
        cursor = after
        categories = {
            name: CacheClassStats()
            for name in ("versions", "shared", "staging", "trash", "unknown")
        }
        retained: Counter[str] = Counter()
        candidates: list[_Candidate] = []
        observations: dict[str, dict[str, Any]] = {}
        inactive_staging: list[tuple[Path, str, str]] = []
        staging_sizes: dict[Path, int] = {}
        reservation_covered_staging_bytes = 0
        try:
            reservation = self.cache.reservation_snapshot(blocking=False)
            active_tokens = reservation["active_reservation_tokens"]
            reserved_bytes = int(reservation["reserved_bytes"])
            reservation_complete = True
        except CacheError as error:
            if error.code != "cache_lock_busy":
                raise
            active_tokens = frozenset()
            reserved_bytes = 0
            reservation_complete = False
            complete = False
            retained["cache_accounting_busy"] += 1
        now = self.clock()
        idle_threshold = (
            self.policy.idle_ttl_seconds if mode == "periodic" else self.policy.min_idle_seconds
        )
        for path in paths:
            category = categories["unknown"]
            try:
                cursor = path.name
                ready_match = _READY_KEY.fullmatch(path.name)
                staging_match = _STAGING_KEY.fullmatch(path.name)
                if ready_match is not None:
                    category = categories["versions"]
                    inspected = self.cache.inspect_entry_bounded(path, budget=budget.scan)
                    if inspected is None:
                        category.reasons["cache_identity_unverified"] += 1
                        retained["cache_identity_unverified"] += 1
                        continue
                    category.entries += 1
                    category.bytes += int(inspected["physical_bytes"])
                    identity = dict(inspected["identity"])
                    key = path.name
                    if target_keys is not None and key not in target_keys:
                        category.reasons["cache_not_requested"] += 1
                        retained["cache_not_requested"] += 1
                        continue
                    if identity.get("adapter_id") != int(
                        ready_match.group("adapter")
                    ) or identity.get("version_id") != int(ready_match.group("version")):
                        category.reasons["cache_ownership_unknown"] += 1
                        retained["cache_ownership_unknown"] += 1
                        continue
                    try:
                        facts = self.lifecycle.lifecycle(key)
                    except CacheError:
                        category.reasons["cache_lifecycle_unknown"] += 1
                        retained["cache_lifecycle_unknown"] += 1
                        continue
                    digest = str(inspected["digest"])
                    observation = {
                        "cache_key": key,
                        "kind": "version",
                        "adapter_id": int(ready_match.group("adapter")),
                        "version_id": int(ready_match.group("version")),
                        "identity": identity,
                        "digest": digest,
                        "bytes": int(inspected["physical_bytes"]),
                        "pinned": bool(facts["pinned"]),
                        "rebuildability": str(facts["rebuildability"]),
                        "reasons": [],
                    }
                    observations[key] = observation
                    reason = self.lifecycle.reclamation_reason(
                        key,
                        identity=identity,
                        digest=digest,
                        offline_protection=self.policy.offline_protection,
                        offline_mode=self.policy.offline_mode,
                        now=now,
                    )
                    if reason is None and now - float(facts["last_used_at"]) < idle_threshold:
                        reason = "cache_recently_used"
                    if reason is not None:
                        observation["reasons"] = [reason]
                        category.reasons[reason] += 1
                        retained[reason] += 1
                        continue
                    try:
                        self.deletion.preview_local_safe(
                            key,
                            max_records=self.policy.max_scan_entries_per_round,
                            budget=budget.scan,
                        )
                    except CacheError as error:
                        observation["reasons"] = [error.code]
                        category.reasons[error.code] += 1
                        retained[error.code] += 1
                        continue
                    candidate = _Candidate(
                        key=key,
                        adapter_id=int(ready_match.group("adapter")),
                        version_id=int(ready_match.group("version")),
                        identity=identity,
                        digest=digest,
                        last_used_at=float(facts["last_used_at"]),
                        physical_bytes=int(inspected["physical_bytes"]),
                    )
                    candidates.append(candidate)
                    cursor = path.name
                    continue
                if staging_match is not None:
                    category = categories["staging"]
                    category.entries += 1
                    key = staging_match.group("key")
                    if target_keys is not None and key not in target_keys:
                        category.reasons["cache_not_requested"] += 1
                        retained["cache_not_requested"] += 1
                        cursor = path.name
                        continue
                    size = _bounded_tree_size(path, budget=budget.scan)
                    category.bytes += size
                    token = staging_match.group("token")
                    staging_reason: str | None = None
                    if not reservation_complete:
                        staging_reason = "cache_accounting_busy"
                    elif token in active_tokens:
                        staging_reason = "cache_staging_active"
                        reservation_covered_staging_bytes += size
                    elif now - path.lstat().st_mtime < self.policy.staging_ttl_seconds:
                        staging_reason = "cache_staging_recent"
                    else:
                        try:
                            self.deletion.preview_local_safe(
                                key,
                                max_records=self.policy.max_scan_entries_per_round,
                                budget=budget.scan,
                            )
                        except CacheError as error:
                            staging_reason = error.code
                        else:
                            inactive_staging.append((path, key, token))
                            staging_sizes[path] = size
                    if staging_reason is not None:
                        category.reasons[staging_reason] += 1
                        retained[staging_reason] += 1
                    observations.setdefault(
                        key,
                        {
                            "cache_key": key,
                            "kind": "staging",
                            "adapter_id": int(key.split("-", 1)[0]),
                            "version_id": int(key.split("-", 1)[1]),
                            "identity": None,
                            "digest": None,
                            "bytes": size,
                            "pinned": False,
                            "rebuildability": "unknown",
                            "reasons": [staging_reason] if staging_reason is not None else [],
                        },
                    )
                    cursor = path.name
                    continue
                if path.name.startswith(".dlr-trash-"):
                    category = categories["trash"]
                    category.entries += 1
                    category.bytes += _bounded_tree_size(path, budget=budget.scan)
                    category.reasons["cache_operation_in_progress"] += 1
                    cursor = path.name
                    continue
                category.entries += 1
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    category.bytes += _bounded_tree_size(path, budget=budget.scan)
                category.reasons["cache_ownership_unknown"] += 1
                retained["cache_ownership_unknown"] += 1
                cursor = path.name
            except CacheError as error:
                if error.code == "cache_scan_budget_exhausted":
                    category.reasons["cache_scan_budget_exhausted"] += 1
                    retained["cache_scan_budget_exhausted"] += 1
                    complete = False
                    cursor = path.name
                    break
                category.entries += 1
                category.reasons["cache_ownership_unknown"] += 1
                retained["cache_ownership_unknown"] += 1
                cursor = path.name
        if target_keys is not None or (page_complete and paths and cursor == paths[-1].name):
            cursor = None
        for shared in self.shared_roots:
            if not shared.exists():
                continue
            category = categories["shared"]
            category.entries += 1
            try:
                category.bytes += _bounded_tree_size(shared, budget=budget.scan)
                category.reasons["shared_cache_not_supported"] += 1
            except CacheError as error:
                category.reasons[error.code] += 1
                complete = False
                break
        clear_keys, control_reasons = self._control_clear_keys(
            {candidate.key for candidate in candidates}
            | {key for _path, key, _token in inactive_staging},
            deadline=budget.scan.deadline,
        )
        filtered_candidates: list[_Candidate] = []
        for candidate in candidates:
            if candidate.key in clear_keys:
                filtered_candidates.append(candidate)
                categories["versions"].reclaimable_bytes += candidate.physical_bytes
            else:
                reason = control_reasons.get(candidate.key, "cache_reference_unknown")
                observations[candidate.key]["reasons"] = [reason]
                categories["versions"].reasons[reason] += 1
                retained[reason] += 1
        filtered_staging: list[tuple[Path, str, str]] = []
        for staging, key, token in inactive_staging:
            if key in clear_keys:
                filtered_staging.append((staging, key, token))
                categories["staging"].reclaimable_bytes += staging_sizes[staging]
            else:
                reason = control_reasons.get(key, "cache_reference_unknown")
                observations[key]["reasons"] = [reason]
                categories["staging"].reasons[reason] += 1
                retained[reason] += 1
        if target_keys is None:
            _write_json(
                self.cursor_path,
                {"schema": SCHEMA_VERSION, "after_name": cursor},
                strict_sync=True,
            )
        filtered_candidates.sort(
            key=lambda item: (item.last_used_at, -item.physical_bytes, item.key)
        )
        return CacheScanResult(
            complete=complete,
            cursor=cursor,
            categories={name: value.public() for name, value in categories.items()},
            candidates=tuple(filtered_candidates),
            inactive_staging=tuple(filtered_staging),
            retained_reasons=dict(sorted(retained.items())),
            committed_bytes=sum(
                int(value.bytes) for name, value in categories.items() if name != "shared"
            )
            - reservation_covered_staging_bytes,
            reserved_bytes=reserved_bytes,
            observations=tuple(observations.values()),
        )

    def snapshot(
        self, *, mode: Literal["periodic", "pressure", "manual"] = "manual"
    ) -> dict[str, Any]:
        """Return bounded non-sensitive facts suitable for a later Worker DTO."""

        report = self.scan(mode=mode)
        return {
            "schema": SCHEMA_VERSION,
            "sampled_at": self.clock(),
            "complete": report.complete,
            "cursor": report.cursor,
            "accounting": {
                "committed_bytes": report.committed_bytes,
                "reserved_bytes": report.reserved_bytes,
            },
            "categories": report.categories,
            "retained_reasons": report.retained_reasons,
            "policy": self.policy.public_values(),
        }

    def management_snapshot(self, *, max_items: int = 200) -> dict[str, Any]:
        """Return one scan page; totals and keys share the same bounded observation."""

        report = self.scan(mode="manual", page_limit=max_items)
        failed_cursor_path = self.lifecycle.state_root / "management-failed-guard-cursor.json"
        failed_cursor_value = _read_bounded_json(failed_cursor_path, max_bytes=1024)
        failed_after = None
        if failed_cursor_value is not None:
            if (
                set(failed_cursor_value) != {"schema", "after_operation_id"}
                or failed_cursor_value.get("schema") != SCHEMA_VERSION
                or (
                    failed_cursor_value.get("after_operation_id") is not None
                    and not isinstance(failed_cursor_value.get("after_operation_id"), str)
                )
            ):
                raise CacheError("cache_lifecycle_invalid")
            failed_after = failed_cursor_value["after_operation_id"]
        failed_items, failed_next, failed_page_complete = self.deletion.failed_items_page(
            after=failed_after, max_items=100
        )
        _write_json(
            failed_cursor_path,
            {"schema": SCHEMA_VERSION, "after_operation_id": failed_next},
            strict_sync=True,
        )
        return {
            "schema": SCHEMA_VERSION,
            "sampled_at": self.clock(),
            "complete": report.complete,
            "cursor": report.cursor,
            "accounting": {
                "committed_bytes": report.committed_bytes,
                "reserved_bytes": report.reserved_bytes,
            },
            "categories": report.categories,
            "retained_reasons": report.retained_reasons,
            "policy": self.policy.public_values(),
            "items": list(report.observations[:max_items]),
            "failed_guard_items": failed_items,
            "failed_guard_cursor": failed_next,
            "failed_guard_complete": failed_page_complete and failed_after is None,
        }

    def run_round(
        self,
        *,
        mode: Literal["periodic", "pressure", "manual"],
        required_bytes: int = 0,
        target_keys: frozenset[str] | None = None,
        operation_namespace: uuid.UUID | None = None,
    ) -> CacheRoundResult:
        if mode == "periodic" and not self.policy.gc_enabled:
            return CacheRoundResult("disabled", 0, 0, 0, 0, self._cursor(), {})
        if mode == "pressure" and not self.policy.pressure_gc_enabled:
            return CacheRoundResult("disabled", 0, 0, 0, 0, self._cursor(), {})
        try:
            round_lock = self.lifecycle.entry_lock("governance-round", blocking=False)
            round_lock.__enter__()
        except CacheError as error:
            if error.code == "cache_lock_busy":
                return CacheRoundResult("busy", 0, 0, 0, 0, self._cursor(), {})
            raise
        try:
            budget = self._budget()
            report = self.scan(mode=mode, budget=budget, target_keys=target_keys)
            deleted = 0
            freed = 0
            status = "complete" if report.complete else "budget_exhausted"
            child_operations: list[dict[str, object]] = []
            low_target = self.policy.max_bytes * self.policy.low_watermark_percent // 100
            try:
                disk_free = shutil.disk_usage(self.cache.root).free
            except OSError:
                disk_free = 0

            def pressure_satisfied() -> bool:
                if mode != "pressure" or not report.complete:
                    return False
                used_after = max(0, report.committed_bytes + report.reserved_bytes - freed)
                capacity_after = min(
                    self.policy.max_bytes - used_after,
                    disk_free + freed - self.policy.disk_reserve_bytes,
                )
                if required_bytes > 0:
                    return capacity_after >= required_bytes
                return (
                    used_after <= low_target and disk_free + freed >= self.policy.disk_reserve_bytes
                )

            for staging, key, token in report.inactive_staging:
                if pressure_satisfied():
                    break
                if deleted >= self.policy.max_delete_entries_per_round:
                    status = "budget_exhausted"
                    break
                try:
                    complete, removed = self.deletion.cleanup_inactive_staging(
                        staging,
                        key=key,
                        reservation_token=token,
                        older_than=self.clock() - self.policy.staging_ttl_seconds,
                        budget=budget,
                        max_records=self.policy.max_scan_entries_per_round,
                    )
                    freed += removed
                    if complete:
                        deleted += 1
                        if pressure_satisfied():
                            break
                    else:
                        status = "budget_exhausted"
                        break
                except CacheError as error:
                    if error.code in {
                        "cache_lock_busy",
                        "cache_staging_active",
                        "cache_entry_in_use",
                        "cache_journal_protected",
                        "cache_cleanup_unknown",
                    }:
                        continue
                    if error.code in {"cache_budget_exhausted", "cache_scan_budget_exhausted"}:
                        status = "budget_exhausted"
                        break
                    raise
            for candidate in report.candidates:
                if pressure_satisfied():
                    break
                if deleted >= self.policy.max_delete_entries_per_round:
                    status = "budget_exhausted"
                    break
                remaining = self.policy.max_delete_bytes_per_round - budget.deletion.removed_bytes
                if candidate.physical_bytes > remaining:
                    status = "budget_exhausted"
                    continue
                try:
                    result = self.deletion.begin(
                        DeletionEligibility(
                            adapter_id=candidate.adapter_id,
                            version_id=candidate.version_id,
                            identity=candidate.identity,
                            digest=candidate.digest,
                            last_used_before=candidate.last_used_at,
                        ),
                        operation_id=(
                            uuid.uuid5(operation_namespace, candidate.key)
                            if operation_namespace is not None
                            else None
                        ),
                        management_operation_id=operation_namespace,
                        max_seconds=max(0.001, budget.deletion.deadline - self.monotonic()),
                        round_budget=budget,
                    )
                except CacheError as error:
                    if error.code in {
                        "cache_lock_busy",
                        "cache_recently_used",
                        "cache_pinned",
                        "cache_rebuild_unknown",
                        "cache_rebuild_unavailable",
                    }:
                        continue
                    raise
                if result.status == "completed":
                    deleted += 1
                    freed += result.freed_bytes
                child_operations.append(self.deletion.operation_fact(result.operation_id))
                if pressure_satisfied():
                    break
            if not report.candidates and status == "complete":
                status = "no_safe_candidates"
            return CacheRoundResult(
                status=status,
                scanned=sum(value["entries"] for value in report.categories.values()),
                candidates=len(report.candidates),
                deleted=deleted,
                freed_bytes=freed,
                cursor=report.cursor,
                retained_reasons=report.retained_reasons,
                child_operations=tuple(child_operations),
            )
        finally:
            round_lock.__exit__(None, None, None)

    def pressure_cleanup(self, required_bytes: int) -> bool:
        result = self.run_round(mode="pressure", required_bytes=required_bytes)
        return result.freed_bytes > 0

    def pressure_due(self) -> bool:
        report = self.scan(mode="pressure")
        if not report.complete:
            return True
        used = report.committed_bytes + report.reserved_bytes
        high = self.policy.max_bytes * self.policy.high_watermark_percent // 100
        try:
            disk_low = shutil.disk_usage(self.cache.root).free <= self.policy.disk_reserve_bytes
        except OSError:
            return True
        return used >= high or disk_low
