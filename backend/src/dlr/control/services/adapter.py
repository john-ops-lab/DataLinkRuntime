"""Adapter and immutable Revision domain service (M5.4.1)."""

import secrets as stdlib_secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterSchedule, AdapterVersion, AdapterWebhook, Worker
from dlr.control.models.platform import AdapterCredentialBinding
from dlr.control.schemas.adapter import (
    AdapterCreate,
    AdapterResponse,
    AdapterUpdate,
    CloneRequest,
    VersionCreate,
)
from dlr.control.services import adapter_runtime, worker_availability


def domain_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the stable domain error format (detail object with a code)."""
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
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")


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


def create_adapter(session: Session, data: AdapterCreate) -> Adapter:
    existing = session.scalar(select(Adapter).where(Adapter.name == data.name))
    if existing is not None:
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    adapter = Adapter(
        name=data.name,
        description=data.description,
        language=data.language,
        adapter_type=data.adapter_type,
    )
    session.add(adapter)
    try:
        session.flush()
        if adapter.adapter_type == "webhook":
            session.add(
                AdapterWebhook(
                    adapter_id=adapter.id,
                    public_id=stdlib_secrets.token_hex(8),
                    enabled=False,
                    credential_id=None,
                )
            )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def _require_worker_capability(worker: Worker, language: str) -> None:
    if language not in worker.capabilities:
        raise domain_error(
            409,
            "worker_capability_missing",
            f"Worker does not support {language}",
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

    if data.name is not None and data.name != adapter.name:
        conflict = session.scalar(
            select(Adapter).where(Adapter.name == data.name, Adapter.id != adapter_id)
        )
        if conflict is not None:
            raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
        adapter.name = data.name
    if data.description is not None:
        adapter.description = data.description

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def delete_adapter(session: Session, adapter_id: int) -> None:
    """Soft-delete an unlocked Adapter while retaining Revision/Execution facts."""
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        return
    adapter_runtime.require_runtime_unlocked(session, adapter)
    adapter.archived_at = func.now()
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
    if adapter.latest_version_id is None:
        if adapter.adapter_type == "webhook":
            webhook = session.scalar(
                select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter.id)
            )
            if webhook is None or webhook.credential_id is None:
                raise domain_error(
                    409,
                    "webhook_token_required",
                    "Choose a Token Credential before the first Webhook Revision is saved",
                )
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


def clone_adapter(session: Session, adapter_id: int, data: CloneRequest) -> Adapter:
    """Clone common M5.4 facts; the clone has its own Revision 1 and no runs."""
    # Freeze the source configuration while the copy is assembled. Runtime
    # writes use the same Adapter-first lock order, so a clone cannot combine
    # Worker/mode/Schedule/binding facts from different moments.
    source = session.get(Adapter, adapter_id, with_for_update=True)
    if source is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _require_not_archived(source)
    existing = session.scalar(select(Adapter).where(Adapter.name == data.name))
    if existing is not None:
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    clone = Adapter(
        name=data.name,
        description=data.description if data.description is not None else source.description,
        language=source.language,
        adapter_type=source.adapter_type,
        run_mode=source.run_mode,
        runtime_worker_id=source.runtime_worker_id,
    )
    session.add(clone)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None

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
        source_schedule = session.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter_id)
            .with_for_update()
        )
        if source_schedule is not None:
            session.add(
                AdapterSchedule(
                    adapter_id=clone.id,
                    cron=source_schedule.cron,
                    timezone=source_schedule.timezone,
                    input=source_schedule.input,
                    enabled=False,
                    next_run_at=None,
                )
            )
    else:
        source_webhook = session.scalar(
            select(AdapterWebhook)
            .where(AdapterWebhook.adapter_id == adapter_id)
            .with_for_update()
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
