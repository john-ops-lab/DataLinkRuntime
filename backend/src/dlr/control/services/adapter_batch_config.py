"""Configuration-only fingerprints, loaded in a fixed number of catalog queries."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterCredentialBinding,
    AdapterInputConfig,
    AdapterSchedule,
    AdapterWebhook,
    EventSubscription,
)


@dataclass(frozen=True)
class BatchConfiguration:
    token: str
    mode: str
    schedule: AdapterSchedule | None
    webhook: AdapterWebhook | None
    subscription: EventSubscription | None


def configurations(session: Session, adapters: list[Adapter]) -> dict[int, BatchConfiguration]:
    """Do not hash mutable cursors, names, health, or secret values.

    All configuration writers serialize on Adapter. Mutating callers must
    hold that lock; catalog reads intentionally take an optimistic snapshot.
    """
    ids = [adapter.id for adapter in adapters]
    if not ids:
        return {}
    inputs = {
        row.adapter_id: row.revision
        for row in session.execute(
            select(AdapterInputConfig.adapter_id, AdapterInputConfig.revision).where(
                AdapterInputConfig.adapter_id.in_(ids)
            )
        )
    }
    schedules = {
        row.adapter_id: row
        for row in session.scalars(
            select(AdapterSchedule)
            .where(AdapterSchedule.adapter_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    webhooks = {
        row.adapter_id: row
        for row in session.scalars(
            select(AdapterWebhook)
            .where(AdapterWebhook.adapter_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    subscriptions = {
        row.adapter_id: row
        for row in session.scalars(
            select(EventSubscription)
            .where(EventSubscription.adapter_id.in_(ids), EventSubscription.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
    }
    bindings: dict[int, list[list[Any]]] = {}
    for row in session.execute(
        select(
            AdapterCredentialBinding.adapter_id,
            AdapterCredentialBinding.id,
            AdapterCredentialBinding.env_key,
            AdapterCredentialBinding.credential_id,
            AdapterCredentialBinding.field,
        )
        .where(AdapterCredentialBinding.adapter_id.in_(ids))
        .order_by(AdapterCredentialBinding.id)
    ):
        bindings.setdefault(row.adapter_id, []).append(list(row)[1:])
    result: dict[int, BatchConfiguration] = {}
    for adapter in adapters:
        schedule = schedules.get(adapter.id)
        webhook = webhooks.get(adapter.id)
        subscription = subscriptions.get(adapter.id)
        mode = (
            "subscription"
            if subscription is not None
            else "webhook"
            if adapter.adapter_type == "webhook"
            else adapter.run_mode
        )
        facts = {
            "schema": 1,
            "id": adapter.id,
            "type": adapter.adapter_type,
            "mode": adapter.run_mode,
            "version": adapter.latest_version_id,
            "worker": adapter.runtime_worker_id,
            "timeout": adapter.timeout_seconds,
            "input_revision": inputs.get(adapter.id),
            "bindings": bindings.get(adapter.id, []),
            "schedule": None
            if schedule is None
            else [
                schedule.id,
                schedule.cron,
                schedule.timezone,
                schedule.misfire_policy,
                schedule.max_catchup_count,
                schedule.max_catchup_age_seconds,
            ],
            "webhook": None
            if webhook is None
            else [
                webhook.id,
                webhook.public_id,
                webhook.credential_id,
                webhook.response_mode,
                webhook.response_timeout_seconds,
            ],
            "subscription": None
            if subscription is None
            else [
                subscription.id,
                subscription.source_type,
                subscription.config_version,
                subscription.source_epoch,
            ],
        }
        token = hashlib.sha256(
            json.dumps(facts, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        result[adapter.id] = BatchConfiguration(token, mode, schedule, webhook, subscription)
    return result
