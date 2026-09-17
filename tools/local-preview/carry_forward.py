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
import uuid
from pathlib import Path
from typing import Any


FORMAT_VERSION = 1
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
    if len(set(cleanup_ids)) != len(cleanup_ids) or seen_executions.intersection(cleanup_ids):
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
    tables: dict[str, dict[str, Any]], *, required: tuple[str, ...] = RESPONSIBILITY_TABLES
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
            or not all(isinstance(row, dict) and set(row) == set(columns) for row in rows)
        ):
            raise CarryForwardError("schema_projection_invalid")
        rows = sorted(rows, key=lambda row: canonical_bytes([row[key] for key in primary_key]))
        projection[name] = {
            "columns": columns,
            "primary_key": primary_key,
            "rows": [row_digest(row) for row in rows],
            "count": len(rows),
        }
    return projection


def _by_execution(rows: list[dict[str, Any]], execution_id: int) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("execution_id") == execution_id]


def derive_responsibilities(
    tables: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> dict[str, Any]:
    selection = normalize_selection(selection)
    selected = selected_execution_ids(selection)
    rows = {name: value["rows"] for name, value in tables.items()}
    executions = {row.get("id"): row for row in rows["executions"]}
    attempts = rows["execution_attempts"]
    incidents = {row.get("id"): row for row in rows["execution_infrastructure_incidents"]}
    active = [row for row in attempts if row.get("status") in ACTIVE_ATTEMPTS]
    if active:
        raise CarryForwardError("active_attempt_present")
    if any(row.get("active_attempt_id") is not None for row in rows["adapter_execution_slots"]):
        raise CarryForwardError("active_slot_present")
    if any(row.get("status") in {"pending", "running"} for row in rows["worker_cleanup_requests"]):
        raise CarryForwardError("worker_cleanup_active")
    for row in rows["executions"]:
        if row.get("status") in {"queued", "running", "retry_wait"} and row.get("id") not in selected:
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
                or incident.get("dispatch_generation") != execution.get("dispatch_generation")
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
            _responsibility(execution, _by_execution(attempts, execution_id), execution_incidents)
        )
    for execution_id in selection["cleanup_execution_ids"]:
        execution = executions.get(execution_id)
        if not execution or execution.get("status") not in TERMINAL_EXECUTIONS:
            raise CarryForwardError("cleanup_execution_invalid")
        result.append(_responsibility(execution, _by_execution(attempts, execution_id), []))
    return {"executions": sorted(result, key=lambda item: item["execution_id"])}


def _responsibility(
    execution: dict[str, Any], attempts: list[dict[str, Any]], incident_ids: list[int]
) -> dict[str, Any]:
    execution_id = _positive(execution.get("id"), "execution_identity_invalid")
    attempt_ids = sorted(_positive(row.get("id"), "attempt_identity_invalid") for row in attempts)
    attempt_count = execution.get("attempt_count")
    if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count < 0:
        raise CarryForwardError("execution_attempt_count_invalid")
    if attempt_count != len(attempts):
        raise CarryForwardError("execution_attempt_count_invalid")
    if any(row.get("status") not in TERMINAL_ATTEMPTS for row in attempts):
        raise CarryForwardError("attempt_state_unknown")
    attempt_cleanup: dict[int, str] = {}
    for row in attempts:
        summary = row.get("cleanup_summary")
        status = summary.get("workspace_cleanup_status") if isinstance(summary, dict) else None
        if status not in {"completed", "deferred"}:
            raise CarryForwardError("attempt_cleanup_state_unknown")
        attempt_cleanup[row["id"]] = status
    deferred_attempt_ids = sorted(
        attempt_id for attempt_id, status in attempt_cleanup.items() if status == "deferred"
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
    elif attempts and cleanup_status in {"completed", "deferred"} and deferred_attempt_ids:
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
                "claim_token_hash": row.get("claim_token_hash"),
                "cleanup_token_hash": row.get("cleanup_token_hash"),
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
        if left.get("rows") != right.get("rows") or left.get("count") != right.get("count"):
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
                isinstance(column, str)
                and re.fullmatch(r"[a-z][a-z0-9_]*", column)
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
            if parent == root and allowed_top is not None and child.name not in allowed_top:
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
                    descriptor = os.open(child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
            or not re.fullmatch(r"input-[0-9]{2}(?:\.[A-Za-z0-9]+)?", descriptor["mount_name"])
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
    runtime_root: Path, journal_root: Path, expected_uid: int | None = None
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
                "cleanup_token_hash": hashlib.sha256(
                    value["cleanup_token"].encode()
                ).hexdigest(),
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
                path, attempt_fields, "attempt_journal_invalid", expected_uid=expected_uid
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
                    "claim_token_hash": hashlib.sha256(
                        value["claim_token"].encode()
                    ).hexdigest(),
                    "cleanup_token_hash": hashlib.sha256(
                        value["cleanup_token"].encode()
                    ).hexdigest(),
                    "lease_expires_at": datetime_text(value["lease_expires_at"]),
                }
            )
    return {"cleanup": cleanup, "attempt": attempt}


def capture_files(
    runtime_root: Path,
    journal_root: Path,
    material_roots: dict[str, Path] | None = None,
    expected_uid: int | None = None,
) -> dict[str, Any]:
    runtime = _safe_tree(
        runtime_root,
        allowed_top={".dlr-instance.lock", "attempt-journal", "workspaces"},
        ignored_top={"dependency-cache", "environments"},
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
        "journal_facts": _journal_facts(runtime_root, journal_root, expected_uid),
    }


def validate_file_responsibilities(
    evidence: dict[str, Any], responsibilities: dict[str, Any]
) -> None:
    runtime_paths = {entry["path"] for entry in evidence["runtime"]["entries"]}
    journal_paths = {entry["path"] for entry in evidence["journal"]["entries"]}
    if any(
        entry.get("type") == "symlink"
        for entry in evidence["runtime"]["entries"] + evidence["journal"]["entries"]
    ):
        raise CarryForwardError("responsibility_symlink_rejected")
    cleanup_facts = evidence.get("journal_facts", {}).get("cleanup", [])
    attempt_facts = evidence.get("journal_facts", {}).get("attempt", [])
    selected_ids = {item["execution_id"] for item in responsibilities["executions"]}
    if any(fact["execution_id"] not in selected_ids for fact in cleanup_facts + attempt_facts):
        raise CarryForwardError("unselected_storage_responsibility")
    for path in runtime_paths:
        match = re.search(r"(^|/)dlr-exec-([1-9][0-9]*)($|/)", path)
        if match and int(match.group(2)) not in selected_ids:
            raise CarryForwardError("unselected_storage_responsibility")
        if path.startswith("attempt-journal/") and path != "attempt-journal/.dlr-instance.lock":
            if not re.fullmatch(
                r"attempt-journal/attempt-[1-9][0-9]*\.attempt\.json", path
            ):
                raise CarryForwardError("attempt_journal_unknown")
    for path in journal_paths:
        if path in {".dlr-instance.lock", "sandbox-recovery"}:
            continue
        if path.startswith("sandbox-recovery/"):
            raise CarryForwardError("sandbox_recovery_unknown")
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
            if re.fullmatch(rf"execution-{execution_id}-attempt-[1-9][0-9]*\.cleanup\.json", path)
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
            facts = [fact for fact in cleanup_facts if fact["execution_id"] == execution_id]
            attempts = {attempt["attempt_id"]: attempt for attempt in item["attempts"]}
            if (
                not facts
                or any(
                    fact["attempt_id"] not in item["deferred_attempt_ids"]
                    for fact in facts
                )
                or any(
                    not isinstance(attempts[fact["attempt_id"]].get("cleanup_token_hash"), str)
                    or not hmac.compare_digest(
                        fact["cleanup_token_hash"],
                        attempts[fact["attempt_id"]]["cleanup_token_hash"],
                    )
                    for fact in facts
                )
            ):
                raise CarryForwardError("deferred_journal_identity_invalid")
        attempts = {attempt["attempt_id"]: attempt for attempt in item.get("attempts", [])}
        for fact in (
            fact for fact in attempt_facts if fact["execution_id"] == execution_id
        ):
            attempt = attempts.get(fact["attempt_id"])
            if not attempt or any(
                (
                    not isinstance(attempt.get(key), str)
                    or not hmac.compare_digest(fact[key], attempt[key])
                )
                if key in {"claim_token_hash", "cleanup_token_hash"}
                else fact[key] != attempt.get(key)
                for key in (
                    "attempt_no",
                    "fencing_token",
                    "claim_token_hash",
                    "cleanup_token_hash",
                    "lease_expires_at",
                )
            ):
                raise CarryForwardError("attempt_journal_identity_invalid")


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error


def capture_kernel(
    unit: str, *, require_idle: bool, expected_description: str | None = None
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
            for name in ("ActiveState", "Delegate", "ControlGroup", "MainPID", "Description")
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
            pids = [int(value) for value in _read_text(child / "cgroup.procs").splitlines()]
        except (KeyError, ValueError) as error:
            raise CarryForwardError("kernel_events_invalid") from error
        if populated not in {0, 1} or any(pid <= 0 for pid in pids):
            raise CarryForwardError("kernel_events_invalid")
        relative = child.relative_to(parent).as_posix()
        tree[relative] = {
            "populated": populated,
            "process_count": len(pids),
            "process_digest": digest(sorted(pids)),
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
            for name in ("ActiveState", "Delegate", "ControlGroup", "MainPID", "Description")
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_status_unavailable") from error
    if repeated != values or _read_text(Path(f"/proc/{main_pid}/stat")).split()[21] != stat_fields[21]:
        raise CarryForwardError("kernel_keeper_changed")
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


def validate_candidate_tables(
    revision: str, existing: set[str], counts: dict[str, int]
) -> None:
    for name in EMPTY_TABLES_BY_REVISION.get(revision, ()):
        if name not in existing:
            raise CarryForwardError("candidate_table_missing")
        if counts.get(name) != 0:
            raise CarryForwardError("candidate_table_not_empty")


def inspect_database(
    selection: dict[str, Any], baseline_projection: dict[str, Any] | None = None
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
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        candidate_counts = {
            name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
            for name in EMPTY_TABLES_BY_REVISION.get(revision, ())
            if name in existing
        }
        validate_candidate_tables(revision, existing, candidate_counts)
        for name in RESPONSIBILITY_TABLES:
            if name not in existing:
                raise CarryForwardError("schema_table_missing")
            actual_columns = [column["name"] for column in inspector.get_columns(name)]
            actual_primary_key = list(
                (inspector.get_pk_constraint(name) or {}).get("constrained_columns") or []
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
    return {"projection": projection, "responsibilities": responsibilities}


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
        "storage_identity",
        "file_evidence",
        "kernel_evidence",
        "manifest_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CarryForwardError("manifest_shape_invalid")
    if value["format_version"] != FORMAT_VERSION:
        raise CarryForwardError("manifest_version_invalid")
    if not isinstance(value["manifest_id"], str) or not MANIFEST_ID.fullmatch(value["manifest_id"]):
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
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value[key]):
            raise CarryForwardError("manifest_schema_invalid")
    normalize_selection(value["selection"])
    validate_projection_evidence(value["old_runtime_projection"])
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
        baseline_projection = baseline.get("old_runtime_projection", baseline.get("projection"))
    result = inspect_database(selection, baseline_projection)
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


def _command_kernel(args: argparse.Namespace) -> dict[str, Any]:
    evidence = capture_kernel(
        args.unit,
        require_idle=args.require_idle,
        expected_description=args.expected_description,
    )
    if args.baseline:
        baseline = read_private(args.baseline)
        if "manifest_digest" in baseline:
            baseline = validate_manifest(baseline)
        compare_kernel(baseline.get("kernel_evidence", baseline), evidence)
    write_private(args.output, evidence)
    return {"code": "kernel_ok", "children": len(evidence["children"])}


def _command_plan(args: argparse.Namespace) -> dict[str, Any]:
    context = read_private(args.context)
    selection = normalize_selection(read_private(args.ids))
    db = read_private(args.db)
    files = read_private(args.files)
    kernel = read_private(args.kernel)
    validate_file_responsibilities(files, db["responsibilities"])
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
        "storage_identity": context["storage_identity"],
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
    capture = commands.add_parser("capture")
    capture.add_argument("--runtime-root", type=Path, required=True)
    capture.add_argument("--journal-root", type=Path, required=True)
    capture.add_argument("--db", type=Path)
    capture.add_argument("--material-root", action="append", default=[])
    capture.add_argument("--expected-uid", type=int)
    capture.add_argument("--baseline", type=Path)
    capture.add_argument("--output", type=Path, required=True)
    kernel = commands.add_parser("check-kernel")
    kernel.add_argument("--unit", required=True)
    kernel.add_argument("--expected-description")
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
        print(json.dumps({"code": "verifier_internal_error"}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
