"""Real Broker DLQ regression with a supported active replacement guard."""

from __future__ import annotations

import json
import os
import time
import uuid

import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    Execution,
    ExecutionAttempt,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)
from dlr.control.services import infrastructure_dlq, outbox, rabbitmq
from test_issue130_b2_runtime import _dispatch, _execution
from test_issue161_cache_governance import (
    WORKER_HEADERS,
    _fixture,
    _replacement_context,
    _start_replacement_attempt,
)

BROKER_URL = os.environ.get("DLR_TEST_RABBITMQ_URL")


def active_replacement_with_queued_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> tuple[dict, dict, dict, dict, uuid.UUID, int]:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, name)
    _owner, payload = _start_replacement_attempt(
        api_client, session_factory, int(worker["id"]), int(adapter["id"])
    )
    queued = _execution(api_client, int(adapter["id"]))
    operation_id = uuid.uuid4()
    acquired = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/acquire",
        json={
            "adapter_id": adapter["id"],
            "version_id": version_id,
            "operation_id": str(operation_id),
            "replacement_context": _replacement_context(payload),
        },
        headers=WORKER_HEADERS,
    )
    assert acquired.status_code == 200, acquired.text
    assert acquired.json()["operation_kind"] == "replacement"
    return worker, adapter, queued, payload, operation_id, acquired.json()["generation"]


@pytest.mark.skipif(not BROKER_URL, reason="requires dedicated real RabbitMQ fixture")
@pytest.mark.parametrize("publication_recorded", [False, True])
def test_guard_conflict_is_durable_manual_review_and_does_not_block_next_dlq_message(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    publication_recorded: bool,
) -> None:
    worker, _adapter, queued, owner_payload, operation_id, guard_generation = (
        active_replacement_with_queued_execution(
            api_client, session_factory, monkeypatch, "issue175-real-guard"
        )
    )
    healthy_worker, healthy_adapter, _version = _fixture(
        api_client, session_factory, monkeypatch, "issue175-real-healthy"
    )
    healthy = _execution(api_client, int(healthy_adapter["id"]))
    dlq = f"dlr.issue175.{uuid.uuid4().hex}.dlq"
    monkeypatch.setattr(infrastructure_dlq, "INFRASTRUCTURE_DLQ", dlq)
    connection = pika.BlockingConnection(pika.URLParameters(str(BROKER_URL)))
    channel = connection.channel()
    queues: list[str] = []
    relay_owner = "issue175-confirmed-publication"
    leased_by_execution = {}
    if publication_recorded:
        with session_factory() as session:
            leased_by_execution = {
                row.execution_id: row for row in outbox.lease_due_outbox(session, relay_owner)
            }
    try:
        channel.queue_declare(queue=dlq, durable=True, arguments={"x-queue-type": "quorum"})
        channel.confirm_delivery()
        for target_worker, execution in [(worker, queued), (healthy_worker, healthy)]:
            queue = rabbitmq.topology_names(int(target_worker["id"])).queue
            queues.append(queue)
            channel.queue_declare(
                queue=queue,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": dlq,
                },
            )
            channel.basic_publish(
                exchange="",
                routing_key=queue,
                body=json.dumps(_dispatch(session_factory, int(execution["id"]))),
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
            if publication_recorded:
                with session_factory() as session:
                    assert outbox.mark_outbox_published(
                        session, leased_by_execution[int(execution["id"])].id, relay_owner
                    )
            method, _props, _body = channel.basic_get(queue=queue, auto_ack=False)
            assert method is not None
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        deadline = time.monotonic() + 5
        while channel.queue_declare(queue=dlq, passive=True).method.message_count != 2:
            assert time.monotonic() < deadline
            connection.process_data_events(time_limit=0.05)
        publication_fields = (
            "id",
            "status",
            "available_at",
            "lease_owner",
            "lease_expires_at",
            "publish_attempts",
            "last_error_code",
            "published_at",
            "message_id",
        )
        with session_factory() as session:
            before_dlq = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(queued["id"]))
            )
            assert before_dlq is not None
            original_publication_state = tuple(
                getattr(before_dlq, name) for name in publication_fields
            )
        with session_factory() as session:
            assert infrastructure_dlq.drain_once(session, channel, limit=2) == 2
        assert channel.queue_declare(queue=dlq, passive=True).method.message_count == 0
        with session_factory() as session:
            incidents = list(session.scalars(select(ExecutionInfrastructureIncident)))
            by_execution = {incident.execution_id: incident for incident in incidents}
            blocked = by_execution[int(queued["id"])]
            assert blocked.kind == "rejected" and blocked.status == "open"
            assert blocked.last_error == "cache_reclamation_in_progress; broker_reason=rejected"
            incident_id = blocked.id
            original_generation = blocked.dispatch_generation
            original_message_id = blocked.message_id
            original_row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(queued["id"]))
            )
            assert original_row is not None
            assert tuple(getattr(original_row, name) for name in publication_fields) == (
                original_publication_state
            )
            original_outbox_id = original_row.id
            assert original_row.status == ("published" if publication_recorded else "pending")
            assert by_execution[int(healthy["id"])].kind == "rejected"
            for execution_id in [int(queued["id"]), int(healthy["id"])]:
                row = session.get(Execution, execution_id)
                assert row is not None and row.status == "queued" and row.attempt_count == 0
            row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(healthy["id"]))
            )
            assert row is not None and row.status == "pending"

        disposition_url = f"/api/executions/{queued['id']}/incidents/{incident_id}/dispositions"
        idempotency_key = str(uuid.uuid4())
        recover_body = {
            "action": "recover",
            "expected_generation": original_generation,
            "reason_code": "capacity_repaired",
        }
        recover_headers = {"Idempotency-Key": idempotency_key}
        blocked_recover = api_client.post(
            disposition_url, json=recover_body, headers=recover_headers
        )
        assert blocked_recover.status_code == 409, blocked_recover.text
        assert blocked_recover.json()["detail"]["code"] == "cache_reclamation_in_progress"
        with session_factory() as session:
            assert (
                session.scalar(
                    select(ExecutionAttempt).where(
                        ExecutionAttempt.execution_id == int(queued["id"])
                    )
                )
                is None
            )
            assert (
                session.scalar(
                    select(ExecutionIncidentDisposition).where(
                        ExecutionIncidentDisposition.incident_id == incident_id
                    )
                )
                is None
            )

        # A following healthy responsibility can be claimed while the other
        # key is guarded; DLQ isolation does not require the guard to clear.
        healthy_claim = api_client.post(
            f"/api/workers/{healthy_worker['id']}/v3/claim",
            json=_dispatch(session_factory, int(healthy["id"])),
            headers=WORKER_HEADERS,
        )
        assert healthy_claim.status_code == 200, healthy_claim.text
        assert healthy_claim.json()["decision"] == "EXECUTE"

        released = api_client.post(
            f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
            json={"generation": guard_generation, "outcome": "aborted"},
            headers=WORKER_HEADERS,
        )
        assert released.status_code == 200, released.text
        with session_factory() as session:
            assert infrastructure_dlq.drain_once(session, channel, limit=2) == 0
        with session_factory() as session:
            incident = session.get(ExecutionInfrastructureIncident, incident_id)
            original = session.get(ExecutionOutbox, original_outbox_id)
            assert incident is not None and incident.status == "open"
            assert incident.dispatch_generation == original_generation
            assert incident.message_id == original_message_id
            assert original is not None
            assert original.status == ("published" if publication_recorded else "pending")
            rows = list(
                session.scalars(
                    select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(queued["id"]))
                )
            )
            assert len(rows) == 1
            execution = session.get(Execution, int(queued["id"]))
            assert execution is not None and execution.attempt_count == 0
            assert execution.dispatch_generation == original_generation

        if publication_recorded:
            # The Relay's actual due-row selection must not reopen a settled
            # publication simply because its guard has cleared. A healthy
            # pending row is a positive control for the same selection call.
            with session_factory() as session:
                due = outbox.lease_due_outbox(session, "issue175-after-guard")
            assert int(queued["id"]) not in {row.execution_id for row in due}
            assert int(healthy["id"]) in {row.execution_id for row in due}
        # In the confirm-before-mark window the original row is still pending.
        # Its existing Relay responsibility can replay without manual recovery;
        # the assertions above only require DLQ handling not to create/reset it.
        # Manual recover below resolves the Incident by acknowledging that same
        # pending generation, rather than being a prerequisite for Relay replay.

        # Complete the replacement owner's supported protocol before claiming
        # the queued execution: the Adapter's single slot is still occupied.
        owner_result = api_client.post(
            f"/api/workers/{worker['id']}/attempts/{owner_payload['attempt_id']}/result",
            json={
                "attempt_id": owner_payload["attempt_id"],
                "fencing_token": owner_payload["fencing_token"],
                "claim_token": owner_payload["claim_token"],
                "status": "succeeded",
                "workspace_cleanup_status": "completed",
            },
            headers=WORKER_HEADERS,
        )
        assert owner_result.status_code == 200, owner_result.text
        assert owner_result.json()["decision"] == "ACK_NOOP"
        recovered = api_client.post(disposition_url, json=recover_body, headers=recover_headers)
        assert recovered.status_code == 200, recovered.text
        body = recovered.json()
        receipt = body["receipt"]
        assert body["incident_status"] == "resolved"
        assert receipt["execution_id"] == queued["id"]
        assert receipt["from_generation"] == original_generation
        expected_generation = original_generation + int(publication_recorded)
        assert receipt["to_generation"] == expected_generation
        assert receipt["from_outbox_id"] == str(original_outbox_id)
        assert receipt["outcome"] == (
            "recovery_dispatched" if publication_recorded else "dispatch_already_pending"
        )
        replayed = api_client.post(disposition_url, json=recover_body, headers=recover_headers)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == body
        with session_factory() as session:
            rows = list(
                session.scalars(
                    select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(queued["id"]))
                )
            )
            assert len(rows) == 1 + int(publication_recorded)
            current = next(row for row in rows if row.dispatch_generation == expected_generation)
            assert current.status == "pending"
            assert str(current.id) == receipt["to_outbox_id"]
            assert (current.message_id == original_message_id) is (not publication_recorded)
            dispatch = dict(current.payload_json)
            receipts = list(
                session.scalars(
                    select(ExecutionIncidentDisposition).where(
                        ExecutionIncidentDisposition.incident_id == incident_id
                    )
                )
            )
            assert len(receipts) == 1

        resumed = api_client.post(
            f"/api/workers/{worker['id']}/v3/claim", json=dispatch, headers=WORKER_HEADERS
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["decision"] == "EXECUTE"
        duplicate = api_client.post(
            f"/api/workers/{worker['id']}/v3/claim", json=dispatch, headers=WORKER_HEADERS
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["decision"] == "ACK_NOOP"
        with session_factory() as session:
            attempts = list(
                session.scalars(
                    select(ExecutionAttempt).where(
                        ExecutionAttempt.execution_id == int(queued["id"])
                    )
                )
            )
            assert len(attempts) == 1
    finally:
        if channel.is_open:
            for queue in queues + [dlq]:
                channel.queue_delete(queue=queue)
        if connection.is_open:
            connection.close()
