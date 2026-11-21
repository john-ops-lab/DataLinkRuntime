"""Committed-candidate checks with real PostgreSQL; no router is mounted."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update

from dlr.control.models import (
    Worker,
    WorkerCredential,
    WorkerInstanceHistory,
    WorkerLeaseCandidateV4,
)
from dlr.control.services import worker_credentials, worker_instances
from dlr.control.services.worker_lease_candidates_v4 import (
    CandidateRef,
    LeaseCandidateError,
    ProofAuthority,
    _issue_in_session,
    create_initial_candidate,
    has_current_committed_lease,
    issue_committed_candidate_proof,
    observe_expired_candidate,
    stage_renewal_candidate,
    validate_current_proof,
)


def _worker(session_factory):
    with session_factory.begin() as session:
        worker = Worker(name=f"instance-{uuid4()}", status="offline", capabilities=[])
        session.add(worker)
        session.flush()
        worker_id = worker.id
        enrollment = worker_credentials.issue_enrollment(session, worker_id)
        runtime = worker_credentials.exchange_enrollment(session, worker_id, enrollment.secret)
    return worker_id, runtime.secret


@pytest.fixture
def authority():
    return ProofAuthority(uuid4(), uuid4(), b"s" * 32)  # Synthetic test material.


def _initial(factory, authority, *, short=False):
    worker_id, credential = _worker(factory)
    instance = uuid4()
    with factory.begin() as session:
        ref = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )
        if short:
            deadline = session.scalar(select(func.clock_timestamp())) + timedelta(seconds=1.5)
            worker = session.get(Worker, worker_id)
            candidate = session.get(WorkerLeaseCandidateV4, worker_id)
            worker.instance_lease_until = deadline
            candidate.nominal_until = candidate.guard_until = candidate.proof_before = deadline
    proof = issue_committed_candidate_proof(
        factory, expected=ref, credential=credential, authority=authority
    )
    return worker_id, credential, instance, ref, proof


def _validate(session, worker_id, credential, instance, token, authority):
    return validate_current_proof(
        session,
        worker_id=worker_id,
        credential=credential,
        instance_id=instance,
        generation=1,
        token=token,
        authority=authority,
    )


def _renew(session, worker_id, credential, instance, token, authority):
    return stage_renewal_candidate(
        session,
        worker_id=worker_id,
        credential=credential,
        instance_id=instance,
        generation=1,
        token=token,
        authority=authority,
    )


@pytest.mark.parametrize("operation", ["sign", "validate"])
def test_credential_expiring_before_proof_observation_is_rejected(
    session_factory, authority, monkeypatch, operation
):
    from dlr.control.services import worker_availability

    worker_id, credential, instance, ref, proof = _initial(session_factory, authority)
    with session_factory.begin() as session:
        row = session.scalar(
            select(WorkerCredential).where(
                WorkerCredential.worker_id == worker_id, WorkerCredential.kind == "runtime"
            )
        )
        expiry = session.scalar(select(func.clock_timestamp())) + timedelta(seconds=0.5)
        row.expires_at = expiry
    current_time = worker_availability.current_time
    observed = []

    def cross_expiry(session):
        time.sleep(0.7)
        now = current_time(session)
        observed.append(now)
        return now

    monkeypatch.setattr(worker_availability, "current_time", cross_expiry)
    with pytest.raises(LeaseCandidateError):
        if operation == "sign":
            issue_committed_candidate_proof(
                session_factory, expected=ref, credential=credential, authority=authority
            )
        else:
            with session_factory.begin() as session:
                _validate(session, worker_id, credential, instance, proof.token, authority)
    assert observed and observed[-1] >= expiry


def test_uncommitted_candidate_cannot_sign_itself(session_factory, authority):
    worker_id, credential = _worker(session_factory)
    instance = uuid4()
    with session_factory() as session:
        ref = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )
        with pytest.raises(LeaseCandidateError):
            _issue_in_session(session, expected=ref, credential=credential, authority=authority)
        session.commit()
    proof = issue_committed_candidate_proof(
        session_factory, expected=ref, credential=credential, authority=authority
    )
    with session_factory.begin() as session:
        assert (
            _validate(session, worker_id, credential, instance, proof.token, authority).revision
            == 1
        )
    assert proof.token not in repr(proof) and authority.key.hex() not in repr(authority)


def test_initial_registration_retry_does_not_extend_or_replace_candidate(
    session_factory, authority
):
    worker_id, credential, instance, original, proof = _initial(session_factory, authority)
    with session_factory.begin() as session:
        before = session.get(WorkerLeaseCandidateV4, worker_id).nominal_until
        retried = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )
        assert retried == original
        assert session.get(WorkerLeaseCandidateV4, worker_id).nominal_until == before
    retried_proof = issue_committed_candidate_proof(
        session_factory, expected=retried, credential=credential, authority=authority
    )
    with session_factory.begin() as session:
        assert _validate(session, worker_id, credential, instance, retried_proof.token, authority)
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=uuid4()
        )
    with session_factory.begin() as session:
        successor = _renew(session, worker_id, credential, instance, proof.token, authority)
    assert successor.revision == 2
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )


def test_old_v4_without_candidate_is_unknown_not_safe_takeover(session_factory, authority):
    worker_id, credential = _worker(session_factory)
    instance = uuid4()
    with session_factory.begin() as session:
        worker = worker_instances.register_instance(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance,
            protocol_version=4,
        )
        worker.instance_lease_until = session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    with session_factory.begin() as session:
        assert not observe_expired_candidate(session, worker_id=worker_id)
        with pytest.raises(LeaseCandidateError):
            create_initial_candidate(
                session, worker_id=worker_id, credential=credential, instance_id=uuid4()
            )
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        _validate(session, worker_id, credential, instance, "missing", authority)


def test_new_generation_waits_for_candidate_and_starts_reconciling(session_factory, authority):
    worker_id, credential, old_instance, old_ref, old_proof = _initial(session_factory, authority)
    new_instance = uuid4()
    with session_factory.begin() as session:
        now = session.scalar(select(func.pg_catalog.clock_timestamp()))
        xid = session.scalar(select(func.pg_catalog.txid_current()))
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.scheduling_state = "draining"
        worker.capacity_state = "ready"
        worker.instance_lease_until = candidate.nominal_until = now + timedelta(seconds=30)
        candidate.guard_until = candidate.proof_before = now - timedelta(seconds=1)
        candidate.writer_xid = xid

    # Even when the effective guard has elapsed, an unresolved candidate's
    # later nominal bound must not be discarded to accelerate takeover.
    with session_factory.begin() as session:
        assert not observe_expired_candidate(session, worker_id=worker_id)
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=new_instance
        )
    with session_factory.begin() as session:
        now = session.scalar(select(func.pg_catalog.clock_timestamp()))
        xid = session.scalar(select(func.pg_catalog.txid_current()))
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.instance_lease_until = candidate.nominal_until = now - timedelta(seconds=1)
        candidate.guard_until = candidate.proof_before = now - timedelta(seconds=2)
        candidate.writer_xid = xid

    with session_factory.begin() as session:
        new_ref = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=new_instance
        )
        worker = session.get(Worker, worker_id)
        assert new_ref.generation == 2 and new_ref.revision == 1
        assert new_ref.candidate_id != old_ref.candidate_id
        assert (worker.capacity_state, worker.scheduling_state) == ("reconciling", "draining")
        assert session.get(WorkerInstanceHistory, (worker_id, old_instance)).generation == 1
        assert session.get(WorkerInstanceHistory, (worker_id, new_instance)).generation == 2
        expires = worker.instance_lease_until

    with session_factory.begin() as session:
        assert (
            create_initial_candidate(
                session, worker_id=worker_id, credential=credential, instance_id=new_instance
            )
            == new_ref
        )
        assert session.get(Worker, worker_id).instance_lease_until == expires
    proof = issue_committed_candidate_proof(
        session_factory, expected=new_ref, credential=credential, authority=authority
    )
    with session_factory.begin() as session:
        assert validate_current_proof(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=new_instance,
            generation=2,
            token=proof.token,
            authority=authority,
        )
        with pytest.raises(LeaseCandidateError):
            _validate(session, worker_id, credential, old_instance, old_proof.token, authority)
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=old_instance
        )
    with session_factory.begin() as session:
        now = session.scalar(select(func.pg_catalog.clock_timestamp()))
        xid = session.scalar(select(func.pg_catalog.txid_current()))
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.instance_lease_until = candidate.nominal_until = now - timedelta(seconds=1)
        candidate.guard_until = candidate.proof_before = now - timedelta(seconds=1)
        candidate.writer_xid = xid
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=old_instance
        )
    with session_factory.begin() as session:
        third = create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=uuid4()
        )
        assert third.generation == 3


def test_competing_takeovers_only_create_one_new_generation(session_factory, authority):
    worker_id, credential, _, _, _ = _initial(session_factory, authority)
    with session_factory.begin() as session:
        now = session.scalar(select(func.pg_catalog.clock_timestamp()))
        xid = session.scalar(select(func.pg_catalog.txid_current()))
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.instance_lease_until = candidate.nominal_until = now - timedelta(seconds=1)
        candidate.guard_until = candidate.proof_before = now - timedelta(seconds=1)
        candidate.writer_xid = xid
    barrier = threading.Barrier(2)

    def compete(instance):
        barrier.wait(timeout=5)
        try:
            with session_factory.begin() as session:
                return create_initial_candidate(
                    session, worker_id=worker_id, credential=credential, instance_id=instance
                )
        except LeaseCandidateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, (uuid4(), uuid4())))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and winners[0].generation == 2
    with session_factory.begin() as session:
        assert session.get(Worker, worker_id).instance_id == winners[0].instance_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkerInstanceHistory)
                .where(WorkerInstanceHistory.worker_id == worker_id)
            )
            == 2
        )


def test_takeover_waits_for_late_renewal_commit_and_its_candidate(session_factory, authority):
    worker_id, credential, old_instance, _, old_proof = _initial(
        session_factory, authority, short=True
    )
    entered = threading.Event()
    with session_factory() as writer:
        late = _renew(writer, worker_id, credential, old_instance, old_proof.token, authority)

        def takeover():
            entered.set()
            with session_factory.begin() as session:
                return create_initial_candidate(
                    session, worker_id=worker_id, credential=credential, instance_id=uuid4()
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(takeover)
            assert entered.wait(2)
            time.sleep(1.8)
            assert not pending.done()
            writer.commit()
            with pytest.raises(LeaseCandidateError):
                pending.result(timeout=5)
    with pytest.raises(LeaseCandidateError):
        issue_committed_candidate_proof(
            session_factory, expected=late, credential=credential, authority=authority
        )
    with session_factory.begin() as session:
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        now = session.scalar(select(func.pg_catalog.clock_timestamp()))
        assert worker.instance_id == old_instance and candidate.candidate_id == late.candidate_id
        assert candidate.guard_until <= now < candidate.nominal_until


def test_timely_successor_invalidates_old_proof_and_accepts_new(session_factory, authority):
    worker_id, credential, instance, initial, proof = _initial(session_factory, authority)
    with session_factory.begin() as session:
        successor = _renew(session, worker_id, credential, instance, proof.token, authority)
    assert (
        successor.revision == initial.revision + 1
        and successor.candidate_id != initial.candidate_id
    )
    newer = issue_committed_candidate_proof(
        session_factory, expected=successor, credential=credential, authority=authority
    )
    with session_factory.begin() as session:
        with pytest.raises(LeaseCandidateError):
            _validate(session, worker_id, credential, instance, proof.token, authority)
        assert (
            _validate(session, worker_id, credential, instance, newer.token, authority).revision
            == 2
        )
    other_key = ProofAuthority(authority.cluster_id, authority.authority_epoch, b"x" * 32)
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        _validate(session, worker_id, credential, instance, newer.token, other_key)


@pytest.mark.parametrize("second_renewal", [False, True])
def test_candidate_committing_after_previous_guard_cannot_be_proven(
    session_factory, authority, second_renewal
):
    worker_id, credential, instance, _, proof = _initial(session_factory, authority, short=True)
    if second_renewal:
        with session_factory.begin() as session:
            timely = _renew(session, worker_id, credential, instance, proof.token, authority)
        proof = issue_committed_candidate_proof(
            session_factory, expected=timely, credential=credential, authority=authority
        )
    with session_factory() as session:
        late = _renew(session, worker_id, credential, instance, proof.token, authority)
        session.execute(select(func.pg_sleep(1.8)))
        session.commit()
    with pytest.raises(LeaseCandidateError):
        issue_committed_candidate_proof(
            session_factory, expected=late, credential=credential, authority=authority
        )
    with session_factory.begin() as session:
        with pytest.raises(LeaseCandidateError):
            _validate(session, worker_id, credential, instance, proof.token, authority)
        if second_renewal:
            node = session.get(Worker, worker_id, with_for_update=True)
            candidate = session.get(WorkerLeaseCandidateV4, worker_id)
            sampled_at = session.scalar(select(func.clock_timestamp()))
            assert candidate.proof_before <= sampled_at < candidate.guard_until
            assert not has_current_committed_lease(session, node, now=sampled_at)
        # Neither an unproven first nor second renewal permits takeover while
        # its nominal deadline remains in the future.
        assert not observe_expired_candidate(session, worker_id=worker_id)


def test_candidate_reader_waits_for_commit_then_rejects_late_observation(
    session_factory, authority
):
    worker_id, credential, instance, _, proof = _initial(session_factory, authority, short=True)
    with session_factory.begin() as session:
        timely = _renew(session, worker_id, credential, instance, proof.token, authority)
    proof = issue_committed_candidate_proof(
        session_factory, expected=timely, credential=credential, authority=authority
    )
    proof_entered, expiry_entered = threading.Event(), threading.Event()
    with session_factory() as writer:
        late = _renew(writer, worker_id, credential, instance, proof.token, authority)

        def observe():
            proof_entered.set()
            return issue_committed_candidate_proof(
                session_factory, expected=late, credential=credential, authority=authority
            )

        def expiration():
            expiry_entered.set()
            with session_factory.begin() as session:
                return observe_expired_candidate(session, worker_id=worker_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(observe)
            pending_expiry = pool.submit(expiration)
            assert proof_entered.wait(2) and expiry_entered.wait(2)
            time.sleep(1.8)
            assert not pending.done() and not pending_expiry.done()
            writer.commit()
            with pytest.raises(LeaseCandidateError):
                pending.result(timeout=5)
            assert pending_expiry.result(timeout=5) is False


def test_commit_itself_stalls_past_guard_and_prevents_proof(session_factory, authority):
    worker_id, credential, instance, _, proof = _initial(session_factory, authority, short=True)
    with session_factory.begin() as session:
        timely = _renew(session, worker_id, credential, instance, proof.token, authority)
    proof = issue_committed_candidate_proof(
        session_factory, expected=timely, credential=credential, authority=authority
    )
    with session_factory.begin() as session:
        session.execute(
            text(
                "CREATE FUNCTION dlr129_delay_lease_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.8); RETURN NULL; END $$"
            )
        )
        session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER dlr129_delay_lease_commit "
                "AFTER UPDATE ON worker_lease_candidates_v4 "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION dlr129_delay_lease_commit()"
            )
        )
    try:
        with session_factory() as writer:
            late = _renew(writer, worker_id, credential, instance, proof.token, authority)
            old_guard = writer.get(WorkerLeaseCandidateV4, worker_id).proof_before
            writer_pid = writer.scalar(select(func.pg_backend_pid()))
            with ThreadPoolExecutor(max_workers=3) as pool:
                commit = pool.submit(writer.commit)
                issued = pool.submit(
                    issue_committed_candidate_proof,
                    session_factory,
                    expected=late,
                    credential=credential,
                    authority=authority,
                )

                def expiration():
                    with session_factory.begin() as session:
                        return observe_expired_candidate(session, worker_id=worker_id)

                expiry = pool.submit(expiration)
                end = time.monotonic() + 5
                saw_commit_wait = False
                while time.monotonic() < end:
                    with session_factory() as observer:
                        now = observer.scalar(select(func.clock_timestamp()))
                        wait = observer.scalar(
                            text("SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": writer_pid},
                        )
                    if wait == "PgSleep":
                        saw_commit_wait = True
                    if now >= old_guard:
                        break
                    time.sleep(0.02)
                assert saw_commit_wait and now >= old_guard
                assert not commit.done() and not issued.done() and not expiry.done()
                commit.result(timeout=8)
                with pytest.raises(LeaseCandidateError):
                    issued.result(timeout=5)
                assert expiry.result(timeout=5) is False
    finally:
        with session_factory.begin() as session:
            session.execute(
                text("DROP TRIGGER dlr129_delay_lease_commit ON worker_lease_candidates_v4")
            )
            session.execute(text("DROP FUNCTION dlr129_delay_lease_commit()"))


def test_competing_heartbeats_cannot_reuse_one_proof(session_factory, authority):
    worker_id, credential, instance, _, proof = _initial(session_factory, authority)
    barrier = threading.Barrier(2)

    def compete():
        barrier.wait(timeout=5)
        try:
            with session_factory.begin() as session:
                return _renew(session, worker_id, credential, instance, proof.token, authority)
        except LeaseCandidateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10) for future in (pool.submit(compete), pool.submit(compete))
        ]
    winners = [value for value in results if value is not None]
    assert len(winners) == 1 and winners[0].revision == 2
    with session_factory.begin() as session:
        assert (
            session.get(WorkerLeaseCandidateV4, worker_id).candidate_id == winners[0].candidate_id
        )


def test_tuple_change_or_credential_revocation_rejects_proof(session_factory, authority):
    worker_id, credential, instance, ref, proof = _initial(session_factory, authority)
    with session_factory.begin() as session:
        # Simulate preexisting corruption even if a privileged actor bypassed
        # the database's candidate writer-xid trigger.
        session.execute(
            text(
                "ALTER TABLE worker_lease_candidates_v4 "
                "DISABLE TRIGGER worker_candidate_xid_guard_v4"
            )
        )
        session.execute(
            text(
                "ALTER TABLE worker_lease_candidates_v4 "
                "DISABLE TRIGGER worker_candidate_xid_postguard_v4"
            )
        )
        session.execute(
            update(WorkerLeaseCandidateV4)
            .where(WorkerLeaseCandidateV4.worker_id == worker_id)
            .values(revision=WorkerLeaseCandidateV4.revision)
        )
        session.execute(
            text(
                "ALTER TABLE worker_lease_candidates_v4 "
                "ENABLE ALWAYS TRIGGER worker_candidate_xid_guard_v4"
            )
        )
        session.execute(
            text(
                "ALTER TABLE worker_lease_candidates_v4 "
                "ENABLE ALWAYS TRIGGER worker_candidate_xid_postguard_v4"
            )
        )
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        _validate(session, worker_id, credential, instance, proof.token, authority)
    with pytest.raises(LeaseCandidateError):
        issue_committed_candidate_proof(
            session_factory, expected=ref, credential=credential, authority=authority
        )

    other_id, other_credential, other_instance, _, other_proof = _initial(
        session_factory, authority
    )
    with session_factory.begin() as session:
        session.execute(
            update(WorkerCredential)
            .where(WorkerCredential.worker_id == other_id, WorkerCredential.kind == "runtime")
            .values(revoked_at=func.clock_timestamp())
        )
    with session_factory.begin() as session, pytest.raises(LeaseCandidateError):
        _validate(session, other_id, other_credential, other_instance, other_proof.token, authority)


def test_generation_inconsistent_with_immutable_history_cannot_be_signed(
    session_factory, authority
):
    worker_id, credential, instance, ref, _ = _initial(session_factory, authority)
    with session_factory.begin() as session:
        xid = session.scalar(select(func.txid_current()))
        # Preserve the fail-closed corruption case beyond the instance FK.
        session.execute(text("SET LOCAL session_replication_role = replica"))
        worker = session.get(Worker, worker_id)
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.instance_generation = 2
        candidate.instance_generation = 2
        candidate.writer_xid = xid
        session.flush()
        session.execute(text("SET LOCAL session_replication_role = origin"))
    inconsistent = CandidateRef(worker_id, instance, 2, ref.revision, ref.candidate_id)
    with pytest.raises(LeaseCandidateError):
        issue_committed_candidate_proof(
            session_factory, expected=inconsistent, credential=credential, authority=authority
        )
    with session_factory.begin() as session:
        assert not observe_expired_candidate(session, worker_id=worker_id)


def test_candidate_reference_rejects_boolean_identity():
    with pytest.raises(LeaseCandidateError):
        CandidateRef(True, uuid4(), 1, 1, uuid4())


def test_candidate_storage_requires_every_named_trigger_and_rejects_inherited_delete(
    session_factory,
):
    from dlr.control.services.worker_candidate_storage_preflight_v4 import inspect_candidate_storage

    suffix = uuid4().hex
    runtime = f"posture_runtime_{suffix}"
    group = f"posture_group_{suffix}"
    with session_factory() as session:
        # Roles, grants and trigger changes exist only inside this test transaction.
        database = session.get_bind().dialect.identifier_preparer.quote(
            session.scalar(text("SELECT current_database()"))
        )
        session.execute(text(f"CREATE ROLE {runtime} NOLOGIN"))
        session.execute(text(f"CREATE ROLE {group} NOLOGIN"))
        session.execute(text(f"GRANT {group} TO {runtime}"))
        session.execute(text(f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC"))
        session.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        session.execute(text(f"GRANT USAGE ON SCHEMA public TO {runtime}"))
        session.execute(
            text(
                "GRANT SELECT ON alembic_version, worker_placement_cutover, "
                f"worker_capacity_verifiers TO {runtime}"
            )
        )
        session.execute(text(f"GRANT SELECT, INSERT ON execution_placement_actions TO {runtime}"))
        session.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE ON worker_lease_candidates_v4, "
                "attempt_lease_candidates_v4, attempt_result_intents_v4, "
                f"worker_cleanup_intents_v4, worker_maintenance_operations TO {runtime}"
            )
        )

        def posture():
            session.execute(text(f"SET LOCAL SESSION AUTHORIZATION {runtime}"))
            result = inspect_candidate_storage(session, expected_schema="public")
            session.execute(text("SET LOCAL SESSION AUTHORIZATION DEFAULT"))
            return result.state

        assert posture() == "observed"
        session.execute(
            text(
                "ALTER TRIGGER dlr_worker_maintenance_immutable ON worker_maintenance_operations "
                "RENAME TO unexpected_audit_trigger"
            )
        )
        assert posture() == "blocked"
        session.execute(
            text(
                "ALTER TRIGGER unexpected_audit_trigger ON worker_maintenance_operations "
                "RENAME TO dlr_worker_maintenance_immutable"
            )
        )
        session.execute(text(f"GRANT DELETE ON worker_cleanup_intents_v4 TO {group}"))
        assert posture() == "blocked"
