"""Domain service for Adapter and AdapterVersion management.

Owns transactions, version numbering and publish semantics. Pointer fields
(``latest_version_id`` / ``published_version_id``) are only ever modified
here, never from public API input.

M3.2 adds the production lifecycle: the publish gate, Start/Stop of the
production entry, Unpublish, Archive/Restore and Clone. The stored
``production_state`` only ever holds ``idle/running/stopped``; richer UI
states (未发布/待启动/异常/已归档) are derived from pointers, the active
Production Execution and ``archived_at`` on the client side.
"""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterVersion, Execution, Worker
from dlr.control.models.platform import AdapterCredentialBinding
from dlr.control.schemas.adapter import (
    AdapterCreate,
    AdapterResponse,
    AdapterUpdate,
    CloneRequest,
    PublishGateLastTest,
    PublishGateResponse,
    VersionCreate,
)
from dlr.control.services import worker_availability
from dlr.control.services.execution_cancellation import (
    lock_active_production_execution,
    request_cancellation,
)

# Statuses that make a Production Execution "active" (at most one per
# Adapter, enforced by the partial unique index in migration 0003).
ACTIVE_PRODUCTION_STATUSES = ("pending", "running")

# M5.1: trigger values that count as production-class for the active-execution
# unique constraint and the production lifecycle queries. ``manual`` stays
# outside: test runs are never constrained by the production slot.
PRODUCTION_TRIGGERS = ("production", "schedule", "webhook")


def _require_worker_capability(worker: Worker, language: str) -> None:
    if language not in worker.capabilities:
        raise domain_error(
            409,
            "worker_capability_missing",
            f"Worker does not support {language}",
        )


def domain_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the stable M1 domain error format (detail object with a code)."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def list_adapters(session: Session) -> list[Adapter]:
    return list(
        session.scalars(
            select(Adapter).order_by(Adapter.updated_at.desc(), Adapter.id.desc())
        ).all()
    )


def get_adapter(session: Session, adapter_id: int) -> Adapter:
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    return adapter


def _require_not_archived(adapter: Adapter) -> None:
    """Archived Adapters are read-only: no Save/Publish/Test/Start (M3.2)."""
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_archived", "Adapter is archived")


def _active_production_execution(session: Session, adapter_id: int) -> Execution | None:
    """The Adapter's active Production Execution, or None."""
    return session.scalar(
        select(Execution)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.trigger.in_(PRODUCTION_TRIGGERS),
            Execution.status.in_(ACTIVE_PRODUCTION_STATUSES),
        )
        .limit(1)
    )


def _latest_production_executions(session: Session, adapter_ids: list[int]) -> dict[int, Execution]:
    """Return each Adapter's latest Production Execution in one query.

    Execution ids are creation ordered throughout the existing history API.
    Using a grouped max-id subquery keeps Adapter list serialization bounded
    to one extra query instead of adding one query per Catalog row.
    """
    if not adapter_ids:
        return {}
    latest_ids = (
        select(func.max(Execution.id).label("id"))
        .where(
            Execution.adapter_id.in_(adapter_ids),
            Execution.trigger.in_(PRODUCTION_TRIGGERS),
        )
        .group_by(Execution.adapter_id)
        .subquery()
    )
    executions = session.scalars(
        select(Execution).join(latest_ids, Execution.id == latest_ids.c.id)
    ).all()
    return {execution.adapter_id: execution for execution in executions}


def _active_production_executions(session: Session, adapter_ids: list[int]) -> dict[int, Execution]:
    """Return active Production Executions for a group of Adapters."""
    if not adapter_ids:
        return {}
    executions = session.scalars(
        select(Execution).where(
            Execution.adapter_id.in_(adapter_ids),
            Execution.trigger.in_(PRODUCTION_TRIGGERS),
            Execution.status.in_(ACTIVE_PRODUCTION_STATUSES),
        )
    ).all()
    return {execution.adapter_id: execution for execution in executions}


def _version_seqs(session: Session, version_ids: set[int]) -> dict[int, int]:
    """Return Adapter-local version numbers for a group of version ids."""
    if not version_ids:
        return {}
    rows = session.execute(
        select(AdapterVersion.id, AdapterVersion.seq).where(AdapterVersion.id.in_(version_ids))
    ).all()
    return {version_id: seq for version_id, seq in rows}


def _adapter_response(
    adapter: Adapter,
    active: Execution | None,
    latest: Execution | None,
    version_seqs: dict[int, int],
) -> AdapterResponse:
    """Build an Adapter response from its latest Production Execution."""
    response = AdapterResponse.model_validate(adapter)
    if adapter.published_version_id is not None:
        response.published_version_seq = version_seqs.get(adapter.published_version_id)
    if adapter.production_version_id is not None:
        response.production_version_seq = version_seqs.get(adapter.production_version_id)
    if latest is not None:
        response.last_production_execution_id = latest.id
        response.last_production_execution_status = latest.status
        response.last_production_version_id = latest.version_id
        response.last_production_version_seq = version_seqs.get(latest.version_id)
    if active is not None:
        response.running_version_id = active.version_id
        response.running_version_seq = version_seqs.get(active.version_id)
        response.running_execution_id = active.id
    return response


def adapter_response(session: Session, adapter: Adapter) -> AdapterResponse:
    """Serialize one Adapter including active and latest production facts."""
    latest = _latest_production_executions(session, [adapter.id]).get(adapter.id)
    active = _active_production_executions(session, [adapter.id]).get(adapter.id)
    version_ids = {
        version_id
        for version_id in (
            adapter.published_version_id,
            adapter.production_version_id,
            active.version_id if active is not None else None,
            latest.version_id if latest is not None else None,
        )
        if version_id is not None
    }
    return _adapter_response(adapter, active, latest, _version_seqs(session, version_ids))


def adapter_responses(session: Session, adapters: list[Adapter]) -> list[AdapterResponse]:
    """Serialize an Adapter list without per-Adapter Execution queries."""
    latest_by_adapter = _latest_production_executions(session, [adapter.id for adapter in adapters])
    active_by_adapter = _active_production_executions(session, [adapter.id for adapter in adapters])
    version_ids = {
        version_id
        for adapter in adapters
        for version_id in (
            adapter.published_version_id,
            adapter.production_version_id,
            active_by_adapter[adapter.id].version_id if adapter.id in active_by_adapter else None,
            latest_by_adapter[adapter.id].version_id if adapter.id in latest_by_adapter else None,
        )
        if version_id is not None
    }
    version_seqs = _version_seqs(session, version_ids)
    return [
        _adapter_response(
            adapter,
            active_by_adapter.get(adapter.id),
            latest_by_adapter.get(adapter.id),
            version_seqs,
        )
        for adapter in adapters
    ]


def create_adapter(session: Session, data: AdapterCreate) -> Adapter:
    existing = session.scalar(select(Adapter).where(Adapter.name == data.name))
    if existing is not None:
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    adapter = Adapter(name=data.name, description=data.description, language=data.language)
    session.add(adapter)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Lost a race against a concurrent create with the same name.
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def update_adapter(session: Session, adapter_id: int, data: AdapterUpdate) -> Adapter:
    adapter = get_adapter(session, adapter_id)
    if data.name is not None and data.name != adapter.name:
        conflict = session.scalar(
            select(Adapter).where(Adapter.name == data.name, Adapter.id != adapter_id)
        )
        if conflict is not None:
            raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
        adapter.name = data.name
    if data.description is not None:
        adapter.description = data.description
    # Explicit null clears the pointer (invalidating the publish gate by
    # design); omitting the field leaves it unchanged.
    if "production_worker_id" in data.model_fields_set:
        # M5.1: the production Worker is locked while the entry is running.
        if adapter.production_state == "running" and (
            data.production_worker_id != adapter.production_worker_id
        ):
            raise domain_error(
                409,
                "production_running",
                "Stop production before changing the production Worker",
            )
        if data.production_worker_id is not None:
            worker = session.get(Worker, data.production_worker_id)
            if worker is None:
                raise domain_error(404, "worker_not_found", "Worker not found")
            _require_worker_capability(worker, adapter.language)
        adapter.production_worker_id = data.production_worker_id
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def delete_adapter(session: Session, adapter_id: int) -> None:
    adapter = get_adapter(session, adapter_id)
    # M2: execution history must survive, so an Adapter with any Execution
    # can no longer be physically deleted.
    if (
        session.scalar(select(Execution.id).where(Execution.adapter_id == adapter_id).limit(1))
        is not None
    ):
        raise domain_error(
            409,
            "adapter_has_executions",
            "Adapter has execution history and cannot be deleted",
        )
    # Clear the version pointers first so the FK checks never block deletion;
    # versions themselves are removed by adapter_versions ON DELETE CASCADE.
    adapter.latest_version_id = None
    adapter.published_version_id = None
    adapter.production_version_id = None
    session.delete(adapter)
    session.commit()


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
    """Fetch one version; cross-adapter lookups never leak and return 404."""
    get_adapter(session, adapter_id)
    version = session.get(AdapterVersion, version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    return version


def save_version(session: Session, adapter_id: int, data: VersionCreate) -> AdapterVersion:
    """Save new version: single transaction with a row lock on the Adapter.

    The lock guarantees concurrent saves on the same Adapter cannot produce
    duplicate seq values or leave latest pointing at the wrong version.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    max_seq = session.scalar(
        select(func.max(AdapterVersion.seq)).where(AdapterVersion.adapter_id == adapter_id)
    )
    next_seq = (max_seq or 0) + 1
    version = AdapterVersion(
        adapter_id=adapter_id,
        seq=next_seq,
        code=data.code,
        requirements=data.requirements,
        runtime_config=data.runtime_config,
    )
    session.add(version)
    session.flush()  # assign version.id inside the locked transaction
    adapter.latest_version_id = version.id
    session.commit()
    session.refresh(version)
    return version


def _evaluate_publish_gate(
    session: Session, adapter: Adapter, version_id: int
) -> PublishGateResponse:
    """Gate rule: the target version's most recent test run on the current
    production Worker must be ``succeeded``. Historical test rows without a
    target Worker never satisfy the gate and require a re-test.
    """
    worker_id = adapter.production_worker_id
    if worker_id is None:
        return PublishGateResponse(allowed=False, reason="no_production_worker")
    last = session.scalar(
        select(Execution)
        .where(
            Execution.adapter_id == adapter.id,
            Execution.version_id == version_id,
            Execution.trigger == "manual",
            Execution.target_worker_id == worker_id,
        )
        .order_by(Execution.id.desc())
        .limit(1)
    )
    if last is None:
        return PublishGateResponse(allowed=False, reason="not_tested_on_production_worker")
    last_test = PublishGateLastTest(
        execution_id=last.id, status=last.status, ended_at=last.ended_at
    )
    if last.status != "succeeded":
        return PublishGateResponse(
            allowed=False, reason="last_test_not_succeeded", last_test=last_test
        )
    return PublishGateResponse(allowed=True, last_test=last_test)


def publish_gate(session: Session, adapter_id: int, version_id: int) -> PublishGateResponse:
    """Read-only gate evaluation for the Publish confirmation dialog."""
    adapter = get_adapter(session, adapter_id)
    version = get_version(session, adapter_id, version_id)
    return _evaluate_publish_gate(session, adapter, version.id)


def publish_version(session: Session, adapter_id: int, version_id: int) -> Adapter:
    """Point published_version_id at an existing version of this Adapter.

    Publish never creates or mutates versions and never touches latest.
    M3.2 enforces the publish gate server-side (409 ``publish_gate_locked``)
    and rejects archived Adapters. Publish only changes the production target:
    an active Production Execution remains pinned to its original version.
    Re-publishing the already published version stays idempotent and gate-free.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    version = session.get(AdapterVersion, version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    if adapter.published_version_id == version_id:
        session.refresh(adapter)
        return adapter
    gate = _evaluate_publish_gate(session, adapter, version.id)
    if not gate.allowed:
        raise domain_error(
            409,
            "publish_gate_locked",
            f"Publish gate locked: {gate.reason}",
        )
    adapter.published_version_id = version_id
    session.commit()
    session.refresh(adapter)
    return adapter


# --- M3.2 production lifecycle --------------------------------------------------


def _resolve_production_worker(session: Session, adapter: Adapter, *, now: datetime) -> Worker:
    """Resolve one effective-online, language-compatible production Worker."""
    if adapter.production_worker_id is not None:
        worker = session.get(Worker, adapter.production_worker_id)
        if worker is None:
            raise domain_error(404, "worker_not_found", "The production Worker was not found")
        if not worker_availability.is_effectively_online(worker, now=now):
            raise domain_error(409, "worker_offline", "The production Worker is offline")
        _require_worker_capability(worker, adapter.language)
        return worker

    online = worker_availability.list_effectively_online_workers(session, now=now)
    compatible = [worker for worker in online if adapter.language in worker.capabilities]
    if len(compatible) == 1:
        adapter.production_worker_id = compatible[0].id
        return compatible[0]
    if len(compatible) > 1:
        raise domain_error(
            409,
            "production_worker_required",
            "Multiple online Workers exist; configure a production Worker first",
        )
    if not online:
        raise domain_error(409, "worker_offline", "No online Worker is available")
    raise domain_error(
        409,
        "worker_capability_missing",
        f"No online Worker supports {adapter.language}",
    )


def start_production(session: Session, adapter_id: int) -> Adapter:
    """Open the production entry and lock the production version.

    M5.1: Start no longer creates an Execution. It sets production_state to
    running, locks production_version_id to the current published_version_id
    and keeps the production Worker. All preconditions (409) are preserved:
    not archived, a version is published, the production entry was not
    already running, no active production Execution exists, the production
    Worker resolves and is online, the Published Version most recently tested
    successfully on that current Worker.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(adapter)
    if adapter.published_version_id is None:
        raise domain_error(
            409, "adapter_not_published", "Publish a version before starting production"
        )
    if (
        adapter.production_state == "running"
        or _active_production_execution(session, adapter_id) is not None
    ):
        raise domain_error(
            409,
            "production_already_running",
            "Stop production before starting it again",
        )
    # Validate the production Worker (online + compatible) and auto-adopt
    # when exactly one compatible Worker is online; the return value is
    # unused because Start no longer creates an Execution.
    _resolve_production_worker(
        session,
        adapter,
        now=worker_availability.current_time(session),
    )
    gate = _evaluate_publish_gate(session, adapter, adapter.published_version_id)
    if not gate.allowed:
        raise domain_error(
            409,
            "production_test_required",
            "Run a successful test of the published version on the production Worker before "
            "starting",
        )
    adapter.production_state = "running"
    adapter.production_version_id = adapter.published_version_id
    session.commit()
    session.refresh(adapter)
    return adapter


def stop_production(session: Session, adapter_id: int, mode: str) -> Adapter:
    """Close the production entry.

    ``wait`` only flips the state (an active Execution still runs to
    completion); ``terminate`` additionally cancels it: pending becomes
    cancelled immediately, running gets the cancel flag the owning Worker
    picks up on its next progress round trip.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter.production_state = "stopped"
    # M5.1: Stop clears the locked production version.
    adapter.production_version_id = None
    if mode == "terminate":
        # Serialize with Worker claim: after waiting for the same Execution
        # row lock, the status is re-read as either pending (cancel now) or
        # running (request the owning Worker to terminate it).
        active = lock_active_production_execution(session, adapter_id)
        if active is not None:
            request_cancellation(active)
    session.commit()
    session.refresh(adapter)
    return adapter


def unpublish_adapter(session: Session, adapter_id: int) -> Adapter:
    """Clear the published pointer; requires production to be stopped.

    Idempotent when nothing is published. A still-active Production
    Execution blocks Unpublish (409), matching the "Stop first" rule.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.published_version_id is None:
        session.refresh(adapter)
        return adapter
    if (
        adapter.production_state == "running"
        or _active_production_execution(session, adapter_id) is not None
    ):
        raise domain_error(409, "production_running", "Stop production before unpublishing")
    adapter.published_version_id = None
    session.commit()
    session.refresh(adapter)
    return adapter


def archive_adapter(session: Session, adapter_id: int) -> Adapter:
    """Archive the Adapter (read-only afterwards); requires production stopped.

    Idempotent for already archived Adapters.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        session.refresh(adapter)
        return adapter
    if (
        adapter.production_state == "running"
        or _active_production_execution(session, adapter_id) is not None
    ):
        raise domain_error(409, "production_running", "Stop production before archiving")
    adapter.archived_at = func.now()
    session.commit()
    session.refresh(adapter)
    return adapter


def restore_adapter(session: Session, adapter_id: int) -> Adapter:
    """Restore an archived Adapter; idempotent for non-archived ones."""
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter.archived_at = None
    session.commit()
    session.refresh(adapter)
    return adapter


def clone_adapter(session: Session, adapter_id: int, data: CloneRequest) -> Adapter:
    """Copy an Adapter: working copy becomes v1, bindings are referenced.

    The clone is unpublished, not running, without a production Worker.
    language/code/requirements/runtime_config come from the source's latest
    version; credential binding rows are copied by reference.
    """
    source = get_adapter(session, adapter_id)
    existing = session.scalar(select(Adapter).where(Adapter.name == data.name))
    if existing is not None:
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    clone = Adapter(
        name=data.name,
        description=data.description if data.description is not None else source.description,
        language=source.language,
    )
    session.add(clone)
    try:
        session.flush()  # assign clone.id before child rows
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    if source.latest_version_id is not None:
        version = session.get(AdapterVersion, source.latest_version_id)
        if version is not None:
            first = AdapterVersion(
                adapter_id=clone.id,
                seq=1,
                code=version.code,
                requirements=version.requirements,
                runtime_config=version.runtime_config,
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
    session.commit()
    session.refresh(clone)
    return clone
