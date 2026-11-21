"""Selected real PostgreSQL regressions for the production Attempt candidate service."""

import hashlib
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    AttemptLeaseCandidateV4,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerCredential,
    WorkerInstanceHistory,
    WorkerLeaseCandidateV4,
)
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import attempt as attempt_service
from dlr.control.services.attempt_lease_candidates_v4 import (
    AttemptLeaseCandidateError,
    _issue_in_session,
    issue_attempt_candidate_proof,
    observe_expired_attempt_candidate,
    stage_attempt_renewal,
    stage_initial_attempt_candidate,
    validate_current_attempt_proof,
)
from dlr.control.services.worker_lease_candidates_v4 import (
    CandidateRef,
    ProofAuthority,
    issue_committed_candidate_proof,
)
from test_adapters import create_adapter, save_version
from test_issue127_c0 import _claim, _register_worker


def _fixture(
    api_client: TestClient, factory: sessionmaker[Session], *, legacy: bool = False
) -> dict:
    worker = _register_worker(api_client, "v4-attempt-owner")
    adapter = create_adapter(api_client, name="v4-attempt-adapter")
    save_version(api_client, adapter["id"])
    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 202, response.text
    claim = _claim(api_client, worker["id"])
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    secret, instance_id = "dlrw4_" + secrets.token_hex(32), uuid.uuid4()
    with factory.begin() as session:
        row = session.get(Worker, worker["id"])
        attempt = session.get(ExecutionAttempt, payload["attempt_id"])
        assert row is not None and attempt is not None
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        # Synthetic persisted v4 binding; no unimplemented enrollment or
        # Claim route is presented as live by this isolated service test.
        credential_id = None
        if not legacy:
            row.identity_enrolled_at = now
            row.protocol_version = 4
            row.instance_id = instance_id
            row.instance_generation = 1
            row.instance_lease_until = now + timedelta(minutes=5)
            row.scheduling_state = "draining"
            attempt.worker_identity_id = row.identity_id
            attempt.worker_instance_id = instance_id
            attempt.worker_instance_generation = 1
            credential = WorkerCredential(
                worker_id=row.id,
                kind="runtime",
                secret_digest=hashlib.sha256(bytes.fromhex(secret.removeprefix("dlrw4_"))).digest(),
            )
            session.add(credential)
            session.flush()
            credential_id = credential.id
    return {
        "worker_id": worker["id"],
        "execution_id": response.json()["id"],
        "attempt_id": payload["attempt_id"],
        "adapter_id": adapter["id"],
        "instance_id": instance_id,
        "instance_generation": 1,
        "credential": secret,
        "credential_id": credential_id,
        "action": AttemptActionBody(
            attempt_id=payload["attempt_id"],
            fencing_token=payload["fencing_token"],
            claim_token=payload["claim_token"],
        ),
    }


def _setup(api_client, factory, *, short=False, short_seconds=None):
    facts = _fixture(api_client, factory)
    authority = ProofAuthority(uuid4(), uuid4(), b"s" * 32)
    candidate_id = uuid4()
    with factory.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        worker = session.get(Worker, facts["worker_id"])
        worker.instance_lease_until = now + timedelta(seconds=30)
        session.add(
            WorkerInstanceHistory(
                worker_id=worker.id, instance_id=facts["instance_id"], generation=1
            )
        )
        session.flush()
        session.add(
            WorkerLeaseCandidateV4(
                worker_id=worker.id,
                worker_identity_id=worker.identity_id,
                instance_id=facts["instance_id"],
                instance_generation=1,
                revision=1,
                candidate_id=candidate_id,
                nominal_until=worker.instance_lease_until,
                guard_until=worker.instance_lease_until,
                proof_before=worker.instance_lease_until,
                writer_xid=session.scalar(select(func.txid_current())),
            )
        )
    instance_ref = CandidateRef(facts["worker_id"], facts["instance_id"], 1, 1, candidate_id)
    instance_proof = issue_committed_candidate_proof(
        factory, expected=instance_ref, credential=facts["credential"], authority=authority
    ).token
    with factory.begin() as session:
        execution, _adapter, old, slot = attempt_service._lock_attempt_context(
            session, facts["worker_id"], facts["attempt_id"], existing_only=True
        )
        old.status = "worker_lost"
        slot.active_attempt_id = None
        slot.lease_expires_at = None
        session.flush()
        now = session.scalar(select(func.clock_timestamp()))
        new = ExecutionAttempt(
            execution_id=execution.id,
            adapter_id=old.adapter_id,
            attempt_no=old.attempt_no + 1,
            worker_id=old.worker_id,
            worker_identity_id=old.worker_identity_id,
            worker_instance_id=old.worker_instance_id,
            worker_instance_generation=old.worker_instance_generation,
            fencing_token=slot.fencing_token + 1,
            claim_token_hash=old.claim_token_hash,
            cleanup_token_hash=old.cleanup_token_hash,
            lease_expires_at=now
            + timedelta(
                seconds=short_seconds if short_seconds is not None else 1.5 if short else 20
            ),
            status="claimed",
            claimed_at=now,
        )
        ref = stage_initial_attempt_candidate(
            session,
            execution=execution,
            slot=slot,
            attempt=new,
            credential=facts["credential"],
            instance_proof=instance_proof,
            authority=authority,
        )
        execution.attempt_count = new.attempt_no
        slot.active_attempt_id = ref.attempt_id
        slot.fencing_token = ref.fencing_token
        slot.lease_expires_at = new.lease_expires_at
        session.flush()
        action = AttemptActionBody(
            attempt_id=ref.attempt_id,
            fencing_token=ref.fencing_token,
            claim_token=facts["action"].claim_token,
        )
        with pytest.raises(AttemptLeaseCandidateError):
            _issue_in_session(
                session,
                expected=ref,
                credential=facts["credential"],
                instance_proof=instance_proof,
                action=action,
                authority=authority,
            )
    issued = issue_attempt_candidate_proof(
        factory,
        expected=ref,
        credential=facts["credential"],
        instance_proof=instance_proof,
        action=action,
        authority=authority,
    )
    return facts, authority, instance_proof, ref, action, issued.token


def _args(facts, authority, instance_proof, ref, action):
    return dict(
        worker_id=facts["worker_id"],
        attempt_id=ref.attempt_id,
        credential=facts["credential"],
        instance_id=facts["instance_id"],
        generation=1,
        instance_proof=instance_proof,
        action=action,
        authority=authority,
    )


@pytest.fixture
def successful_result_context(api_client, session_factory):
    from dlr.control.schemas.reliable_runtime import AttemptResultBody
    from dlr.control.services import attempt_result_intent_v4 as intents

    facts, authority, instance_proof, ref, action, attempt_proof = _setup(
        api_client, session_factory
    )
    result = AttemptResultBody(**action.model_dump(), status="succeeded", output={"ok": True})
    return intents.ResultIntentContext(
        facts["worker_id"],
        ref.attempt_id,
        facts["credential"],
        facts["instance_id"],
        1,
        instance_proof,
        attempt_proof,
        result,
        authority,
    )


@pytest.mark.parametrize("change", ["clock", "credential", "expiry", "transaction"])
def test_result_recheck_keeps_current_time_locked_rows_and_transaction(
    successful_result_context, session_factory, monkeypatch, change
):
    from dlr.control.services import attempt_result_intent_v4 as intents

    context = successful_result_context
    with session_factory() as session:
        checks = intents._IntentChecks(session, context)
        assert checks.versions is not None
        checks.recheck()
        if change == "clock":
            monkeypatch.setattr(
                intents.worker_availability, "current_time", lambda _: checks.instance.guard_until
            )
        elif change == "credential":
            session.execute(
                text(
                    "UPDATE worker_credentials SET revoked_at = clock_timestamp() "
                    "WHERE worker_id = :worker AND kind = 'runtime'"
                ),
                {"worker": context.worker_id},
            )
        elif change == "expiry":
            expiry = min(checks.instance.guard_until, checks.lease.guard_until) - timedelta(
                seconds=1
            )
            session.execute(
                text(
                    "UPDATE worker_credentials SET expires_at = :expiry "
                    "WHERE worker_id = :worker AND kind = 'runtime'"
                ),
                {"worker": context.worker_id, "expiry": expiry},
            )
            checks.recheck()
            monkeypatch.setattr(intents.worker_availability, "current_time", lambda _: expiry)
        else:
            session.rollback()
        with pytest.raises(intents.ResultIntentError):
            checks.recheck()


@pytest.mark.parametrize("phase", ["stage", "observe"])
def test_result_checks_expiry_after_the_intent_write_or_lock(
    successful_result_context, session_factory, monkeypatch, phase
):
    from dlr.control.services import attempt_result_intent_v4 as intents

    ctx = successful_result_context
    if phase == "observe":
        with session_factory.begin() as session:
            ref = intents.stage_result_intent(session, ctx, max_body_bytes=100_000)
    with session_factory() as session:
        expires = session.get(WorkerLeaseCandidateV4, ctx.worker_id).guard_until
        blocked = False
        original_clock = intents.worker_availability.current_time
        monkeypatch.setattr(
            intents.worker_availability,
            "current_time",
            lambda s: expires if blocked else original_clock(s),
        )

        def after_sql(_conn, _cursor, statement, *_args):
            nonlocal blocked
            sql = statement.lower()
            if phase == "stage":
                blocked |= "insert into attempt_result_intents_v4" in sql
            else:
                blocked |= "from attempt_result_intents_v4" in sql and "for update" in sql

        connection = session.connection()
        event.listen(connection, "after_cursor_execute", after_sql)
        try:
            with pytest.raises(intents.ResultIntentError):
                if phase == "stage":
                    intents.stage_result_intent(session, ctx, max_body_bytes=100_000)
                else:
                    intents.observe_result_in_session(
                        session, ctx, ref, max_body_bytes=100_000, observation_nonce=uuid4()
                    )
            assert blocked
        finally:
            event.remove(connection, "after_cursor_execute", after_sql)


def test_unchanged_result_locks_are_acquired_once_per_transaction(
    successful_result_context, session_factory, monkeypatch
):
    from unittest.mock import Mock

    from dlr.control.services import attempt_result_intent_v4 as intents

    snapshot = Mock(wraps=intents._snapshot)
    monkeypatch.setattr(intents, "_snapshot", snapshot)
    ctx = successful_result_context
    with session_factory.begin() as session:
        ref = intents.stage_result_intent(session, ctx, max_body_bytes=100_000)
    assert snapshot.call_count == 1
    with session_factory.begin() as session:
        observed = intents.observe_result_in_session(
            session, ctx, ref, max_body_bytes=100_000, observation_nonce=uuid4()
        )
    assert observed.token and snapshot.call_count == 2


def test_finalize_rechecks_proof_after_intent_lock_before_terminal_write(
    successful_result_context, session_factory, monkeypatch
):
    from unittest.mock import Mock

    from dlr.control.models import AttemptResultIntentV4
    from dlr.control.services import attempt_result_finalize_v4 as finalizer
    from dlr.control.services import attempt_result_intent_v4 as intents

    ctx = successful_result_context
    with session_factory.begin() as session:
        ref = intents.stage_result_intent(session, ctx, max_body_bytes=100_000)
    with session_factory() as session:
        proof = intents.observe_result_in_session(
            session, ctx, ref, max_body_bytes=100_000, observation_nonce=uuid4()
        )
    before_attestation = proof.observed_at - timedelta(microseconds=1)
    # The case exercises ownership in real PG transactions. The deployment
    # fence itself is exercised by the restricted-role integration tests.
    gate = Mock(spec=["check"])
    control = finalizer.ResultControlFence(Mock(), uuid4(), "fixture", gate.check)
    monkeypatch.setattr(finalizer, "_control_authority", lambda *_: (gate, ctx.authority))
    locked = False

    def after_sql(_conn, _cursor, statement, *_args):
        nonlocal locked
        sql = statement.lower()
        locked |= "from attempt_result_intents_v4" in sql and "for update" in sql

    def finalize():
        finalizer.finalize_unconfirmed_result(
            session_factory, ctx, ref, proof, max_body_bytes=100_000, _control=control
        )

    engine = session_factory.kw["bind"]
    with monkeypatch.context() as patch_clock:
        clock = intents.worker_availability.current_time
        verify = Mock(wraps=finalizer.verify_result_attestation)
        finish = Mock(wraps=finalizer.attempt.finish_attempt)
        patch_clock.setattr(finalizer, "verify_result_attestation", verify)
        patch_clock.setattr(finalizer.attempt, "finish_attempt", finish)
        patch_clock.setattr(
            intents.worker_availability,
            "current_time",
            lambda s: before_attestation if locked else clock(s),
        )
        event.listen(engine, "after_cursor_execute", after_sql)
        try:
            with pytest.raises(finalizer.ResultFinalizeRejected):
                finalize()
        finally:
            event.remove(engine, "after_cursor_execute", after_sql)
        verify.assert_called_once()
        assert verify.call_args.kwargs["now"] == before_attestation
        finish.assert_not_called()
    with session_factory() as session:
        assert locked
        assert (
            session.get(ExecutionAttempt, ctx.attempt_id).status
            not in attempt_service.TERMINAL_ATTEMPT_STATUSES
        )
        assert session.get(AttemptResultIntentV4, ctx.attempt_id).finalization_id is None
    with pytest.raises(finalizer.ResultFinalizeUnknown):
        finalize()
    with session_factory() as session:
        assert session.get(ExecutionAttempt, ctx.attempt_id).status == "succeeded"
        assert session.get(AttemptResultIntentV4, ctx.attempt_id).finalization_id is not None


def test_cancel_before_attempt_renewal_does_not_extend_lease(api_client, session_factory):
    facts, authority, instance, initial, action, old = _setup(api_client, session_factory)
    args = _args(facts, authority, instance, initial, action)
    with session_factory.begin() as session:
        execution = session.get(Execution, facts["execution_id"])
        execution.cancel_requested = True
        previous = session.get(ExecutionAttempt, initial.attempt_id).lease_expires_at
    with session_factory.begin() as session, pytest.raises(AttemptLeaseCandidateError):
        stage_attempt_renewal(session, attempt_proof=old, **args)
    with session_factory.begin() as session:
        assert session.get(ExecutionAttempt, initial.attempt_id).lease_expires_at == previous
        assert session.get(AttemptLeaseCandidateV4, initial.attempt_id).revision == 1
        # Cancellation does not invalidate the old proof for a separately
        # authorized result/cleanup path while its existing lease remains live.
        assert validate_current_attempt_proof(session, attempt_proof=old, **args).revision == 1


def test_cancel_after_attempt_candidate_commit_blocks_proof(api_client, session_factory):
    facts, authority, instance, initial, action, old = _setup(api_client, session_factory)
    args = _args(facts, authority, instance, initial, action)
    with session_factory.begin() as session:
        successor = stage_attempt_renewal(session, attempt_proof=old, **args)
    with session_factory.begin() as session:
        session.get(Execution, facts["execution_id"]).cancel_requested = True
    with pytest.raises(AttemptLeaseCandidateError):
        issue_attempt_candidate_proof(
            session_factory,
            expected=successor,
            credential=facts["credential"],
            instance_proof=instance,
            action=action,
            authority=authority,
        )
    with session_factory.begin() as session, pytest.raises(AttemptLeaseCandidateError):
        validate_current_attempt_proof(session, attempt_proof=old, **args)


def test_second_attempt_renewal_commit_after_guard_cannot_be_proven(api_client, session_factory):
    facts, authority, instance, initial, action, old = _setup(
        api_client, session_factory, short=True
    )
    args = _args(facts, authority, instance, initial, action)
    with session_factory.begin() as session:
        timely = stage_attempt_renewal(session, attempt_proof=old, **args)
    current = issue_attempt_candidate_proof(
        session_factory,
        expected=timely,
        credential=facts["credential"],
        instance_proof=instance,
        action=action,
        authority=authority,
    ).token
    with session_factory.begin() as session:
        session.execute(
            text(
                "CREATE FUNCTION dlr129_delay_candidate_attempt_commit() "
                "RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN PERFORM pg_sleep(1.8); RETURN NULL; END $$"
            )
        )
        session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER dlr129_delay_candidate_attempt_commit "
                "AFTER UPDATE ON attempt_lease_candidates_v4 DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION dlr129_delay_candidate_attempt_commit()"
            )
        )
    try:
        with session_factory() as writer:
            late = stage_attempt_renewal(writer, attempt_proof=current, **args)
            commit_started, observe_started, issue_started = Event(), Event(), Event()

            def commit():
                commit_started.set()
                writer.commit()

            def observe():
                observe_started.set()
                with session_factory.begin() as session:
                    return observe_expired_attempt_candidate(
                        session, worker_id=facts["worker_id"], attempt_id=initial.attempt_id
                    )

            def issue():
                issue_started.set()
                return issue_attempt_candidate_proof(
                    session_factory,
                    expected=late,
                    credential=facts["credential"],
                    instance_proof=instance,
                    action=action,
                    authority=authority,
                )

            with ThreadPoolExecutor(max_workers=3) as pool:
                committing = pool.submit(commit)
                observing = pool.submit(observe)
                issuing = pool.submit(issue)
                assert commit_started.wait(2) and observe_started.wait(2) and issue_started.wait(2)
                time.sleep(0.2)
                assert not committing.done() and not observing.done() and not issuing.done()
                committing.result(timeout=5)
                assert not observing.result(timeout=5)
                with pytest.raises(AttemptLeaseCandidateError):
                    issuing.result(timeout=5)
    finally:
        with session_factory.begin() as session:
            session.execute(
                text(
                    "DROP TRIGGER dlr129_delay_candidate_attempt_commit "
                    "ON attempt_lease_candidates_v4"
                )
            )
            session.execute(text("DROP FUNCTION dlr129_delay_candidate_attempt_commit()"))
    with session_factory.begin() as session:
        with pytest.raises(AttemptLeaseCandidateError):
            validate_current_attempt_proof(session, attempt_proof=current, **args)
        assert not observe_expired_attempt_candidate(
            session, worker_id=facts["worker_id"], attempt_id=initial.attempt_id
        )
