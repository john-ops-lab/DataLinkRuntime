"""Restricted dependency-free Worker tasks at acceptance and final Claim."""

from typing import Any

from sqlalchemy import Text, cast, exists, func, select
from sqlalchemy.orm import Session

from dlr.common import managed_files_v4 as files
from dlr.common.business_credentials_v4 import BusinessCredentialError
from dlr.common.config import settings
from dlr.common.managed_input import controlled_input_mount_name
from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    AdapterVersion,
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    Worker,
)
from dlr.control.models.platform import PackageSource
from dlr.control.services import business_credentials_v4 as business_credentials
from dlr.control.services.adapter import domain_error


def require_trigger(trigger: str) -> None:
    if settings.worker_remote_v4_enabled and trigger not in {"manual", "schedule"}:
        _reject("trigger")


def require_input_source(source_type: str) -> None:
    if settings.worker_remote_v4_enabled and source_type not in {"none", "json", "managed_files"}:
        _reject("input_source")


def _reject(reason: str) -> None:
    raise domain_error(
        409,
        "worker_task_capability_unavailable",
        "Remote Worker stage supports dependency-free Python/JavaScript tasks "
        "with qualified none/JSON or bounded managed file input",
        {"reason": reason},
    )


def _configuration_reason(
    session: Session,
    adapter: Adapter,
    *,
    version_id: int | None,
    trigger: str,
    input_source_type: str,
    dependency_check: bool,
) -> str | None:
    if adapter.adapter_type != "task" or trigger not in {"manual", "schedule"}:
        return "trigger"
    if adapter.language not in {"python", "javascript"}:
        return "language"
    if dependency_check:
        return "dependency_check"
    if input_source_type not in {"none", "json", "managed_files"}:
        return "input_source"
    # Do not load business code or dependency text into the Control parent.
    if (
        session.scalar(
            select(
                exists().where(
                    AdapterVersion.id == version_id,
                    AdapterVersion.adapter_id == adapter.id,
                    AdapterVersion.requirements == "",
                )
            )
        )
        is not True
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
) -> None:
    if not settings.worker_remote_v4_enabled:
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
            refs = business_credentials.adapter_bindings(session, adapter.id)
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
    if reason is None and session.scalar(
        select(
            exists().where(
                PackageSource.kind == ("npm" if adapter.language == "javascript" else "pypi"),
                PackageSource.is_default.is_(True),
                PackageSource.index_url
                == (
                    "dlr-builtin://npm"
                    if adapter.language == "javascript"
                    else "dlr-builtin://pypi"
                ),
            )
        )
    ):
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
    unsupported = session.execute(
        select(
            func.coalesce(func.jsonb_typeof(Execution.builtin_package_snapshot), "null") != "null",
            Execution.credential_bindings_snapshot != [],
        ).where(Execution.id == execution.id)
    ).one()
    if unsupported[0]:
        return "builtin_packages"
    if unsupported[1]:
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
            AdapterVersion.requirements == "",
            Execution.credential_bindings_snapshot == [],
            func.coalesce(func.jsonb_typeof(Execution.builtin_package_snapshot), "null") == "null",
            Worker.capabilities,
            Execution.target_worker_id_snapshot,
            Execution.adapter_id,
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
        empty,
        no_credentials,
        no_builtin,
        languages,
        target,
        adapter_id,
    ) = row
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
        try:
            business_credentials.check(
                session, business_credentials.execution_bindings(session, execution_id), target
            )
        except BusinessCredentialError:
            return True
    return not (
        language in {"python", "javascript"}
        and kind == "task"
        and trigger in {"manual", "schedule"}
        and source in {"none", "json", "managed_files"}
        and dependency is False
        and empty is True
        and no_builtin is True
        and valid_capabilities(languages)
        and language in languages
    )
