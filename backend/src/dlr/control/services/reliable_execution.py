"""Batch 1 reliable ingress preparation.

This module creates the PostgreSQL Execution, Admission and Outbox facts in
one transaction when the explicit RabbitMQ ingress gate is enabled.  It does
not consume RabbitMQ messages or create Attempts; those are intentionally
left to the later v3 Worker batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.control.models import Adapter, AdapterCredentialBinding, AdapterVersion, Execution, Worker
from dlr.control.models.subscription import SubscriptionActivation
from dlr.control.schemas.worker_pool import FixedRoute, PoolRoute, RouteSource
from dlr.control.services import admission, idempotency, outbox, worker_pool
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import (
    _create_pending_execution_locked,
)

RETRY_POLICY_KEYS = frozenset(
    {
        "max_attempts",
        "initial_backoff_seconds",
        "multiplier",
        "max_backoff_seconds",
        "jitter_ratio",
        "retryable_error_classes",
    }
)
RETRYABLE_ERROR_CLASSES = ("platform_transient", "worker_lost")
RETRYABLE_ERROR_CLASS_SET = frozenset(RETRYABLE_ERROR_CLASSES)
DEFAULT_RETRY_POLICY: dict[str, object] = {
    "max_attempts": 3,
    "initial_backoff_seconds": 5.0,
    "multiplier": 2.0,
    "max_backoff_seconds": 300.0,
    "jitter_ratio": 0.2,
    "retryable_error_classes": ["platform_transient", "worker_lost"],
}
RESOURCE_PROFILE_CLASS = "standard"
RESOURCE_PROFILE_SCHEMA_VERSION = 1


def validate_retry_policy(policy: Mapping[str, object]) -> dict[str, object]:
    """Validate and return the closed Retry Policy snapshot shape.

    Batch 1 only freezes this data.  Attempt transitions and retry dispatch
    remain disabled until their later Batch.
    """
    if set(policy) != RETRY_POLICY_KEYS:
        raise ValueError("retry policy must contain the closed required fields")
    max_attempts = policy["max_attempts"]
    initial_backoff = policy["initial_backoff_seconds"]
    multiplier = policy["multiplier"]
    max_backoff = policy["max_backoff_seconds"]
    jitter_ratio = policy["jitter_ratio"]
    classes = policy["retryable_error_classes"]
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 100
    ):
        raise ValueError("retry policy max_attempts must be between 1 and 100")
    numeric = (initial_backoff, multiplier, max_backoff, jitter_ratio)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
        raise ValueError("retry policy numeric fields must be finite numbers")
    initial = float(cast(int | float, initial_backoff))
    factor = float(cast(int | float, multiplier))
    maximum = float(cast(int | float, max_backoff))
    jitter = float(cast(int | float, jitter_ratio))
    if not 0 < initial <= 300:
        raise ValueError("retry policy initial_backoff_seconds must be between 0 and 300")
    if not 1 <= factor <= 10:
        raise ValueError("retry policy multiplier must be between 1 and 10")
    if not 0 < maximum <= 3_600:
        raise ValueError("retry policy max_backoff_seconds must be between 0 and 3600")
    if initial > maximum:
        raise ValueError("retry policy initial backoff must not exceed max backoff")
    if not 0 <= jitter <= 0.2:
        raise ValueError("retry policy jitter_ratio must be between 0 and 0.2")
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(item, str) for item in classes)
        or len(set(classes)) != len(classes)
        or not set(classes).issubset(RETRYABLE_ERROR_CLASS_SET)
    ):
        raise ValueError("retry policy error classes are not conservative and closed")
    return {
        "max_attempts": max_attempts,
        "initial_backoff_seconds": initial,
        "multiplier": factor,
        "max_backoff_seconds": maximum,
        "jitter_ratio": jitter,
        "retryable_error_classes": list(classes),
    }


def default_retry_policy() -> dict[str, object]:
    """Build one immutable policy from the currently validated deployment config."""
    return validate_retry_policy(
        {
            "max_attempts": settings.execution_retry_max_attempts,
            "initial_backoff_seconds": settings.execution_retry_initial_backoff_seconds,
            "multiplier": settings.execution_retry_multiplier,
            "max_backoff_seconds": settings.execution_retry_max_backoff_seconds,
            "jitter_ratio": settings.execution_retry_jitter_ratio,
            "retryable_error_classes": list(RETRYABLE_ERROR_CLASSES),
        }
    )


def default_resource_profile(adapter_timeout_seconds: int | None = None) -> dict[str, object]:
    """Return the closed, immutable ``standard`` profile for one Execution."""

    timeout_seconds = (
        settings.execution_timeout_seconds
        if adapter_timeout_seconds is None
        else adapter_timeout_seconds
    )
    return {
        "schema_version": RESOURCE_PROFILE_SCHEMA_VERSION,
        "resource_class": RESOURCE_PROFILE_CLASS,
        "backend": settings.sandbox_backend,
        "cpu_cores": settings.sandbox_cpu_cores,
        "memory_bytes": settings.sandbox_memory_bytes,
        "pids": settings.sandbox_pids,
        "tmp_bytes": settings.sandbox_tmp_bytes,
        "nofile": settings.sandbox_nofile,
        "execution_timeout_seconds": timeout_seconds,
        "claim_timeout_seconds": settings.execution_claim_timeout_seconds,
        "recovery_grace_seconds": settings.execution_recovery_grace_seconds,
        "workspace_cleanup_attempt_timeout_seconds": (
            settings.workspace_cleanup_attempt_timeout_seconds
        ),
        "workspace_cleanup_total_timeout_seconds": settings.workspace_cleanup_total_timeout_seconds,
        "stream_max_bytes": settings.execution_stream_max_bytes,
        "output_max_bytes": settings.execution_output_max_bytes,
        "output_preview_max_bytes": settings.execution_output_preview_max_bytes,
    }


def resolve_queue_target_worker(
    session: Session, adapter: Adapter, worker_id_override: int | None = None
) -> Worker:
    """Validate a fixed target without requiring it to be effectively online."""

    if worker_id_override is None:
        worker_pool.require_fixed_route(worker_pool.adapter_route(adapter))
    target_worker_id = (
        worker_id_override if worker_id_override is not None else adapter.runtime_worker_id
    )
    if target_worker_id is None:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "A fixed runtime Worker is required for reliable dispatch",
        )
    worker = session.get(Worker, target_worker_id)
    if worker is None:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not exist",
        )
    from dlr.control.services import worker_legacy_window

    compatible_legacy = worker.protocol_version == 3 and (
        not settings.worker_remote_v4_enabled
        or worker_legacy_window.target_allowed(session, worker)
    )
    if adapter.language not in worker.capabilities or (
        settings.worker_remote_v4_enabled
        and not compatible_legacy
        and not valid_capabilities(worker.capabilities)
    ):
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not support the Adapter language",
            {"language": adapter.language},
        )
    compatible_remote = (
        worker.protocol_version == 4
        and settings.worker_remote_v4_enabled
        and worker.identity_lifecycle == "reserved"
        and worker.identity_enrolled_at is not None
    )
    if not (compatible_legacy or compatible_remote):
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not support RabbitMQ dispatch",
        )
    return worker


def configured_dispatch_route(
    session: Session, adapter: Adapter, *, use_worker_reservations: bool
) -> FixedRoute | PoolRoute:
    if not use_worker_reservations:
        fixed_route = worker_pool.fixed_route(resolve_queue_target_worker(session, adapter).id)
        assert fixed_route is not None
        return fixed_route
    route = worker_pool.adapter_route(adapter)
    if isinstance(route, PoolRoute):
        return worker_pool.validate_pool_route(session, route)
    if isinstance(route, FixedRoute):
        worker = session.get(Worker, route.worker_id)
        if (
            worker is not None
            and worker.protocol_version == 4
            and worker.identity_lifecycle == "reserved"
            and worker.identity_enrolled_at is not None
        ):
            return route
    raise domain_error(409, "runtime_worker_invalid", "Runtime route is unavailable")


def _db_now(session: Session) -> datetime:
    from dlr.control.services.input_config import database_now

    return database_now(session)


def _credential_bindings_snapshot(session: Session, adapter_id: int) -> list[dict[str, object]]:
    """Freeze binding references while the owning Adapter row is locked."""
    bindings = session.scalars(
        select(AdapterCredentialBinding)
        .where(AdapterCredentialBinding.adapter_id == adapter_id)
        .order_by(AdapterCredentialBinding.env_key, AdapterCredentialBinding.id)
        .with_for_update()
    ).all()
    return [
        {
            "binding_id": binding.id,
            "credential_id": binding.credential_id,
            "env_key": binding.env_key,
            "field": binding.field,
        }
        for binding in bindings
    ]


def _subscription_credential_snapshot(
    session: Session,
    adapter: Adapter,
    activation_id: int,
    snapshots: Sequence[dict[str, object]],
    *,
    version_id: int | None,
    worker_id: int | None,
) -> list[dict[str, object]]:
    """Validate frozen references without consulting today's Adapter binding set."""
    from dlr.control.models.platform import CREDENTIAL_FIELDS, Credential
    from dlr.control.models.subscription import (
        ActivationCredentialReference,
        EventSubscription,
        SubscriptionActivation,
    )
    from dlr.control.services.secrets import ENV_KEY_PATTERN

    def invalid() -> NoReturn:
        raise domain_error(
            409, "subscription_activation_invalid", "Subscription activation references are invalid"
        )

    activation = session.get(SubscriptionActivation, activation_id)
    subscription = (
        session.get(EventSubscription, activation.subscription_id) if activation else None
    )
    if (
        activation is None
        or subscription is None
        or subscription.adapter_id != adapter.id
        or activation.worker_id != worker_id
        or version_id is None
        or session.scalar(
            select(AdapterVersion.id).where(
                AdapterVersion.id == version_id, AdapterVersion.adapter_id == adapter.id
            )
        )
        is None
    ):
        invalid()
    frozen = list(
        session.scalars(
            select(ActivationCredentialReference)
            .where(ActivationCredentialReference.activation_id == activation_id)
            .order_by(ActivationCredentialReference.env_key)
        )
    )
    validated: list[dict[str, object]] = []
    env_keys: set[str] = set()
    binding_ids: set[int] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "binding_id",
            "credential_id",
            "env_key",
            "field",
        }:
            invalid()
        binding_id, credential_id = snapshot["binding_id"], snapshot["credential_id"]
        env_key, field = snapshot["env_key"], snapshot["field"]
        if (
            isinstance(binding_id, bool)
            or not isinstance(binding_id, int)
            or binding_id <= 0
            or isinstance(credential_id, bool)
            or not isinstance(credential_id, int)
            or credential_id <= 0
            or not isinstance(env_key, str)
            or not ENV_KEY_PATTERN.fullmatch(env_key)
            or not isinstance(field, str)
            or env_key in env_keys
            or binding_id in binding_ids
        ):
            invalid()
        # Narrow types explicitly after the closed-shape check above.
        assert isinstance(credential_id, int) and isinstance(env_key, str)
        assert isinstance(binding_id, int) and isinstance(field, str)
        credential = session.get(Credential, credential_id)
        if credential is None or field not in CREDENTIAL_FIELDS[credential.type]:
            invalid()
        env_keys.add(env_key)
        binding_ids.add(binding_id)
        validated.append(dict(snapshot))
    expected = [
        {
            "binding_id": row.binding_id,
            "credential_id": row.credential_id,
            "env_key": row.env_key,
            "field": row.field,
        }
        for row in frozen
    ]
    if sorted(validated, key=lambda row: str(row["env_key"])) != expected:
        invalid()
    return sorted(
        validated, key=lambda row: (str(row["env_key"]), int(cast(int, row["binding_id"])))
    )


def accept_execution(
    session: Session,
    adapter: Adapter,
    *,
    trigger: str,
    runtime_input: object,
    input_source_type: str,
    input_config_revision: int,
    input_snapshot: dict[str, Any],
    artifact_ids: tuple[int, ...] = (),
    scheduled_for: datetime | None = None,
    idempotency_key: str | None = None,
    idempotency_body: Any = None,
    idempotency_lookup: idempotency.IdempotencyLookup | None = None,
    schedule_policy_snapshot: dict[str, object] | None = None,
    version_id: int | None = None,
    dependency_check: bool = False,
    worker_id_override: int | None = None,
    credential_bindings_override: Sequence[dict[str, object]] | None = None,
    subscription_activation_id: int | None = None,
    subscription_delivery_kind: str | None = None,
    route_source: RouteSource | None = None,
    use_worker_reservations: bool = False,
) -> Execution:
    """Atomically accept one gated RabbitMQ Execution or its idempotent hit.

    The reservation path is an internal staging seam until the W1 cutover.
    Production HTTP and background callers retain the default.
    """

    from dlr.control.services import rabbitmq, worker_placement_cutover, worker_placement_wait

    if trigger != "subscription" and (
        credential_bindings_override is not None or subscription_activation_id is not None
    ):
        raise domain_error(
            409,
            "subscription_activation_invalid",
            "Frozen activation overrides require a subscription",
        )
    if trigger == "subscription" and (
        subscription_activation_id is None
        or credential_bindings_override is None
        or version_id is None
        or dependency_check
        or input_source_type != "json"
        or input_config_revision != 1
        or input_snapshot != {"source_type": "json", "revision": None}
        or artifact_ids
        or scheduled_for is not None
        or schedule_policy_snapshot is not None
    ):
        raise domain_error(
            409,
            "subscription_activation_invalid",
            "Subscription acceptance requires its frozen activation",
        )
    locked_adapter = session.get(Adapter, adapter.id, with_for_update=True, populate_existing=True)
    if locked_adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter = locked_adapter
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    if adapter.latest_version_id is None:
        raise domain_error(409, "adapter_has_no_version", "Adapter has no saved Revision yet")

    lookup = idempotency_lookup or idempotency.lookup(
        session, adapter.id, trigger, idempotency_body, idempotency_key
    )
    if lookup.record is not None:
        existing = session.get(Execution, lookup.record.execution_id)
        if existing is None:
            # The FK/retention contract should make this impossible.  Refuse
            # to create a second responsibility instead of hiding corruption.
            raise domain_error(
                409, "idempotency_record_invalid", "Idempotency record is unavailable"
            )
        return existing

    reserved_mode = worker_placement_cutover.accepting_mode(session) == "reserved"
    use_worker_reservations = use_worker_reservations or reserved_mode
    activation = None
    source: RouteSource
    if use_worker_reservations and not settings.worker_remote_v4_enabled:
        raise domain_error(409, "worker_reservation_unavailable", "Worker v4 is required")
    if trigger == "subscription":
        activation = session.get(SubscriptionActivation, subscription_activation_id)
        if activation is None:
            raise domain_error(
                409, "subscription_activation_invalid", "Frozen activation unavailable"
            )
        if worker_id_override != worker_pool.delivery_target(
            activation, use_worker_reservations=use_worker_reservations
        ):
            raise domain_error(409, "subscription_activation_invalid", "Frozen Worker unavailable")
        source = route_source or "activation"
    else:
        if not use_worker_reservations and (not dependency_check or worker_id_override is None):
            worker_pool.require_fixed_route(worker_pool.adapter_route(adapter))
        from dlr.control.services.subscription_guards import require_subscription_unlocked

        require_subscription_unlocked(session, adapter.id)
        source = route_source or ("dependency-check" if dependency_check else "adapter")
    if not rabbitmq.ingress_configuration_ready(session):
        raise domain_error(
            503,
            "rabbitmq_not_ready",
            "Reliable RabbitMQ runtime is not verified",
            {"retry_after": 1},
        )
    if trigger == "schedule" and schedule_policy_snapshot is None:
        raise domain_error(
            409,
            "schedule_snapshot_unavailable",
            "Schedule policy snapshot is unavailable",
        )

    retry_policy: dict[str, object]
    if activation is None:
        try:
            retry_policy = default_retry_policy()
        except ValueError:
            raise domain_error(
                503,
                "runtime_configuration_invalid",
                "Reliable runtime Retry Policy configuration is invalid",
            ) from None
    frozen_subscription_bindings = None
    if trigger == "subscription":
        assert subscription_activation_id is not None and credential_bindings_override is not None
        frozen_subscription_bindings = _subscription_credential_snapshot(
            session,
            adapter,
            subscription_activation_id,
            credential_bindings_override,
            version_id=version_id,
            worker_id=worker_id_override,
        )
    from dlr.control.schemas.worker_pool import PlacementWaitPolicy
    from dlr.control.services.worker_stage_capability_v4 import require_new_execution

    require_new_execution(
        session,
        adapter,
        version_id=version_id or adapter.latest_version_id,
        trigger=trigger,
        input_source_type=input_source_type,
        dependency_check=dependency_check,
        target_worker_id=worker_id_override,
        artifact_ids=artifact_ids,
        input_snapshot=input_snapshot,
        frozen_subscription_bindings=frozen_subscription_bindings,
        awaiting_worker=use_worker_reservations,
    )
    worker = (
        None
        if use_worker_reservations
        else resolve_queue_target_worker(session, adapter, worker_id_override)
    )
    if activation is not None:
        route_snapshot = worker_pool.activation_snapshot(
            session, activation, adapter.language, source
        )
    else:
        route = (
            worker_pool.fixed_route(worker.id)
            if worker is not None
            else configured_dispatch_route(session, adapter, use_worker_reservations=True)
            if reserved_mode
            else worker_pool.adapter_route(adapter)
        )
        if route is None:
            raise domain_error(409, "execution_route_unavailable", "Runtime route is unavailable")
        route_snapshot = worker_pool.snapshot_route(session, route, adapter.language, source)
    if worker is not None and not rabbitmq.ingress_configuration_ready(
        session, worker_id=worker.id
    ):
        raise domain_error(
            503,
            "rabbitmq_not_ready",
            "Reliable RabbitMQ topology is not verified for the target Worker",
            {"retry_after": 1},
        )
    resource_profile: dict[str, object]
    if trigger == "subscription":
        assert subscription_activation_id is not None and frozen_subscription_bindings is not None
        credential_bindings_snapshot = frozen_subscription_bindings
        from dlr.control.services.subscription_activation_policy import ensure_policy

        assert activation is not None  # validated by the frozen references above
        policy = ensure_policy(session, activation, adapter)
        retry_policy = policy["retry_policy"]
        resource_profile = policy["resource_profile"]
    else:
        resource_profile = default_resource_profile(adapter.timeout_seconds)
        credential_bindings_snapshot = (
            [] if dependency_check else _credential_bindings_snapshot(session, adapter.id)
        )
    try:
        logical_bytes = admission.logical_input_bytes(
            input_source_type,
            runtime_input,
            input_snapshot,
        )
    except ValueError:
        raise domain_error(
            422,
            "input_invalid",
            "Request input is outside the canonical JSON number domain",
        ) from None
    admission.reserve_admission(session, adapter.id, logical_bytes)
    if input_source_type == "managed_files":
        from dlr.control.services.attempt import ensure_managed_file_hold_capacity

        ensure_managed_file_hold_capacity(session)
    now = _db_now(session)
    execution = _create_pending_execution_locked(
        session,
        adapter,
        trigger=trigger,
        runtime_input=runtime_input,
        input_source_type=input_source_type,
        input_config_revision=input_config_revision,
        input_snapshot=input_snapshot,
        target_worker_id=(
            route_snapshot.route.worker_id if isinstance(route_snapshot.route, FixedRoute) else None
        ),
        route_snapshot=route_snapshot.model_dump(mode="json"),
        scheduled_for=scheduled_for,
        artifact_ids=artifact_ids,
        dispatch_backend="rabbitmq",
        dispatch_generation=1,
        logical_input_bytes=logical_bytes,
        max_attempts_snapshot=cast(int, retry_policy["max_attempts"]),
        retry_policy_snapshot=retry_policy,
        resource_profile_snapshot=resource_profile,
        credential_bindings_snapshot=credential_bindings_snapshot,
        schedule_policy_snapshot=schedule_policy_snapshot,
        resource_class=RESOURCE_PROFILE_CLASS,
        version_id_override=version_id,
        dependency_check=dependency_check,
        subscription_delivery_kind=subscription_delivery_kind,
        requires_worker_reservation=use_worker_reservations,
    )
    # Keep the DB clock as the accepted-at source for all generated facts.
    execution.queued_at = now
    if reserved_mode:
        wait_policy = route_snapshot.wait_policy
        timeout = (
            wait_policy.placement_wait_timeout_seconds
            if isinstance(wait_policy, PlacementWaitPolicy)
            else settings.worker_placement_wait_timeout_seconds
        )
        execution.placement_origin = "v4"
        worker_placement_wait.start_cycle(execution, now=now, timeout_seconds=timeout)
    elif worker is not None and worker.protocol_version == 3:
        # The guarded legacy window can still accept work after schema upgrade.
        execution.placement_origin = "legacy_v3"
    if lookup.record is not None:  # pragma: no cover - handled above
        raise AssertionError("duplicate idempotency record")
    if idempotency_key is not None:
        record = idempotency.create_record(
            session,
            adapter_id=adapter.id,
            execution_id=execution.id,
            key_digest=lookup.key_hash,
            request_digest=lookup.payload_hash,
            now=now,
        )
        execution.idempotency_record_id = record.id
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    # The protection line charges exact bytes for W1, and the full envelope
    # for unassigned work. Rejecting either charge rolls back the acceptance.
    outbox.require_outbox_capacity(session, additional_count=0, additional_bytes=0)
    return execution
