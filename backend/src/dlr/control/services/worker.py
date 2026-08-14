"""Domain service for Worker registration, heartbeat and task claiming.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so concurrent Workers can never
claim the same Execution, while different Executions are claimed in
parallel. Long polling simply retries the atomic claim until the deadline.
"""

import time

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterVersion, Execution, Worker
from dlr.control.schemas.worker import TaskPayload, WorkerRegister
from dlr.control.services import package_source as package_source_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services.adapter import domain_error

MAX_CLAIM_WAIT_SECONDS = 30
CLAIM_POLL_INTERVAL_SECONDS = 1.0


def get_worker(session: Session, worker_id: int) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    return worker


def list_workers(session: Session) -> list[Worker]:
    """All registered Workers with stored state, oldest registration first."""
    return list(session.scalars(select(Worker).order_by(Worker.id.asc())).all())


def register_worker(session: Session, data: WorkerRegister) -> Worker:
    """Upsert by name: restarts reuse the existing row."""
    worker = session.scalar(select(Worker).where(Worker.name == data.name).with_for_update())
    if worker is None:
        worker = Worker(
            name=data.name,
            status="online",
            last_heartbeat=func.now(),
            capabilities=data.capabilities,
        )
        session.add(worker)
    else:
        worker.status = "online"
        worker.last_heartbeat = func.now()
        worker.capabilities = data.capabilities
    session.commit()
    session.refresh(worker)
    return worker


def heartbeat(session: Session, worker_id: int) -> None:
    worker = get_worker(session, worker_id)
    worker.status = "online"
    worker.last_heartbeat = func.now()
    session.commit()


def mark_offline(session: Session, worker_id: int) -> None:
    worker = get_worker(session, worker_id)
    worker.status = "offline"
    session.commit()


def build_task_payload(session: Session, execution: Execution) -> TaskPayload:
    """Assemble the task payload from the immutable version snapshot.

    Bound credential fields are decrypted here at claim time and travel
    inside the payload as ``secrets`` — an Execution only ever receives the
    secrets its own Adapter bound. The language-specific platform default
    dependency-source URL travels as ``index_url``.
    """
    version = session.get(AdapterVersion, execution.version_id)
    adapter = session.get(Adapter, execution.adapter_id)
    # Guaranteed by the restrict-delete foreign keys; a missing row here is
    # an invariant violation, not a user error.
    if version is None or adapter is None:
        raise RuntimeError("execution references a missing adapter version")
    return TaskPayload(
        execution_id=execution.id,
        adapter_id=execution.adapter_id,
        version_id=execution.version_id,
        language=adapter.language,
        code=version.code,
        requirements=version.requirements,
        runtime_config=version.runtime_config,
        input=execution.input,
        latest_version_id=adapter.latest_version_id,
        execution_timeout_seconds=settings.execution_timeout_seconds,
        secrets=secrets_service.resolve_adapter_secrets(session, execution.adapter_id),
        index_url=package_source_service.resolve_default_index_url(
            session,
            {"python": "pypi", "javascript": "npm", "java": "maven"}[adapter.language],
        ),
    )


def try_claim(session: Session, worker_id: int) -> TaskPayload | None:
    """One atomic claim attempt; None when no pending Execution is free.

    M3.2 scheduling rules: Executions flagged for cancellation are never
    claimed, and an Execution with a scheduling target may only be claimed
    by that Worker (a NULL target stays claimable by any Worker, which keeps
    historical rows working).
    """
    worker = get_worker(session, worker_id)
    execution = session.scalar(
        select(Execution)
        .join(Adapter, Adapter.id == Execution.adapter_id)
        .where(
            Execution.status == "pending",
            Execution.cancel_requested.is_(False),
            Adapter.language.in_(worker.capabilities),
            or_(
                Execution.target_worker_id.is_(None),
                Execution.target_worker_id == worker_id,
            ),
        )
        .order_by(Execution.created_at.asc(), Execution.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if execution is None:
        # Release any snapshot state before the next poll iteration.
        session.rollback()
        return None
    execution.status = "running"
    execution.worker_id = worker_id
    execution.started_at = func.now()
    session.commit()
    session.refresh(execution)
    return build_task_payload(session, execution)


def claim_task(session: Session, worker_id: int, wait_seconds: int) -> TaskPayload | None:
    """Long-poll: retry the atomic claim until a task or the deadline."""
    get_worker(session, worker_id)
    wait_seconds = max(0, min(wait_seconds, MAX_CLAIM_WAIT_SECONDS))
    deadline = time.monotonic() + wait_seconds
    while True:
        payload = try_claim(session, worker_id)
        if payload is not None:
            return payload
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(CLAIM_POLL_INTERVAL_SECONDS, remaining))
