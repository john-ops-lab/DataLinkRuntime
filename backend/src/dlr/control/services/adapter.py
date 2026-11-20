"""Adapter and immutable Revision domain service (M5.4.1)."""

import logging
import secrets as stdlib_secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import JSON, delete, func, null, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterInputConfig,
    AdapterPermission,
    AdapterSchedule,
    AdapterVersion,
    AdapterWebhook,
    Execution,
    ExecutionAttempt,
    ExecutionIdempotencyRecord,
    ExecutionOutbox,
    Worker,
    WorkerCleanupRequest,
    WorkerIdentityReservation,
)
from dlr.control.models.platform import AdapterCredentialBinding
from dlr.control.models.reliable_execution import OPEN_OUTBOX_STATES, WorkerAdapterStorageOwnerV4
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterDeleteResult:
    """Outcome of one permanent-delete request."""

    waiting_for_worker: bool = False
    active_execution_id: int | None = None
    cleanup_request_id: int | None = None
    waiting_for_cleanup: bool = False


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
    from dlr.control.services.worker_pool import adapter_route

    response = AdapterResponse.model_validate(adapter)
    response.route = adapter_route(adapter)
    active = runtime_state.active_execution
    response.runtime_locked = runtime_state.locked
    response.running_execution_id = active.id if active is not None else None
    return response


def adapter_response(session: Session, adapter: Adapter) -> AdapterResponse:
    return adapter_responses(session, [adapter])[0]


def adapter_responses(session: Session, adapters: list[Adapter]) -> list[AdapterResponse]:
    from dlr.control.services.adapter_batch_config import configurations
    from dlr.control.services.worker_placement_cutover import mode

    states = adapter_runtime.runtime_states(session, adapters)
    configs = configurations(session, adapters)
    pool_enabled = (
        bool(adapters) and settings.worker_remote_v4_enabled and mode(session) == "reserved"
    )
    responses = []
    for adapter in adapters:
        response = _adapter_response(adapter, states[adapter.id])
        response.pool_placement_enabled = pool_enabled
        response.batch_token = configs[adapter.id].token
        responses.append(response)
    return responses


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
        # Slot 0 is the single concurrency authority for this Adapter.
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


def _validate_runtime_worker_assignment(
    session: Session,
    worker_id: int,
    language: str,
) -> Worker:
    """Validate a durable fixed assignment without requiring a live heartbeat."""

    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    _require_worker_capability(worker, language)
    return worker


def resolve_runtime_worker(session: Session, adapter: Adapter, *, now: datetime) -> Worker:
    """Resolve the deterministic effective-online compatible runtime Worker."""
    from dlr.control.services.worker_pool import adapter_route, require_fixed_route

    require_fixed_route(adapter_route(adapter))
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
        adapter.route_version += 1
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
    from dlr.control.schemas.worker_pool import FixedRoute, PoolRoute
    from dlr.control.services.worker_pool import adapter_route, fixed_route, validate_pool_route

    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    metadata_changed = (data.name is not None and data.name != adapter.name) or (
        data.description is not None and data.description != adapter.description
    )

    current_route = adapter_route(adapter)
    if (
        data.expected_route_version is not None
        and data.expected_route_version != adapter.route_version
    ):
        raise domain_error(
            409,
            "adapter_route_version_conflict",
            "Adapter route has changed",
            {"current_version": adapter.route_version},
        )
    next_route = current_route
    if "route" in data.model_fields_set:
        next_route = data.route
    elif "runtime_worker_id" in data.model_fields_set and (
        data.runtime_worker_id is not None or not isinstance(current_route, PoolRoute)
    ):
        # Old clients echo null for a Pool. Only a legacy Worker ID or an
        # explicit new-style route selection may replace a Pool binding.
        if data.runtime_worker_id is not None and not 0 < data.runtime_worker_id <= 2**63 - 1:
            raise domain_error(404, "worker_not_found", "Worker not found")
        next_route = fixed_route(data.runtime_worker_id)

    route_changed = next_route != current_route
    if route_changed:
        adapter_runtime.require_runtime_unlocked(session, adapter)
        if isinstance(next_route, FixedRoute):
            _validate_runtime_worker_assignment(session, next_route.worker_id, adapter.language)
        elif isinstance(next_route, PoolRoute):
            next_route = validate_pool_route(session, next_route)

    run_mode_changed = "run_mode" in data.model_fields_set and data.run_mode != adapter.run_mode
    if run_mode_changed:
        from dlr.control.services.subscription_guards import require_no_subscription

        require_no_subscription(session, adapter.id)
        if adapter.adapter_type != "task":
            raise domain_error(
                409,
                "adapter_type_mismatch",
                "Only task Adapters have a run mode",
            )
        adapter_runtime.require_runtime_unlocked(session, adapter)

    # M5.5.11: the single-run execution timeout is runtime configuration:
    # every active/pending Execution and an enabled Schedule must be gone
    # before it can change (same lock contract as the runtime Worker).
    timeout_changed = (
        "timeout_seconds" in data.model_fields_set
        and data.timeout_seconds != adapter.timeout_seconds
    )
    if timeout_changed:
        adapter_runtime.require_runtime_unlocked(session, adapter)
    if (
        data.name is not None
        and data.name != adapter.name
        and _active_name_conflict(session, data.name, exclude_id=adapter.id)
    ):
        raise domain_error(
            409, "adapter_name_conflict", "Adapter name already exists", {"name": data.name}
        )

    if route_changed:
        adapter.runtime_worker_id = (
            next_route.worker_id if isinstance(next_route, FixedRoute) else None
        )
        adapter.pool_route = (
            next_route.model_dump(mode="json") if isinstance(next_route, PoolRoute) else None
        )
        adapter.route_version += 1
        if next_route is not None and adapter.configuration_notes.get(
            "runtime_route_requires_rebind"
        ):
            adapter.configuration_notes = {
                k: v
                for k, v in adapter.configuration_notes.items()
                if k != "runtime_route_requires_rebind"
            }
    if run_mode_changed and data.run_mode is not None:
        adapter.run_mode = data.run_mode
    if timeout_changed and data.timeout_seconds is not None:
        adapter.timeout_seconds = data.timeout_seconds
    if data.name is not None:
        adapter.name = data.name
    if data.description is not None:
        adapter.description = data.description
    if metadata_changed and adapter.configuration_notes.get("review_environment"):
        adapter.configuration_notes = {**adapter.configuration_notes, "review_environment": False}

    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint == "fk_adapters_pool_revision":
            raise domain_error(
                409, "pool_route_revision_unavailable", "Pool revision no longer exists"
            ) from None
        if constraint != "uq_adapters_active_name":
            raise
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


def _require_cleanup_workers(
    session: Session, adapter: Adapter, *, confirmed_v4: bool = False
) -> list[Worker]:
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
        session.scalars(
            select(WorkerAdapterStorageOwnerV4.worker_id).where(
                WorkerAdapterStorageOwnerV4.adapter_id == adapter.id
            )
        )
    )
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
    workers = []
    for worker_id in sorted(worker_ids):
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if worker is not None and confirmed_v4 and worker.protocol_version == 4:
            workers.append(worker)
            continue
        if worker is None or not worker_availability.is_effectively_online(
            worker, now=worker_availability.current_time(session)
        ):
            raise domain_error(
                409,
                "worker_offline",
                "A Worker owning the Adapter's private environment is offline; "
                "permanent deletion is safely blocked",
            )
        if worker.protocol_version == 4:
            reservation = session.scalar(
                select(WorkerIdentityReservation).where(
                    WorkerIdentityReservation.worker_id == worker_id
                )
            )
            if (
                worker.identity_lifecycle != "reserved"
                or reservation is None
                or reservation.legacy_stop_evidence_sha256 is not None
                or not worker_availability.v4_adapter_cleanup_ready(worker)
            ):
                raise domain_error(
                    409,
                    "worker_cleanup_storage_unverified",
                    "Worker storage ownership cannot be verified for Adapter cleanup",
                )
        workers.append(worker)
    return workers


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
    from dlr.control.services.execution import cancel_execution_locked

    cancel_execution_locked(session, execution)


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
        if (
            session.scalar(
                select(ExecutionAttempt.id)
                .where(
                    ExecutionAttempt.execution_id == execution.id,
                    ExecutionAttempt.worker_identity_id.is_not(None),
                    ExecutionAttempt.cleanup_settled_at.is_(None),
                )
                .limit(1)
            )
            is not None
        ):
            raise domain_error(
                409,
                "worker_cleanup_pending",
                "The original Worker Attempt still has cleanup responsibility",
                {"execution_id": execution.id},
            )
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
            ExecutionOutbox.status.in_(OPEN_OUTBOX_STATES),
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

    from dlr.control.services.subscription_guards import require_no_subscription

    # Even stop=true must not cancel/delete accepted subscription responsibility.
    require_no_subscription(session, adapter.id)

    # Upload creation and permanent deletion both serialize on the Adapter
    # row.  Check the writer state before any runtime stop/cancellation work so
    # a concurrent upload is never converted into an orphaned reservation.
    from dlr.control.services import managed_input_gc

    managed_input_gc.ensure_no_active_upload(session, adapter.id)
    # A rejected delete must not leak a successful history purge into a
    # caller's later commit. The Adapter lock predates both savepoints.
    for _ in range(2):
        with session.begin_nested() as deletion:
            deleted = _delete_adapter_attempt(session, adapter_id, stop=stop)
            if deleted is None:
                deletion.rollback()
        if deleted is not None:
            result, deletion_job_id = deleted
            session.commit()
            if result.waiting_for_cleanup:
                return result
            managed_input_gc.record_audit_event(
                "adapter_delete", "success", adapter_id=adapter_id, deletion_job_id=deletion_job_id
            )
            return result

        # Rollback released Quota/Admission/Execution locks and restored all
        # purged rows. Re-read cancellation state; never use the old running ID.
        with session.begin_nested() as cancellation:
            waiting = _stop_adapter_delete_wait(session, adapter_id)
            if waiting is None:
                cancellation.rollback()
        if waiting is not None:
            session.commit()
            return waiting
        # Fresh state no longer has a running row. Admission locks must be
        # released before retrying the Quota-first deletion path once.
    raise domain_error(
        409, "adapter_runtime_locked", "Adapter runtime changed during deletion; retry the request"
    )


def _stop_adapter_delete_wait(session: Session, adapter_id: int) -> AdapterDeleteResult | None:
    """Cancel only fresh locked runtime rows; historical input is untouched."""
    from dlr.control.services import admission

    if admission.lock_admission_scope(session, adapter_id) is None:  # pragma: no cover
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter = session.get(Adapter, adapter_id)
    assert adapter is not None
    nonterminal = lock_nonterminal_executions(session, adapter_id)
    running = [execution for execution in nonterminal if execution.status == "running"]
    if not running:
        return None
    _require_running_execution_workers_online(session, running)
    _disable_trigger_for_delete(session, adapter)
    for execution in nonterminal:
        if execution.status == "running":
            request_cancellation(execution)
        else:
            _cancel_queued_execution_for_delete(session, execution)
    return AdapterDeleteResult(waiting_for_worker=True, active_execution_id=running[0].id)


def _delete_adapter_attempt(
    session: Session, adapter_id: int, *, stop: bool, finalize_v4: bool = False
) -> tuple[AdapterDeleteResult, int | None] | None:
    """One savepoint owns all history, quota and permanent-delete mutations."""
    from dlr.control.services import managed_input_gc
    from dlr.control.services.subscription_delivery import (
        lock_drained_adapter_subscriptions,
        purge_deleted_adapter_history,
    )

    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    if adapter is None or (adapter.archived_at is not None) != finalize_v4:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    v4_attempts = select(ExecutionAttempt.id).where(
        ExecutionAttempt.execution_id.in_(
            select(Execution.id).where(Execution.adapter_id == adapter_id)
        ),
        ExecutionAttempt.worker_identity_id.is_not(None),
    )
    has_v4_storage = (
        session.scalar(v4_attempts.limit(1)) is not None
        or session.scalar(
            select(WorkerAdapterStorageOwnerV4.worker_id)
            .where(WorkerAdapterStorageOwnerV4.adapter_id == adapter_id)
            .limit(1)
        )
        is not None
    )
    # The temporary archive satisfies the DB batch guard. On any failure the
    # surrounding savepoint restores both database and ORM state; never write
    # a replacement flag into an already-failed SQL transaction.
    if not has_v4_storage or finalize_v4:
        archived_at = adapter.archived_at
        adapter.archived_at = worker_availability.current_time(session)
        session.flush()
        purge_deleted_adapter_history(session, adapter.id)
        adapter.archived_at = archived_at
        session.flush()
    else:
        lock_drained_adapter_subscriptions(session, adapter.id)
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
        if any(execution.status == "running" for execution in nonterminal):
            return None
        _disable_trigger_for_delete(session, adapter)
        for execution in nonterminal:
            _cancel_queued_execution_for_delete(session, execution)

    _settle_terminal_rabbitmq_responsibilities(session, adapter.id)
    cleanup_workers = _require_cleanup_workers(session, adapter, confirmed_v4=finalize_v4)
    fixed_workers = [worker for worker in cleanup_workers if worker.protocol_version == 4]
    if has_v4_storage and not finalize_v4:
        # Freeze the Adapter first. Control settlement precedes retirement of
        # the original local journal, which may still need its proof rows.
        adapter.archived_at = worker_availability.current_time(session)
        tickets = [
            WorkerCleanupRequest(
                worker_id=worker.id, adapter_id=adapter.id, v4_identity_id=worker.identity_id
            )
            for worker in fixed_workers
        ]
        if not tickets:
            raise domain_error(409, "worker_cleanup_storage_unverified", "Worker ownership changed")
        session.add_all(tickets)
        session.flush()
        return AdapterDeleteResult(waiting_for_cleanup=True, cleanup_request_id=tickets[0].id), None
    if finalize_v4:
        requests = list(
            session.scalars(
                select(WorkerCleanupRequest)
                .where(
                    WorkerCleanupRequest.adapter_id == adapter_id,
                    WorkerCleanupRequest.v4_identity_id.is_not(None),
                )
                .with_for_update()
            )
        )
        required = {(worker.id, worker.identity_id) for worker in fixed_workers}
        if (
            not required
            or required != {(row.worker_id, row.v4_identity_id) for row in requests}
            or any(row.status != "completed" or row.completed_at is None for row in requests)
        ):
            raise domain_error(409, "worker_cleanup_pending", "Adapter cleanup is still pending")
        # Delete child Attempts while the DB trigger can still inspect their
        # parent, exact v4 completion and original Worker cleanup receipt.
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.id.in_(v4_attempts)))

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
    for worker in cleanup_workers:
        if finalize_v4 and worker.protocol_version == 4:
            continue
        cleanup = WorkerCleanupRequest(
            worker_id=worker.id,
            adapter_id=adapter.id,
            v4_identity_id=worker.identity_id if worker.protocol_version == 4 else None,
        )
        session.add(cleanup)
        session.flush()
        if cleanup_request_id is None:
            cleanup_request_id = cleanup.id
    return (
        AdapterDeleteResult(cleanup_request_id=cleanup_request_id),
        deletion_job_ids[0] if deletion_job_ids else None,
    )


def finalize_one_v4_adapter_deletion(session: Session, *, after_adapter_id: int = 0) -> int | None:
    """Visit one ready Adapter so a blocked owner cannot stall later cleanup."""
    from dlr.control.services.control_ha_capacity import assert_local_control

    assert_local_control(session)
    pending = select(WorkerCleanupRequest.id).where(
        WorkerCleanupRequest.adapter_id == Adapter.id,
        WorkerCleanupRequest.v4_identity_id.is_not(None),
    )
    ready = pending.where(WorkerCleanupRequest.status != "completed")
    adapter = session.scalar(
        select(Adapter)
        .where(
            Adapter.id > after_adapter_id,
            Adapter.archived_at.is_not(None),
            pending.exists(),
            ~ready.exists(),
        )
        .order_by(Adapter.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if adapter is None:
        session.rollback()
        return None
    from dlr.control.services import managed_input_gc

    adapter_id = adapter.id
    try:
        managed_input_gc.ensure_no_active_upload(session, adapter_id)
        result = _delete_adapter_attempt(session, adapter_id, stop=False, finalize_v4=True)
        if result is None:
            session.rollback()
            return adapter_id
        session.commit()
    except (HTTPException, SQLAlchemyError) as error:
        session.rollback()
        logger.warning(
            "Adapter %s finalization remains pending (%s)", adapter_id, type(error).__name__
        )
        return adapter_id
    _, deletion_job_id = result
    managed_input_gc.record_audit_event(
        "adapter_delete", "success", adapter_id=adapter_id, deletion_job_id=deletion_job_id
    )
    return adapter_id


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


def save_version(
    session: Session,
    adapter_id: int,
    data: VersionCreate,
    *,
    dismiss_import_review: bool = True,
) -> AdapterVersion:
    """Create one immutable Revision and atomically advance latest_version_id."""
    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    adapter_runtime.require_runtime_unlocked(session, adapter)
    if (
        adapter.latest_version_id is None
        and adapter.adapter_type == "task"
        and adapter.pool_route is None
        and not adapter.configuration_notes.get("runtime_route_requires_rebind")
    ):
        if adapter.runtime_worker_id is None:
            resolve_runtime_worker(
                session,
                adapter,
                now=worker_availability.current_time(session),
            )
        else:
            _validate_runtime_worker_assignment(
                session,
                adapter.runtime_worker_id,
                adapter.language,
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
    if dismiss_import_review and adapter.configuration_notes.get("review_environment"):
        adapter.configuration_notes = {**adapter.configuration_notes, "review_environment": False}
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
        configuration_notes=(
            {"runtime_route_requires_rebind": True}
            if source.runtime_worker_id is not None
            or source.pool_route is not None
            or source.configuration_notes.get("runtime_route_requires_rebind")
            else {}
        ),
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
                response_mode=source_webhook.response_mode if source_webhook else "accepted",
                response_timeout_seconds=(
                    source_webhook.response_timeout_seconds if source_webhook else 30
                ),
            )
        )
    from dlr.control.services.subscription_management import clone_for_adapter

    clone_for_adapter(session, source.id, clone.id)
    session.commit()
    session.refresh(clone)
    return clone
