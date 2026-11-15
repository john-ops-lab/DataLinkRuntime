"""W1-B PostgreSQL/API protocol assertions; no physical-stop qualification.

Reuse the existing enrolled restricted-role fixture rather than another test
deployment. Expiry is synthetic DB setup; real stopped-container acceptance is
separate and must use actual new-version local checkpoints.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from dlr.common.recovery_observation_v4 import verify_observation
from dlr.control.models import (
    AdapterExecutionSlot,
    AttemptResultIntentV4,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerCleanupIntentV4,
    WorkerLeaseCandidateV4,
)
from dlr.worker.lease_session_v4 import WorkerLeaseSession


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
    client, factory, admin_factory, worker_id, old, claimed, trust_file, public_key
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
        "prepared_sha256": "3" * 64,
    }
    request = {"request_id": str(uuid4()), "expected_version": version, "binding": binding}
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
    created = client.post(admin_endpoint, json=request)
    assert created.status_code == 200, created.text
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
        return proof

    assert observe(lease).completion_id is None
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
        cleanup = session.get(WorkerCleanupIntentV4, task.attempt_id)
        assert (
            cleanup.completion_id,
            cleanup.completion_xid,
            cleanup.intent_id,
        ) == completion_snapshot
        assert session.get(ExecutionAttempt, task.attempt_id).cleanup_settled_at == settled
    return lease
