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

from dlr.control.models import Execution, ExecutionInfrastructureIncident, ExecutionOutbox
from dlr.control.services import infrastructure_dlq, rabbitmq
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
def test_guard_conflict_is_durable_manual_review_and_does_not_block_next_dlq_message(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _adapter, queued, _payload, _op, _generation = active_replacement_with_queued_execution(
        api_client, session_factory, monkeypatch, "issue175-real-guard"
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
            method, _props, _body = channel.basic_get(queue=queue, auto_ack=False)
            assert method is not None
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        deadline = time.monotonic() + 5
        while channel.queue_declare(queue=dlq, passive=True).method.message_count != 2:
            assert time.monotonic() < deadline
            connection.process_data_events(time_limit=0.05)
        with session_factory() as session:
            assert infrastructure_dlq.drain_once(session, channel, limit=2) == 2
        assert channel.queue_declare(queue=dlq, passive=True).method.message_count == 0
        with session_factory() as session:
            incidents = list(session.scalars(select(ExecutionInfrastructureIncident)))
            by_execution = {incident.execution_id: incident for incident in incidents}
            blocked = by_execution[int(queued["id"])]
            assert blocked.kind == "rejected" and blocked.status == "open"
            assert blocked.last_error == "cache_reclamation_in_progress; broker_reason=rejected"
            assert by_execution[int(healthy["id"])].kind == "rejected"
            for execution_id in [int(queued["id"]), int(healthy["id"])]:
                row = session.get(Execution, execution_id)
                assert row is not None and row.status == "queued" and row.attempt_count == 0
            row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(healthy["id"]))
            )
            assert row is not None and row.status == "pending"
    finally:
        if channel.is_open:
            for queue in queues + [dlq]:
                channel.queue_delete(queue=queue)
        if connection.is_open:
            connection.close()
