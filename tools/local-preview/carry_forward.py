#!/usr/bin/env python3
"""Read-only evidence verifier for an explicit local-preview carry-forward.

The controller passes only private files to this program.  Successful output is
limited to canonical hashes, counts and stable codes; raw database values and
journal tokens never reach stdout.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import decimal
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


FORMAT_VERSION = 2
MANIFEST_ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TERMINAL_EXECUTIONS = {"succeeded", "dead_letter", "cancelled", "expired"}
ACTIVE_ATTEMPTS = {"claimed", "running"}
TERMINAL_ATTEMPTS = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "worker_lost",
    "resource_exceeded",
}
RESPONSIBILITY_TABLES = (
    "executions",
    "execution_attempts",
    "adapter_execution_slots",
    "execution_infrastructure_incidents",
    "execution_outbox",
    "adapter_execution_admission",
    "global_execution_admission",
    "execution_input_artifact_leases",
    "execution_artifact_holds",
    "execution_credential_binding_snapshots",
    "execution_idempotency_records",
    "schedule_dispatch_outcomes",
    "worker_cleanup_requests",
)
MAX_CAPTURE_ENTRIES = 100_000
MAX_CAPTURE_BYTES = 16 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
EMPTY_TABLES_BY_REVISION = {
    "0040_issue152_dispositions": ("execution_incident_dispositions",),
}
SCHEMA_ADDITIONS = {
    ("0038_issue138_languages", "0039_issue134_reconcile"): {
        "runtime_reconciliation_cursors"
    },
    ("0039_issue134_reconcile", "0040_issue152_dispositions"): {
        "execution_incident_dispositions"
    },
    ("0038_issue138_languages", "0040_issue152_dispositions"): {
        "runtime_reconciliation_cursors",
        "execution_incident_dispositions",
    },
    ("0040_issue152_dispositions", "0040_issue152_dispositions"): set(),
}


class CarryForwardError(RuntimeError):
    """A stable fail-closed verifier result."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9_]+", code):
            raise ValueError("invalid verifier error code")
        super().__init__(code)
        self.code = code


def _positive(value: Any, code: str = "selection_invalid") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CarryForwardError(code)
    return value


def canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise CarryForwardError("noncanonical_value")
        return value
    if isinstance(value, decimal.Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        normalized = value
        if isinstance(value, dt.datetime) and value.tzinfo is not None:
            normalized = value.astimezone(dt.timezone.utc)
        return {"$datetime": normalized.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CarryForwardError("noncanonical_value")
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    raise CarryForwardError("noncanonical_value")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _secure_file(path: Path, *, output: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CarryForwardError("private_path_invalid")
    parent = path.parent
    try:
        parent_info = parent.stat()
    except OSError as error:
        raise CarryForwardError("private_parent_invalid") from error
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or parent_info.st_mode & 0o077
    ):
        raise CarryForwardError("private_parent_invalid")
    if output and not path.exists():
        return
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError("private_file_invalid") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise CarryForwardError("private_file_invalid")


def read_private(path: Path) -> Any:
    _secure_file(path)
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("private_json_invalid") from error


def write_private(path: Path, value: Any) -> None:
    _secure_file(path, output=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=".carry-forward-", delete=False
    ) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(canonical_bytes(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"queued", "cleanup_execution_ids"}:
        raise CarryForwardError("selection_invalid")
    queued = value["queued"]
    cleanup = value["cleanup_execution_ids"]
    if not isinstance(queued, list) or not isinstance(cleanup, list):
        raise CarryForwardError("selection_invalid")
    normalized_queued: list[dict[str, Any]] = []
    seen_executions: set[int] = set()
    seen_incidents: set[int] = set()
    for item in queued:
        if not isinstance(item, dict) or set(item) != {"execution_id", "incident_ids"}:
            raise CarryForwardError("selection_invalid")
        execution_id = _positive(item["execution_id"])
        incidents = item["incident_ids"]
        if not isinstance(incidents, list) or not incidents:
            raise CarryForwardError("selection_invalid")
        incident_ids = [_positive(entry) for entry in incidents]
        if len(set(incident_ids)) != len(incident_ids):
            raise CarryForwardError("selection_duplicate")
        if execution_id in seen_executions or seen_incidents.intersection(incident_ids):
            raise CarryForwardError("selection_duplicate")
        seen_executions.add(execution_id)
        seen_incidents.update(incident_ids)
        normalized_queued.append(
            {"execution_id": execution_id, "incident_ids": sorted(incident_ids)}
        )
    cleanup_ids = [_positive(entry) for entry in cleanup]
    if len(set(cleanup_ids)) != len(cleanup_ids) or seen_executions.intersection(
        cleanup_ids
    ):
        raise CarryForwardError("selection_duplicate")
    if not normalized_queued and not cleanup_ids:
        raise CarryForwardError("selection_empty")
    return {
        "queued": sorted(normalized_queued, key=lambda item: item["execution_id"]),
        "cleanup_execution_ids": sorted(cleanup_ids),
    }


def selected_execution_ids(selection: dict[str, Any]) -> set[int]:
    return {
        *(item["execution_id"] for item in selection["queued"]),
        *selection["cleanup_execution_ids"],
    }


def datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError as error:
            raise CarryForwardError("attempt_lease_invalid") from error
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.isoformat()
    raise CarryForwardError("attempt_lease_invalid")


def row_digest(row: dict[str, Any]) -> str:
    return digest(row)


def project_rows(
    tables: dict[str, dict[str, Any]],
    *,
    required: tuple[str, ...] = RESPONSIBILITY_TABLES,
) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for name in required:
        table = tables.get(name)
        if not isinstance(table, dict):
            raise CarryForwardError("schema_table_missing")
        columns, primary_key, rows = (
            table.get("columns"),
            table.get("primary_key"),
            table.get("rows"),
        )
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(column, str) for column in columns)
            or len(set(columns)) != len(columns)
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
            or not all(
                isinstance(row, dict) and set(row) == set(columns) for row in rows
            )
        ):
            raise CarryForwardError("schema_projection_invalid")
        rows = sorted(
            rows, key=lambda row: canonical_bytes([row[key] for key in primary_key])
        )
        projection[name] = {
            "columns": columns,
            "primary_key": primary_key,
            "rows": [row_digest(row) for row in rows],
            "count": len(rows),
        }
    return projection


def _by_execution(
    rows: list[dict[str, Any]], execution_id: int
) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("execution_id") == execution_id]


def derive_responsibilities(
    tables: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> dict[str, Any]:
    selection = normalize_selection(selection)
    selected = selected_execution_ids(selection)
    rows = {name: value["rows"] for name, value in tables.items()}
    executions = {row.get("id"): row for row in rows["executions"]}
    attempts = rows["execution_attempts"]
    incidents = {
        row.get("id"): row for row in rows["execution_infrastructure_incidents"]
    }
    active = [row for row in attempts if row.get("status") in ACTIVE_ATTEMPTS]
    if active:
        raise CarryForwardError("active_attempt_present")
    if any(
        row.get("active_attempt_id") is not None
        for row in rows["adapter_execution_slots"]
    ):
        raise CarryForwardError("active_slot_present")
    if any(
        row.get("status") in {"pending", "running"}
        for row in rows["worker_cleanup_requests"]
    ):
        raise CarryForwardError("worker_cleanup_active")
    for row in rows["executions"]:
        if (
            row.get("status") in {"queued", "running", "retry_wait"}
            and row.get("id") not in selected
        ):
            raise CarryForwardError("unselected_execution_busy")
        if (
            row.get("workspace_cleanup_status") in {"pending", "deferred"}
            and row.get("id") not in selected
        ):
            raise CarryForwardError("unselected_cleanup_responsibility")
    for row in rows["execution_attempts"]:
        summary = row.get("cleanup_summary")
        if (
            isinstance(summary, dict)
            and summary.get("workspace_cleanup_status") == "deferred"
            and row.get("execution_id") not in selected
        ):
            raise CarryForwardError("unselected_cleanup_responsibility")

    result: list[dict[str, Any]] = []
    for queued in selection["queued"]:
        execution_id = queued["execution_id"]
        execution = executions.get(execution_id)
        if not execution or execution.get("status") != "queued":
            raise CarryForwardError("queued_execution_invalid")
        if execution.get("dispatch_backend") != "rabbitmq":
            raise CarryForwardError("queued_backend_invalid")
        if execution.get("admission_released_at") is not None:
            raise CarryForwardError("queued_admission_released")
        adapter_id = execution.get("adapter_id")
        adapter_admission = next(
            (
                row
                for row in rows["adapter_execution_admission"]
                if row.get("adapter_id") == adapter_id
            ),
            None,
        )
        global_admission = next(
            (
                row
                for row in rows["global_execution_admission"]
                if row.get("singleton_key") == "global"
            ),
            None,
        )
        if (
            not adapter_admission
            or not isinstance(adapter_admission.get("outstanding_count"), int)
            or adapter_admission["outstanding_count"] <= 0
            or not global_admission
            or not isinstance(global_admission.get("outstanding_count"), int)
            or global_admission["outstanding_count"] <= 0
        ):
            raise CarryForwardError("queued_admission_invalid")
        outbox = [
            row
            for row in rows["execution_outbox"]
            if row.get("execution_id") == execution_id
            and row.get("dispatch_generation") == execution.get("dispatch_generation")
        ]
        if len(outbox) != 1:
            raise CarryForwardError("queued_outbox_missing")
        execution_incidents = []
        for incident_id in queued["incident_ids"]:
            incident = incidents.get(incident_id)
            if (
                not incident
                or incident.get("execution_id") != execution_id
                or incident.get("status") != "open"
                or incident.get("dispatch_generation")
                != execution.get("dispatch_generation")
            ):
                raise CarryForwardError("queued_incident_invalid")
            execution_incidents.append(incident_id)
        all_open_incidents = {
            row.get("id")
            for row in rows["execution_infrastructure_incidents"]
            if row.get("execution_id") == execution_id and row.get("status") == "open"
        }
        if all_open_incidents != set(execution_incidents):
            raise CarryForwardError("queued_incident_unselected")
        result.append(
            _responsibility(
                execution, _by_execution(attempts, execution_id), execution_incidents
            )
        )
    for execution_id in selection["cleanup_execution_ids"]:
        execution = executions.get(execution_id)
        if not execution or execution.get("status") not in TERMINAL_EXECUTIONS:
            raise CarryForwardError("cleanup_execution_invalid")
        result.append(
            _responsibility(execution, _by_execution(attempts, execution_id), [])
        )
    return {"executions": sorted(result, key=lambda item: item["execution_id"])}


def _responsibility(
    execution: dict[str, Any], attempts: list[dict[str, Any]], incident_ids: list[int]
) -> dict[str, Any]:
    execution_id = _positive(execution.get("id"), "execution_identity_invalid")
    attempt_ids = sorted(
        _positive(row.get("id"), "attempt_identity_invalid") for row in attempts
    )
    attempt_count = execution.get("attempt_count")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
    ):
        raise CarryForwardError("execution_attempt_count_invalid")
    if attempt_count != len(attempts):
        raise CarryForwardError("execution_attempt_count_invalid")
    if any(row.get("status") not in TERMINAL_ATTEMPTS for row in attempts):
        raise CarryForwardError("attempt_state_unknown")
    attempt_cleanup: dict[int, str] = {}
    for row in attempts:
        summary = row.get("cleanup_summary")
        status = (
            summary.get("workspace_cleanup_status")
            if isinstance(summary, dict)
            else None
        )
        if status not in {"completed", "deferred"}:
            raise CarryForwardError("attempt_cleanup_state_unknown")
        attempt_cleanup[row["id"]] = status
    deferred_attempt_ids = sorted(
        attempt_id
        for attempt_id, status in attempt_cleanup.items()
        if status == "deferred"
    )
    cleanup_status = execution.get("workspace_cleanup_status")
    if (
        attempt_count == 0
        and not attempts
        and execution.get("worker_id") is None
        and execution.get("started_at") is None
        and cleanup_status == "pending"
    ):
        cleanup = "not_applicable"
    elif attempts and cleanup_status == "completed" and not deferred_attempt_ids:
        cleanup = "completed"
    elif (
        attempts
        and cleanup_status in {"completed", "deferred"}
        and deferred_attempt_ids
    ):
        cleanup = "deferred_preserved"
    else:
        raise CarryForwardError("cleanup_state_unknown")
    return {
        "execution_id": execution_id,
        "status": execution.get("status"),
        "generation": execution.get("dispatch_generation"),
        "attempt_count": attempt_count,
        "attempt_ids": attempt_ids,
        "deferred_attempt_ids": deferred_attempt_ids,
        "attempts": [
            {
                "attempt_id": row["id"],
                "attempt_no": row.get("attempt_no"),
                "fencing_token": row.get("fencing_token"),
                "status": row.get("status"),
                "lease_expires_at": datetime_text(row.get("lease_expires_at")),
                "cleanup_status": attempt_cleanup[row["id"]],
            }
            for row in sorted(attempts, key=lambda row: row["id"])
        ],
        "incident_ids": incident_ids,
        "cleanup": cleanup,
    }


def compare_projection(before: dict[str, Any], after: dict[str, Any]) -> None:
    if set(before) != set(after):
        raise CarryForwardError("projection_table_changed")
    for table in before:
        left, right = before[table], after[table]
        if left.get("columns") != right.get("columns"):
            raise CarryForwardError("projection_columns_changed")
        if left.get("primary_key") != right.get("primary_key"):
            raise CarryForwardError("projection_primary_key_changed")
        if left.get("rows") != right.get("rows") or left.get("count") != right.get(
            "count"
        ):
            raise CarryForwardError("projection_rows_changed")


def validate_projection_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(RESPONSIBILITY_TABLES):
        raise CarryForwardError("projection_table_changed")
    for table in RESPONSIBILITY_TABLES:
        item = value[table]
        if not isinstance(item, dict) or set(item) != {
            "columns",
            "primary_key",
            "rows",
            "count",
        }:
            raise CarryForwardError("schema_projection_invalid")
        columns, primary_key, rows = item["columns"], item["primary_key"], item["rows"]
        if (
            not isinstance(columns, list)
            or not columns
            or not all(
                isinstance(column, str) and re.fullmatch(r"[a-z][a-z0-9_]*", column)
                for column in columns
            )
            or len(set(columns)) != len(columns)
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
            or not all(isinstance(row, str) and DIGEST.fullmatch(row) for row in rows)
            or item["count"] != len(rows)
        ):
            raise CarryForwardError("schema_projection_invalid")
    return value


def _safe_tree(
    root: Path,
    *,
    allowed_top: set[str] | None = None,
    ignored_top: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        root_info = root.lstat()
    except OSError as error:
        raise CarryForwardError("storage_root_unavailable") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise CarryForwardError("storage_root_invalid")
    root_device = root_info.st_dev
    result: list[dict[str, Any]] = []
    total_bytes = 0
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise CarryForwardError("storage_read_failed") from error
        for child in children:
            try:
                info = child.lstat()
            except OSError as error:
                raise CarryForwardError("storage_read_failed") from error
            if info.st_dev != root_device:
                raise CarryForwardError("storage_device_changed")
            if (
                parent == root
                and allowed_top is not None
                and child.name not in allowed_top
            ):
                if child.name in (ignored_top or set()):
                    continue
                raise CarryForwardError("storage_entry_unknown")
            relative = child.relative_to(root).as_posix()
            metadata = {
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mtime_ns": info.st_mtime_ns,
            }
            if stat.S_ISDIR(info.st_mode):
                result.append({"path": relative, "type": "directory", **metadata})
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > MAX_CAPTURE_BYTES:
                    raise CarryForwardError("storage_capture_limit")
                hasher = hashlib.sha256()
                descriptor = -1
                try:
                    descriptor = os.open(
                        child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                    opened = os.fstat(descriptor)
                    stable = (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_mode,
                        opened.st_uid,
                        opened.st_gid,
                        opened.st_size,
                        opened.st_mtime_ns,
                    )
                    expected = (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_uid,
                        info.st_gid,
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    if stable != expected or not stat.S_ISREG(opened.st_mode):
                        raise CarryForwardError("storage_changed_during_read")
                    with os.fdopen(descriptor, "rb") as source:
                        descriptor = -1
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            hasher.update(block)
                except OSError as error:
                    raise CarryForwardError("storage_read_failed") from error
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                result.append(
                    {
                        "path": relative,
                        "type": "file",
                        **metadata,
                        "size": info.st_size,
                        "sha256": hasher.hexdigest(),
                    }
                )
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(child)
                except OSError as error:
                    raise CarryForwardError("storage_read_failed") from error
                result.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        **metadata,
                        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
                    }
                )
            else:
                raise CarryForwardError("storage_entry_unknown")
            if len(result) > MAX_CAPTURE_ENTRIES:
                raise CarryForwardError("storage_capture_limit")
    return sorted(result, key=lambda item: item["path"])


def _root_identity(root: Path) -> dict[str, int]:
    try:
        info = root.lstat()
    except OSError as error:
        raise CarryForwardError("storage_root_unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CarryForwardError("storage_root_invalid")
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
    }


def _validate_directory(
    path: Path, *, modes: set[int], expected_uid: int | None, code: str
) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError(code) from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) not in modes
        or (expected_uid is not None and info.st_uid != expected_uid)
    ):
        raise CarryForwardError(code)


def _validate_lock(path: Path, expected_uid: int | None) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError("storage_lock_invalid") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (expected_uid is not None and info.st_uid != expected_uid)
    ):
        raise CarryForwardError("storage_lock_invalid")


def _load_closed_json(
    path: Path, fields: set[str], code: str, *, expected_uid: int | None = None
) -> dict[str, Any]:
    descriptor = -1
    try:
        info = path.lstat()
        if info.st_size > MAX_JSON_BYTES:
            raise CarryForwardError(code)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        ):
            raise CarryForwardError(code)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (expected_uid is not None and info.st_uid != expected_uid)
        or not isinstance(value, dict)
        or set(value) != fields
    ):
        raise CarryForwardError(code)
    return value


def _workspace_identity(
    runtime_root: Path, execution_id: int, attempt_id: int, expected_uid: int | None
) -> bool:
    attempt_root = runtime_root / "workspaces" / f"attempt-{attempt_id}"
    workspace = attempt_root / f"dlr-exec-{execution_id}"
    try:
        workspace.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CarryForwardError("workspace_identity_invalid") from error
    for path in (runtime_root, runtime_root / "workspaces", attempt_root, workspace):
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (expected_uid is not None and info.st_uid != expected_uid)
        ):
            raise CarryForwardError("workspace_identity_invalid")
    if stat.S_IMODE(workspace.lstat().st_mode) != 0o700:
        raise CarryForwardError("workspace_identity_invalid")
    marker = _load_closed_json(
        workspace / ".dlr-execution-workspace",
        {"execution_id", "format"},
        "workspace_identity_invalid",
        expected_uid=expected_uid,
    )
    manifest = _load_closed_json(
        workspace / "input_manifest.json",
        {"execution_id", "files"},
        "workspace_identity_invalid",
        expected_uid=expected_uid,
    )
    if (
        marker != {"execution_id": execution_id, "format": 1}
        or manifest.get("execution_id") != execution_id
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) > 8
    ):
        raise CarryForwardError("workspace_identity_invalid")
    descriptor_fields = {
        "artifact_id",
        "ordinal",
        "mount_name",
        "original_filename",
        "content_type",
        "size_bytes",
        "sha256",
    }
    for ordinal, descriptor in enumerate(manifest["files"]):
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != descriptor_fields
            or not isinstance(descriptor.get("artifact_id"), int)
            or isinstance(descriptor.get("artifact_id"), bool)
            or descriptor["artifact_id"] <= 0
            or descriptor.get("ordinal") != ordinal
            or not isinstance(descriptor.get("mount_name"), str)
            or not re.fullmatch(
                r"input-[0-9]{2}(?:\.[A-Za-z0-9]+)?", descriptor["mount_name"]
            )
            or not isinstance(descriptor.get("original_filename"), str)
            or not isinstance(descriptor.get("content_type"), str)
            or not isinstance(descriptor.get("size_bytes"), int)
            or isinstance(descriptor.get("size_bytes"), bool)
            or descriptor["size_bytes"] < 0
            or not isinstance(descriptor.get("sha256"), str)
            or not DIGEST.fullmatch(descriptor["sha256"])
        ):
            raise CarryForwardError("workspace_identity_invalid")
    return True


def _journal_facts(
    runtime_root: Path,
    journal_root: Path,
    expected_uid: int | None = None,
    credential_hashes: dict[int, dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    cleanup_fields = {
        "cleanup_token",
        "execution_id",
        "protocol_version",
        "workspace_path",
        "attempt_id",
    }
    attempt_fields = {
        "attempt_id",
        "attempt_no",
        "claim_token",
        "cleanup_token",
        "execution_id",
        "fencing_token",
        "lease_expires_at",
        "protocol_version",
        "workspace_path",
    }
    cleanup: list[dict[str, Any]] = []
    for path in sorted(journal_root.iterdir(), key=lambda item: item.name):
        match = re.fullmatch(
            r"execution-([1-9][0-9]*)-attempt-([1-9][0-9]*)\.cleanup\.json",
            path.name,
        )
        if not match:
            continue
        value = _load_closed_json(
            path, cleanup_fields, "cleanup_journal_invalid", expected_uid=expected_uid
        )
        execution_id, attempt_id = map(int, match.groups())
        if (
            value["execution_id"] != execution_id
            or value["attempt_id"] != attempt_id
            or value["protocol_version"] != 3
            or not isinstance(value["cleanup_token"], str)
            or not value["cleanup_token"]
            or not isinstance(value["workspace_path"], str)
            or value["workspace_path"]
            != f"/var/lib/dlr/runtime/workspaces/attempt-{attempt_id}/dlr-exec-{execution_id}"
        ):
            raise CarryForwardError("cleanup_journal_invalid")
        cleanup.append(
            {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "cleanup_token_matches": (
                    hmac.compare_digest(
                        hashlib.sha256(value["cleanup_token"].encode()).hexdigest(),
                        expected,
                    )
                    if credential_hashes is not None
                    and isinstance(
                        expected := credential_hashes.get(attempt_id, {}).get(
                            "cleanup_token_hash"
                        ),
                        str,
                    )
                    else None
                ),
                "workspace_suffix": (
                    f"workspaces/attempt-{attempt_id}/dlr-exec-{execution_id}"
                ),
                "workspace_present": _workspace_identity(
                    runtime_root, execution_id, attempt_id, expected_uid
                ),
            }
        )
    attempt: list[dict[str, Any]] = []
    attempt_root = runtime_root / "attempt-journal"
    if attempt_root.exists():
        for path in sorted(attempt_root.iterdir(), key=lambda item: item.name):
            match = re.fullmatch(r"attempt-([1-9][0-9]*)\.attempt\.json", path.name)
            if not match:
                continue
            value = _load_closed_json(
                path,
                attempt_fields,
                "attempt_journal_invalid",
                expected_uid=expected_uid,
            )
            attempt_id = int(match.group(1))
            positive = ("execution_id", "attempt_id", "attempt_no", "fencing_token")
            if (
                any(
                    not isinstance(value[key], int)
                    or isinstance(value[key], bool)
                    or value[key] <= 0
                    for key in positive
                )
                or value["attempt_id"] != attempt_id
                or value["protocol_version"] != 3
                or not isinstance(value["claim_token"], str)
                or not value["claim_token"]
                or not isinstance(value["cleanup_token"], str)
                or not value["cleanup_token"]
                or not isinstance(value["workspace_path"], str)
                or value["workspace_path"]
                != (
                    f"/var/lib/dlr/runtime/workspaces/attempt-{attempt_id}/"
                    f"dlr-exec-{value['execution_id']}"
                )
            ):
                raise CarryForwardError("attempt_journal_invalid")
            attempt.append(
                {
                    "execution_id": value["execution_id"],
                    "attempt_id": attempt_id,
                    "attempt_no": value["attempt_no"],
                    "fencing_token": value["fencing_token"],
                    "claim_token_matches": (
                        hmac.compare_digest(
                            hashlib.sha256(value["claim_token"].encode()).hexdigest(),
                            expected_claim,
                        )
                        if credential_hashes is not None
                        and isinstance(
                            expected_claim := credential_hashes.get(attempt_id, {}).get(
                                "claim_token_hash"
                            ),
                            str,
                        )
                        else None
                    ),
                    "cleanup_token_matches": (
                        hmac.compare_digest(
                            hashlib.sha256(value["cleanup_token"].encode()).hexdigest(),
                            expected_cleanup,
                        )
                        if credential_hashes is not None
                        and isinstance(
                            expected_cleanup := credential_hashes.get(
                                attempt_id, {}
                            ).get("cleanup_token_hash"),
                            str,
                        )
                        else None
                    ),
                    "lease_expires_at": datetime_text(value["lease_expires_at"]),
                }
            )
    recovery: list[dict[str, Any]] = []
    recovery_root = journal_root / "sandbox-recovery"
    _validate_directory(
        recovery_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="sandbox_recovery_invalid",
    )
    marker_fields = {
        "cgroup_name",
        "execution_id",
        "mount_name",
        "mount_path",
        "namespace_identity",
        "cgroup_device",
        "cgroup_inode",
    }
    namespace_fields = {
        "boot_id",
        "parent_device",
        "parent_inode",
        "root_device",
        "root_inode",
    }
    for path in sorted(recovery_root.iterdir(), key=lambda item: item.name):
        value = _load_closed_json(
            path, marker_fields, "sandbox_recovery_invalid", expected_uid=expected_uid
        )
        name = value.get("cgroup_name")
        attempt_match = (
            re.fullmatch(r"attempt-([1-9][0-9]*)-([1-9][0-9]*)", name)
            if isinstance(name, str)
            else None
        )
        preflight_match = (
            re.fullmatch(r"dlr-preflight-([0-9a-f]{16,64})", name)
            if isinstance(name, str)
            else None
        )
        if (
            (attempt_match is None and preflight_match is None)
            or path.name != f"sandbox-{name}.json"
            or value.get("mount_name") != ".dlr-sandbox-mount"
            or type(value.get("execution_id")) is not int
            or value["execution_id"] <= 0
            or type(value.get("cgroup_device")) is not int
            or value["cgroup_device"] < 0
            or type(value.get("cgroup_inode")) is not int
            or value["cgroup_inode"] < 0
        ):
            raise CarryForwardError("sandbox_recovery_invalid")
        identity = value.get("namespace_identity")
        if (
            not isinstance(identity, dict)
            or set(identity) != namespace_fields
            or not isinstance(identity.get("boot_id"), str)
            or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                identity["boot_id"],
            )
            or any(
                type(identity[key]) is not int or identity[key] < 0
                for key in namespace_fields - {"boot_id"}
            )
        ):
            raise CarryForwardError("sandbox_recovery_invalid")
        if attempt_match is not None:
            execution_id, attempt_id = map(int, attempt_match.groups())
            expected_parent = runtime_root / "workspaces" / f"attempt-{attempt_id}"
            expected_mount = expected_parent / ".dlr-sandbox-mount"
            kind = "attempt"
            if value["execution_id"] != execution_id:
                raise CarryForwardError("sandbox_recovery_invalid")
        else:
            execution_id, attempt_id = 1, None
            expected_parent = runtime_root / name
            expected_mount = expected_parent / ".dlr-sandbox-mount"
            kind = "preflight"
            if value["execution_id"] != 1:
                raise CarryForwardError("sandbox_recovery_invalid")
        _validate_directory(
            expected_parent,
            modes={0o700},
            expected_uid=expected_uid,
            code="sandbox_recovery_invalid",
        )
        if value.get("mount_path") != str(expected_mount):
            raise CarryForwardError("sandbox_recovery_invalid")
        if expected_mount.exists():
            _validate_directory(
                expected_mount,
                modes={0o700},
                expected_uid=expected_uid,
                code="sandbox_recovery_invalid",
            )
        recovery.append(
            {
                "kind": kind,
                "cgroup_name": name,
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "namespace_identity": identity,
                "cgroup_device": value["cgroup_device"],
                "cgroup_inode": value["cgroup_inode"],
                "marker_fingerprint": digest(value),
            }
        )
    return {"cleanup": cleanup, "attempt": attempt, "sandbox_recovery": recovery}


def capture_files(
    runtime_root: Path,
    journal_root: Path,
    material_roots: dict[str, Path] | None = None,
    expected_uid: int | None = None,
    credential_hashes: dict[int, dict[str, str | None]] | None = None,
    attempt_statuses: dict[int, str] | None = None,
) -> dict[str, Any]:
    _validate_directory(
        runtime_root,
        modes={0o700, 0o711},
        expected_uid=expected_uid,
        code="runtime_root_invalid",
    )
    _validate_directory(
        journal_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="journal_root_invalid",
    )
    _validate_lock(runtime_root / ".dlr-instance.lock", expected_uid)
    _validate_lock(journal_root / ".dlr-instance.lock", expected_uid)
    attempt_root = runtime_root / "attempt-journal"
    _validate_directory(
        attempt_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="attempt_journal_root_invalid",
    )
    _validate_lock(attempt_root / ".dlr-instance.lock", expected_uid)
    workspaces = runtime_root / "workspaces"
    _validate_directory(
        workspaces,
        modes={0o700},
        expected_uid=expected_uid,
        code="workspace_root_invalid",
    )
    preflight_roots = {
        child.name
        for child in runtime_root.iterdir()
        if re.fullmatch(r"dlr-preflight-[0-9a-f]{16,64}", child.name)
    }
    runtime = _safe_tree(
        runtime_root,
        allowed_top={
            ".dlr-instance.lock",
            "attempt-journal",
            "version-cache",
            "workspaces",
            *preflight_roots,
        },
    )
    journal = _safe_tree(journal_root)
    materials = {
        name: {
            "root": _root_identity(path),
            "entries": (entries := _safe_tree(path)),
            "digest": digest(entries),
        }
        for name, path in sorted((material_roots or {}).items())
    }
    journal_facts = _journal_facts(
        runtime_root, journal_root, expected_uid, credential_hashes
    )
    associated_attempts = {
        item["attempt_id"]
        for name in ("cleanup", "attempt", "sandbox_recovery")
        for item in journal_facts[name]
        if type(item.get("attempt_id")) is int
    }
    workspace_device = workspaces.lstat().st_dev
    empty_attempt_shells = []
    for child in sorted(workspaces.iterdir(), key=lambda item: item.name):
        match = re.fullmatch(r"attempt-([1-9][0-9]*)", child.name)
        if match is None:
            continue
        before = child.lstat()
        children = list(child.iterdir())
        after = child.lstat()
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_mtime_ns,
        )
        if children:
            continue
        attempt_id = int(match.group(1))
        if (
            not stable
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_dev != workspace_device
            or (expected_uid is not None and before.st_uid != expected_uid)
        ):
            raise CarryForwardError("workspace_identity_invalid")
        status = (attempt_statuses or {}).get(attempt_id)
        if status is not None and status not in TERMINAL_ATTEMPTS:
            raise CarryForwardError("attempt_state_unknown")
        empty_attempt_shells.append(
            {
                "attempt_id": attempt_id,
                "classification": (
                    "deferred_responsibility_empty_shell"
                    if attempt_id in associated_attempts
                    else "terminal_attempt_empty_shell"
                    if status is not None
                    else "owned_empty_shell_without_db_row"
                ),
            }
        )
    empty_attempt_shells.sort(key=lambda item: item["attempt_id"])
    return {
        "runtime": {
            "root": _root_identity(runtime_root),
            "entries": runtime,
            "digest": digest(runtime),
        },
        "journal": {
            "root": _root_identity(journal_root),
            "entries": journal,
            "digest": digest(journal),
        },
        "materials": materials,
        "journal_facts": journal_facts,
        "empty_attempt_shells": empty_attempt_shells,
    }


def validate_file_responsibilities(
    evidence: dict[str, Any], responsibilities: dict[str, Any]
) -> None:
    runtime_entries = {entry["path"]: entry for entry in evidence["runtime"]["entries"]}
    journal_entries = {entry["path"]: entry for entry in evidence["journal"]["entries"]}
    runtime_paths = set(runtime_entries)
    journal_paths = {entry["path"] for entry in evidence["journal"]["entries"]}
    if any(
        entry.get("type") == "symlink" for entry in evidence["journal"]["entries"]
    ) or any(
        entry.get("type") == "symlink"
        and not entry.get("path", "").startswith("version-cache/entries/")
        for entry in evidence["runtime"]["entries"]
    ):
        raise CarryForwardError("responsibility_symlink_rejected")
    cleanup_facts = evidence.get("journal_facts", {}).get("cleanup", [])
    attempt_facts = evidence.get("journal_facts", {}).get("attempt", [])
    selected_ids = {item["execution_id"] for item in responsibilities["executions"]}
    selected_attempts = {
        attempt["attempt_id"]: item["execution_id"]
        for item in responsibilities["executions"]
        for attempt in item.get("attempts", [])
    }
    deferred_attempts = {
        attempt_id: item["execution_id"]
        for item in responsibilities["executions"]
        for attempt_id in item.get("deferred_attempt_ids", [])
    }
    recovery_facts = evidence.get("journal_facts", {}).get("sandbox_recovery", [])
    empty_shells = evidence.get("empty_attempt_shells")
    if not isinstance(empty_shells, list) or any(
        not isinstance(item, dict)
        or set(item) != {"attempt_id", "classification"}
        or type(item["attempt_id"]) is not int
        or item["attempt_id"] <= 0
        or item["classification"]
        not in {
            "deferred_responsibility_empty_shell",
            "terminal_attempt_empty_shell",
            "owned_empty_shell_without_db_row",
        }
        for item in empty_shells
    ):
        raise CarryForwardError("workspace_identity_invalid")
    empty_shell_ids = [item["attempt_id"] for item in empty_shells]
    if empty_shell_ids != sorted(set(empty_shell_ids)):
        raise CarryForwardError("workspace_identity_invalid")
    captured_empty_ids = {
        int(match.group(1))
        for path, entry in runtime_entries.items()
        if (match := re.fullmatch(r"workspaces/attempt-([1-9][0-9]*)", path))
        and entry.get("type") == "directory"
        and not any(other.startswith(f"{path}/") for other in runtime_paths)
    }
    if captured_empty_ids != set(empty_shell_ids):
        raise CarryForwardError("workspace_identity_invalid")
    associated_shell_ids = {
        item["attempt_id"]
        for item in cleanup_facts + attempt_facts + recovery_facts
        if type(item.get("attempt_id")) is int
    }
    shell_classifications = {
        item["attempt_id"]: item["classification"] for item in empty_shells
    }
    for attempt_id, classification in shell_classifications.items():
        deferred_execution = deferred_attempts.get(attempt_id)
        if classification == "deferred_responsibility_empty_shell":
            if (
                attempt_id not in associated_shell_ids
                or deferred_execution is None
                or selected_attempts.get(attempt_id) != deferred_execution
                or any(
                    item.get("execution_id") != deferred_execution
                    for item in cleanup_facts + attempt_facts + recovery_facts
                    if item.get("attempt_id") == attempt_id
                )
            ):
                raise CarryForwardError("workspace_identity_invalid")
        elif attempt_id in associated_shell_ids:
            raise CarryForwardError("workspace_identity_invalid")
    retired_preflight_names = {
        marker["cgroup_name"]
        for marker in recovery_facts
        if marker.get("kind") == "preflight"
    }
    if any(
        fact["execution_id"] not in selected_ids
        for fact in cleanup_facts + attempt_facts
    ):
        raise CarryForwardError("unselected_storage_responsibility")
    cache_allowed = {
        "version-cache",
        "version-cache/entries",
        "version-cache/.dlr-cache-reservations.json",
        "version-cache/.dlr-cache-reservations.lock",
    }
    for path, entry in runtime_entries.items():
        match = re.search(r"(^|/)dlr-exec-([1-9][0-9]*)($|/)", path)
        if match and int(match.group(2)) not in selected_ids:
            raise CarryForwardError("unselected_storage_responsibility")
        if path == ".dlr-instance.lock" and entry.get("type") != "file":
            raise CarryForwardError("storage_lock_invalid")
        if (
            path.startswith("attempt-journal/")
            and path != "attempt-journal/.dlr-instance.lock"
        ):
            if (
                not re.fullmatch(
                    r"attempt-journal/attempt-[1-9][0-9]*\.attempt\.json", path
                )
                or entry.get("type") != "file"
            ):
                raise CarryForwardError("attempt_journal_unknown")
        if path.startswith("version-cache"):
            if path not in cache_allowed and not path.startswith(
                "version-cache/entries/"
            ):
                raise CarryForwardError("version_cache_unknown")
            if path in {"version-cache", "version-cache/entries"} and (
                entry.get("type") != "directory" or entry.get("mode") != 0o711
            ):
                raise CarryForwardError("version_cache_invalid")
            if path == "version-cache/.dlr-cache-reservations.json" and (
                entry.get("type") != "file" or entry.get("mode") != 0o600
            ):
                raise CarryForwardError("version_cache_invalid")
            if path == "version-cache/.dlr-cache-reservations.lock" and (
                entry.get("type") != "file" or entry.get("mode") != 0o644
            ):
                raise CarryForwardError("version_cache_invalid")
        if path == "workspaces":
            if entry.get("type") != "directory" or entry.get("mode") != 0o700:
                raise CarryForwardError("workspace_root_invalid")
            continue
        if path.startswith("workspaces/"):
            parts = path.split("/")
            attempt_match = re.fullmatch(r"attempt-([1-9][0-9]*)", parts[1])
            if not attempt_match:
                raise CarryForwardError("workspace_entry_unknown")
            attempt_id = int(attempt_match.group(1))
            if len(parts) == 2:
                if entry.get("type") != "directory" or entry.get("mode") != 0o700:
                    raise CarryForwardError("workspace_identity_invalid")
                continue
            execution_id = deferred_attempts.get(attempt_id)
            if (
                execution_id is None
                or selected_attempts.get(attempt_id) != execution_id
            ):
                raise CarryForwardError("workspace_entry_unknown")
            expected = f"dlr-exec-{execution_id}"
            if parts[2] not in {expected, ".dlr-sandbox-mount"}:
                raise CarryForwardError("workspace_identity_invalid")
        preflight = path.split("/", 1)[0]
        if (
            preflight.startswith("dlr-preflight-")
            and preflight not in retired_preflight_names
        ):
            raise CarryForwardError("sandbox_recovery_unselected")
    for path in journal_paths:
        if path == ".dlr-instance.lock":
            if journal_entries[path].get("type") != "file":
                raise CarryForwardError("storage_lock_invalid")
            continue
        if path == "sandbox-recovery":
            if journal_entries[path].get("type") != "directory":
                raise CarryForwardError("sandbox_recovery_unknown")
            continue
        if path.startswith("sandbox-recovery/"):
            if (
                not re.fullmatch(
                    r"sandbox-recovery/sandbox-(?:attempt-[1-9][0-9]*-[1-9][0-9]*|dlr-preflight-[0-9a-f]{16,64})\.json",
                    path,
                )
                or journal_entries[path].get("type") != "file"
            ):
                raise CarryForwardError("sandbox_recovery_unknown")
            continue
        if not re.fullmatch(
            r"execution-[1-9][0-9]*-attempt-[1-9][0-9]*\.cleanup\.json", path
        ):
            raise CarryForwardError("cleanup_journal_unknown")
    for item in responsibilities["executions"]:
        execution_id = item["execution_id"]
        attempt_ids = item["attempt_ids"]
        related_runtime = {
            path
            for path in runtime_paths
            if re.search(rf"(^|/)dlr-exec-{execution_id}($|/)", path)
        }
        related_cleanup = {
            path
            for path in journal_paths
            if re.fullmatch(
                rf"execution-{execution_id}-attempt-[1-9][0-9]*\.cleanup\.json", path
            )
        }
        related_attempt = {
            path
            for path in runtime_paths
            if re.fullmatch(r"attempt-journal/attempt-[1-9][0-9]*\.attempt\.json", path)
            and int(path.rsplit("-", 1)[1].split(".", 1)[0]) in attempt_ids
        }
        if item["cleanup"] == "not_applicable" and (
            related_runtime
            or related_cleanup
            or related_attempt
            or any(fact["execution_id"] == execution_id for fact in cleanup_facts)
            or any(fact["execution_id"] == execution_id for fact in attempt_facts)
        ):
            raise CarryForwardError("never_claimed_storage_present")
        if item["cleanup"] == "completed" and (
            related_runtime or related_cleanup or related_attempt
        ):
            raise CarryForwardError("completed_storage_present")
        if item["cleanup"] == "deferred_preserved":
            expected = {
                f"execution-{execution_id}-attempt-{attempt_id}.cleanup.json"
                for attempt_id in item["deferred_attempt_ids"]
            }
            if related_cleanup != expected:
                raise CarryForwardError("deferred_journal_missing")
            facts = [
                fact for fact in cleanup_facts if fact["execution_id"] == execution_id
            ]
            attempts = {attempt["attempt_id"]: attempt for attempt in item["attempts"]}
            if (
                not facts
                or any(
                    fact["attempt_id"] not in item["deferred_attempt_ids"]
                    for fact in facts
                )
                or any(fact.get("cleanup_token_matches") is not True for fact in facts)
            ):
                raise CarryForwardError("deferred_journal_identity_invalid")
        attempts = {
            attempt["attempt_id"]: attempt for attempt in item.get("attempts", [])
        }
        for fact in (
            fact for fact in attempt_facts if fact["execution_id"] == execution_id
        ):
            attempt = attempts.get(fact["attempt_id"])
            if not attempt or any(
                fact.get(key) is not True
                if key in {"claim_token_matches", "cleanup_token_matches"}
                else fact[key] != attempt.get(key)
                for key in (
                    "attempt_no",
                    "fencing_token",
                    "claim_token_matches",
                    "cleanup_token_matches",
                    "lease_expires_at",
                )
            ):
                raise CarryForwardError("attempt_journal_identity_invalid")
    for marker in evidence.get("journal_facts", {}).get("sandbox_recovery", []):
        if marker["kind"] == "attempt" and (
            deferred_attempts.get(marker["attempt_id"]) != marker["execution_id"]
        ):
            raise CarryForwardError("sandbox_recovery_unselected")


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error


def _mountinfo(
    path: Path,
    *,
    budget: dict[str, int] | None = None,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    def decode(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)

    if deadline is not None and time.monotonic() > deadline:
        raise CarryForwardError("kernel_inventory_limit")
    try:
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode()
    except OSError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    except UnicodeError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise CarryForwardError("kernel_inventory_limit")
    if budget is not None:
        budget["mountinfo_bytes"] = budget.get("mountinfo_bytes", 0) + len(raw_bytes)
        if budget["mountinfo_bytes"] > 128 * 1024 * 1024:
            raise CarryForwardError("kernel_inventory_limit")
    result = []
    for line in raw.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            result.append(
                {
                    "major_minor": fields[2],
                    "root": decode(fields[3]),
                    "mountpoint": decode(fields[4]),
                    "filesystem": fields[separator + 1],
                    "source": decode(fields[separator + 2]),
                }
            )
        except (ValueError, IndexError) as error:
            raise CarryForwardError("kernel_identity_unknown") from error
    return result


def _docker_text(arguments: list[str]) -> str:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_identity_unknown") from error


def _worker_runtime_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise CarryForwardError("kernel_identity_unknown")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(item, str) and "=" in item for item in environment
    ):
        raise CarryForwardError("kernel_identity_unknown")
    values = dict(item.split("=", 1) for item in environment)
    runtime = values.get("DLR_RUNTIME_ROOT", "/var/lib/dlr/runtime")
    result = {
        "user": config.get("User") or "0",
        "runtime_root": runtime,
        "journal_root": values.get(
            "DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", "/var/lib/dlr/journal"
        ),
        "attempt_journal_root": values.get(
            "DLR_ATTEMPT_JOURNAL_ROOT", f"{runtime}/attempt-journal"
        ),
        "cgroup_path": values.get("DLR_SANDBOX_CGROUP_PATH"),
    }
    if result != {
        "user": result["user"],
        "runtime_root": "/var/lib/dlr/runtime",
        "journal_root": "/var/lib/dlr/journal",
        "attempt_journal_root": "/var/lib/dlr/runtime/attempt-journal",
        "cgroup_path": "/run/dlr-cgroup",
    } or not re.fullmatch(r"[0-9]+(?::[0-9]+)?", result["user"]):
        raise CarryForwardError("kernel_identity_unknown")
    return result


def _delegated_worker_root(cgroup: str, control_group: str) -> Path:
    cgroup_path = Path(cgroup.removeprefix("0::"))
    parent_path = Path(control_group)
    try:
        delegated = cgroup_path.relative_to(parent_path)
    except ValueError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if len(delegated.parts) != 2 or delegated.parts[-1] != "agent":
        raise CarryForwardError("kernel_identity_unknown")
    return Path("/sys/fs/cgroup").joinpath(*parent_path.parts[1:], delegated.parts[0])


def _worker_authority(
    container: str,
    runtime_volume: str,
    journal_volume: str,
    control_group: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", container):
        raise CarryForwardError("kernel_identity_unknown")
    container_id = _docker_text(["docker", "inspect", container, "--format", "{{.Id}}"])
    image_id = _docker_text(["docker", "inspect", container, "--format", "{{.Image}}"])
    started_at = _docker_text(
        ["docker", "inspect", container, "--format", "{{.State.StartedAt}}"]
    )
    pid_text = _docker_text(
        ["docker", "inspect", container, "--format", "{{.State.Pid}}"]
    )
    labels = {
        "com.docker.compose.project": _docker_text(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
            ]
        ),
        "com.docker.compose.service": _docker_text(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
            ]
        ),
    }
    try:
        runtime_config = _worker_runtime_config(
            json.loads(
                _docker_text(
                    ["docker", "inspect", container, "--format", "{{json .Config}}"]
                )
            )
        )
    except json.JSONDecodeError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    try:
        pid = int(pid_text)
    except ValueError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if pid <= 0 or labels["com.docker.compose.service"] != "worker":
        raise CarryForwardError("kernel_identity_unknown")
    stat_fields = _read_text(Path(f"/proc/{pid}/stat")).split()
    cgroup = _read_text(Path(f"/proc/{pid}/cgroup"))
    if (
        len(stat_fields) < 22
        or not cgroup.startswith(f"0::{control_group}/")
        or not cgroup.endswith("/agent")
    ):
        raise CarryForwardError("kernel_identity_unknown")
    worker_root = _delegated_worker_root(cgroup, control_group)
    worker_root_info = worker_root.lstat()
    parent = Path("/sys/fs/cgroup") / control_group.removeprefix("/")
    parent_info = parent.lstat()
    process_cgroup_info = Path(f"/proc/{pid}/root/sys/fs/cgroup").lstat()
    if (worker_root_info.st_dev, worker_root_info.st_ino) != (
        process_cgroup_info.st_dev,
        process_cgroup_info.st_ino,
    ):
        raise CarryForwardError("kernel_identity_unknown")
    volume_identities: dict[str, Any] = {}
    mounts = _mountinfo(Path(f"/proc/{pid}/mountinfo"))
    for name, volume, target in (
        ("runtime", runtime_volume, "/var/lib/dlr/runtime"),
        ("journal", journal_volume, "/var/lib/dlr/journal"),
    ):
        mountpoint = _docker_text(
            ["docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"]
        )
        source_info = Path(mountpoint).lstat()
        process_info = Path(f"/proc/{pid}/root{target}").lstat()
        matches = [item for item in mounts if item["mountpoint"] == target]
        if len(matches) != 1 or (source_info.st_dev, source_info.st_ino) != (
            process_info.st_dev,
            process_info.st_ino,
        ):
            raise CarryForwardError("kernel_identity_unknown")
        volume_identities[name] = {
            "name": volume,
            "device": source_info.st_dev,
            "inode": source_info.st_ino,
            "mount": matches[0],
        }
    authority = {
        "container_id": container_id,
        "image_id": image_id,
        "started_at": started_at,
        "pid": pid,
        "pid_starttime": stat_fields[21],
        "mount_namespace": os.readlink(f"/proc/{pid}/ns/mnt"),
        "cgroup_namespace": os.readlink(f"/proc/{pid}/ns/cgroup"),
        "parent_device": parent_info.st_dev,
        "parent_inode": parent_info.st_ino,
        "root_device": worker_root_info.st_dev,
        "root_inode": worker_root_info.st_ino,
        "labels": labels,
        "runtime_config": runtime_config,
        "volumes": volume_identities,
    }
    if _read_text(Path(f"/proc/{pid}/stat")).split()[21] != stat_fields[21]:
        raise CarryForwardError("kernel_identity_unknown")
    return authority


def _mount_root_contains(root: str, candidate: str) -> bool:
    try:
        Path(candidate).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _selected_mount_matches(
    mount: dict[str, str], targets: set[tuple[str, str, str]]
) -> bool:
    return any(
        mount["filesystem"] == filesystem
        and mount["major_minor"] == major_minor
        and _mount_root_contains(root, mount["root"])
        for filesystem, major_minor, root in targets
    )


def _related_mount_namespaces(authority: dict[str, Any]) -> dict[str, Any]:
    targets = {
        (
            item["mount"]["filesystem"],
            item["mount"]["major_minor"],
            item["mount"]["root"],
        )
        for item in authority["volumes"].values()
    }
    old_namespaces = {
        authority.get("mount_namespace"),
        authority.get("cgroup_namespace"),
    }
    if None in old_namespaces or not all(
        isinstance(value, str)
        and re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", value)
        for value in old_namespaces
    ):
        raise CarryForwardError("kernel_identity_unknown")
    proc = Path("/proc")
    deadline = time.monotonic() + 20
    budget = {"mountinfo_bytes": 0, "fd_entries": 0}
    try:
        pids = sorted(int(path.name) for path in proc.iterdir() if path.name.isdigit())
    except OSError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    tasks: list[tuple[int, int, Path]] = []
    for pid in pids:
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        process = proc / str(pid)
        tasks.append((pid, pid, process))
        task_root = proc / str(pid) / "task"
        try:
            tids = sorted(
                int(path.name) for path in task_root.iterdir() if path.name.isdigit()
            )
        except FileNotFoundError:
            tids = []
        except OSError as error:
            if process.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        tasks.extend((pid, tid, task_root / str(tid)) for tid in tids if tid != pid)
        if len(tasks) > 8192:
            raise CarryForwardError("kernel_inventory_limit")

    representatives: dict[str, tuple[int, int, Path]] = {}
    task_namespaces: dict[tuple[int, int], tuple[str, str]] = {}
    for pid, tid, task in tasks:
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        try:
            mount_namespace = os.readlink(task / "ns/mnt")
            cgroup_namespace = os.readlink(task / "ns/cgroup")
        except FileNotFoundError:
            continue
        except OSError as error:
            if task.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        if not re.fullmatch(
            r"mnt:\[[1-9][0-9]*\]", mount_namespace
        ) or not re.fullmatch(r"cgroup:\[[1-9][0-9]*\]", cgroup_namespace):
            raise CarryForwardError("kernel_identity_unknown")
        representatives.setdefault(mount_namespace, (pid, tid, task))
        task_namespaces[(pid, tid)] = (mount_namespace, cgroup_namespace)
        if len(representatives) > 2048:
            raise CarryForwardError("kernel_inventory_limit")

    member_namespaces = {
        namespace for namespaces in task_namespaces.values() for namespace in namespaces
    }

    related: list[dict[str, Any]] = []
    namespace_pins: list[dict[str, Any]] = []
    unknown_pins: list[dict[str, Any]] = []
    for namespace, (pid, tid, task) in sorted(representatives.items()):
        try:
            mounts = _mountinfo(task / "mountinfo", budget=budget, deadline=deadline)
        except CarryForwardError:
            if not task.exists():
                continue
            raise
        matches = [item for item in mounts if _selected_mount_matches(item, targets)]
        if matches:
            related.append(
                {
                    "namespace": namespace,
                    "mounts_digest": digest(matches),
                }
            )
        for item in mounts:
            if item.get("filesystem") != "nsfs":
                continue
            pinned = {
                value
                for value in (item.get("root"), item.get("source"))
                if isinstance(value, str)
                and re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", value)
            }
            for value in sorted(pinned):
                record = {
                    "kind": "nsfs",
                    "owner_mount_namespace": namespace,
                    "pinned": value,
                    "mountpoint_digest": digest(item.get("mountpoint")),
                }
                if value in old_namespaces:
                    namespace_pins.append(record)
                elif value not in member_namespaces:
                    unknown_pins.append(record)

    fd_pins: list[dict[str, Any]] = []
    for pid, tid, task in tasks:
        if (pid, tid) not in task_namespaces:
            continue
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        fd_root = task / "fd"
        try:
            descriptors = sorted(fd_root.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            if task.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        budget["fd_entries"] += len(descriptors)
        if budget["fd_entries"] > 65536:
            raise CarryForwardError("kernel_inventory_limit")
        for descriptor in descriptors:
            if time.monotonic() > deadline:
                raise CarryForwardError("kernel_inventory_limit")
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            except OSError as error:
                if task.exists():
                    raise CarryForwardError("kernel_identity_unknown") from error
                continue
            if not re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", target):
                continue
            if target in old_namespaces or target not in member_namespaces:
                mount_namespace, cgroup_namespace = task_namespaces[(pid, tid)]
                record = {
                    "kind": "fd",
                    "owner_mount_namespace": mount_namespace,
                    "owner_cgroup_namespace": cgroup_namespace,
                    "pinned": target,
                }
                if target in old_namespaces:
                    fd_pins.append(record)
                else:
                    unknown_pins.append(record)

    if unknown_pins:
        raise CarryForwardError("kernel_identity_unknown")

    related = sorted(related, key=lambda item: item["namespace"])
    pins = sorted([*namespace_pins, *fd_pins], key=lambda item: canonical_bytes(item))
    target = {"related": related, "pins": pins}
    return {
        "task_count": len(task_namespaces),
        "namespace_count": len(representatives),
        "mountinfo_bytes": budget["mountinfo_bytes"],
        "fd_entries": budget["fd_entries"],
        "related_count": len(related),
        "pin_count": len(pins),
        "target_digest": digest(target),
    }


def validate_retired_markers(
    markers: list[dict[str, Any]],
    *,
    boot_id: str,
    parent_device: int,
    parent_inode: int,
    children: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    child_tuples = {
        (item["device"], item["inode"])
        for item in children.values()
        if isinstance(item, dict)
        and type(item.get("device")) is int
        and type(item.get("inode")) is int
    }
    result = []
    for marker in markers:
        identity = marker.get("namespace_identity")
        if not isinstance(identity, dict):
            raise CarryForwardError("sandbox_recovery_invalid")
        retired = identity.get("boot_id") != boot_id or (
            (identity.get("parent_device"), identity.get("parent_inode"))
            == (parent_device, parent_inode)
            and (identity.get("root_device"), identity.get("root_inode"))
            not in child_tuples
        )
        if not retired or marker.get("cgroup_name") in children:
            raise CarryForwardError("sandbox_namespace_not_retired")
        result.append(
            {
                "cgroup_name": marker["cgroup_name"],
                "marker_fingerprint": marker["marker_fingerprint"],
            }
        )
    return sorted(result, key=lambda item: item["cgroup_name"])


def capture_kernel(
    unit: str,
    *,
    require_idle: bool,
    expected_description: str | None = None,
    worker_container: str | None = None,
    runtime_volume: str | None = None,
    journal_volume: str | None = None,
    baseline_authority: dict[str, Any] | None = None,
    recovery_markers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"dlr-[A-Za-z0-9][A-Za-z0-9_.-]*\.service", unit):
        raise CarryForwardError("kernel_unit_invalid")
    try:
        values = [
            subprocess.run(
                ["systemctl", "show", unit, f"--property={name}", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            for name in (
                "ActiveState",
                "Delegate",
                "ControlGroup",
                "MainPID",
                "Description",
            )
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_status_unavailable") from error
    active, delegate, control_group, main_pid_text, description = values
    expected = f"/system.slice/{unit}"
    if (
        active != "active"
        or delegate != "yes"
        or control_group != expected
        or (expected_description is not None and description != expected_description)
        or not description.startswith(f"DataLinkRuntime Sandbox {unit} ")
    ):
        raise CarryForwardError("kernel_keeper_invalid")
    try:
        main_pid = int(main_pid_text)
    except ValueError as error:
        raise CarryForwardError("kernel_keeper_invalid") from error
    if main_pid <= 0 or _read_text(Path(f"/proc/{main_pid}/comm")) != "sleep":
        raise CarryForwardError("kernel_keeper_invalid")
    if _read_text(Path(f"/proc/{main_pid}/cgroup")) != f"0::{expected}/agent":
        raise CarryForwardError("kernel_keeper_invalid")
    stat_fields = _read_text(Path(f"/proc/{main_pid}/stat")).split()
    if len(stat_fields) < 22:
        raise CarryForwardError("kernel_keeper_invalid")
    parent = Path("/sys/fs/cgroup") / control_group.removeprefix("/")
    try:
        parent_info = parent.lstat()
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error
    agent_pids = _read_text(parent / "agent/cgroup.procs").splitlines()
    if agent_pids != [str(main_pid)] or _read_text(parent / "cgroup.procs"):
        raise CarryForwardError("kernel_keeper_invalid")
    tree: dict[str, dict[str, Any]] = {}
    try:
        pending = sorted(
            (path for path in parent.iterdir() if path.is_dir()), reverse=True
        )
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error
    while pending:
        child = pending.pop()
        try:
            child_info = child.lstat()
            descendants = sorted(
                (path for path in child.iterdir() if path.is_dir()), reverse=True
            )
        except OSError as error:
            raise CarryForwardError("kernel_read_failed") from error
        pending.extend(descendants)
        events = dict(
            line.split(None, 1)
            for line in _read_text(child / "cgroup.events").splitlines()
            if line.strip()
        )
        try:
            populated = int(events["populated"])
            pids = [
                int(value) for value in _read_text(child / "cgroup.procs").splitlines()
            ]
        except (KeyError, ValueError) as error:
            raise CarryForwardError("kernel_events_invalid") from error
        if populated not in {0, 1} or any(pid <= 0 for pid in pids):
            raise CarryForwardError("kernel_events_invalid")
        relative = child.relative_to(parent).as_posix()
        tree[relative] = {
            "populated": populated,
            "process_count": len(pids),
            "process_digest": digest(sorted(pids)),
            "device": child_info.st_dev,
            "inode": child_info.st_ino,
        }
    keeper = tree.get("agent")
    if (
        keeper is None
        or keeper["populated"] != 1
        or keeper["process_count"] != 1
        or keeper["process_digest"] != digest([main_pid])
    ):
        raise CarryForwardError("kernel_keeper_invalid")
    if require_idle and set(tree) != {"agent"}:
        raise CarryForwardError("kernel_not_idle")
    try:
        repeated = [
            subprocess.run(
                ["systemctl", "show", unit, f"--property={name}", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            for name in (
                "ActiveState",
                "Delegate",
                "ControlGroup",
                "MainPID",
                "Description",
            )
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_status_unavailable") from error
    if (
        repeated != values
        or _read_text(Path(f"/proc/{main_pid}/stat")).split()[21] != stat_fields[21]
    ):
        raise CarryForwardError("kernel_keeper_changed")
    supplied = (worker_container, runtime_volume, journal_volume)
    if any(supplied) and not all(supplied):
        raise CarryForwardError("kernel_identity_unknown")
    authority = baseline_authority
    if all(supplied):
        authority = _worker_authority(
            worker_container or "",
            runtime_volume or "",
            journal_volume or "",
            control_group,
        )
    namespace_evidence = None
    retired_markers: list[dict[str, Any]] = []
    if require_idle:
        if not isinstance(authority, dict):
            raise CarryForwardError("kernel_identity_unknown")
        first = _related_mount_namespaces(authority)
        second = _related_mount_namespaces(authority)
        stable_keys = ("related_count", "pin_count", "target_digest")
        if any(first.get(key) != second.get(key) for key in stable_keys):
            raise CarryForwardError("kernel_identity_unknown")
        if first["related_count"]:
            raise CarryForwardError("kernel_namespace_active")
        if first["pin_count"]:
            raise CarryForwardError("kernel_namespace_active")
        namespace_evidence = second
        boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id"))
        retired_markers = validate_retired_markers(
            recovery_markers or [],
            boot_id=boot_id,
            parent_device=parent_info.st_dev,
            parent_inode=parent_info.st_ino,
            children=tree,
        )
    return {
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "unit": unit,
        "control_group": control_group,
        "keeper_pid": main_pid,
        "keeper_starttime": stat_fields[21],
        "description": description,
        "parent_device": parent_info.st_dev,
        "parent_inode": parent_info.st_ino,
        "children": tree,
        "old_worker_authority": authority,
        "namespace_evidence": namespace_evidence,
        "retired_markers": sorted(
            retired_markers, key=lambda item: item["cgroup_name"]
        ),
    }


def compare_kernel(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in (
        "boot_id",
        "unit",
        "control_group",
        "keeper_pid",
        "keeper_starttime",
        "description",
        "parent_device",
        "parent_inode",
    ):
        if before.get(key) != after.get(key):
            raise CarryForwardError("kernel_keeper_changed")
    children = after.get("children", {})
    agent = children.get("agent") if isinstance(children, dict) else None
    if (
        set(children) != {"agent"}
        or not isinstance(agent, dict)
        or agent.get("populated") != 1
        or agent.get("process_count") != 1
        or agent.get("process_digest") != digest([after["keeper_pid"]])
    ):
        raise CarryForwardError("kernel_not_idle")
    if before.get("old_worker_authority") != after.get("old_worker_authority"):
        raise CarryForwardError("kernel_identity_unknown")
    namespace = after.get("namespace_evidence")
    if (
        not isinstance(namespace, dict)
        or namespace.get("related_count") != 0
        or namespace.get("pin_count") != 0
        or not isinstance(namespace.get("target_digest"), str)
    ):
        raise CarryForwardError("kernel_namespace_active")


def validate_candidate_tables(
    revision: str, existing: set[str], counts: dict[str, int]
) -> None:
    for name in EMPTY_TABLES_BY_REVISION.get(revision, ()):
        if name not in existing:
            raise CarryForwardError("candidate_table_missing")
        if counts.get(name) != 0:
            raise CarryForwardError("candidate_table_not_empty")


def validate_schema_transition(from_revision: str, to_revision: str) -> set[str]:
    allowed = SCHEMA_ADDITIONS.get((from_revision, to_revision))
    if allowed is None:
        raise CarryForwardError("candidate_schema_path_unknown")
    return allowed


def validate_schema_inventory(
    from_revision: str,
    to_revision: str,
    baseline_tables: set[str],
    existing: set[str],
    counts: dict[str, int],
    cursor_rows: list[tuple[Any, ...]],
) -> None:
    allowed = validate_schema_transition(from_revision, to_revision)
    if existing != baseline_tables | allowed:
        raise CarryForwardError("candidate_schema_inventory_changed")
    if (
        "execution_incident_dispositions" in allowed
        and counts.get("execution_incident_dispositions") != 0
    ):
        raise CarryForwardError("candidate_table_not_empty")
    if "runtime_reconciliation_cursors" in allowed and cursor_rows != [
        ("expired_attempts", 0, 0)
    ]:
        raise CarryForwardError("candidate_seed_invalid")


def validate_storage_identity(storage: Any) -> list[dict[str, Any]]:
    if not isinstance(storage, list):
        raise CarryForwardError("storage_identity_invalid")
    identities: list[dict[str, Any]] = []
    for item in storage:
        if (
            not isinstance(item, dict)
            or set(item) != {"service", "type", "source", "destination", "read_only"}
            or item["type"] not in {"volume", "bind", "tmpfs"}
            or not isinstance(item["service"], str)
            or not isinstance(item["source"], str)
            or not isinstance(item["destination"], str)
            or not item["destination"].startswith("/")
            or type(item["read_only"]) is not bool
            or (item["type"] != "tmpfs" and not item["source"])
        ):
            raise CarryForwardError("storage_identity_invalid")
        identities.append(item)
    keys = [(item["service"], item["destination"]) for item in identities]
    if len(keys) != len(set(keys)):
        raise CarryForwardError("storage_identity_invalid")
    for protected in (item for item in identities if item["type"] == "volume"):
        root = Path(protected["destination"])
        for item in identities:
            if item["service"] != protected["service"] or item is protected:
                continue
            candidate = Path(item["destination"])
            try:
                candidate.relative_to(root)
            except ValueError:
                try:
                    root.relative_to(candidate)
                except ValueError:
                    continue
            raise CarryForwardError("storage_identity_shadowed")
    return sorted(identities, key=lambda item: (item["service"], item["destination"]))


def inspect_database(
    selection: dict[str, Any],
    baseline_projection: dict[str, Any] | None = None,
    *,
    baseline_schema_inventory: dict[str, Any] | None = None,
    from_revision: str | None = None,
    to_revision: str | None = None,
    schema_phase: str | None = None,
) -> dict[str, Any]:
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError as error:
        raise CarryForwardError("sqlalchemy_unavailable") from error
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise CarryForwardError("database_url_missing")
    engine = create_engine(url)
    tables: dict[str, dict[str, Any]] = {}
    with engine.connect() as connection:
        connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        candidate_counts = {
            name: connection.execute(
                text(f'SELECT count(*) FROM "{name}"')
            ).scalar_one()
            for name in EMPTY_TABLES_BY_REVISION.get(revision, ())
            if name in existing
        }
        validate_candidate_tables(revision, existing, candidate_counts)
        if baseline_schema_inventory is not None:
            if not isinstance(baseline_schema_inventory, dict) or set(
                baseline_schema_inventory
            ) != {"tables"}:
                raise CarryForwardError("schema_inventory_invalid")
            baseline_tables = baseline_schema_inventory["tables"]
            if (
                not isinstance(baseline_tables, list)
                or not all(isinstance(name, str) for name in baseline_tables)
                or baseline_tables != sorted(set(baseline_tables))
                or from_revision is None
                or to_revision is None
                or schema_phase not in {"before", "after"}
            ):
                raise CarryForwardError("schema_inventory_invalid")
            if schema_phase == "before":
                if revision != from_revision or existing != set(baseline_tables):
                    raise CarryForwardError("schema_inventory_invalid")
            else:
                if revision != to_revision:
                    raise CarryForwardError("schema_inventory_invalid")
                cursor_rows = (
                    [
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT name, after_id, upper_id "
                                "FROM runtime_reconciliation_cursors ORDER BY name"
                            )
                        )
                    ]
                    if "runtime_reconciliation_cursors" in existing
                    and "runtime_reconciliation_cursors" not in set(baseline_tables)
                    else []
                )
                validate_schema_inventory(
                    from_revision,
                    to_revision,
                    set(baseline_tables),
                    existing,
                    candidate_counts,
                    cursor_rows,
                )
        for name in RESPONSIBILITY_TABLES:
            if name not in existing:
                raise CarryForwardError("schema_table_missing")
            actual_columns = [column["name"] for column in inspector.get_columns(name)]
            actual_primary_key = list(
                (inspector.get_pk_constraint(name) or {}).get("constrained_columns")
                or []
            )
            if not actual_primary_key:
                raise CarryForwardError("schema_primary_key_missing")
            if baseline_projection is not None and actual_primary_key != list(
                baseline_projection[name]["primary_key"]
            ):
                raise CarryForwardError("projection_primary_key_changed")
            columns = (
                list(baseline_projection[name]["columns"])
                if baseline_projection is not None
                else actual_columns
            )
            if not set(columns).issubset(actual_columns):
                raise CarryForwardError("projection_columns_missing")
            primary_key = actual_primary_key
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            order = ",".join(f'"{column}"' for column in primary_key)
            values = connection.execute(
                text(f'SELECT {quoted_columns} FROM "{name}" ORDER BY {order}')
            ).mappings()
            tables[name] = {
                "columns": columns,
                "primary_key": primary_key,
                "rows": [dict(row) for row in values],
            }
    projection = project_rows(tables)
    responsibilities = derive_responsibilities(tables, selection)
    credential_hashes = {
        row["id"]: {
            "claim_token_hash": row.get("claim_token_hash"),
            "cleanup_token_hash": row.get("cleanup_token_hash"),
        }
        for row in tables["execution_attempts"]["rows"]
    }
    attempt_statuses = {
        row["id"]: row["status"] for row in tables["execution_attempts"]["rows"]
    }
    return {
        "projection": projection,
        "responsibilities": responsibilities,
        "schema_inventory": {"tables": sorted(existing)},
        "_credential_hashes": credential_hashes,
        "_attempt_statuses": attempt_statuses,
    }


def manifest_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "manifest_digest"}


def seal_manifest(value: dict[str, Any]) -> dict[str, Any]:
    sealed = canonical(dict(value))
    sealed["manifest_digest"] = digest(manifest_payload(sealed))
    validate_manifest(sealed)
    return sealed


def validate_manifest(value: Any) -> dict[str, Any]:
    required = {
        "format_version",
        "manifest_id",
        "created_at",
        "repo",
        "pr",
        "from_sha",
        "to_sha",
        "from_schema",
        "to_schema",
        "controller_files_digest",
        "migration_graph_digest",
        "old_image_ids",
        "candidate_image_ids",
        "selection",
        "responsibilities",
        "old_runtime_projection",
        "schema_inventory",
        "storage_identity",
        "old_containers",
        "file_evidence",
        "kernel_evidence",
        "manifest_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CarryForwardError("manifest_shape_invalid")
    if value["format_version"] != FORMAT_VERSION:
        raise CarryForwardError("manifest_version_invalid")
    if not isinstance(value["manifest_id"], str) or not MANIFEST_ID.fullmatch(
        value["manifest_id"]
    ):
        raise CarryForwardError("manifest_id_invalid")
    for key in ("from_sha", "to_sha"):
        if not isinstance(value[key], str) or not SHA.fullmatch(value[key]):
            raise CarryForwardError("manifest_sha_invalid")
    for key in ("controller_files_digest", "migration_graph_digest", "manifest_digest"):
        if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
            raise CarryForwardError("manifest_digest_invalid")
    _positive(value["pr"], "manifest_pr_invalid")
    if not isinstance(value["repo"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repo"]
    ):
        raise CarryForwardError("manifest_repo_invalid")
    try:
        dt.datetime.fromisoformat(value["created_at"])
    except (TypeError, ValueError) as error:
        raise CarryForwardError("manifest_time_invalid") from error
    for key in ("from_schema", "to_schema"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-zA-Z0-9_.-]+", value[key]
        ):
            raise CarryForwardError("manifest_schema_invalid")
    validate_schema_transition(value["from_schema"], value["to_schema"])
    normalize_selection(value["selection"])
    validate_storage_identity(value["storage_identity"])
    validate_projection_evidence(value["old_runtime_projection"])
    inventory = value["schema_inventory"]
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"tables"}
        or not isinstance(inventory["tables"], list)
        or inventory["tables"] != sorted(set(inventory["tables"]))
        or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]*", name)
            for name in inventory["tables"]
        )
    ):
        raise CarryForwardError("schema_inventory_invalid")
    if digest(manifest_payload(value)) != value["manifest_digest"]:
        raise CarryForwardError("manifest_digest_mismatch")
    return value


def _command_check_db(args: argparse.Namespace) -> dict[str, Any]:
    baseline = read_private(args.baseline) if args.baseline else None
    if baseline and "manifest_digest" in baseline:
        baseline = validate_manifest(baseline)
    selection = normalize_selection(
        read_private(args.ids)
        if args.ids
        else baseline.get("selection")
        if baseline
        else None
    )
    baseline_projection = None
    if baseline:
        baseline_projection = baseline.get(
            "old_runtime_projection", baseline.get("projection")
        )
    result = inspect_database(
        selection,
        baseline_projection,
        baseline_schema_inventory=baseline.get("schema_inventory")
        if baseline
        else None,
        from_revision=baseline.get("from_schema") if baseline else None,
        to_revision=baseline.get("to_schema") if baseline else None,
        schema_phase=args.schema_phase,
    )
    result.pop("_credential_hashes", None)
    result.pop("_attempt_statuses", None)
    if args.baseline:
        compare_projection(baseline_projection, result["projection"])
        if baseline["responsibilities"] != result["responsibilities"]:
            raise CarryForwardError("responsibility_changed")
    write_private(args.output, result)
    return {"code": "db_ok", "tables": len(result["projection"])}


def _command_capture(args: argparse.Namespace) -> dict[str, Any]:
    materials: dict[str, Path] = {}
    for item in args.material_root:
        if "=" not in item:
            raise CarryForwardError("material_root_invalid")
        name, path = item.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in materials:
            raise CarryForwardError("material_root_invalid")
        materials[name] = Path(path)
    evidence = capture_files(
        args.runtime_root, args.journal_root, materials, args.expected_uid
    )
    if args.db:
        db = read_private(args.db)
        validate_file_responsibilities(evidence, db["responsibilities"])
    if args.baseline:
        baseline = read_private(args.baseline)
        if "manifest_digest" in baseline:
            baseline = validate_manifest(baseline)
        baseline_evidence = baseline.get("file_evidence", baseline)
        responsibilities = baseline.get("responsibilities")
        if responsibilities:
            validate_file_responsibilities(evidence, responsibilities)
        if baseline_evidence != evidence:
            raise CarryForwardError("file_evidence_changed")
    write_private(args.output, evidence)
    return {
        "code": "files_ok",
        "runtime_entries": len(evidence["runtime"]["entries"]),
        "journal_entries": len(evidence["journal"]["entries"]),
        "material_roots": len(evidence["materials"]),
    }


def _material_roots(values: list[str]) -> dict[str, Path]:
    materials: dict[str, Path] = {}
    for item in values:
        if "=" not in item:
            raise CarryForwardError("material_root_invalid")
        name, path = item.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in materials:
            raise CarryForwardError("material_root_invalid")
        materials[name] = Path(path)
    return materials


def _command_capture_state(args: argparse.Namespace) -> dict[str, Any]:
    baseline = read_private(args.baseline) if args.baseline else None
    if baseline and "manifest_digest" in baseline:
        baseline = validate_manifest(baseline)
    selection = normalize_selection(
        read_private(args.ids)
        if args.ids
        else baseline.get("selection")
        if baseline
        else None
    )
    baseline_projection = None
    if baseline:
        baseline_projection = baseline.get(
            "old_runtime_projection", baseline.get("projection")
        )
    db = inspect_database(
        selection,
        baseline_projection,
        baseline_schema_inventory=baseline.get("schema_inventory")
        if baseline
        else None,
        from_revision=baseline.get("from_schema") if baseline else None,
        to_revision=baseline.get("to_schema") if baseline else None,
        schema_phase=args.schema_phase,
    )
    credential_hashes = db.pop("_credential_hashes")
    attempt_statuses = db.pop("_attempt_statuses")
    files = capture_files(
        args.runtime_root,
        args.journal_root,
        _material_roots(args.material_root),
        args.expected_uid,
        credential_hashes,
        attempt_statuses,
    )
    validate_file_responsibilities(files, db["responsibilities"])
    if baseline:
        compare_projection(baseline_projection, db["projection"])
        if baseline["responsibilities"] != db["responsibilities"]:
            raise CarryForwardError("responsibility_changed")
        baseline_files = baseline.get("file_evidence", baseline)
        if baseline_files != files:
            raise CarryForwardError("file_evidence_changed")
    write_private(args.db_output, db)
    write_private(args.files_output, files)
    return {
        "code": "state_ok",
        "tables": len(db["projection"]),
        "runtime_entries": len(files["runtime"]["entries"]),
        "journal_entries": len(files["journal"]["entries"]),
    }


def _command_kernel(args: argparse.Namespace) -> dict[str, Any]:
    baseline = None
    baseline_kernel = None
    recovery_markers = None
    if args.baseline:
        baseline = read_private(args.baseline)
        if "manifest_digest" in baseline:
            baseline = validate_manifest(baseline)
        baseline_kernel = baseline.get("kernel_evidence", baseline)
        recovery_markers = (
            baseline.get("file_evidence", {})
            .get("journal_facts", {})
            .get("sandbox_recovery", [])
        )
    evidence = capture_kernel(
        args.unit,
        require_idle=args.require_idle,
        expected_description=args.expected_description,
        worker_container=args.worker_container,
        runtime_volume=args.runtime_volume,
        journal_volume=args.journal_volume,
        baseline_authority=(
            baseline_kernel.get("old_worker_authority")
            if isinstance(baseline_kernel, dict)
            else None
        ),
        recovery_markers=recovery_markers,
    )
    if baseline_kernel is not None:
        compare_kernel(baseline_kernel, evidence)
    write_private(args.output, evidence)
    return {"code": "kernel_ok", "children": len(evidence["children"])}


def _command_plan(args: argparse.Namespace) -> dict[str, Any]:
    context = read_private(args.context)
    selection = normalize_selection(read_private(args.ids))
    db = read_private(args.db)
    files = read_private(args.files)
    kernel = read_private(args.kernel)
    validate_file_responsibilities(files, db["responsibilities"])
    validate_storage_identity(context.get("storage_identity"))
    workers = [
        item
        for item in context.get("old_containers", [])
        if item.get("service") == "worker"
    ]
    authority = kernel.get("old_worker_authority")
    if (
        len(workers) != 1
        or not isinstance(authority, dict)
        or any(
            authority.get(key) != workers[0].get(key)
            for key in ("container_id", "image_id", "labels")
        )
        or authority.get("runtime_config") != workers[0].get("runtime_config")
    ):
        raise CarryForwardError("kernel_identity_unknown")
    expected_volumes = {
        item["destination"]: item["source"]
        for item in context.get("storage_identity", [])
        if item.get("service") == "worker" and item.get("type") == "volume"
    }
    if {
        name: authority.get("volumes", {}).get(name, {}).get("name")
        for name in ("runtime", "journal")
    } != {
        "runtime": expected_volumes.get("/var/lib/dlr/runtime"),
        "journal": expected_volumes.get("/var/lib/dlr/journal"),
    }:
        raise CarryForwardError("kernel_identity_unknown")
    value = {
        "format_version": FORMAT_VERSION,
        "manifest_id": context["manifest_id"],
        "created_at": context["created_at"],
        "repo": context["repo"],
        "pr": context["pr"],
        "from_sha": context["from_sha"],
        "to_sha": context["to_sha"],
        "from_schema": context["from_schema"],
        "to_schema": context["to_schema"],
        "controller_files_digest": context["controller_files_digest"],
        "migration_graph_digest": context["migration_graph_digest"],
        "old_image_ids": context["old_image_ids"],
        "candidate_image_ids": context["candidate_image_ids"],
        "selection": selection,
        "responsibilities": db["responsibilities"],
        "old_runtime_projection": db["projection"],
        "schema_inventory": db["schema_inventory"],
        "storage_identity": context["storage_identity"],
        "old_containers": context["old_containers"],
        "file_evidence": files,
        "kernel_evidence": kernel,
    }
    manifest = seal_manifest(value)
    write_private(args.output, manifest)
    return {"code": "manifest_ready", "manifest_id": manifest["manifest_id"]}


def _command_compare(args: argparse.Namespace) -> dict[str, Any]:
    before, after = read_private(args.before), read_private(args.after)
    compare_projection(before["projection"], after["projection"])
    if before["responsibilities"] != after["responsibilities"]:
        raise CarryForwardError("responsibility_changed")
    return {"code": "projection_ok", "tables": len(before["projection"])}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    db = commands.add_parser("check-db")
    db.add_argument("--ids", type=Path)
    db.add_argument("--output", type=Path, required=True)
    db.add_argument("--baseline", type=Path)
    db.add_argument("--schema-phase", choices=("before", "after"))
    capture = commands.add_parser("capture")
    capture.add_argument("--runtime-root", type=Path, required=True)
    capture.add_argument("--journal-root", type=Path, required=True)
    capture.add_argument("--db", type=Path)
    capture.add_argument("--material-root", action="append", default=[])
    capture.add_argument("--expected-uid", type=int)
    capture.add_argument("--baseline", type=Path)
    capture.add_argument("--output", type=Path, required=True)
    state = commands.add_parser("capture-state")
    state.add_argument("--ids", type=Path)
    state.add_argument("--baseline", type=Path)
    state.add_argument("--schema-phase", choices=("before", "after"))
    state.add_argument("--runtime-root", type=Path, required=True)
    state.add_argument("--journal-root", type=Path, required=True)
    state.add_argument("--material-root", action="append", default=[])
    state.add_argument("--expected-uid", type=int)
    state.add_argument("--db-output", type=Path, required=True)
    state.add_argument("--files-output", type=Path, required=True)
    kernel = commands.add_parser("check-kernel")
    kernel.add_argument("--unit", required=True)
    kernel.add_argument("--expected-description")
    kernel.add_argument("--worker-container")
    kernel.add_argument("--runtime-volume")
    kernel.add_argument("--journal-volume")
    kernel.add_argument("--require-idle", action="store_true")
    kernel.add_argument("--baseline", type=Path)
    kernel.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("plan")
    for name in ("ids", "context", "db", "files", "kernel", "output"):
        plan.add_argument(f"--{name}", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "check-db":
            result = _command_check_db(args)
        elif args.command == "capture":
            result = _command_capture(args)
        elif args.command == "capture-state":
            result = _command_capture_state(args)
        elif args.command == "check-kernel":
            result = _command_kernel(args)
        elif args.command == "plan":
            result = _command_plan(args)
        else:
            result = _command_compare(args)
        print(json.dumps(result, sort_keys=True))
    except CarryForwardError as error:
        print(json.dumps({"code": error.code}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print(
            json.dumps({"code": "verifier_internal_error"}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
