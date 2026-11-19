"""Isolated W2 assignment, custody and v4 Claim with real PostgreSQL sessions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    EventDelivery,
    EventSubscription,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    SubscriptionActivation,
    SubscriptionExecutionBatch,
    SubscriptionQuota,
    Worker,
    WorkerPoolMember,
    WorkerResourceReservation,
)
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.schemas.worker_pool import EnvironmentRule, PoolRoute
from dlr.control.security import Principal
from dlr.control.services import (
    outbox,
    rabbitmq,
    subscription_delivery,
    worker_availability,
    worker_capacity,
    worker_claim_v4,
    worker_credentials,
    worker_identity_reservation,
    worker_pool,
    worker_reservation,
)
from dlr.control.services.attempt import V4AttemptAuthority
from dlr.control.services.execution import cancel_execution
from dlr.control.services.reliable_execution import accept_execution
from dlr.control.services.worker_lease_candidates_v4 import (
    ProofAuthority,
    create_initial_candidate,
    issue_committed_candidate_proof,
)
from test_issue129_pool_routing import pool_setup  # noqa: F401
from test_issue219_delivery import delivery_id, receive, world  # noqa: F401
from test_issue222_business_batch import batch_world  # noqa: F401
from test_issue222_business_batch import receive as receive_batch


@pytest.fixture
def placement(session_factory, pool_setup, monkeypatch):  # noqa: F811
    pool, route, environment = pool_setup
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *_, **__: True)
    monkeypatch.setattr(settings, "sandbox_cpu_cores", 0.000001)
    authority = ProofAuthority(uuid4(), uuid4(), b"t" * 32)
    return session_factory, pool, route, EnvironmentRule.model_validate(environment), authority


def _capacity(session, worker_id, environment, *, slots=1, nofile=None, age=0):
    worker = session.get(Worker, worker_id)
    headroom = worker_capacity.Resources(
        cpu_millis=250, memory_bytes=32 * 1024 * 1024, pids=3, tmp_bytes=4096, slots=0
    )
    return worker_capacity.record_verified_capacity(
        session,
        worker_id,
        worker_capacity.VerifiedHostEnvelope(
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
            nofile=settings.sandbox_nofile if nofile is None else nofile,
            storage_ready=True,
            environment=environment,
        ),
    )


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


def _ordinary(session, route, name):
    adapter = Adapter(
        name=name, language="python", adapter_type="task", run_mode="manual", pool_route=route
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
        use_worker_reservations=True,
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


def test_claim_rechecks_capacity_and_attaches_attempt_only_on_commit(placement):
    factory, _, route, environment, _ = placement
    worker = _worker(placement)
    with factory.begin() as session:
        execution_id = _ordinary(session, route, "claim").id
        held = worker_reservation.reserve_execution(session, execution_id)
        message = outbox.deserialize_outbox_payload(_intent(session, execution_id).payload_json)
    decision, proposal = _prepare(factory, worker, message)
    assert decision.decision == "EXECUTE" and proposal is not None
    with factory.begin() as session:
        assert session.get(WorkerResourceReservation, held).status == "held"
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
        assert _capacity(session, worker[0], environment, slots=0).capacity_version == 2
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
        assert execution.attempt_count == attempt.attempt_no == 1
        assert reservation.status == "claimed"
        assert reservation.attempt_id == attempt.id == slot.active_attempt_id
        assert reservation.fencing_token == attempt.fencing_token == slot.fencing_token
        assert reservation.cpu_millis == 10 and attempt.cleanup_settled_at is None
        with pytest.raises(ValueError, match="cleanup binding"):
            worker_reservation.release_cleaned_attempt(session, execution, attempt)
    repeat, proposal = _prepare(factory, worker, message)
    assert repeat.decision == "ACK_NOOP" and proposal is None


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
    with monkeypatch.context() as bounded:
        bounded.setattr(settings, "outbox_max_pending_bytes", 1)
        with session_factory.begin() as session:
            assert (
                subscription_delivery.dispatch_delivery(session, head, use_worker_reservations=True)
                is None
            )
        with session_factory() as session:
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
