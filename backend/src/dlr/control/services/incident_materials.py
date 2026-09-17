"""Two-phase, fail-closed validation of frozen recovery materials."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, cast

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.builtin_packages import KINDS, PackageValidationError, safe_path
from dlr.common.jcs import canonicalize
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    ArtifactDeletionJob,
    BuiltinPackage,
    Credential,
    Execution,
    ExecutionCredentialBindingSnapshot,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    Worker,
)
from dlr.control.schemas.reliable_runtime import ResourceProfile
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.services import builtin_package
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.secrets import decrypt_fields

_GUARDS_KEY = "incident_recovery_material_guards"
_MANAGED_SNAPSHOT_KEYS = {
    "ordinal",
    "original_filename",
    "content_type",
    "size_bytes",
    "sha256",
}
_BUILTIN_SNAPSHOT_KEYS = {
    "id",
    "filename",
    "repository_path",
    "size_bytes",
    "sha256",
    "metadata",
}


@dataclass(frozen=True)
class FileProof:
    row_id: int
    storage_key: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class FileTarget:
    row_id: int
    storage_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RecoveryMaterialProof:
    fingerprint: str | None
    managed: tuple[FileProof, ...] = ()
    builtin: tuple[FileProof, ...] = ()
    valid: bool = True


def close_material_guards(session: Session) -> None:
    stack = session.info.pop(_GUARDS_KEY, None)
    if isinstance(stack, ExitStack):
        stack.close()


def _normalized_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("material digest is invalid")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError("material digest is invalid") from None
    return value.lower()


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _fingerprint(execution: Execution) -> str:
    value = {
        "execution_id": execution.id,
        "dispatch_generation": execution.dispatch_generation,
        "adapter_id": execution.adapter_id,
        "version_id": execution.version_id,
        "target_worker_id": execution.target_worker_id,
        "target_worker_id_snapshot": execution.target_worker_id_snapshot,
        "resource_class": execution.resource_class,
        "max_attempts_snapshot": execution.max_attempts_snapshot,
        "retry_policy_snapshot": execution.retry_policy_snapshot,
        "resource_profile_snapshot": execution.resource_profile_snapshot,
        "input_source_type": execution.input_source_type,
        "input_config_revision": execution.input_config_revision,
        "input_snapshot": execution.input_snapshot,
        "credential_bindings_snapshot": execution.credential_bindings_snapshot,
        "builtin_package_snapshot": execution.builtin_package_snapshot,
        "timeout_seconds_snapshot": execution.timeout_seconds_snapshot,
        "recovery_grace_seconds_snapshot": execution.recovery_grace_seconds_snapshot,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": (
            execution.workspace_cleanup_attempt_timeout_seconds_snapshot
        ),
        "workspace_cleanup_total_timeout_seconds_snapshot": (
            execution.workspace_cleanup_total_timeout_seconds_snapshot
        ),
    }
    return hashlib.sha256(canonicalize(value)).hexdigest()


def _hash_stream(stream: Any, expected_size: int) -> tuple[str, os.stat_result]:
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
        raise ValueError("material file identity mismatch")
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, expected_size + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > expected_size:
            raise ValueError("material file exceeds expected size")
        digest.update(chunk)
    after = os.fstat(stream.fileno())
    if total != expected_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("material changed while hashing")
    return digest.hexdigest(), after


def _file_proof(row_id: int, storage_key: str, expected_size: int, stream: Any) -> FileProof:
    digest, info = _hash_stream(stream, expected_size)
    return FileProof(
        row_id=row_id,
        storage_key=storage_key,
        size_bytes=info.st_size,
        sha256=digest,
        device=info.st_dev,
        inode=info.st_ino,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def _managed_snapshot(execution: Execution) -> list[dict[str, Any]]:
    snapshot = execution.input_snapshot
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("source_type") != "managed_files"
        or isinstance(snapshot.get("revision"), bool)
        or not isinstance(snapshot.get("revision"), int)
        or snapshot.get("revision") != execution.input_config_revision
        or not isinstance(snapshot.get("artifacts"), list)
    ):
        raise ValueError("managed input snapshot is invalid")
    artifacts = cast(list[dict[str, Any]], snapshot["artifacts"])
    if not 1 <= len(artifacts) <= 8 or any(not isinstance(item, dict) for item in artifacts):
        raise ValueError("managed input artifact snapshot is invalid")
    for ordinal, item in enumerate(artifacts):
        if (
            set(item) != _MANAGED_SNAPSHOT_KEYS
            or isinstance(item.get("ordinal"), bool)
            or item.get("ordinal") != ordinal
            or not isinstance(item.get("original_filename"), str)
            or not isinstance(item.get("content_type"), str)
            or isinstance(item.get("size_bytes"), bool)
            or not isinstance(item.get("size_bytes"), int)
            or item["size_bytes"] < 0
        ):
            raise ValueError("managed input artifact snapshot is invalid")
        _normalized_sha256(item.get("sha256"))
    return artifacts


def _preflight_managed_targets(session: Session, execution: Execution) -> tuple[FileTarget, ...]:
    snapshots = _managed_snapshot(execution)
    leases = tuple(
        session.scalars(
            select(ExecutionInputArtifactLease)
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.ordinal.asc())
        ).all()
    )
    if [lease.ordinal for lease in leases] != list(range(len(snapshots))):
        raise ValueError("managed input leases are incomplete")
    artifacts = {
        row.id: row
        for row in session.scalars(
            select(ManagedInputArtifact).where(
                ManagedInputArtifact.id.in_([lease.artifact_id for lease in leases])
            )
        ).all()
    }
    if len(artifacts) != len(leases):
        raise ValueError("managed input artifact is missing")
    targets: list[FileTarget] = []
    for snapshot, lease in zip(snapshots, leases, strict=True):
        artifact = artifacts[lease.artifact_id]
        if (
            snapshot
            != {
                "ordinal": lease.ordinal,
                "original_filename": artifact.original_filename,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            or artifact.adapter_id != execution.adapter_id
            or artifact.sha256 is None
        ):
            raise ValueError("managed input snapshot identity mismatch")
        targets.append(
            FileTarget(
                row_id=artifact.id,
                storage_key=artifact.storage_key,
                size_bytes=artifact.size_bytes,
                sha256=_normalized_sha256(artifact.sha256),
            )
        )
    return tuple(targets)


def _builtin_files(execution: Execution) -> tuple[str, list[dict[str, Any]]]:
    snapshot = execution.builtin_package_snapshot
    if not isinstance(snapshot, dict) or set(snapshot) != {"kind", "files"}:
        raise ValueError("builtin package snapshot is invalid")
    kind = snapshot["kind"]
    files = snapshot["files"]
    if not isinstance(kind, str) or not isinstance(files, list):
        raise ValueError("builtin package snapshot is invalid")
    if any(not isinstance(item, dict) for item in files):
        raise ValueError("builtin package file snapshot is invalid")
    for item in files:
        if (
            set(item) != _BUILTIN_SNAPSHOT_KEYS
            or isinstance(item.get("id"), bool)
            or not isinstance(item.get("id"), int)
            or item["id"] <= 0
            or not isinstance(item.get("filename"), str)
            or not isinstance(item.get("repository_path"), str)
            or isinstance(item.get("size_bytes"), bool)
            or not isinstance(item.get("size_bytes"), int)
            or item["size_bytes"] <= 0
            or not isinstance(item.get("metadata"), dict)
        ):
            raise ValueError("builtin package file snapshot is invalid")
        _normalized_sha256(item.get("sha256"))
    return kind, files


def _preflight_builtin_targets(session: Session, execution: Execution) -> tuple[FileTarget, ...]:
    _kind, snapshots = _builtin_files(execution)
    ids = [item.get("id") for item in snapshots]
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
        raise ValueError("builtin package id is invalid")
    if len(set(ids)) != len(ids):
        raise ValueError("builtin package ids are duplicated")
    rows = {
        row.id: row
        for row in session.scalars(select(BuiltinPackage).where(BuiltinPackage.id.in_(ids))).all()
    }
    if len(rows) != len(ids):
        raise ValueError("builtin package is missing")
    targets: list[FileTarget] = []
    for snapshot in snapshots:
        row = rows[snapshot["id"]]
        targets.append(
            FileTarget(
                row.id,
                row.storage_key,
                row.size_bytes,
                _normalized_sha256(row.sha256),
            )
        )
    return tuple(targets)


def _hash_managed_targets(targets: tuple[FileTarget, ...]) -> tuple[FileProof, ...]:
    if not targets:
        return ()
    store = LocalFileArtifactStore()
    proofs: list[FileProof] = []
    for target in targets:
        with store.open(target.storage_key) as stream:
            proof = _file_proof(target.row_id, target.storage_key, target.size_bytes, stream)
        if proof.sha256 != target.sha256:
            raise ValueError("managed input digest mismatch")
        proofs.append(proof)
    return tuple(proofs)


def _hash_builtin_targets(targets: tuple[FileTarget, ...]) -> tuple[FileProof, ...]:
    proofs: list[FileProof] = []
    for target in targets:
        with builtin_package.file_lock(target.storage_key, shared=True):
            descriptor = os.open(
                builtin_package.storage_path(target.storage_key),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                proof = _file_proof(target.row_id, target.storage_key, target.size_bytes, stream)
        if proof.sha256 != target.sha256:
            raise ValueError("builtin package digest mismatch")
        proofs.append(proof)
    return tuple(proofs)


def preflight_recovery_materials(session: Session, execution: Execution) -> RecoveryMaterialProof:
    """Hash file materials without holding business row or file locks."""

    try:
        fingerprint = _fingerprint(execution)
        managed_targets = (
            _preflight_managed_targets(session, execution)
            if execution.input_source_type == "managed_files"
            else ()
        )
        builtin_targets = (
            _preflight_builtin_targets(session, execution)
            if execution.builtin_package_snapshot is not None
            else ()
        )
        session.rollback()
        managed = _hash_managed_targets(managed_targets)
        builtin = _hash_builtin_targets(builtin_targets)
    except (ValueError, OSError, HTTPException):
        return RecoveryMaterialProof(fingerprint=None, valid=False)
    return RecoveryMaterialProof(fingerprint, managed, builtin, True)


def _validate_execution_snapshots(session: Session, execution: Execution) -> bool:
    if (
        execution.target_worker_id is None
        or execution.target_worker_id != execution.target_worker_id_snapshot
    ):
        return False
    worker = session.get(
        Worker,
        execution.target_worker_id_snapshot,
        with_for_update=True,
        populate_existing=True,
    )
    adapter = session.get(Adapter, execution.adapter_id, populate_existing=True)
    version = session.get(AdapterVersion, execution.version_id, populate_existing=True)
    if (
        worker is None
        or adapter is None
        or version is None
        or version.adapter_id != execution.adapter_id
        or worker.protocol_version != 3
        or worker.rabbitmq_execution_v3 is not True
        or adapter.language not in worker.capabilities
        or not isolation_capabilities_ready(worker.isolation_capabilities)
        or (
            execution.builtin_package_snapshot is not None
            and worker.isolation_capabilities.get("builtin_packages_v1") is not True
        )
    ):
        return False
    try:
        profile = ResourceProfile.model_validate(execution.resource_profile_snapshot)
        from dlr.control.services.attempt import _retry_policy

        policy = _retry_policy(execution)
    except (ValidationError, HTTPException):
        return False
    if (
        profile.resource_class != execution.resource_class
        or profile.execution_timeout_seconds != execution.timeout_seconds_snapshot
        or profile.recovery_grace_seconds != execution.recovery_grace_seconds_snapshot
        or profile.workspace_cleanup_attempt_timeout_seconds
        != execution.workspace_cleanup_attempt_timeout_seconds_snapshot
        or profile.workspace_cleanup_total_timeout_seconds
        != execution.workspace_cleanup_total_timeout_seconds_snapshot
        or policy["max_attempts"] != execution.max_attempts_snapshot
    ):
        return False
    snapshot = execution.input_snapshot
    if not isinstance(snapshot, dict) or snapshot.get("source_type") != execution.input_source_type:
        return False
    if "revision" in snapshot:
        revision = snapshot.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return False
        if revision != execution.input_config_revision:
            return False
    return execution.input_source_type in {"none", "json", "webhook", "managed_files"}


def _same_stat(info: os.stat_result, proof: FileProof) -> bool:
    return bool(
        stat.S_ISREG(info.st_mode)
        and info.st_size == proof.size_bytes
        and info.st_dev == proof.device
        and info.st_ino == proof.inode
        and info.st_mtime_ns == proof.modified_ns
        and info.st_ctime_ns == proof.changed_ns
    )


def _validate_managed(
    session: Session,
    execution: Execution,
    proof: RecoveryMaterialProof,
    guards: ExitStack,
) -> bool:
    snapshots = _managed_snapshot(execution)
    leases = tuple(
        session.scalars(
            select(ExecutionInputArtifactLease)
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.artifact_id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(leases) != len(snapshots) or len(proof.managed) != len(leases):
        return False
    artifacts = tuple(
        session.scalars(
            select(ManagedInputArtifact)
            .where(ManagedInputArtifact.id.in_([lease.artifact_id for lease in leases]))
            .order_by(ManagedInputArtifact.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(artifacts) != len(leases):
        return False
    by_ordinal = {lease.ordinal: lease for lease in leases}
    by_artifact = {artifact.id: artifact for artifact in artifacts}
    proof_by_id = {item.row_id: item for item in proof.managed}
    if set(by_ordinal) != set(range(len(snapshots))) or len(by_ordinal) != len(leases):
        return False
    deletion_keys = set(
        session.scalars(
            select(ArtifactDeletionJob.storage_key)
            .where(
                ArtifactDeletionJob.storage_key.in_(
                    [artifact.storage_key for artifact in artifacts]
                )
            )
            .order_by(ArtifactDeletionJob.id.asc())
            .with_for_update()
        ).all()
    )
    store = LocalFileArtifactStore()
    for ordinal, snapshot in enumerate(snapshots):
        lease = by_ordinal[ordinal]
        artifact = by_artifact.get(lease.artifact_id)
        item = proof_by_id.get(lease.artifact_id)
        if artifact is None or item is None:
            return False
        if (
            artifact.adapter_id != execution.adapter_id
            or artifact.status not in {"READY", "PENDING_DELETE"}
            or artifact.delete_attempts != 0
            or artifact.delete_started_at is not None
            or artifact.delete_lease_until is not None
            or artifact.deleted_at is not None
            or artifact.last_error_code is not None
            or artifact.storage_key in deletion_keys
            or artifact.storage_key != item.storage_key
            or _normalized_sha256(artifact.sha256) != item.sha256
            or artifact.size_bytes != item.size_bytes
            or snapshot
            != {
                "ordinal": ordinal,
                "original_filename": artifact.original_filename,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
        ):
            return False
        stream = guards.enter_context(store.open(artifact.storage_key))
        if not _same_stat(os.fstat(stream.fileno()), item):
            return False
    return True


def _validate_builtin(
    session: Session,
    execution: Execution,
    proof: RecoveryMaterialProof,
    guards: ExitStack,
) -> bool:
    kind, snapshots = _builtin_files(execution)
    adapter = session.get(Adapter, execution.adapter_id)
    if adapter is None or KINDS.get(adapter.language) != kind:
        return False
    ids = [item.get("id") for item in snapshots]
    if (
        any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids)
        or len(ids) != len(set(ids))
        or len(proof.builtin) != len(ids)
    ):
        return False
    builtin_package.lock_library(session)
    rows = tuple(
        session.scalars(
            select(BuiltinPackage)
            .where(BuiltinPackage.id.in_(ids))
            .order_by(BuiltinPackage.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(rows) != len(ids):
        return False
    by_id = {row.id: row for row in rows}
    proof_by_id = {item.row_id: item for item in proof.builtin}
    for snapshot in snapshots:
        row = by_id.get(snapshot["id"])
        item = proof_by_id.get(snapshot["id"])
        if row is None or item is None:
            return False
        try:
            safe_path(row.filename)
            if "/" in row.filename:
                raise PackageValidationError("invalid filename")
            if kind in {"maven", "goproxy"}:
                safe_path(row.repository_path)
        except PackageValidationError:
            return False
        if (
            row.status != "uploaded"
            or row.kind != kind
            or row.storage_key != item.storage_key
            or row.size_bytes != item.size_bytes
            or _normalized_sha256(row.sha256) != item.sha256
            or snapshot
            != {
                "id": row.id,
                "filename": row.filename,
                "repository_path": row.repository_path,
                "size_bytes": row.size_bytes,
                "sha256": row.sha256,
                "metadata": row.package_metadata,
            }
        ):
            return False
        try:
            guards.enter_context(builtin_package.file_lock(row.storage_key, shared=True))
            descriptor = os.open(
                builtin_package.storage_path(row.storage_key),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except (OSError, HTTPException):
            return False
        stream = guards.enter_context(os.fdopen(descriptor, "rb"))
        if not _same_stat(os.fstat(stream.fileno()), item):
            return False
    return True


def _validate_credentials(session: Session, execution: Execution) -> bool:
    snapshots = execution.credential_bindings_snapshot
    if not isinstance(snapshots, list):
        return False
    rows = tuple(
        session.scalars(
            select(ExecutionCredentialBindingSnapshot)
            .where(ExecutionCredentialBindingSnapshot.execution_id == execution.id)
            .order_by(ExecutionCredentialBindingSnapshot.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(rows) != len(snapshots):
        return False
    seen_bindings: set[int] = set()
    seen_env: set[str] = set()
    required: dict[int, set[str]] = {}
    for snapshot, row in zip(snapshots, rows, strict=True):
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"binding_id", "credential_id", "env_key", "field"}
            or not _positive_int(snapshot.get("binding_id"))
            or not _positive_int(snapshot.get("credential_id"))
            or not isinstance(snapshot.get("env_key"), str)
            or not snapshot["env_key"]
            or not isinstance(snapshot.get("field"), str)
            or not snapshot["field"]
            or snapshot
            != {
                "binding_id": row.binding_id,
                "credential_id": row.credential_id,
                "env_key": row.env_key,
                "field": row.field,
            }
            or row.binding_id in seen_bindings
            or row.env_key in seen_env
            or isinstance(row.binding_id, bool)
            or row.binding_id <= 0
            or isinstance(row.credential_id, bool)
            or row.credential_id <= 0
            or not row.env_key
            or not row.field
        ):
            return False
        seen_bindings.add(row.binding_id)
        seen_env.add(row.env_key)
        required.setdefault(row.credential_id, set()).add(row.field)
    credentials = tuple(
        session.scalars(
            select(Credential)
            .where(Credential.id.in_(sorted(required)))
            .order_by(Credential.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(credentials) != len(required):
        return False
    for credential in credentials:
        try:
            fields = decrypt_fields(credential.ciphertext)
        except HTTPException:
            return False
        valid = all(
            isinstance(fields.get(field), str) and bool(fields[field])
            for field in required[credential.id]
        )
        del fields
        if not valid:
            return False
    return True


def _validate_absent_managed_leases(session: Session, execution: Execution) -> bool:
    rows = tuple(
        session.scalars(
            select(ExecutionInputArtifactLease)
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.artifact_id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    return not rows


def validate_recovery_materials(
    session: Session,
    execution: Execution,
    proof: RecoveryMaterialProof,
) -> bool:
    """Lock and reopen frozen materials, retaining file guards until commit."""

    close_material_guards(session)
    guards = ExitStack()
    transferred = False
    try:
        if not proof.valid or proof.fingerprint != _fingerprint(execution):
            return False
        if not _validate_execution_snapshots(session, execution):
            return False
        if execution.input_source_type == "managed_files" and not _validate_managed(
            session, execution, proof, guards
        ):
            return False
        if execution.input_source_type != "managed_files" and not _validate_absent_managed_leases(
            session, execution
        ):
            return False
        if execution.builtin_package_snapshot is not None and not _validate_builtin(
            session, execution, proof, guards
        ):
            return False
        if not _validate_credentials(session, execution):
            return False
        session.info[_GUARDS_KEY] = guards
        transferred = True
        return True
    except (ValueError, OSError, HTTPException):
        return False
    finally:
        if not transferred:
            guards.close()
