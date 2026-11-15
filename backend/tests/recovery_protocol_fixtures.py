"""W1-B PostgreSQL/API protocol assertions; no physical-stop qualification.

Reuse the existing enrolled restricted-role fixture rather than another test
deployment. Expiry is synthetic DB setup; real stopped-container acceptance is
separate and must use actual new-version local checkpoints.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from dlr.common.recovery_observation_v4 import sign_observation, verify_observation
from dlr.control.models import (
    AdapterExecutionSlot,
    AttemptResultIntentV4,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerCleanupIntentV4,
    WorkerLeaseCandidateV4,
    WorkerMaintenanceOperation,
)
from dlr.worker.lease_session_v4 import WorkerLeaseSession
from dlr.worker.recovery_checkpoint_v4 import digest
from dlr.worker.recovery_execution_v4 import RecoveryExchange, RecoveryState


def _expire(factory, worker_id):
    """Fixture only: the prior memory-only lease has already been retired."""
    with factory.begin() as session:
        now = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        worker = session.get(Worker, worker_id)
        row = session.get(WorkerLeaseCandidateV4, worker_id)
        worker.instance_lease_until = now
        row.nominal_until = row.guard_until = row.proof_before = now
        row.writer_xid = session.scalar(select(func.txid_current()))


def assert_controlled_recovery(
    client,
    factory,
    admin_factory,
    worker_id,
    old,
    claimed,
    trust_file,
    public_key,
    *,
    terminal=None,
    pending=None,
):
    task = claimed.payload
    assert task is not None and old._claims is not None
    original_id, original_gen = old._instance_id, old._claims.generation
    credential, credential_record_id = old._credential, old._credential_record_id
    old_headers = {
        "Authorization": f"Bearer {credential}",
        "X-DLR-Worker-Instance": str(original_id),
        "X-DLR-Worker-Generation": str(original_gen),
        "X-DLR-Worker-Lease-Proof": old._proof,
        "X-DLR-Cleanup-Token": task.cleanup_token,
    }
    if terminal is None:
        # Leave a genuine committed but not finalized old cleanup intent.
        staged = client.post(
            f"/api/workers/{worker_id}/v4/executions/{task.execution_id}/cleanup/stage",
            headers={
                **old_headers,
                "X-DLR-Cleanup-Intent": str(uuid4()),
                "X-DLR-Cleanup-Observation-Nonce": str(uuid4()),
            },
            json={
                "status": "completed",
                "attempt_id": task.attempt_id,
                "fencing_token": task.fencing_token,
                "worker_identity_id": str(old._identity_id),
                "original_instance_id": str(original_id),
                "original_instance_generation": original_gen,
            },
        )
        assert staged.status_code == 200, staged.text
    with factory() as session:
        result = session.get(AttemptResultIntentV4, task.attempt_id)
        execution = session.get(Execution, task.execution_id)
        worker = session.get(Worker, worker_id)
        expected_business = (
            execution.status,
            execution.output,
            execution.error,
            execution.ended_at,
            execution.attempt_count,
        )
        intent_id, mac, finalization = (
            str(result.intent_id),
            bytes(result.body_mac).hex(),
            result.finalization_id,
        )
        version = worker.maintenance_version
    # Protocol metadata only: this does not qualify any physical checkpoint.
    prepared = {
        "execution_id": task.execution_id,
        "attempt_id": task.attempt_id,
        "fencing_token": task.fencing_token,
        "instance_id": str(original_id),
        "instance_generation": original_gen,
    }
    binding = {
        "execution_id": task.execution_id,
        "attempt": {
            "attempt_id": task.attempt_id,
            "fencing_token": task.fencing_token,
            "worker_identity_id": str(old._identity_id),
            "original_instance_id": str(original_id),
            "original_instance_generation": original_gen,
        },
        "old_container_id": "1" * 64,
        "deployment_record_id": str(uuid4()),
        "evidence_sha256": "2" * 64,
        "boot_id": str(uuid4()),
        "prepared_sha256": digest(prepared),
    }
    request = {"request_id": str(uuid4()), "expected_version": version, "binding": binding}
    stopped = {"intent_id": intent_id, "body_mac": mac}
    if terminal is not None:
        request.update(
            mode="settle_stopped_uncommitted",
            settlement={
                "stopped_sha256": digest(stopped),
                "result_intent_id": intent_id,
                "body_mac": mac,
                "attempt_status": terminal[0],
                "execution_status": terminal[1],
                "acknowledgement": "result_uncommitted_outcome_unknown",
            },
        )
    admin_endpoint = f"/api/workers/{worker_id}/v4/recovery-intent"
    assert (
        client.post(
            admin_endpoint, json=request, headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )
    assert client.post(admin_endpoint, json=request).status_code == 409  # Still-live old lease.
    old.retire()
    _expire(factory, worker_id)
    if terminal is not None:
        _terminalize_uncommitted(
            client, factory, worker_id, task, terminal, admin_endpoint, request, pending
        )
        with factory() as session:
            execution = session.get(Execution, task.execution_id)
            expected_business = (
                execution.status,
                execution.output,
                execution.error,
                execution.ended_at,
                execution.attempt_count,
            )
    if terminal is None:
        # A result that won commit cannot be demoted to an unknown C outcome.
        result_first = {
            **request,
            "mode": "settle_stopped_uncommitted",
            "settlement": {
                "stopped_sha256": digest(stopped),
                "result_intent_id": intent_id,
                "body_mac": mac,
                "attempt_status": "worker_lost",
                "execution_status": "dead_letter",
                "acknowledgement": "result_uncommitted_outcome_unknown",
            },
        }
        assert client.post(admin_endpoint, json=result_first).status_code == 409
    lost_reply = {}
    if terminal == ("cancelled", "cancelled"):
        from dlr.control.services import worker_recovery_v4

        original_request = worker_recovery_v4.request_recovery
        original_commit = factory.class_.commit

        def mark_recovery(session, *args):
            result = original_request(session, *args)
            session.info["lose_C_admin_reply"] = True
            lost_reply.update(result)
            return result

        def commit_then_disconnect(session):
            lose = session.info.pop("lose_C_admin_reply", False)
            original_commit(session)
            if lose:
                raise ConnectionError("committed C response lost")

        with (
            patch.object(worker_recovery_v4, "request_recovery", mark_recovery),
            patch.object(factory.class_, "commit", commit_then_disconnect),
        ):
            assert client.post(admin_endpoint, json=request).status_code == 503
    created = client.post(admin_endpoint, json=request)
    assert created.status_code == 200, created.text
    if lost_reply:
        assert created.json() == lost_reply
    assert created.json()["host_evidence"] == "operator_attestation"
    assert client.post(admin_endpoint, json=request).json() == created.json()
    conflicting = deepcopy(request)
    conflicting["binding"]["evidence_sha256"] = "4" * 64
    assert client.post(admin_endpoint, json=conflicting).status_code == 409
    stale = {**request, "request_id": str(uuid4())}
    assert client.post(admin_endpoint, json=stale).status_code == 409
    lookup = {
        "operation_id": request["request_id"],
        "binding": binding,
        "result_intent_id": intent_id,
        "body_mac": mac,
    }
    if terminal is not None:
        lookup.update(
            mode=request["mode"],
            settlement=request["settlement"],
            disposition_id=created.json()["disposition_id"],
        )
    base = f"/api/workers/{worker_id}/v4/executions/{task.execution_id}/recovery"

    def register():
        lease = WorkerLeaseSession.from_trusted_file(
            trust_file=trust_file,
            base_url=old._base_url,
            worker_id=worker_id,
            worker_identity_id=old._identity_id,
            credential=credential,
            credential_record_id=credential_record_id,
            instance_id=uuid4(),
        )
        before = client.post(
            f"/api/workers/{worker_id}/v4/recovery-intent/read",
            json={
                "operation_id": request["request_id"],
                "prepared_sha256": binding["prepared_sha256"],
            },
            headers={
                "Authorization": f"Bearer {credential}",
                "X-DLR-Worker-Instance": str(lease._instance_id),
            },
        )
        assert before.status_code == 200, before.text
        assert before.json()["binding"] == binding
        lease.register()
        return lease

    lease = register()

    def headers(lease):
        return {
            **old_headers,
            "X-DLR-Worker-Instance": str(lease._instance_id),
            "X-DLR-Worker-Generation": str(lease._claims.generation),
            "X-DLR-Worker-Lease-Proof": lease._proof,
        }

    observations = []

    def observe(lease, body=lookup, extra=None, status=200):
        nonce = uuid4()
        response = client.post(
            base + "/observe",
            json=body,
            headers={
                **headers(lease),
                "X-DLR-Result-Recovery-Nonce": str(nonce),
                **(extra or {}),
            },
        )
        assert response.status_code == status, response.text
        if status != 200:
            return None
        proof = verify_observation(
            response.json()["recovery_proof"], public_key=public_key, instance_token=lease._proof
        )
        assert proof is not None and proof.observation_nonce == nonce
        assert proof.operation_id == UUID(request["request_id"])
        assert proof.prepared_sha256.hex() == binding["prepared_sha256"]
        assert proof.finalization_id == finalization and proof.body_mac.hex() == mac
        if terminal is not None:
            assert proof.kind == "platform_terminal" and proof.finalization_id is None
            assert str(proof.disposition_id) == lookup["disposition_id"]
            assert proof.stopped_sha256.hex() == digest(stopped)
            assert (proof.attempt_status, proof.execution_status) == terminal
        assert proof.version == 2
        if not observations:
            key = client.app.state.control_process_v4._deployment.worker_proof_signing_key
            if terminal is None:
                legacy = replace(proof, version=1)
                assert (
                    verify_observation(
                        sign_observation(legacy, key),
                        public_key=public_key,
                        instance_token=lease._proof,
                    )
                    == legacy
                )
            else:
                for mixed in (
                    replace(proof, finalization_id=uuid4()),
                    replace(proof, kind="committed_result"),
                    replace(proof, version=1),
                ):
                    with pytest.raises(ValueError):
                        sign_observation(mixed, key)
        observations.append((response.json()["recovery_proof"], lease._proof))
        return proof

    assert observe(lease).completion_id is None
    if terminal is not None:
        observe(lease, {**lookup, "disposition_id": str(uuid4())}, status=503)
        wrong_stopped = deepcopy(lookup)
        wrong_stopped["settlement"]["stopped_sha256"] = "0" * 64
        observe(lease, wrong_stopped, status=503)
        observe(
            lease,
            {
                key: value
                for key, value in lookup.items()
                if key not in {"mode", "disposition_id", "settlement"}
            },
            status=503,
        )
    # A genuine API signature for a different nonce must not pass the client.
    state = RecoveryState(
        prepared,
        stopped,
        {"cleanup_token": task.cleanup_token},
        None,
    )
    exchange = RecoveryExchange(lease, state, lookup, threading.Event())
    replay = json.dumps({"recovery_proof": observations[-1][0]}).encode()
    with (
        patch.object(exchange, "_call", return_value=(replay, lease._now(), lease._now())),
        pytest.raises(ValueError, match="recovery observation conflict"),
    ):
        exchange.observe()
    # A matching finalization written in this transaction is not committed.
    from dlr.control.services.attempt_result_intent_v4 import ResultIntentError
    from dlr.control.services.worker_recovery_v4 import (
        RecoveryLookup,
        require_committed_result,
    )

    if terminal is None:
        with factory() as session:
            marker = session.get(AttemptResultIntentV4, task.attempt_id)
            marker.finalization_xid = marker.writer_xid = session.scalar(
                select(func.txid_current())
            )
            session.flush()
            with pytest.raises(ResultIntentError):
                require_committed_result(
                    session,
                    RecoveryLookup.model_validate(lookup),
                    worker_id=worker_id,
                    instance_id=lease._instance_id,
                    generation=lease._claims.generation,
                )
            session.rollback()
    else:
        _assert_disposition_guards(factory, lookup, lease, worker_id)
    observe(lease, extra={"X-DLR-Cleanup-Token": "invalid"}, status=503)
    for key, value in (
        ("body_mac", "0" * 64),
        ("result_intent_id", str(uuid4())),
        ("operation_id", str(uuid4())),
    ):
        observe(lease, {**lookup, key: value}, status=503)
    foreign = deepcopy(lookup)
    foreign["binding"]["attempt"]["worker_identity_id"] = str(uuid4())
    observe(lease, foreign, status=503)
    if terminal is None:
        with factory.begin() as session:
            session.get(Execution, task.execution_id).cancel_requested = True
        observe(lease, status=503)
        with factory.begin() as session:
            session.get(Execution, task.execution_id).cancel_requested = False

    intent, nonce, receipt_nonce = uuid4(), uuid4(), uuid4()
    stage_headers = {
        **headers(lease),
        "X-DLR-Cleanup-Intent": str(intent),
        "X-DLR-Cleanup-Observation-Nonce": str(nonce),
    }
    staged = client.post(base + "/cleanup/stage", json=lookup, headers=stage_headers)
    assert staged.status_code == 200, staged.text
    final_headers = {
        **stage_headers,
        "X-DLR-Cleanup-Proof": staged.json()["cleanup_proof"],
        "X-DLR-Cleanup-Receipt-Nonce": str(receipt_nonce),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(
                    base + "/cleanup/finalize", json=lookup, headers=final_headers
                ),
                range(2),
            )
        )
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    first = observe(lease)
    assert first.completion_id is not None
    with factory() as session:
        cleanup = session.get(WorkerCleanupIntentV4, task.attempt_id)
        completion_snapshot = (cleanup.completion_id, cleanup.completion_xid, cleanup.intent_id)
        settled = session.get(ExecutionAttempt, task.attempt_id).cleanup_settled_at
        assert settled is not None
        assert session.get(AdapterExecutionSlot, (task.adapter_id, 0)).active_attempt_id is None
    # Heartbeat revision and a second new generation read the same committed
    # completion. Staging another intent cannot reset it.
    lease.heartbeat()
    assert (
        verify_observation(observations[-1][0], public_key=public_key, instance_token=lease._proof)
        is None
    )
    assert observe(lease).completion_id == first.completion_id
    denied = client.post(
        base + "/cleanup/stage",
        json=lookup,
        headers={
            **headers(lease),
            "X-DLR-Cleanup-Intent": str(uuid4()),
            "X-DLR-Cleanup-Observation-Nonce": str(uuid4()),
        },
    )
    assert denied.status_code == 503
    lease.retire()
    _expire(factory, worker_id)
    lease = register()
    assert observe(lease).completion_id == first.completion_id
    with factory() as session:
        execution = session.get(Execution, task.execution_id)
        assert (
            execution.status,
            execution.output,
            execution.error,
            execution.ended_at,
            execution.attempt_count,
        ) == expected_business
        if terminal is not None:
            assert session.get(AttemptResultIntentV4, task.attempt_id).finalization_id is None
            disposition = client.get(f"/api/workers/{worker_id}/v4/status").json()[
                "last_recovery_disposition"
            ]
            assert disposition["reason"] == "result_uncommitted_outcome_unknown"
            assert disposition["disposition_id"] == lookup["disposition_id"]
        cleanup = session.get(WorkerCleanupIntentV4, task.attempt_id)
        assert (
            cleanup.completion_id,
            cleanup.completion_xid,
            cleanup.intent_id,
        ) == completion_snapshot
        assert session.get(ExecutionAttempt, task.attempt_id).cleanup_settled_at == settled
    return lease


def _terminalize_uncommitted(
    client, factory, worker_id, task, terminal, endpoint, request, pending
):
    from dlr.control.services import attempt as attempts
    from dlr.control.services import attempt_result_finalize_v4 as finalizer

    assert client.post(endpoint, json=request).status_code == 409  # Still running.
    with factory.begin() as session:
        execution = session.get(Execution, task.execution_id)
        policy = dict(execution.retry_policy_snapshot)
        policy.update(
            max_attempts=3 if terminal[1] == "cancelled" else 1,
            retryable_error_classes=["worker_lost"],
        )
        execution.retry_policy_snapshot = policy
        attempt = session.get(ExecutionAttempt, task.attempt_id)
        attempt.lease_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    if terminal[0] == "cancelled":
        assert client.post(f"/api/executions/{task.execution_id}/cancel").status_code == 200
        with factory() as session:
            execution, _, attempt, slot = attempts._lock_attempt_context(
                session, worker_id, task.attempt_id
            )
            attempts._apply_terminal_locked(
                session,
                execution,
                attempt,
                slot,
                status="cancelled",
                error_code="execution_cancelled",
                error_class="cancelled",
                error=None,
                now=session.scalar(select(func.clock_timestamp())),
            )
            session.commit()
    else:

        def reconcile():
            with factory() as session:
                return attempts._recover_reserved_attempt(session, task.attempt_id, now=None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            recovered = pool.submit(reconcile)
            raced = pool.submit(lambda: client.post(endpoint, json=request))
            assert recovered.result()
            assert raced.result().status_code in {200, 409}
        if terminal[1] == "cancelled":
            with factory() as session:
                execution = session.get(Execution, task.execution_id)
                assert execution.status == "retry_wait"
                dispatch_generation = execution.dispatch_generation
            assert client.post(endpoint, json=request).status_code == 409
            with factory() as session:
                assert (
                    attempts.retry_dispatcher_once(
                        session,
                        now=session.scalar(select(func.clock_timestamp())) + timedelta(hours=1),
                    )
                    == 0
                )
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancelled = pool.submit(
                    lambda: client.post(f"/api/executions/{task.execution_id}/cancel")
                )
                raced = pool.submit(lambda: client.post(endpoint, json=request))
                assert cancelled.result().status_code == 200
                assert raced.result().status_code in {200, 409}
            with factory() as session:
                assert (
                    session.get(Execution, task.execution_id).dispatch_generation
                    == dispatch_generation
                )
    context, ref, proof, nonce, fence = pending
    # A late old result cannot supersede the platform terminal state.
    with pytest.raises(finalizer.ResultFinalizeRejected):
        finalizer.finalize_unconfirmed_result_from_token(
            factory,
            context,
            ref,
            proof.token,
            observation_nonce=nonce,
            control=fence,
            max_body_bytes=100000,
        )
    with factory() as session:
        execution = session.get(Execution, task.execution_id)
        attempt = session.get(ExecutionAttempt, task.attempt_id)
        assert (attempt.status, execution.status) == terminal
        assert execution.attempt_count == 1 and attempt.cleanup_settled_at is None
        assert session.get(AttemptResultIntentV4, task.attempt_id).finalization_id is None
        assert (
            session.get(AdapterExecutionSlot, (task.adapter_id, 0)).active_attempt_id
            == task.attempt_id
        )


def _assert_disposition_guards(factory, lookup, lease, worker_id):
    from sqlalchemy.exc import IntegrityError

    from dlr.control.security import SUPERADMIN_PRINCIPAL
    from dlr.control.services.worker_recovery_v4 import (
        RecoveryLookup,
        RecoveryRequest,
        RecoveryUnavailable,
        request_recovery,
        require_disposition,
    )

    parsed = RecoveryLookup.model_validate(lookup)
    with factory() as session:
        operation = session.get(WorkerMaintenanceOperation, parsed.operation_id)
        # No-op retry is permitted, mutation and forged transaction IDs are not.
        with pytest.raises(IntegrityError), session.begin_nested():
            operation.recovery_disposition_xid += 1
            session.flush()
        session.expire_all()
        operation = session.get(WorkerMaintenanceOperation, parsed.operation_id)
        original_id = operation.recovery_disposition_id
        # Create a fresh request in this transaction, with the old expired
        # instance temporarily restored. No new proof/claim authority is issued.
        worker = session.get(Worker, worker_id)
        worker.instance_id = parsed.binding.attempt.original_instance_id
        worker.instance_generation = parsed.binding.attempt.original_instance_generation
        row = session.get(WorkerLeaseCandidateV4, worker_id)
        row.instance_id = worker.instance_id
        row.instance_generation = worker.instance_generation
        row.retired = False
        row.writer_xid = session.scalar(select(func.txid_current()))
        now = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        worker.instance_lease_until = now
        row.nominal_until = row.guard_until = row.proof_before = now
        session.flush()
        request = RecoveryRequest(
            request_id=uuid4(),
            expected_version=worker.maintenance_version,
            binding=parsed.binding,
            mode=parsed.mode,
            settlement=parsed.settlement,
        )
        uncommitted = request_recovery(session, worker_id, request, SUPERADMIN_PRINCIPAL)
        # require_operation needs current identity and history semantics too.
        worker.instance_id = lease._instance_id
        worker.instance_generation = lease._claims.generation
        session.flush()
        with pytest.raises(RecoveryUnavailable):
            require_disposition(
                session,
                parsed.model_copy(
                    update={
                        "operation_id": request.request_id,
                        "disposition_id": UUID(uncommitted["disposition_id"]),
                    }
                ),
                worker_id=worker_id,
                instance_id=lease._instance_id,
                generation=lease._claims.generation,
            )
        session.rollback()
    with factory() as session:
        assert (
            session.get(WorkerMaintenanceOperation, parsed.operation_id).recovery_disposition_id
            == original_id
        )
