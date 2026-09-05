"""Authoritative Adapter runtime lock predicate (M5.4.1)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterSchedule, AdapterWebhook, Execution

ACTIVE_EXECUTION_STATUSES = ("running",)


@dataclass(frozen=True)
class AdapterRuntimeState:
    """One authoritative runtime-lock snapshot for API serialization."""

    locked: bool
    active_execution: Execution | None


def active_execution(session: Session, adapter_id: int) -> Execution | None:
    """Return the Adapter's single active Execution, if any."""
    return session.scalar(
        select(Execution)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.status.in_(ACTIVE_EXECUTION_STATUSES),
        )
        .limit(1)
    )


def runtime_state(session: Session, adapter: Adapter) -> AdapterRuntimeState:
    """Load one Adapter's runtime state without repeating the active query."""
    active = active_execution(session, adapter.id)
    if active is not None:
        return AdapterRuntimeState(locked=True, active_execution=active)
    if adapter.adapter_type == "task":
        enabled = session.scalar(
            select(AdapterSchedule.enabled).where(AdapterSchedule.adapter_id == adapter.id)
        )
    else:
        enabled = session.scalar(
            select(AdapterWebhook.enabled).where(AdapterWebhook.adapter_id == adapter.id)
        )
    return AdapterRuntimeState(locked=enabled is True, active_execution=None)


def runtime_states(session: Session, adapters: list[Adapter]) -> dict[int, AdapterRuntimeState]:
    """Load runtime states for a Catalog response in three bounded queries."""
    if not adapters:
        return {}
    adapter_ids = [adapter.id for adapter in adapters]
    active_by_adapter = {
        execution.adapter_id: execution
        for execution in session.scalars(
            select(Execution).where(
                Execution.adapter_id.in_(adapter_ids),
                Execution.status.in_(ACTIVE_EXECUTION_STATUSES),
            )
        ).all()
    }
    enabled_task_ids = set(
        session.scalars(
            select(AdapterSchedule.adapter_id).where(
                AdapterSchedule.adapter_id.in_(adapter_ids),
                AdapterSchedule.enabled.is_(True),
            )
        ).all()
    )
    enabled_webhook_ids = set(
        session.scalars(
            select(AdapterWebhook.adapter_id).where(
                AdapterWebhook.adapter_id.in_(adapter_ids),
                AdapterWebhook.enabled.is_(True),
            )
        ).all()
    )
    states: dict[int, AdapterRuntimeState] = {}
    for adapter in adapters:
        active = active_by_adapter.get(adapter.id)
        trigger_enabled = (
            adapter.id in enabled_task_ids
            if adapter.adapter_type == "task"
            else adapter.id in enabled_webhook_ids
        )
        states[adapter.id] = AdapterRuntimeState(
            locked=active is not None or trigger_enabled,
            active_execution=active,
        )
    return states


def adapter_runtime_locked(session: Session, adapter: Adapter) -> bool:
    """Whether execution-affecting Adapter configuration is immutable now."""
    return runtime_state(session, adapter).locked


def require_runtime_unlocked(session: Session, adapter: Adapter) -> None:
    """Reject runtime-affecting writes with one stable, actionable 409."""
    if adapter_runtime_locked(session, adapter):
        # Local import avoids an adapter <-> adapter_runtime import cycle.
        from dlr.control.services.adapter import domain_error

        raise domain_error(
            409,
            "adapter_runtime_locked",
            "Stop the Adapter and wait for its active Execution to finish before changing "
            "runtime configuration",
        )
