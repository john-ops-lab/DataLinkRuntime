"""Adapter and immutable Revision domain service (M5.4.1)."""

import secrets as stdlib_secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import JSON, delete, func, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterInputConfig,
    AdapterPermission,
    AdapterSchedule,
    AdapterVersion,
    AdapterWebhook,
    Execution,
    ExecutionIdempotencyRecord,
    ExecutionOutbox,
    Worker,
    WorkerCleanupRequest,
)
from dlr.control.models.platform import AdapterCredentialBinding
from dlr.control.schemas.adapter import (
    AdapterCreate,
    AdapterResponse,
    AdapterUpdate,
    CloneRequest,
    VersionCreate,
)
from dlr.control.services import adapter_runtime, worker_availability
from dlr.control.services.execution_cancellation import (
    lock_nonterminal_executions,
    request_cancellation,
)


@dataclass(frozen=True)
class AdapterDeleteResult:
    """Outcome of one permanent-delete request."""

    waiting_for_worker: bool = False
    active_execution_id: int | None = None
    cleanup_request_id: int | None = None


def domain_error(
    status_code: int,
    code: str,
    message: str,
    params: Mapping[str, object] | None = None,
) -> HTTPException:
    """Build a compatible machine error with optional structured params.

    ``message`` remains the legacy fallback for old clients. New clients use
    ``code`` and ``params`` as the stable contract and never branch on text.
    Empty params are omitted so existing response shapes remain compatible.
    """
    detail: dict[str, object] = {"code": code, "message": message}
    if params:
        detail["params"] = dict(params)
    headers: dict[str, str] | None = None
    retry_after = params.get("retry_after") if params is not None else None
    if status_code in {429, 503} and isinstance(retry_after, (int, float)):
        headers = {"Retry-After": str(max(1, int(retry_after)))}
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def list_adapters(session: Session) -> list[Adapter]:
    return list(
        session.scalars(
            select(Adapter)
            .where(Adapter.archived_at.is_(None))
            .order_by(Adapter.updated_at.desc(), Adapter.id.desc())
        ).all()
    )


def get_adapter(session: Session, adapter_id: int) -> Adapter:
    adapter = session.get(Adapter, adapter_id)
    if adapter is None or adapter.archived_at is not None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    return adapter


def _require_not_archived(adapter: Adapter) -> None:
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")


def _active_name_conflict(session: Session, name: str, *, exclude_id: int | None = None) -> bool:
    """M5.5.9: a name conflicts only with a currently active Adapter.

    Soft-deleted Adapter names are reusable. ``name`` is already trimmed by the
    request schema; the case rule is exact-match (consistent front/back).
    """
    query = select(Adapter).where(Adapter.name == name, Adapter.archived_at.is_(None))
    if exclude_id is not None:
        query = query.where(Adapter.id != exclude_id)
    return session.scalar(query) is not None


def _adapter_response(
    adapter: Adapter,
    runtime_state: adapter_runtime.AdapterRuntimeState,
) -> AdapterResponse:
    response = AdapterResponse.model_validate(adapter)
    active = runtime_state.active_execution
    response.runtime_locked = runtime_state.locked
    response.running_execution_id = active.id if active is not None else None
    return response


def adapter_response(session: Session, adapter: Adapter) -> AdapterResponse:
    return _adapter_response(adapter, adapter_runtime.runtime_state(session, adapter))


def adapter_responses(session: Session, adapters: list[Adapter]) -> list[AdapterResponse]:
    states = adapter_runtime.runtime_states(session, adapters)
    return [_adapter_response(adapter, states[adapter.id]) for adapter in adapters]


def _add_default_demo_binding(session: Session, adapter: Adapter) -> None:
    """Bind the M5.5.7 demo Credential to a brand-new Adapter.

    Task Adapters get ``PASSWORD`` -> ``demo-passwd.password``. Webhook
    invocation authentication is a separate Bearer Token configuration and
    is never injected into ``context.secrets``. The binding is only created
    when the demo Credential exists, so deployments without a Secret Store or
    with the demo rows deleted simply start without bindings. The lazy import
    keeps the secrets service (which imports ``domain_error`` from here)
    cycle-free.
    """
    from dlr.control.services.secrets import (
        DEMO_PASSWORD_CREDENTIAL_NAME,
        demo_credential_id,
    )

    if adapter.adapter_type != "task":
        return
    demo_name, env_key, field = DEMO_PASSWORD_CREDENTIAL_NAME, "PASSWORD", "password"
    demo_id = demo_credential_id(session, demo_name)
    if demo_id is None:
        return
    session.add(
        AdapterCredentialBinding(
            adapter_id=adapter.id,
            env_key=env_key,
            credential_id=demo_id,
            field=field,
        )
    )


def create_adapter(
    session: Session, data: AdapterCreate, *, owner_user_id: int | None = None
) -> Adapter:
    if _active_name_conflict(session, data.name):
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
            {"name": data.name},
        )
    adapter = Adapter(
        name=data.name,
        description=data.description,
        language=data.language,
        adapter_type=data.adapter_type,
        timeout_seconds=data.timeout_seconds,
        owner_user_id=owner_user_id,
    )
    session.add(adapter)
    try:
        session.flush()
        # The additive B1 schema has one explicit Slot 0 per Adapter.  It is
        # a future Claim authority only; legacy HTTP Claim continues to use
        # the existing execution index until Batch 2.
        session.add(AdapterExecutionSlot(adapter_id=adapter.id, slot_no=0))
        if adapter.adapter_type == "task":
            # A new Task starts with one explicit Adapter-level input object;
            # the later migration handles historical rows.
            session.add(AdapterInputConfig(adapter_id=adapter.id))
        if adapter.adapter_type == "webhook":
            session.add(
                AdapterWebhook(
                    adapter_id=adapter.id,
                    public_id=stdlib_secrets.token_hex(8),
                    enabled=False,
                    credential_id=None,
                )
            )
        _add_default_demo_binding(session, adapter)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
            {"name": data.name},
        ) from None
    session.refresh(adapter)
    return adapter


def _require_worker_capability(worker: Worker, language: str) -> None:
    if language not in worker.capabilities:
        raise domain_error(
            409,
            "worker_capability_missing",
            f"Worker does not support {language}",
            {"language": language},
        )


def _validate_runtime_worker(
    session: Session,
    worker_id: int,
    language: str,
    *,
    now: datetime,
) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    if not worker_availability.is_effectively_online(worker, now=now):
        raise domain_error(409, "worker_offline", "The runtime Worker is offline")
    _require_worker_capability(worker, language)
    return worker


def resolve_runtime_worker(session: Session, adapter: Adapter, *, now: datetime) -> Worker:
    """Resolve the deterministic effective-online compatible runtime Worker."""
    if adapter.runtime_worker_id is not None:
        return _validate_runtime_worker(
            session,
            adapter.runtime_worker_id,
            adapter.language,
            now=now,
        )

    online = worker_availability.list_effectively_online_workers(session, now=now)
    compatible = [worker for worker in online if adapter.language in worker.capabilities]
    if len(compatible) == 1:
        adapter.runtime_worker_id = compatible[0].id
        return compatible[0]
    if len(compatible) > 1:
        raise domain_error(
            409,
            "runtime_worker_required",
            "Multiple compatible runtime Workers are online; choose one before saving",
        )
    if not online:
        raise domain_error(409, "worker_offline", "No online Worker is available")
    raise domain_error(
        409,
        "worker_capability_missing",
        f"No online Worker supports {adapter.language}",
        {"language": adapter.language},
    )


def update_adapter(session: Session, adapter_id: int, data: AdapterUpdate) -> Adapter:
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)

    runtime_worker_changed = (
        "runtime_worker_id" in data.model_fields_set
        and data.runtime_worker_id != adapter.runtime_worker_id
    )
    if runtime_worker_changed:
        adapter_runtime.require_runtime_unlocked(session, adapter)
        if data.runtime_worker_id is not None:
            _validate_runtime_worker(
                session,
                data.runtime_worker_id,
                adapter.language,
                now=worker_availability.current_time(session),
            )
        adapter.runtime_worker_id = data.runtime_worker_id

    run_mode_changed = "run_mode" in data.model_fields_set and data.run_mode != adapter.run_mode
    if run_mode_changed:
        if adapter.adapter_type != "task":
            raise domain_error(
                409,
                "adapter_type_mismatch",
                "Only task Adapters have a run mode",
            )
        adapter_runtime.require_runtime_unlocked(session, adapter)
        if data.run_mode is not None:
            adapter.run_mode = data.run_mode

    # M5.5.11: the single-run execution timeout is runtime configuration:
    # every active/pending Execution and an enabled Schedule must be gone
    # before it can change (same lock contract as the runtime Worker).
    timeout_changed = (
        "timeout_seconds" in data.model_fields_set
        and data.timeout_seconds != adapter.timeout_seconds
    )
    if timeout_changed:
        adapter_runtime.require_runtime_unlocked(session, adapter)
        if data.timeout_seconds is not None:
            adapter.timeout_seconds = data.timeout_seconds

    if data.name is not None and data.name != adapter.name:
        if _active_name_conflict(session, data.name, exclude_id=adapter.id):
            raise domain_error(
                409,
                "adapter_name_conflict",
                "Adapter name already exists",
                {"name": data.name},
            )
        adapter.name = data.name
    if data.description is not None:
        adapter.description = data.description

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
            {"name": data.name},
        ) from None
    session.refresh(adapter)
    return adapter


def _disable_trigger_for_delete(session: Session, adapter: Adapter) -> None:
    """Stop Schedule/Webhook delivery as part of an explicit stop-delete."""
    if adapter.adapter_type == "task":
        schedule = session.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter.id)
            .with_for_update()
        )
        if schedule is not None:
            schedule.enabled = False
            schedule.next_run_at = None
        return
    webhook = session.scalar(
        select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter.id).with_for_update()
    )
    if webhook is not None:
        webhook.enabled = False


def _require_cleanup_workers(session: Session, adapter: Adapter) -> list[int]:
    """Require live Workers for every private environment known to the Adapter.

    The runtime Worker can change after a completed Execution. Collecting the
    actual historical Workers before deleting Execution rows prevents an old
    Worker-local environment from becoming unreachable cleanup residue.
    """
    if adapter.latest_version_id is None:
        return []
    worker_ids: set[int] = set()
    if adapter.runtime_worker_id is not None:
        worker_ids.add(adapter.runtime_worker_id)
    worker_ids.update(
        worker_id
        for worker_id in session.scalars(
            select(Execution.worker_id).where(
                Execution.adapter_id == adapter.id,
                Execution.worker_id.is_not(None),
            )
        ).all()
        if worker_id is not None
    )
    for worker_id in sorted(worker_ids):
        worker = session.get(Worker, worker_id)
        if worker is None or not worker_availability.is_effectively_online(
            worker, now=worker_availability.current_time(session)
        ):
            raise domain_error(
                409,
                "worker_offline",
                "A Worker owning the Adapter's private environment is offline; "
                "permanent deletion is safely blocked",
            )
    return sorted(worker_ids)


def _require_running_execution_workers_online(
    session: Session, executions: list[Execution]
) -> None:
    """Prove every running responsibility can still converge before stopping."""
    for execution in executions:
        worker_id = (
            execution.worker_id or execution.target_worker_id_snapshot or execution.target_worker_id
        )
        worker = session.get(Worker, worker_id) if worker_id is not None else None
        if worker is None or not worker_availability.is_effectively_online(
            worker, now=worker_availability.current_time(session)
        ):
            raise domain_error(
                409,
                "worker_offline",
                "The Worker running this Execution is offline; deletion is safely blocked",
                {"active_execution_id": execution.id},
            )


def _cancel_queued_execution_for_delete(session: Session, execution: Execution) -> None:
    """Cancel one locked non-running responsibility and release every charge."""
    request_cancellation(execution)
    from dlr.control.services.execution import release_execution_leases

    release_execution_leases(session, execution.id)
    if execution.dispatch_backend == "rabbitmq":
        from dlr.control.services import admission, outbox

        admission.release_admission_once(session, execution)
        outbox.settle_cancelled_outbox(session, execution.id)


def _settle_terminal_rabbitmq_responsibilities(session: Session, adapter_id: int) -> None:
    """Close terminal RabbitMQ responsibility before deleting its parent rows."""
    from dlr.control.services import admission, outbox

    terminal_rows = list(
        session.scalars(
            select(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                Execution.dispatch_backend == "rabbitmq",
                Execution.status.in_(("succeeded", "dead_letter", "cancelled", "expired")),
            )
            .order_by(Execution.id.asc())
            .with_for_update()
        ).all()
    )
    for execution in terminal_rows:
        if execution.admission_released_at is None:
            admission.release_admission_once(session, execution)
        outbox.settle_pending_outbox(
            session,
            execution.id,
            disposition="execution_deleted",
        )

    unreleased_id = session.scalar(
        select(Execution.id)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.dispatch_backend == "rabbitmq",
            Execution.status.in_(("succeeded", "dead_letter", "cancelled", "expired")),
            Execution.admission_released_at.is_(None),
        )
        .order_by(Execution.id.asc())
        .limit(1)
    )
    pending_id = session.scalar(
        select(ExecutionOutbox.execution_id)
        .join(Execution, Execution.id == ExecutionOutbox.execution_id)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.dispatch_backend == "rabbitmq",
            ExecutionOutbox.status == "pending",
        )
        .order_by(ExecutionOutbox.execution_id.asc())
        .limit(1)
    )
    if unreleased_id is not None or pending_id is not None:
        raise RuntimeError("RabbitMQ responsibility did not settle before Adapter deletion")


def delete_adapter(session: Session, adapter_id: int, *, stop: bool = False) -> AdapterDeleteResult:
    """Permanently delete one Adapter after safe runtime quiescence.

    Every non-terminal Execution is locked before the decision. RabbitMQ rows
    are not covered by the legacy one-active-row index: several queued or
    retry-wait rows may exist for one Adapter. Stop-cancelled RabbitMQ rows
    release Admission, settle their pending Outbox generations and release
    input Leases before permanent deletion. A running Execution is only
    marked for cancellation and the request returns a waiting result; the
    caller must retry after the Worker reports terminal ``cancelled``. No
    Control-side fallback can remove a live Worker's private environment.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None or adapter.archived_at is not None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")

    # Upload creation and permanent deletion both serialize on the Adapter
    # row.  Check the writer state before any runtime stop/cancellation work so
    # a concurrent upload is never converted into an orphaned reservation.
    from dlr.control.services import managed_input_gc

    managed_input_gc.ensure_no_active_upload(session, adapter.id)

    # Acquire the RabbitMQ Admission scope before locking any Execution.  The
    # scope is harmless for a legacy-only Adapter and makes stop/delete follow
    # the same Adapter -> AdapterAdmission -> Global -> Execution/Outbox order
    # as ingress, cancellation and reconciliation.
    from dlr.control.services import admission

    if admission.lock_admission_scope(session, adapter.id) is None:  # pragma: no cover
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    nonterminal = lock_nonterminal_executions(session, adapter.id)
    if not stop:
        if nonterminal:
            raise domain_error(
                409,
                "adapter_runtime_locked",
                "Stop the Adapter and wait for all non-terminal Executions to finish "
                "before deleting",
            )
        adapter_runtime.require_runtime_unlocked(session, adapter)
    else:
        running = [execution for execution in nonterminal if execution.status == "running"]
        # Validate every running Worker before changing any queued responsibility;
        # this keeps a failed stop request free of partial cancellation effects.
        _require_running_execution_workers_online(session, running)
        _disable_trigger_for_delete(session, adapter)
        for execution in nonterminal:
            if execution.status == "running":
                request_cancellation(execution)
            else:
                _cancel_queued_execution_for_delete(session, execution)
        if running:
            session.commit()
            return AdapterDeleteResult(
                waiting_for_worker=True,
                active_execution_id=running[0].id,
            )

    cleanup_worker_ids = _require_cleanup_workers(session, adapter)
    _settle_terminal_rabbitmq_responsibilities(session, adapter.id)

    # Move charged Blob responsibility to rows that do not reference the
    # Adapter before removing any managed-input metadata.  The detached jobs
    # keep platform actual_bytes charged until their own worker confirms the
    # object is gone.
    deletion_job_ids = managed_input_gc.prepare_adapter_deletion(session, adapter.id)

    # The idempotency record points back to Execution with ON DELETE RESTRICT;
    # explicit Adapter deletion is the one retention exception and removes
    # only this Adapter's records before its Execution rows. Other Adapters'
    # records remain untouched.
    session.execute(
        delete(ExecutionIdempotencyRecord).where(
            ExecutionIdempotencyRecord.adapter_id == adapter.id
        )
    )
    # Child rows are deleted explicitly so the permanent-delete contract is
    # visible in the transaction and remains correct if a future FK changes
    # from CASCADE to RESTRICT. Credential rows are intentionally untouched.
    session.execute(delete(Execution).where(Execution.adapter_id == adapter.id))
    session.execute(
        delete(AdapterCredentialBinding).where(AdapterCredentialBinding.adapter_id == adapter.id)
    )
    session.execute(delete(AdapterPermission).where(AdapterPermission.adapter_id == adapter.id))
    session.execute(delete(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter.id))
    session.execute(delete(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter.id))
    adapter.latest_version_id = None
    session.flush()
    session.execute(delete(AdapterVersion).where(AdapterVersion.adapter_id == adapter.id))
    session.delete(adapter)
    cleanup_request_id = None
    for cleanup_worker_id in cleanup_worker_ids:
        cleanup = WorkerCleanupRequest(worker_id=cleanup_worker_id, adapter_id=adapter.id)
        session.add(cleanup)
        session.flush()
        if cleanup_request_id is None:
            cleanup_request_id = cleanup.id
    session.commit()
    managed_input_gc.record_audit_event(
        "adapter_delete",
        "success",
        adapter_id=adapter.id,
        deletion_job_id=deletion_job_ids[0] if deletion_job_ids else None,
    )
    return AdapterDeleteResult(cleanup_request_id=cleanup_request_id)


def list_versions(session: Session, adapter_id: int) -> list[AdapterVersion]:
    get_adapter(session, adapter_id)
    return list(
        session.scalars(
            select(AdapterVersion)
            .where(AdapterVersion.adapter_id == adapter_id)
            .order_by(AdapterVersion.seq.desc())
        ).all()
    )


def get_version(session: Session, adapter_id: int, version_id: int) -> AdapterVersion:
    get_adapter(session, adapter_id)
    version = session.get(AdapterVersion, version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    return version


def save_version(session: Session, adapter_id: int, data: VersionCreate) -> AdapterVersion:
    """Create one immutable Revision and atomically advance latest_version_id."""
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    adapter_runtime.require_runtime_unlocked(session, adapter)
    if adapter.latest_version_id is None and adapter.adapter_type == "task":
        resolve_runtime_worker(
            session,
            adapter,
            now=worker_availability.current_time(session),
        )

    max_seq = session.scalar(
        select(func.max(AdapterVersion.seq)).where(AdapterVersion.adapter_id == adapter_id)
    )
    version = AdapterVersion(
        adapter_id=adapter_id,
        seq=(max_seq or 0) + 1,
        code=data.code,
        requirements=data.requirements,
        runtime_config=data.runtime_config,
    )
    session.add(version)
    session.flush()
    adapter.latest_version_id = version.id
    session.commit()
    session.refresh(version)
    return version


def clone_adapter(
    session: Session,
    adapter_id: int,
    data: CloneRequest,
    *,
    owner_user_id: int | None = None,
) -> Adapter:
    """Clone common M5.4 facts; the clone has its own Revision 1 and no runs."""
    # Freeze the source configuration while the copy is assembled. Runtime
    # writes use the same Adapter-first lock order, so a clone cannot combine
    # Worker/mode/Schedule/binding facts from different moments.
    source = session.get(Adapter, adapter_id, with_for_update=True)
    if source is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(source)
    source_input_config = None
    source_schedule = None
    if source.adapter_type == "task":
        source_schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == source.id).with_for_update()
        )
        source_input_config = session.scalar(
            select(AdapterInputConfig)
            .where(AdapterInputConfig.adapter_id == source.id)
            .with_for_update()
        )
    if _active_name_conflict(session, data.name):
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
            {"name": data.name},
        )
    clone = Adapter(
        name=data.name,
        description=data.description if data.description is not None else source.description,
        language=source.language,
        adapter_type=source.adapter_type,
        run_mode=source.run_mode,
        runtime_worker_id=source.runtime_worker_id,
        owner_user_id=owner_user_id,
        # M5.5.11: the clone copies the source's single-run execution timeout.
        timeout_seconds=source.timeout_seconds,
    )
    session.add(clone)
    try:
        session.flush()
        session.add(AdapterExecutionSlot(adapter_id=clone.id, slot_no=0))
        session.flush()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
            {"name": data.name},
        ) from None

    if clone.adapter_type == "task":
        # Copy the current source/revision-independent input selection, but
        # never copy operational file identity.  A managed_files clone keeps
        # its retention/source contract with an empty selection.
        clone_json_value: object | None = None
        if source_input_config is None:
            clone_input_config = AdapterInputConfig(adapter_id=clone.id)
        else:
            if source_input_config.source_type == "json":
                # JSONB result processing represents a saved JSON null as
                # Python None; keep it a JSON null when none_as_null=True.
                clone_json_value = (
                    JSON.NULL
                    if source_input_config.json_value is None
                    else source_input_config.json_value
                )
            clone_input_config = AdapterInputConfig(
                adapter_id=clone.id,
                source_type=source_input_config.source_type,
                json_value=(
                    clone_json_value if source_input_config.source_type == "json" else null()
                ),
                retention_mode=source_input_config.retention_mode,
                retention_seconds=source_input_config.retention_seconds,
                revision=1,
            )
        session.add(clone_input_config)

    if source.latest_version_id is not None:
        source_version = session.get(AdapterVersion, source.latest_version_id)
        if source_version is None:
            raise RuntimeError("Adapter latest_version_id references a missing Revision")
        first = AdapterVersion(
            adapter_id=clone.id,
            seq=1,
            code=source_version.code,
            requirements=source_version.requirements,
            runtime_config=source_version.runtime_config,
        )
        session.add(first)
        session.flush()
        clone.latest_version_id = first.id

    bindings = session.scalars(
        select(AdapterCredentialBinding).where(AdapterCredentialBinding.adapter_id == adapter_id)
    ).all()
    for binding in bindings:
        session.add(
            AdapterCredentialBinding(
                adapter_id=clone.id,
                env_key=binding.env_key,
                credential_id=binding.credential_id,
                field=binding.field,
            )
        )
    if source.adapter_type == "task":
        if source_schedule is not None:
            session.add(
                AdapterSchedule(
                    adapter_id=clone.id,
                    cron=source_schedule.cron,
                    timezone=source_schedule.timezone,
                    input=(
                        clone_json_value
                        if source_input_config is not None
                        and source_input_config.source_type == "json"
                        else None
                    ),
                    enabled=False,
                    next_run_at=None,
                )
            )
    else:
        source_webhook = session.scalar(
            select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter_id).with_for_update()
        )
        session.add(
            AdapterWebhook(
                adapter_id=clone.id,
                public_id=(
                    source_webhook.public_id
                    if source_webhook is not None
                    else stdlib_secrets.token_hex(8)
                ),
                credential_id=(
                    source_webhook.credential_id if source_webhook is not None else None
                ),
                enabled=False,
            )
        )
    session.commit()
    session.refresh(clone)
    return clone
