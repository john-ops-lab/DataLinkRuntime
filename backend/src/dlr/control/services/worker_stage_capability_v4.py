"""Restricted dependency-free Worker tasks at acceptance and final Claim."""

import json
from typing import Any

from sqlalchemy import Text, cast, exists, func, select
from sqlalchemy.orm import Session

from dlr.common import builtin_wheel_v4 as wheels
from dlr.common import managed_files_v4 as files
from dlr.common.builtin_packages import KINDS
from dlr.common.business_credentials_v4 import BusinessCredentialError, bindings
from dlr.common.config import settings
from dlr.common.managed_input import controlled_input_mount_name
from dlr.common.worker_languages_v4 import (
    COMPILED_LANGUAGES,
    SUPPORTED_LANGUAGES,
    valid_capabilities,
)
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    AdapterVersion,
    BuiltinPackage,
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    Worker,
)
from dlr.control.models.platform import PackageSource
from dlr.control.services import business_credentials_v4 as business_credentials
from dlr.control.services import worker_legacy_window
from dlr.control.services.adapter import domain_error


def _matches_trigger(kind: str, trigger: str, source: str) -> bool:
    if trigger == "webhook":
        return kind == "webhook" and source == "json"
    if trigger == "subscription":
        return kind == "task" and source == "json"
    return kind == "task" and trigger in {"manual", "schedule"}


def require_input_source(source_type: str) -> None:
    if settings.worker_remote_v4_enabled and source_type not in {"none", "json", "managed_files"}:
        _reject("input_source")


def _reject(reason: str) -> None:
    raise domain_error(
        409,
        "worker_task_capability_unavailable",
        "Remote Worker task requires a qualified toolchain and supported input",
        {"reason": reason},
    )


def _bounded_requirements(session: Session, adapter_id: int, version_id: int | None) -> str | None:
    return session.scalar(
        select(AdapterVersion.requirements).where(
            AdapterVersion.id == version_id,
            AdapterVersion.adapter_id == adapter_id,
            func.octet_length(AdapterVersion.requirements) <= 160,
        )
    )


def _builtin_default(session: Session, language: str) -> bool:
    kind = KINDS[language]
    return (
        session.scalar(
            select(
                exists().where(
                    PackageSource.kind == kind,
                    PackageSource.is_default.is_(True),
                    PackageSource.index_url == f"dlr-builtin://{kind}",
                )
            )
        )
        is True
    )


def _supports_wheel(worker: Worker | None) -> bool:
    return bool(
        worker is not None
        and worker.protocol_version == 4
        and worker.isolation_capabilities.get(wheels.CAPABILITY) is True
        and worker.isolation_capabilities.get(files.CAPABILITY) is True
    )


def freeze_worker_wheel(
    session: Session, adapter: Adapter, version_id: int | None, worker_id: int | None
) -> dict[str, Any] | None:
    """Resolve a single wheel while the library serializes admission and deletion."""
    from dlr.control.services import builtin_package

    requirements = _bounded_requirements(session, adapter.id, version_id)
    if requirements == "":
        return None
    requested = wheels.requirement(requirements)
    if (
        adapter.language != "python"
        or requested is None
        or worker_id is None
        or not _supports_wheel(session.get(Worker, worker_id))
        or not _builtin_default(session, "python")
    ):
        _reject("builtin_packages")
    assert requested is not None
    name, version = requested
    builtin_package.lock_library(session)
    rows = list(
        session.scalars(
            select(BuiltinPackage)
            .where(
                BuiltinPackage.kind == "pypi",
                BuiltinPackage.name == name,
                BuiltinPackage.version == version,
                BuiltinPackage.environment == "py3-none-any",
                BuiltinPackage.status == "uploaded",
                BuiltinPackage.size_bytes <= files.MAX_FILE_BYTES,
                BuiltinPackage.package_metadata == {"dependencies": [], "requires_python": None},
            )
            .limit(2)
        )
    )
    if len(rows) != 1:
        _reject("builtin_packages")
    row = rows[0]
    result = {
        "kind": "pypi",
        "files": [
            {
                "id": row.id,
                "filename": row.filename,
                "repository_path": row.repository_path,
                "size_bytes": row.size_bytes,
                "sha256": row.sha256,
                "metadata": row.package_metadata,
            }
        ],
    }
    try:
        wheels.descriptor(result, requirements)
    except ValueError:
        _reject("builtin_packages")
    return result


def _frozen_materials_allowed(
    session: Session,
    *,
    adapter_id: int,
    version_id: int,
    execution_id: int,
    worker_id: int | None,
    language: str,
    input_source: str,
) -> bool:
    requirements = _bounded_requirements(session, adapter_id, version_id)
    selected = session.execute(
        select(
            func.octet_length(cast(Execution.builtin_package_snapshot, Text)),
            func.jsonb_typeof(Execution.builtin_package_snapshot),
        ).where(Execution.id == execution_id)
    ).one_or_none()
    if selected is None:
        return False
    size, kind = selected
    if requirements == "":
        return kind in {None, "null"}
    if (
        language != "python"
        or input_source not in {"none", "json"}
        or size is None
        or size > 4096
        or worker_id is None
        or not _supports_wheel(session.get(Worker, worker_id))
    ):
        return False
    snapshot = session.scalar(
        select(Execution.builtin_package_snapshot).where(Execution.id == execution_id)
    )
    try:
        wheels.descriptor(snapshot, requirements)
        return True
    except ValueError:
        return False


def _configuration_reason(
    session: Session,
    adapter: Adapter,
    *,
    version_id: int | None,
    trigger: str,
    input_source_type: str,
    dependency_check: bool,
) -> str | None:
    if not _matches_trigger(adapter.adapter_type, trigger, input_source_type):
        return "trigger"
    if adapter.language not in SUPPORTED_LANGUAGES:
        return "language"
    if dependency_check:
        return "dependency_check"
    if input_source_type not in {"none", "json", "managed_files"}:
        return "input_source"
    requirements = _bounded_requirements(session, adapter.id, version_id)
    if requirements != "" and not (
        adapter.language == "python"
        and input_source_type in {"none", "json"}
        and wheels.requirement(requirements) is not None
    ):
        return "requirements"
    return None


def _file_reason(
    session: Session,
    adapter_id: int,
    target: int | None,
    *,
    artifact_ids: tuple[int, ...] | None = None,
    snapshot: dict[str, Any] | None = None,
    execution_id: int | None = None,
) -> str | None:
    """Bound metadata before a new responsibility or a frozen Claim is allowed."""
    from dlr.control.services.worker import READABLE_ARTIFACT_STATUSES

    worker = session.get(Worker, target) if target is not None else None
    if (
        worker is None
        or worker.protocol_version != 4
        or worker.identity_lifecycle != "reserved"
        or worker.instance_id is None
        or (worker.isolation_capabilities or {}).get(files.CAPABILITY) is not True
    ):
        return "input_files_capability"
    if artifact_ids is not None and (
        not 1 <= len(artifact_ids) <= files.MAX_FILES
        or any(type(item) is not int or not 0 < item < 2**63 for item in artifact_ids)
        or len(set(artifact_ids)) != len(artifact_ids)
    ):
        return "input_files_budget"
    columns = (
        ManagedInputArtifact.id,
        ManagedInputArtifact.original_filename,
        ManagedInputArtifact.content_type,
        ManagedInputArtifact.size_bytes,
        ManagedInputArtifact.sha256,
        ManagedInputArtifact.status,
    )
    statement = select(*columns).where(ManagedInputArtifact.adapter_id == adapter_id)
    if execution_id is not None:
        statement = (
            statement.add_columns(ExecutionInputArtifactLease.ordinal)
            .join(
                ExecutionInputArtifactLease,
                ExecutionInputArtifactLease.artifact_id == ManagedInputArtifact.id,
            )
            .where(ExecutionInputArtifactLease.execution_id == execution_id)
            .order_by(ExecutionInputArtifactLease.ordinal)
        )
    elif artifact_ids is None:
        statement = (
            statement.add_columns(AdapterInputArtifactBinding.ordinal)
            .join(
                AdapterInputArtifactBinding,
                AdapterInputArtifactBinding.artifact_id == ManagedInputArtifact.id,
            )
            .where(AdapterInputArtifactBinding.adapter_id == adapter_id)
            .order_by(AdapterInputArtifactBinding.ordinal)
        )
    else:
        statement = statement.where(ManagedInputArtifact.id.in_(artifact_ids))
    rows = [dict(row) for row in session.execute(statement.limit(files.MAX_FILES + 1)).mappings()]
    if artifact_ids is not None:
        indexed = {row["id"]: row for row in rows}
        if len(indexed) != len(artifact_ids):
            return "input_files_snapshot"
        rows = [dict(indexed[key], ordinal=index) for index, key in enumerate(artifact_ids)]
    metadata = []
    try:
        for row in rows:
            if row["status"] not in READABLE_ARTIFACT_STATUSES:
                return "input_files_snapshot"
            metadata.append(
                {
                    "id": row["id"],
                    "ordinal": row["ordinal"],
                    "mount_name": controlled_input_mount_name(
                        row["ordinal"], row["original_filename"]
                    ),
                    **{
                        key: row[key]
                        for key in ("original_filename", "content_type", "size_bytes", "sha256")
                    },
                }
            )
        files.descriptors(metadata)
    except (files.ManagedFilesError, ValueError, TypeError) as error:
        return (
            error.reason if isinstance(error, files.ManagedFilesError) else "input_files_snapshot"
        )
    # Reject an already impossible file-copy budget before creating new
    # responsibility. Existing work keeps its frozen profile, never today's
    # deployment default. The child still checks the full remaining budget
    # including code/harness and any later capacity drift.
    tmp_bytes = (
        settings.sandbox_tmp_bytes
        if execution_id is None
        else session.scalar(
            select(Execution.resource_profile_snapshot["tmp_bytes"]).where(
                Execution.id == execution_id
            )
        )
    )
    manifest = [
        {"artifact_id": item["id"], **{k: v for k, v in item.items() if k != "id"}}
        for item in metadata
    ]
    # Before acceptance, 1 is a lower bound for the positive Execution ID's
    # encoded length. This avoids rejecting work using an invented larger ID.
    manifest_bytes = len(
        json.dumps(
            {"execution_id": execution_id if execution_id is not None else 1, "files": manifest},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if (
        type(tmp_bytes) is not int
        or 2 * (sum(item["size_bytes"] for item in metadata) + manifest_bytes) > tmp_bytes
    ):
        return "input_files_budget"
    if snapshot is not None or execution_id is not None or artifact_ids is not None:
        frozen = [
            {k: v for k, v in item.items() if k not in {"id", "mount_name"}} for item in metadata
        ]
        if (
            type(snapshot) is not dict
            or set(snapshot) != {"source_type", "revision", "artifacts"}
            or snapshot.get("source_type") != "managed_files"
            or type(snapshot.get("revision")) is not int
            or snapshot["revision"] <= 0
            or snapshot.get("artifacts") != frozen
        ):
            return "input_files_snapshot"
    return None


def require_new_execution(
    session: Session,
    adapter: Adapter,
    *,
    version_id: int | None,
    trigger: str,
    input_source_type: str,
    dependency_check: bool = False,
    target_worker_id: int | None = None,
    artifact_ids: tuple[int, ...] | None = None,
    input_snapshot: dict[str, Any] | None = None,
    frozen_subscription_bindings: list[dict[str, object]] | None = None,
) -> None:
    if not settings.worker_remote_v4_enabled:
        return
    if worker_legacy_window.enabled_mode():
        worker_id = adapter.runtime_worker_id if target_worker_id is None else target_worker_id
        worker = session.get(Worker, worker_id) if worker_id is not None else None
        if worker is not None and worker_legacy_window.target_allowed(session, worker):
            return
    reason = _configuration_reason(
        session,
        adapter,
        version_id=version_id,
        trigger=trigger,
        input_source_type=input_source_type,
        dependency_check=dependency_check,
    )
    if reason is None and input_source_type == "managed_files":
        reason = _file_reason(
            session,
            adapter.id,
            adapter.runtime_worker_id if target_worker_id is None else target_worker_id,
            artifact_ids=artifact_ids,
            snapshot=input_snapshot,
        )
    if reason is None:
        try:
            refs = (
                business_credentials.adapter_bindings(session, adapter.id)
                if frozen_subscription_bindings is None
                else bindings(frozen_subscription_bindings)
            )
            if refs and (
                adapter.language in COMPILED_LANGUAGES
                or _bounded_requirements(session, adapter.id, version_id) != ""
            ):
                raise BusinessCredentialError("credentials_mode")
            if refs and not settings.master_key:
                from dlr.control.services.secrets import _require_secret_store

                _require_secret_store()
            business_credentials.check(
                session,
                refs,
                adapter.runtime_worker_id if target_worker_id is None else target_worker_id,
            )
        except BusinessCredentialError as error:
            reason = error.reason
    if reason is None:
        requirements = _bounded_requirements(session, adapter.id, version_id)
        target = adapter.runtime_worker_id if target_worker_id is None else target_worker_id
        if requirements != "":
            if not _builtin_default(session, adapter.language):
                _reject("requirements")
            freeze_worker_wheel(session, adapter, version_id, target)
        elif _builtin_default(session, adapter.language):
            reason = "builtin_packages"
    if reason is not None:
        _reject(reason)


def execution_block_reason(
    session: Session, adapter: Adapter, execution: Execution, *, lock_credentials: bool = False
) -> str | None:
    """Inspect immutable responsibility; never replace it with current defaults."""
    reason = _configuration_reason(
        session,
        adapter,
        version_id=execution.version_id,
        trigger=execution.trigger,
        input_source_type=execution.input_source_type,
        dependency_check=execution.dependency_check,
    )
    if reason is not None:
        return reason
    if execution.input_source_type == "managed_files":
        reason = _file_reason(
            session,
            adapter.id,
            execution.target_worker_id_snapshot,
            snapshot=execution.input_snapshot,
            execution_id=execution.id,
        )
        if reason is not None:
            return reason
    if not _frozen_materials_allowed(
        session,
        adapter_id=adapter.id,
        version_id=execution.version_id,
        execution_id=execution.id,
        worker_id=execution.target_worker_id_snapshot,
        language=adapter.language,
        input_source=execution.input_source_type,
    ):
        return "builtin_packages"
    if execution.credential_bindings_snapshot:
        if adapter.language in COMPILED_LANGUAGES or execution.builtin_package_snapshot is not None:
            return "credentials_mode"
        try:
            business_credentials.check(
                session,
                business_credentials.execution_bindings(session, execution.id),
                execution.target_worker_id_snapshot,
                lock=lock_credentials,
            )
        except BusinessCredentialError as error:
            return error.reason
    return None


def dispatch_blocked(session: Session, execution_id: int) -> bool:
    """Recheck frozen work and target without loading code/input into Relay."""
    if not settings.worker_remote_v4_enabled:
        return False
    row = session.execute(
        select(
            Adapter.language,
            Adapter.adapter_type,
            Execution.trigger,
            Execution.input_source_type,
            Execution.dependency_check,
            Execution.credential_bindings_snapshot == [],
            Worker.capabilities,
            Worker.protocol_version,
            Execution.target_worker_id_snapshot,
            Execution.adapter_id,
            Execution.version_id,
        )
        .select_from(Execution)
        .join(Adapter, Adapter.id == Execution.adapter_id)
        .join(
            AdapterVersion,
            (AdapterVersion.id == Execution.version_id) & (AdapterVersion.adapter_id == Adapter.id),
        )
        .outerjoin(Worker, Worker.id == Execution.target_worker_id_snapshot)
        .where(Execution.id == execution_id)
    ).one_or_none()
    if row is None:
        return True
    (
        language,
        kind,
        trigger,
        source,
        dependency,
        no_credentials,
        languages,
        worker_protocol,
        target,
        adapter_id,
        version_id,
    ) = row
    if worker_protocol == 3:
        worker = session.get(Worker, target)
        return worker is None or not worker_legacy_window.target_allowed(session, worker)
    if source == "managed_files":
        size = session.scalar(
            select(func.octet_length(cast(Execution.input_snapshot, Text))).where(
                Execution.id == execution_id
            )
        )
        if size is None or size > 16_384:
            return True
        snapshot = session.scalar(
            select(Execution.input_snapshot).where(Execution.id == execution_id)
        )
        if (
            _file_reason(
                session,
                execution_id=execution_id,
                adapter_id=adapter_id,
                target=target,
                snapshot=snapshot,
            )
            is not None
        ):
            return True
    if not no_credentials:
        if (
            language in COMPILED_LANGUAGES
            or _bounded_requirements(session, adapter_id, version_id) != ""
        ):
            return True
        try:
            business_credentials.check(
                session, business_credentials.execution_bindings(session, execution_id), target
            )
        except BusinessCredentialError:
            return True
    return not (
        language in SUPPORTED_LANGUAGES
        and _matches_trigger(kind, trigger, source)
        and source in {"none", "json", "managed_files"}
        and dependency is False
        and _frozen_materials_allowed(
            session,
            adapter_id=adapter_id,
            version_id=version_id,
            execution_id=execution_id,
            worker_id=target,
            language=language,
            input_source=source,
        )
        and valid_capabilities(languages)
        and language in languages
    )
