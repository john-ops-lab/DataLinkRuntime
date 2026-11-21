"""Isolated W2 assignment, custody and v4 Claim with real PostgreSQL sessions."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from dlr.common.config import settings
from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION
from dlr.common.worker_capacity_report import CapacityObservation, sign_observation
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    AttemptLeaseCandidateV4,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    SubscriptionActivation,
    SubscriptionExecutionBatch,
    SubscriptionQuota,
    Worker,
    WorkerIdentityReservation,
    WorkerLeaseCandidateV4,
    WorkerPlacementCutover,
    WorkerPoolMember,
    WorkerResourceReservation,
)
from dlr.control.models.worker_capacity import ExecutionPlacementAction
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.schemas.worker_pool import EnvironmentRule, PoolRoute
from dlr.control.security import Principal
from dlr.control.services import (
    attempt,
    incident_disposition,
    infrastructure_dlq,
    outbox,
    rabbitmq,
    subscription_delivery,
    worker_availability,
    worker_capacity,
    worker_claim_v4,
    worker_credentials,
    worker_identity_reservation,
    worker_legacy_window,
    worker_maintenance_observation,
    worker_placement_cutover,
    worker_placement_scheduler,
    worker_placement_wait,
    worker_pool,
    worker_reservation,
)
from dlr.control.services.attempt import V4AttemptAuthority
from dlr.control.services.dispatch import serialize_dispatch_message
from dlr.control.services.execution import cancel_execution
from dlr.control.services.reliable_execution import accept_execution
from dlr.control.services.worker_lease_candidates_v4 import (
    ProofAuthority,
    create_initial_candidate,
    has_current_committed_lease,
    issue_committed_candidate_proof,
    stage_renewal_candidate,
)
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes
from dlr.control.services.worker_remote_deployment_v4 import (
    RemoteDeploymentUnavailable,
    require_dedicated_deployment,
)
from test_issue129_pool_routing import (
    pool_setup,  # noqa: F401
    register,
)
from test_issue219_delivery import delivery_id, receive, world  # noqa: F401
from test_issue222_business_batch import batch_world  # noqa: F401
from test_issue222_business_batch import receive as receive_batch


@pytest.fixture
def placement(session_factory, pool_setup, monkeypatch):  # noqa: F811
    pool, route, environment = pool_setup
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *_, **__: True)
    monkeypatch.setattr(rabbitmq, "ingress_ready_targets", lambda _session, ids: frozenset(ids))
    monkeypatch.setattr(settings, "sandbox_cpu_cores", 0.000001)
    authority = ProofAuthority(uuid4(), uuid4(), b"t" * 32)
    return session_factory, pool, route, EnvironmentRule.model_validate(environment), authority


def _capacity(session, worker_id, environment, *, slots=1, nofile=None, age=0, signing_key=None):
    worker = session.get(Worker, worker_id)
    headroom = worker_capacity.Resources(
        cpu_millis=250, memory_bytes=32 * 1024 * 1024, pids=3, tmp_bytes=4096, slots=0
    )
    envelope = worker_capacity.VerifiedHostEnvelope(
        worker_identity_id=worker.identity_id,
        instance_id=worker.instance_id,
        instance_generation=worker.instance_generation,
        sampled_at=worker_availability.current_time(session) - timedelta(seconds=age),
        envelope_fingerprint="e" * 64,
        total=worker_capacity.Resources(
            cpu_millis=260,
            memory_bytes=settings.sandbox_memory_bytes + headroom.memory_bytes,
            pids=settings.sandbox_pids + headroom.pids,
            tmp_bytes=settings.sandbox_tmp_bytes + headroom.tmp_bytes,
            slots=slots,
        ),
        headroom=headroom,
        task_ceiling=worker_capacity.Resources(
            cpu_millis=10,
            memory_bytes=settings.sandbox_memory_bytes,
            pids=settings.sandbox_pids,
            tmp_bytes=settings.sandbox_tmp_bytes,
            slots=1,
        )
        if signing_key is not None
        else None,
        task_cpu_cores=settings.sandbox_cpu_cores if signing_key is not None else None,
        nofile=settings.sandbox_nofile if nofile is None else nofile,
        storage_ready=True,
        environment=environment,
    )
    if signing_key is not None:
        observation = CapacityObservation.model_validate(
            envelope.model_dump(exclude={"environment"})
        )
        return worker_capacity.record_attested_capacity(
            session, worker, sign_observation(observation, signing_key)
        )
    return worker_capacity.record_verified_capacity(session, worker_id, envelope)


def _worker(placement, *, observe=True, labels=None):
    factory, pool, _, environment, authority = placement
    instance = uuid4()
    with factory.begin() as session:
        identity = worker_identity_reservation.reserve_identity(
            session,
            name=f"capacity-{uuid4()}",
            request_id=uuid4(),
            principal=Principal(kind="superadmin"),
        )
        worker_id = identity.worker_id
        enrollment = worker_credentials.issue_enrollment(session, worker_id)
        credential = worker_credentials.exchange_enrollment(
            session, worker_id, enrollment.secret
        ).secret
        ref = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )
    signed = issue_committed_candidate_proof(
        factory, expected=ref, credential=credential, authority=authority
    )
    with factory.begin() as session:
        worker = session.get(Worker, worker_id)
        worker.capacity_state = "ready"
        worker.status = "online"
        worker.scheduling_state = "active"
        worker.capabilities = ["python"]
        worker.isolation_preflight_status = "passed"
        worker.isolation_capabilities = {key: True for key in REQUIRED_ISOLATION_CAPABILITIES}
        session.add(
            WorkerPoolMember(
                pool_id=pool["id"],
                worker_id=worker_id,
                worker_identity_id=worker.identity_id,
                enabled=True,
                labels=labels if labels is not None else {"zone": "a"},
                placement_scopes=["trusted"],
                material_scopes=[],
                credential_ids=[],
            )
        )
        if observe:
            _capacity(session, worker_id, environment)
    return (
        worker_id,
        V4AttemptAuthority(credential, instance, ref.generation),
        worker_claim_v4.ClaimProofContext(authority, signed.token),
    )


def _ordinary(session, route, name, *, fixed_worker=None, staged=True):
    adapter = Adapter(
        name=name,
        language="python",
        adapter_type="task",
        run_mode="manual",
        pool_route=route,
        runtime_worker_id=fixed_worker,
    )
    session.add(adapter)
    session.flush()
    revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
    session.add_all([revision, AdapterExecutionSlot(adapter_id=adapter.id, slot_no=0)])
    session.flush()
    adapter.latest_version_id = revision.id
    return accept_execution(
        session,
        adapter,
        trigger="manual",
        runtime_input={},
        input_source_type="json",
        input_config_revision=1,
        input_snapshot={"source_type": "json", "revision": 1},
        use_worker_reservations=staged,
    )


def _intent(session, execution_id, generation=1):
    return session.scalar(
        select(ExecutionOutbox).where(
            ExecutionOutbox.execution_id == execution_id,
            ExecutionOutbox.dispatch_generation == generation,
        )
    )


def _prepare(factory, worker, message):
    worker_id, authority, proof = worker
    with factory() as session:
        return worker_claim_v4.prepare_claim(
            session,
            worker_id,
            message,
            authority,
            proof_context=proof,
            wire_budget=worker_claim_v4.ClaimWireBudget(65536),
            check_authority=lambda: None,
        )


def _finish(factory, worker, message, proposal):
    worker_id, authority, proof = worker
    with factory.begin() as session:
        return worker_claim_v4.finalize_claim(
            session,
            worker_id,
            message,
            authority,
            proof_context=proof,
            wire_budget=worker_claim_v4.ClaimWireBudget(65536),
            proposal=proposal,
        )


def test_cutover_pauses_new_claim_and_fences_old_published_dispatch(placement):
    factory, _, route, environment, _ = placement
    worker = _worker(placement)
    with factory.begin() as session:
        old = _ordinary(session, None, "w1-fixed-source", fixed_worker=worker[0], staged=False)
        execution_id = old.id
        assert not old.requires_worker_reservation and old.target_worker_id == worker[0]
    with factory() as session:
        (leased,) = outbox.lease_due_outbox(session, "w1-source")
        message = outbox.deserialize_outbox_payload(leased.payload_json)
        assert outbox.mark_outbox_published(session, leased.id, "w1-source")
    with factory.begin() as session:
        assert worker_placement_cutover.pause(session) == "paused"
    decision, proposal = _prepare(factory, worker, message)
    assert decision.reason == "placement_cutover_paused" and proposal is None
    with pytest.raises(HTTPException) as error, factory.begin() as session:
        _ordinary(session, route, "paused-new", staged=False)
    assert error.value.detail["code"] == "placement_cutover_paused"
    with pytest.raises(HTTPException) as error, factory.begin() as session:
        worker_placement_cutover.activate(session)
    assert error.value.detail["code"] == "placement_cutover_not_drained"
    with factory.begin() as session:
        assert worker_placement_cutover.convert_waiting(session, execution_id)
        node = session.get(Worker, worker[0])
        node.scheduling_state = "disabled"
        signing_key = b"v" * 32  # Synthetic verifier key.
        worker_capacity.approve_host_verifier(
            session, worker[0], environment.name, public_key_bytes(signing_key)
        )
        node.scheduling_state = "active"
        with pytest.raises(ValueError, match="unauthorized"):
            _capacity(session, worker[0], environment, signing_key=b"f" * 32)
        _capacity(session, worker[0], environment, signing_key=signing_key)
    with factory.begin() as session:
        assert worker_placement_cutover.activate(session) == "reserved"
    old_wire, proposal = _prepare(factory, worker, message)
    assert old_wire.decision == "ACK_NOOP" and old_wire.reason == "stale_generation"
    assert proposal is None
    with factory.begin() as session:
        accepted = _ordinary(session, route, "pool-after-cutover", staged=False)
        accepted_id = accepted.id
        assert accepted.requires_worker_reservation
        assert accepted.placement_origin == "v4" and accepted.placement_hold_count == 0
        assert accepted.placement_deadline_at == accepted.queued_at + timedelta(seconds=900)
        converted = session.get(Execution, execution_id)
        assert converted.requires_worker_reservation
        assert converted.attempt_count == 0
        assert _intent(session, execution_id).status == "superseded"
        assert _intent(session, execution_id, 2).status == "awaiting_target"
    with factory() as session:
        require_dedicated_deployment(session)
    with factory() as session:
        session.delete(_intent(session, accepted_id))
        session.flush()
        with pytest.raises(RemoteDeploymentUnavailable, match="incompatible"):
            require_dedicated_deployment(session, readonly=True)


def _enable_test_scheduler(session):
    """The separate cutover test exercises signed authority and the owner gate."""
    now = worker_availability.current_time(session)
    gate = session.get(WorkerPlacementCutover, 1)
    gate.mode = "reserved"
    gate.paused_at = gate.enabled_at = now
    session.flush()


def test_v3_accepted_after_upgrade_has_no_retroactive_placement_expiry(
    placement, api_client, monkeypatch
):
    factory, _, _, _, _ = placement
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", False)
    worker_id = register(api_client, "legacy-after-upgrade")
    with factory.begin() as session:
        old = _ordinary(session, None, "legacy-retained", fixed_worker=worker_id, staged=False)
        assert old.placement_origin == "legacy_v3"
        execution_id = old.id
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    with factory.begin() as session:
        assert worker_placement_cutover.pause(session) == "paused"
    with factory.begin() as session:
        assert worker_placement_cutover.convert_waiting(session, execution_id)
        converted = session.get(Execution, execution_id)
        assert converted.placement_origin == "legacy_v3"
        assert converted.placement_deadline_at is None
        assert converted.placement_wait_timeout_seconds is None
        assert not worker_placement_wait.expire_queued(session, execution_id)
        assert not worker_legacy_window.has_unsettled_worker_work(session, worker_id)


def test_closed_window_preserves_adopted_v3_attempt_and_retry_custody(
    placement, api_client, monkeypatch
):
    factory, _, _, _, _ = placement
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", False)
    worker_id = register(api_client, "legacy-retry-history")
    with factory() as session:
        execution = _ordinary(session, None, "legacy-retry", fixed_worker=worker_id, staged=False)
        execution_id = execution.id
        now = worker_availability.current_time(session)
        due = now + timedelta(minutes=5)
        execution.status = "retry_wait"
        execution.next_attempt_at = due
        execution.attempt_count = 1
        execution.workspace_cleanup_status = "completed"
        attempt_row = ExecutionAttempt(
            execution_id=execution.id,
            adapter_id=execution.adapter_id,
            worker_id=worker_id,
            attempt_no=1,
            fencing_token=1,
            status="failed",
            claimed_at=now - timedelta(seconds=1),
            ended_at=now,
            lease_expires_at=now + timedelta(seconds=30),
            claim_token_hash="a" * 64,
            cleanup_token_hash="b" * 64,
            cleanup_summary={"workspace_cleanup_status": "completed"},
        )
        session.add(attempt_row)
        session.flush()

        monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
        monkeypatch.setattr(settings, "worker_v4_deployment_mode", "A")
        monkeypatch.setattr(settings, "worker_v4_control_slots", 1)
        worker_legacy_window.open_once(session, due)
        assert worker_placement_cutover.pause(session) == "paused"
        assert worker_placement_cutover.convert_waiting(session, execution_id)
        worker = session.get(Worker, worker_id)
        adoption = WorkerIdentityReservation(
            request_id=uuid4(),
            worker_id=worker_id,
            name=worker.name,
            actor_kind="superadmin",
            legacy_stop_evidence_sha256="c" * 64,
            created_at=worker_availability.current_time(session),
        )
        session.add(adoption)
        worker.status = "offline"
        worker.protocol_version = 4
        worker.identity_lifecycle = "reserved"
        session.flush()

        monkeypatch.setattr(settings, "worker_v4_deployment_mode", "C")
        monkeypatch.setattr(settings, "worker_v4_control_slots", 2)
        with pytest.raises(RemoteDeploymentUnavailable):
            require_dedicated_deployment(session)
        monkeypatch.setattr(settings, "worker_v4_deployment_mode", "A")
        monkeypatch.setattr(settings, "worker_v4_control_slots", 1)
        worker_legacy_window.close(session)

        monkeypatch.setattr(settings, "worker_v4_deployment_mode", "C")
        monkeypatch.setattr(settings, "worker_v4_control_slots", 2)
        require_dedicated_deployment(session)
        assert attempt_row.worker_identity_id is None
        assert execution.next_attempt_at == due
        cases = (
            (attempt_row, "cleanup_summary", {"workspace_cleanup_status": "deferred"}),
            (execution, "workspace_cleanup_status", "deferred"),
            (adoption, "legacy_stop_evidence_sha256", None),
            (_intent(session, execution_id), "last_error_code", "unknown"),
        )
        for row, field, value in cases:
            with session.begin_nested() as savepoint:
                setattr(row, field, value)
                session.flush()
                with pytest.raises(RemoteDeploymentUnavailable):
                    require_dedicated_deployment(session, readonly=True)
                savepoint.rollback()


def test_placement_limit_retry_expiration_and_cancel_keep_one_responsibility(placement, api_client):
    factory, _, route, _, _ = placement
    _worker(placement)
    with factory.begin() as session:
        _enable_test_scheduler(session)
        execution = _ordinary(session, route, "placement-limit", staged=False)
        first_id, first_deadline = execution.id, execution.placement_deadline_at

    for ordinal in range(1, 11):
        with factory.begin() as session:
            hold = worker_reservation.reserve_execution(session, first_id)
            reservation = session.get(WorkerResourceReservation, hold)
            assert reservation is not None
            reservation.held_until = worker_availability.current_time(session) - timedelta(
                seconds=1
            )
        with factory.begin() as session:
            assert worker_reservation.invalidate_held(session, first_id)
            execution = session.get(Execution, first_id)
            assert execution.placement_hold_count == ordinal
            assert execution.placement_deadline_at == first_deadline
            assert execution.attempt_count == 0

    with factory.begin() as session:
        execution = session.get(Execution, first_id)
        generation = execution.dispatch_generation
        assert execution.placement_reason == "placement_limit_reached"
        assert worker_reservation.reserve_execution(session, first_id) is None
        assert _intent(session, first_id, generation).status == "awaiting_target"

    metrics = api_client.get("/api/workers/placement-metrics")
    assert metrics.status_code == 200
    assert metrics.json()["waiting"] == {"placement_limit_reached": 1}
    assert metrics.json()["allocatable"]["slots"] == 1
    detail = api_client.get(f"/api/executions/{first_id}/reliable-detail").json()["placement"]
    assert detail["retry_available"] and detail["age_seconds"] >= 0
    url = f"/api/executions/{first_id}/placement-retry"
    assert api_client.post(url, json={"expected_generation": generation - 1}).status_code == 409
    for _ in range(2):
        response = api_client.post(url, json={"expected_generation": generation})
        assert response.status_code == 200, response.text
        assert response.json()["dispatch_generation"] == generation + 1
    for _ in range(2):
        assert api_client.post(f"/api/executions/{first_id}/cancel").json()["status"] == "cancelled"
    with factory.begin() as session:
        audits = list(
            session.scalars(
                select(ExecutionPlacementAction).order_by(
                    ExecutionPlacementAction.dispatch_generation
                )
            )
        )
        assert [(audit.dispatch_generation, audit.action) for audit in audits] == [
            (generation, "retry"),
            (generation + 1, "cancel"),
        ]
        execution = session.get(Execution, first_id)
        assert execution.attempt_count == 0 and execution.placement_hold_count == 0
        assert execution.admission_released_at is not None
        expiring = _ordinary(session, route, "placement-deadline", staged=False)
        expiring.placement_deadline_at = worker_availability.current_time(session)
        expiring_id = expiring.id
    with factory.begin() as session:
        assert worker_placement_wait.expire_queued(session, expiring_id)
        execution = session.get(Execution, expiring_id)
        assert execution.status == "expired" and execution.attempt_count == 0
        assert execution.admission_released_at is not None
        assert _intent(session, expiring_id).status == "superseded"


def test_scheduler_rotates_and_uses_persisted_capacity_change_after_lost_notification(placement):
    factory, _, route, environment, _ = placement
    restricted_worker, *_ = _worker(placement, observe=False, labels={"zone": "blocked"})
    compatible_worker, *_ = _worker(placement)
    with factory.begin() as session:
        _enable_test_scheduler(session)
        blocked_id = _ordinary(
            session,
            None,
            "waiting-first-adapter",
            fixed_worker=restricted_worker,
            staged=False,
        ).id
        compatible_id = _ordinary(session, route, "ready-second-adapter", staged=False).id
    for _ in range(3):
        with factory() as session:
            worker_placement_scheduler.reconcile_once(session)
    with factory.begin() as session:
        blocked = session.get(Execution, blocked_id)
        assert blocked.target_worker_id is None and blocked.placement_reason == "capacity_unknown"
        assert (
            blocked.attempt_count == 0
            and session.get(Execution, compatible_id).target_worker_id == compatible_worker
        )
        blocked.next_placement_at = worker_availability.current_time(session) + timedelta(
            seconds=29
        )
        _capacity(session, restricted_worker, environment)
    worker_placement_scheduler._wake.clear()
    for _ in range(3):
        with factory() as session:
            worker_placement_scheduler.reconcile_once(session)
    with factory() as session:
        blocked = session.get(Execution, blocked_id)
        assert blocked.target_worker_id == restricted_worker and blocked.attempt_count == 0


def test_waiting_deadline_is_checked_even_behind_an_unlimited_adapter_head(placement):
    factory, _, route, _, _ = placement
    with factory.begin() as session:
        _enable_test_scheduler(session)
        head = _ordinary(session, route, "blocked-queue", staged=False)
        head.placement_origin = "legacy_v3"
        head.placement_deadline_at = head.placement_wait_timeout_seconds = None
        tail = accept_execution(
            session,
            session.get(Adapter, head.adapter_id),
            trigger="manual",
            runtime_input={},
            input_source_type="json",
            input_config_revision=1,
            input_snapshot={"source_type": "json", "revision": 1},
        )
        tail.placement_deadline_at = worker_availability.current_time(session)
        head_id, tail_id = head.id, tail.id
    with factory() as session:
        assert worker_placement_scheduler.reconcile_once(session) == 1
    with factory() as session:
        assert session.get(Execution, head_id).status == "queued"
        tail = session.get(Execution, tail_id)
        assert tail.status == "expired" and tail.attempt_count == 0
        assert tail.admission_released_at is not None
        assert _intent(session, tail_id).status == "superseded"


def test_dead_letter_replay_accepts_the_current_pool_before_assignment(placement, api_client):
    factory, _, route, _, _ = placement
    worker = _worker(placement)
    with factory.begin() as session:
        _enable_test_scheduler(session)
        old = _ordinary(session, route, "pool-dead-letter", staged=False)
        execution_id, adapter_id = old.id, old.adapter_id
        worker_reservation.reserve_execution(session, execution_id)
        message = outbox.deserialize_outbox_payload(_intent(session, execution_id).payload_json)
    _, proposal = _prepare(factory, worker, message)
    assert proposal is not None
    claimed = _finish(factory, worker, message, proposal)
    assert claimed.decision == "EXECUTE"
    with factory.begin() as session:
        execution, _, owned, slot = attempt._lock_attempt_context(
            session, worker[0], claimed.attempt_id
        )
        attempt._apply_terminal_locked(
            session,
            execution,
            owned,
            slot,
            status="failed",
            error_code="adapter_failed",
            error_class="business_error",
            error="fixture business error",
            now=worker_availability.current_time(session),
        )
        assert execution.status == "dead_letter"
    assert api_client.get(f"/api/adapters/{adapter_id}").json()["pool_placement_enabled"] is True
    response = api_client.post(f"/api/executions/{execution_id}/replay")
    assert response.status_code == 200, response.text
    assert response.json()["route_snapshot"]["route"] == route
    with factory() as session:
        replayed = session.get(Execution, response.json()["execution_id"])
        assert replayed.replay_of_execution_id == execution_id
        assert replayed.target_worker_id is None and replayed.attempt_count == 0
        assert _intent(session, replayed.id).status == "awaiting_target"


def test_pool_recovery_waits_grace_then_keeps_the_old_physical_charge(placement, monkeypatch):
    factory, _, route, _, _ = placement
    old = _worker(placement)
    with factory.begin() as session:
        _enable_test_scheduler(session)
        execution = _ordinary(session, route, "lost-worker", staged=False)
        execution_id = execution.id
        held = worker_reservation.reserve_execution(session, execution_id)
        first_message = outbox.deserialize_outbox_payload(
            _intent(session, execution_id).payload_json
        )
    _, proposal = _prepare(factory, old, first_message)
    assert proposal is not None
    first = _finish(factory, old, first_message, proposal)
    assert first.attempt_id is not None and first.decision == "EXECUTE"
    with factory() as session:
        lease = session.get(AttemptLeaseCandidateV4, first.attempt_id)
        lease_end = max(lease.nominal_until, lease.guard_until)
        grace = session.get(Execution, execution_id).recovery_grace_seconds_snapshot
    now = lease_end - timedelta(microseconds=1)
    monkeypatch.setattr(worker_availability, "current_time", lambda _: now)
    with factory() as session:
        assert attempt.recover_expired_attempts(session) == 0
    now = lease_end
    with factory() as session:
        assert attempt.recover_expired_attempts(session) == 1
    with factory() as session:
        first_row = session.get(ExecutionAttempt, first.attempt_id)
        until = lease_end + timedelta(seconds=grace)
        assert first_row.recovery_not_before == until
        assert first_row.status == "worker_lost" and first_row.cleanup_settled_at is None
        assert session.get(WorkerResourceReservation, held).status == "reclaiming"
        next_due = session.get(Execution, execution_id).next_attempt_at
        assert next_due > until
        now = until
        assert worker_placement_wait.retry_due(session) == 0
    now = next_due
    replacement = _worker(placement)
    with factory() as session:
        assert worker_placement_wait.retry_due(session) == 1
    with factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution.attempt_count == 1 and execution.dispatch_generation == 2
        replacement_hold = worker_reservation.reserve_execution(session, execution_id)
        assert execution.target_worker_id == replacement[0]
        message = outbox.deserialize_outbox_payload(_intent(session, execution_id, 2).payload_json)
    _, proposal = _prepare(factory, replacement, message)
    assert proposal is not None
    second = _finish(factory, replacement, message, proposal)
    assert second.decision == "EXECUTE" and second.attempt_id != first.attempt_id
    with factory.begin() as session:
        execution = session.get(Execution, execution_id, with_for_update=True)
        assert execution.attempt_count == 2
        assert session.get(WorkerResourceReservation, held).status == "reclaiming"
        slot = session.get(AdapterExecutionSlot, (execution.adapter_id, 0), with_for_update=True)
        prior = session.get(ExecutionAttempt, first.attempt_id, with_for_update=True)
        session.get(Worker, prior.worker_id, with_for_update=True)
        # Supply only the original Attempt's physical receipt boundary.
        prior.cleanup_settled_at = now
        worker_reservation.release_cleaned_attempt(session, execution, prior)
        worker_reservation.release_cleaned_attempt(session, execution, prior)
        assert slot.active_attempt_id == second.attempt_id
        assert session.get(WorkerResourceReservation, replacement_hold).status == "claimed"


def test_post_claim_capacity_failure_keeps_charge_and_needs_fresh_preflight(placement):
    factory, _, route, environment, _ = placement
    worker = _worker(placement)
    signing_key = b"q" * 32
    with factory.begin() as session:
        _enable_test_scheduler(session)
        node = session.get(Worker, worker[0])
        node.scheduling_state = "disabled"
        worker_capacity.approve_host_verifier(
            session, worker[0], environment.name, public_key_bytes(signing_key)
        )
        node.scheduling_state = "active"
        execution_id = _ordinary(session, route, "capacity-drift", staged=False).id
        held = worker_reservation.reserve_execution(session, execution_id)
        message = outbox.deserialize_outbox_payload(_intent(session, execution_id).payload_json)
    _, proposal = _prepare(factory, worker, message)
    assert proposal is not None
    claimed = _finish(factory, worker, message, proposal)
    assert claimed.decision == "EXECUTE" and claimed.attempt_id is not None
    with factory.begin() as session:
        execution, _, claimed_row, slot = attempt._lock_attempt_context(
            session, worker[0], claimed.attempt_id
        )
        now = worker_availability.current_time(session)
        attempt._apply_terminal_locked(
            session,
            execution,
            claimed_row,
            slot,
            status="failed",
            error_code="resource_capacity_unavailable",
            error_class="platform_transient",
            error="Worker resource capacity unavailable",
            now=now,
        )
        assert execution.attempt_count == 1 and execution.status == "retry_wait"
        assert execution.next_attempt_at > now
    with factory.begin() as session:
        node = session.get(Worker, worker[0])
        assert node.capacity_state == "quarantined"
        reserved = session.get(WorkerResourceReservation, held)
        assert reserved.status == "reclaiming"
        capacity = _capacity(session, worker[0], environment, signing_key=signing_key)
        assert capacity.quarantine_until >= now + timedelta(seconds=30)
        assert node.capacity_state == "quarantined"
    ledger = worker_maintenance_observation.read_status(
        factory, worker[0], Principal(kind="superadmin")
    )["placement_ledger"]
    assert ledger["capacity"]["allocatable"] is None
    assert ledger["claimed"]["slots"] == 0 and ledger["reclaiming"]["slots"] == 1


def test_capacity_key_can_load_on_a_standalone_worker_without_control_configuration(tmp_path):
    key_file = tmp_path.resolve() / "capacity-key.json"
    key_file.write_text('{"schema_version":1,"host_capacity_signing_key":"' + "ab" * 32 + '"}')
    key_file.chmod(0o600)
    # A standalone Worker has its own Broker file, with no Control settings.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("DLR_")}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from dlr.worker.capacity_attestation_v4 import read_signing_key; "
            "assert read_signing_key(Path(sys.argv[1])) == bytes.fromhex('ab' * 32)",
            str(key_file),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_two_controls_contend_for_last_capacity_and_cancel_with_maintenance(placement):
    factory, _, route, _, _ = placement
    worker, _, _ = _worker(placement)
    with factory.begin() as session:
        first = _ordinary(session, route, "first").id
        second = _ordinary(session, route, "second").id
        waiting = outbox.backlog(session)
        assert waiting.pending_count == 2 and waiting.oldest_created_at is not None
        assert waiting.pending_bytes == 2 * settings.rabbitmq_dispatch_message_max_bytes
    with factory() as session:
        assert outbox.lease_due_outbox(session, "no-worker-yet") == []

    simultaneous = Barrier(2)

    def assign(execution_id):
        with factory.begin() as session:
            pid = session.scalar(select(func.pg_backend_pid()))
            simultaneous.wait(timeout=3)
            return pid, worker_reservation.reserve_execution(session, execution_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(assign, (first, second)))
    assert results[0][0] != results[1][0]
    won, lost = (first, second) if results[0][1] is not None else (second, first)
    assert sum(reservation_id is not None for _, reservation_id in results) == 1

    with factory() as session:
        reservation = session.scalar(select(WorkerResourceReservation))
        assert reservation.status == "held"
        assert reservation.cpu_accounting_version == CPU_ACCOUNTING_VERSION
        assert (
            reservation.cpu_millis,
            reservation.memory_bytes,
            reservation.pids,
            reservation.tmp_bytes,
            reservation.slots,
        ) == (
            10,
            settings.sandbox_memory_bytes,
            settings.sandbox_pids,
            settings.sandbox_tmp_bytes,
            1,
        )
        assert session.get(Execution, won).target_worker_id == worker
        waiting = _intent(session, lost)
        selected = _intent(session, won)
        assert waiting.status == "awaiting_target" and waiting.routing_key is None
        assert waiting.payload_json is None
        assert selected.status == "pending" and selected.reservation_id == reservation.id
        assert (
            outbox.backlog(session).pending_bytes == selected.payload_bytes + waiting.payload_bytes
        )
        assert selected.payload_bytes < waiting.payload_bytes
    with factory.begin() as session:
        session.get(Worker, worker, with_for_update=True)
        reservation = session.get(WorkerResourceReservation, selected.reservation_id)
        reservation.cpu_accounting_version = None
        reservation.cpu_millis = 1
        assert worker_capacity.occupied_resources(session, worker) is None
        assert reservation.status == "held"

    locked = Event()
    cancelling = Event()

    def maintain():
        with factory.begin() as session:
            node = session.get(Worker, worker, with_for_update=True)
            locked.set()
            assert cancelling.wait(timeout=3)
            node.scheduling_state = "draining"

    def cancel():
        assert locked.wait(timeout=3)
        cancelling.set()
        with factory() as session:
            return cancel_execution(session, won).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        maintenance = executor.submit(maintain)
        cancellation = executor.submit(cancel)
        maintenance.result(timeout=5)
        assert cancellation.result(timeout=5) == "cancelled"
    with factory() as session:
        cancel_execution(session, won)
        assert session.scalar(select(WorkerResourceReservation)).status == "released"
        assert _intent(session, won).status == "superseded"
        assert worker_reservation.reserve_execution(session, lost) is None
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_assignment_rejects_self_report_scope_mismatch_staleness_and_nofile(placement):
    factory, pool, route, environment, _ = placement
    worker_id, _, _ = _worker(placement, observe=False, labels={"zone": "other"})
    with factory.begin() as session:
        execution_id = _ordinary(session, route, "hard-gates").id
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        _capacity(session, worker_id, environment)
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        session.get(WorkerPoolMember, (pool["id"], worker_id)).labels = {"zone": "a"}
        wrong = environment.model_copy(update={"toolchains": {"python": "f" * 64}})
        _capacity(session, worker_id, wrong)
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        capacity = _capacity(session, worker_id, environment, nofile=settings.sandbox_nofile - 1)
        assert capacity.capacity_version == 3
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        with pytest.raises(ValueError):
            _capacity(session, worker_id, environment, age=16)
        capacity = _capacity(session, worker_id, environment)
        assert capacity.capacity_version == 4
        capacity.sampled_at -= timedelta(seconds=16)
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        capacity = _capacity(session, worker_id, environment)
        assert capacity.capacity_version == 5
    with factory.begin() as session:
        assert worker_reservation.reserve_execution(session, execution_id) is not None


def test_bounded_candidate_page_reaches_eligible_later_member(placement, monkeypatch):
    factory, _, route, _, _ = placement
    first, _, _ = _worker(placement, observe=False)
    later, _, _ = _worker(placement)
    monkeypatch.setattr(worker_reservation, "MAX_CANDIDATES", 1)
    with factory.begin() as session:
        execution_id = _ordinary(session, route, "next-candidate-page").id
        assert worker_reservation.reserve_execution(session, execution_id) is None
    with factory.begin() as session:
        assert session.get(Execution, execution_id).placement_after_worker_id == first
        placed = worker_reservation.reserve_execution(session, execution_id)
        assert session.get(WorkerResourceReservation, placed).worker_id == later
        assert session.get(Execution, execution_id).placement_after_worker_id is None


def test_unknown_confirm_and_reused_owner_cannot_revive_old_generation(placement):
    factory, _, route, _, _ = placement
    former = _worker(placement)
    replacement = _worker(placement)
    with factory.begin() as session:
        execution_id = _ordinary(session, route, "unknown-confirm").id
        held = worker_reservation.reserve_execution(session, execution_id)
        assert session.get(WorkerResourceReservation, held).worker_id == former[0]
    with factory() as session:
        (old,) = outbox.lease_due_outbox(session, "same-owner")
        original_message = outbox.deserialize_outbox_payload(old.payload_json)
    with factory.begin() as session:
        _intent(session, execution_id).lease_expires_at = worker_availability.current_time(session)
    with factory() as session:
        (retry,) = outbox.lease_due_outbox(session, "same-owner")
        assert retry.id == old.id and retry.lease_attempt == old.lease_attempt + 1
        assert not outbox.mark_outbox_published(
            session, old.id, "same-owner", lease_attempt=old.lease_attempt
        )
    with factory.begin() as session:
        session.get(Worker, former[0], with_for_update=True).scheduling_state = "draining"
        session.get(WorkerResourceReservation, held).held_until = worker_availability.current_time(
            session
        ) - timedelta(microseconds=1)
        assert worker_reservation.invalidate_held(session, execution_id)
        execution = session.get(Execution, execution_id)
        assert execution.attempt_count == 0 and execution.target_worker_id is None
        assert _intent(session, execution_id, 2).status == "awaiting_target"
    with factory() as session:
        stale = infrastructure_dlq.reconcile_message(
            session, serialize_dispatch_message(original_message)
        )
        assert stale.action == "manual_review" and stale.incident_id > 0
        assert _intent(session, execution_id, 2).status == "awaiting_target"
    with factory.begin() as session:
        new_id = worker_reservation.reserve_execution(session, execution_id)
        assert session.get(WorkerResourceReservation, new_id).worker_id == replacement[0]
    with factory() as session:
        assert not outbox.mark_outbox_published(
            session, retry.id, "same-owner", lease_attempt=retry.lease_attempt
        )
        assert _intent(session, execution_id).status == "superseded"
        assert _intent(session, execution_id, 2).reservation_id == new_id
        assert _intent(session, execution_id, 2).status == "pending"
    decision, proposal = _prepare(factory, former, original_message)
    assert decision.decision == "ACK_NOOP" and decision.reason == "stale_generation"
    assert proposal is None
    with factory() as session:
        current = outbox.deserialize_outbox_payload(_intent(session, execution_id, 2).payload_json)
        incident = infrastructure_dlq.reconcile_message(
            session, serialize_dispatch_message(current)
        )
        actions = incident_disposition.inspect_incident_disposition(
            session.get(Execution, execution_id),
            session.get(ExecutionInfrastructureIncident, incident.incident_id),
            active_attempts=False,
            current_outbox=_intent(session, execution_id, 2),
            can_edit=True,
        )
        assert not actions.recover_available and actions.terminate_available
        assert actions.recover_reason == "incident_execution_unsupported"
        disposed = incident_disposition.dispose_incident(
            session,
            execution_id,
            incident.incident_id,
            "terminate",
            2,
            uuid4(),
            "operator_cancel",
            Principal(kind="superadmin"),
        )
        assert disposed.status_code == 200
    with factory() as session:
        assert session.get(Execution, execution_id).status == "cancelled"
        assert session.get(WorkerResourceReservation, new_id).status == "released"


def test_relay_does_not_finish_held_publish_after_worker_lease_floor(placement, monkeypatch):
    factory, _, route, _, _ = placement
    worker_id, _, _ = _worker(placement)
    monkeypatch.setattr(settings, "rabbitmq_outbox_lease_seconds", 60)
    with factory.begin() as session:
        execution_id = _ordinary(session, route, "confirm-after-worker-proof").id
        reservation_id = worker_reservation.reserve_execution(session, execution_id)
        boundary = session.get(WorkerLeaseCandidateV4, worker_id).proof_before
        assert boundary < session.get(WorkerResourceReservation, reservation_id).held_until
    with factory() as session:
        (lease,) = outbox.lease_due_outbox(session, "confirm-after-worker-proof")
    with factory.begin() as session:
        # Model a newer candidate becoming visible after Relay sampled time.
        sampled_before = worker_availability.current_time(session)
        newer = session.get(WorkerLeaseCandidateV4, worker_id)
        newer.candidate_id = uuid4()
        newer.revision += 1
        newer.proof_before = worker_availability.current_time(session)
        newer.writer_xid = session.scalar(select(func.pg_catalog.txid_current()))
        assert sampled_before < newer.proof_before < newer.guard_until
    with factory() as session:
        assert not outbox.mark_outbox_published(
            session,
            lease.id,
            "confirm-after-worker-proof",
            now=sampled_before,
            lease_attempt=lease.lease_attempt,
        )
        assert _intent(session, execution_id).status == "pending"
        assert session.get(WorkerResourceReservation, reservation_id).status == "held"


def test_claim_rechecks_capacity_and_attaches_attempt_only_on_commit(placement):
    factory, _, route, environment, _ = placement
    worker_id, attempt_authority, proof = _worker(placement)
    with factory.begin() as session:
        ref = stage_renewal_candidate(
            session,
            worker_id=worker_id,
            credential=attempt_authority.credential,
            instance_id=attempt_authority.instance_id,
            generation=attempt_authority.instance_generation,
            authority=proof.authority,
            token=proof.instance_proof,
        )
    signed = issue_committed_candidate_proof(
        factory, expected=ref, credential=attempt_authority.credential, authority=proof.authority
    )
    worker = (
        worker_id,
        attempt_authority,
        worker_claim_v4.ClaimProofContext(proof.authority, signed.token),
    )
    with factory.begin() as session:
        node = session.get(Worker, worker_id, with_for_update=True)
        lease = session.get(WorkerLeaseCandidateV4, worker_id)
        assert lease.guard_until < lease.nominal_until
        assert not has_current_committed_lease(session, node, now=lease.guard_until)
        assert _capacity(session, worker_id, environment).capacity_version == 2
        execution_id = _ordinary(session, route, "claim").id
        other_execution_id = _ordinary(session, route, "another-execution").id
        held = worker_reservation.reserve_execution(session, execution_id)
        message = outbox.deserialize_outbox_payload(_intent(session, execution_id).payload_json)
    # A valid instance/fence binding must also refer to the same Execution.
    with pytest.raises(IntegrityError) as wrong_binding, factory.begin() as session:
        reservation = session.get(WorkerResourceReservation, held)
        now = worker_availability.current_time(session)
        wrong = ExecutionAttempt(
            execution_id=other_execution_id,
            adapter_id=session.get(Execution, other_execution_id).adapter_id,
            attempt_no=1,
            status="claimed",
            fencing_token=1,
            worker_id=reservation.worker_id,
            worker_identity_id=reservation.worker_identity_id,
            worker_instance_id=reservation.instance_id,
            worker_instance_generation=reservation.instance_generation,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=20),
        )
        session.add(wrong)
        session.flush()
        reservation.status = "claimed"
        reservation.attempt_id = wrong.id
        reservation.fencing_token = wrong.fencing_token
    assert (
        wrong_binding.value.orig.diag.constraint_name == "fk_worker_reservations_attempt_execution"
    )
    decision, proposal = _prepare(factory, worker, message)
    assert decision.decision == "EXECUTE" and proposal is not None
    with factory.begin() as session:
        assert session.get(WorkerResourceReservation, held).status == "held"
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
        assert _capacity(session, worker[0], environment, slots=0).capacity_version == 3
    decision = _finish(factory, worker, message, proposal)
    assert decision.decision == "PAUSE_CONSUMER"
    assert decision.reason == "worker_reservation_unavailable"
    with factory.begin() as session:
        assert session.get(WorkerResourceReservation, held).status == "held"
        assert session.get(Execution, execution_id).attempt_count == 0
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
        _capacity(session, worker[0], environment)
    _, proposal = _prepare(factory, worker, message)
    assert proposal is not None
    assert _finish(factory, worker, message, proposal).decision == "EXECUTE"
    with factory() as session:
        execution = session.get(Execution, execution_id)
        reservation = session.get(WorkerResourceReservation, held)
        slot = session.get(AdapterExecutionSlot, (execution.adapter_id, 0))
        attempt = session.get(ExecutionAttempt, slot.active_attempt_id)
        attempt_id = attempt.id
        assert execution.attempt_count == attempt.attempt_no == 1
        assert reservation.status == "claimed"
        assert reservation.attempt_id == attempt.id == slot.active_attempt_id
        assert reservation.fencing_token == attempt.fencing_token == slot.fencing_token
        assert reservation.cpu_millis == 10 and attempt.cleanup_settled_at is None
        with pytest.raises(ValueError, match="cleanup binding"):
            worker_reservation.release_cleaned_attempt(session, execution, attempt)
    repeat, proposal = _prepare(factory, worker, message)
    assert repeat.decision == "ACK_NOOP" and proposal is None
    with factory.begin() as session:
        # Supply the existing v4 cleanup boundary as an isolated fixture.
        execution = session.get(Execution, execution_id, with_for_update=True)
        session.get(Worker, worker_id, with_for_update=True)
        attempt = session.get(ExecutionAttempt, attempt_id)
        attempt.status = "succeeded"
        attempt.cleanup_settled_at = worker_availability.current_time(session)
        worker_reservation.release_cleaned_attempt(session, execution, attempt)
    with factory() as session:
        assert session.get(WorkerResourceReservation, held).status == "released"
        assert worker_capacity.occupied_resources(session, worker_id) == (0, 0, 0, 0, 0)
        assert _intent(session, execution_id).status == "superseded"


def _t2_custody(session, ids, subscription_id):
    members = session.execute(
        select(
            EventDelivery.id,
            EventDelivery.state,
            EventDelivery.execution_id,
            EventDelivery.batch_execution_id,
            EventReceipt.state,
            EventReceipt.payload_bytes,
            EventDelivery.retained_execution_bytes,
            EventDelivery.retained_batch_bytes,
        )
        .join(EventReceipt, EventReceipt.id == EventDelivery.receipt_id)
        .where(EventDelivery.id.in_(ids))
        .order_by(EventDelivery.id)
    ).all()
    quotas = session.execute(
        select(
            SubscriptionQuota.scope,
            SubscriptionQuota.pending_count,
            SubscriptionQuota.pending_bytes,
            SubscriptionQuota.history_count,
            SubscriptionQuota.history_bytes,
        )
        .where(SubscriptionQuota.scope.in_(("global", f"sub:{subscription_id}")))
        .order_by(SubscriptionQuota.scope)
    ).all()
    return members, quotas


@pytest.mark.parametrize("batch", [False, True])
def test_t2_prepaid_handoff_rolls_back_completely_when_outbox_full(
    placement,
    request,
    monkeypatch,
    batch,
):
    session_factory, _, route, _, _ = placement
    worker = _worker(placement)
    fixture = request.getfixturevalue("batch_world" if batch else "world")
    receive_event = receive_batch if batch else receive
    with session_factory.begin() as session:
        subscription = session.get(EventSubscription, fixture["sub"])
        session.get(SubscriptionActivation, subscription.activation_id).worker_id = worker[0]
        session.add(AdapterExecutionSlot(adapter_id=fixture["adapter"], slot_no=0))
        head = delivery_id(session, receive_event(session, fixture, 0))
        ids = [head]
        if batch:
            ids.append(delivery_id(session, receive_event(session, fixture, 1)))
        # T1 production routing stays fixed. Seed the separately frozen Pool
        # activation for the isolated T2 contract without enabling Pool T1.
        snapshot = worker_pool.snapshot_route(
            session, PoolRoute.model_validate(route), "python", "current-config"
        )
        staged = SubscriptionActivation(
            subscription_id=subscription.id,
            config_version_id=fixture["config"],
            revision_id=fixture["revision"],
            route_snapshot=snapshot.model_dump(mode="json"),
        )
        session.add(staged)
        session.flush()
        for delivery_id_value in ids:
            session.get(EventDelivery, delivery_id_value).activation_override_id = staged.id
    with session_factory() as session:
        before = _t2_custody(session, ids, fixture["sub"])
        assert len(before[0]) == len(ids) and len(before[1]) == 2
    with monkeypatch.context() as bounded:
        bounded.setattr(settings, "outbox_max_pending_bytes", 1)
        with session_factory.begin() as session:
            assert (
                subscription_delivery.dispatch_delivery(session, head, use_worker_reservations=True)
                is None
            )
        with session_factory() as session:
            assert _t2_custody(session, ids, fixture["sub"]) == before
            delivery = session.get(EventDelivery, head)
            assert delivery.state == "pending"
            assert delivery.execution_id is None and delivery.batch_execution_id is None
            assert outbox.backlog(session).pending_count == 0
            assert session.scalar(select(func.count()).select_from(Execution)) == 0
            admission = session.get(GlobalExecutionAdmission, "global")
            assert admission is None or admission.outstanding_count == 0
            quota = session.get(SubscriptionQuota, f"sub:{fixture['sub']}")
            assert quota.pending_count == (2 if batch else 1)
            assert quota.pending_bytes > 0
    with session_factory.begin() as session:
        session.get(EventDelivery, head).retry_after = worker_availability.current_time(session)
        # The ordinary single/batch T2 entry uses the same acceptance transaction.
        execution = subscription_delivery.dispatch_delivery(
            session, head, use_worker_reservations=True
        )
        assert execution is not None
        execution_id = execution.id
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        intent = _intent(session, execution_id)
        assert intent.status == "awaiting_target" and intent.payload_json is None
        assert intent.payload_bytes == settings.rabbitmq_dispatch_message_max_bytes
        assert execution.target_worker_id is None and execution.attempt_count == 0
        assert session.get(GlobalExecutionAdmission, "global").outstanding_count == 1
        assert session.get(SubscriptionQuota, f"sub:{fixture['sub']}").pending_count == 0
        if batch:
            assert session.get(SubscriptionExecutionBatch, execution_id).member_count == 2
        assert outbox.lease_due_outbox(session, "t2") == []
    with session_factory.begin() as session:
        reservation_id = worker_reservation.reserve_execution(session, execution_id)
        assert reservation_id is not None
    with session_factory() as session:
        (leased,) = outbox.lease_due_outbox(session, "t2")
        assert outbox.mark_outbox_published(
            session, leased.id, "t2", lease_attempt=leased.lease_attempt
        )
    message = outbox.deserialize_outbox_payload(leased.payload_json)
    _, proposal = _prepare(session_factory, worker, message)
    assert proposal is not None
    assert _finish(session_factory, worker, message, proposal).decision == "EXECUTE"
