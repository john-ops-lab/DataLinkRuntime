"""Immutable runtime policy carried by a subscription Activation, never secrets.

Old activations are frozen once during upgrade, before reception/scheduling.
An existing Execution is the best recorded historical policy. If an old
activation has never created an Execution, its pre-upgrade deployment policy
is unknowable; record an explicit legacy-upgrade capture instead of inventing
history or reading changing settings on every T2/replay.
"""

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    EventDelivery,
    EventReceipt,
    Execution,
    SubscriptionActivation,
)
from dlr.control.schemas.reliable_runtime import ResourceProfile
from dlr.control.services.adapter import domain_error

_POLICY_KEYS = {
    "schema_version",
    "retry_policy",
    "resource_profile",
    "captured_from",
    "execution_id",
}
_MAX_POLICY_BYTES = 8192


def validate_policy(value: Any) -> dict[str, Any]:
    from dlr.control.services.reliable_execution import (
        RESOURCE_PROFILE_CLASS,
        RESOURCE_PROFILE_SCHEMA_VERSION,
        validate_retry_policy,
    )

    try:
        if (
            not isinstance(value, dict)
            or set(value) != _POLICY_KEYS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["captured_from"] not in {"activation", "legacy_execution", "legacy_upgrade"}
            or not isinstance(value["retry_policy"], dict)
            or not isinstance(value["resource_profile"], dict)
        ):
            raise ValueError("Invalid activation policy")
        execution_id = value["execution_id"]
        if value["captured_from"] == "legacy_execution":
            if type(execution_id) is not int or execution_id < 1:
                raise ValueError("Historical execution required")
        elif execution_id is not None:
            raise ValueError("Unexpected historical execution")
        retry = validate_retry_policy(value["retry_policy"])
        profile = ResourceProfile.model_validate(value["resource_profile"])
        if (
            profile.schema_version != RESOURCE_PROFILE_SCHEMA_VERSION
            or profile.resource_class != RESOURCE_PROFILE_CLASS
            or profile.execution_timeout_seconds > 86400
            or not 30 <= profile.claim_timeout_seconds <= 86400
            or not 10 <= profile.recovery_grace_seconds <= 3600
            or not 1 <= profile.workspace_cleanup_attempt_timeout_seconds <= 60
            or not 5 <= profile.workspace_cleanup_total_timeout_seconds <= 300
        ):
            raise ValueError("Unsupported activation runtime policy")
        result = {
            "schema_version": 1,
            "retry_policy": retry,
            "resource_profile": profile.model_dump(mode="json"),
            "captured_from": value["captured_from"],
            "execution_id": execution_id,
        }
        if (
            len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
            > _MAX_POLICY_BYTES
        ):
            raise ValueError("Activation runtime policy is too large")
        return result
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise domain_error(
            409, "subscription_activation_invalid", "Frozen activation runtime policy is invalid"
        ) from None


def snapshot_policy(adapter: Adapter, *, legacy: bool = False) -> dict[str, Any]:
    from dlr.control.services.reliable_execution import (
        default_resource_profile,
        default_retry_policy,
    )

    return validate_policy(
        {
            "schema_version": 1,
            "retry_policy": default_retry_policy(),
            "resource_profile": default_resource_profile(adapter.timeout_seconds),
            "captured_from": "legacy_upgrade" if legacy else "activation",
            "execution_id": None,
        }
    )


def ensure_policy(
    session: Session, activation: SubscriptionActivation, adapter: Adapter
) -> dict[str, Any]:
    if activation.execution_policy_snapshot is not None:
        return validate_policy(activation.execution_policy_snapshot)
    previous = session.scalar(
        select(Execution)
        .join(
            EventDelivery,
            func.coalesce(EventDelivery.execution_id, EventDelivery.batch_execution_id)
            == Execution.id,
        )
        .join(EventReceipt, EventDelivery.receipt_id == EventReceipt.id)
        .where(
            or_(
                EventDelivery.activation_override_id == activation.id,
                and_(
                    EventDelivery.activation_override_id.is_(None),
                    EventReceipt.activation_id == activation.id,
                ),
            )
        )
        .order_by(Execution.id)
        .limit(1)
    )
    if previous is not None:
        policy = validate_policy(
            {
                "schema_version": 1,
                "retry_policy": previous.retry_policy_snapshot,
                "resource_profile": previous.resource_profile_snapshot,
                "captured_from": "legacy_execution",
                "execution_id": previous.id,
            }
        )
    else:
        policy = snapshot_policy(adapter, legacy=True)
    activation.execution_policy_snapshot = policy
    session.flush()
    return policy


def freeze_legacy_policies(session: Session, *, limit: int = 100) -> int:
    """Run bounded transactions repeatedly at startup, before listeners or T2.

    Lock Adapter first, consistent with T1/T2 and configuration mutation. The
    null check is repeated under lock so parallel startup cannot overwrite a
    policy that another process has already fixed.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Invalid activation migration batch size")
    candidates = session.execute(
        select(SubscriptionActivation.id, SubscriptionActivation.subscription_id)
        .where(SubscriptionActivation.execution_policy_snapshot.is_(None))
        .order_by(SubscriptionActivation.id)
        .limit(limit)
    ).all()
    from dlr.control.models import EventSubscription

    for activation_id, subscription_id in candidates:
        adapter_id = session.scalar(
            select(EventSubscription.adapter_id).where(EventSubscription.id == subscription_id)
        )
        adapter = session.get(Adapter, adapter_id, with_for_update=True)
        activation = session.get(
            SubscriptionActivation, activation_id, with_for_update=True, populate_existing=True
        )
        assert adapter is not None and activation is not None
        ensure_policy(session, activation, adapter)
    return len(candidates)


def legacy_policy_gate(session: Session) -> bool:
    """A bounded startup/restart gate; retained Execution settlement stays available."""
    return freeze_legacy_policies(session, limit=100) == 0
