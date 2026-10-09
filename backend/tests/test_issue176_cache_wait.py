"""Real AMQP/Control regression; resource envelope is an explicit test double.

This covers transport ownership, not Linux execution isolation or business output.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Execution, ExecutionOutbox
from dlr.control.services import execution as execution_service
from dlr.control.services import outbox, rabbitmq
from dlr.worker.consumer import ConsumerConfig, V3Consumer
from test_issue130_b2_runtime import _dispatch, _execution, _rabbit_adapter
from test_issue161_cache_governance import WORKER_HEADERS
from test_issue175_guard_dlq import BROKER_URL, active_replacement_with_queued_execution
from worker_runtime_support import unit_resource_envelope, unit_sandbox_config


@pytest.mark.skipif(not BROKER_URL, reason="requires dedicated real RabbitMQ fixture")
def test_long_cache_wait_keeps_connection_and_durable_outbox_without_broker_redelivery(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker, _adapter, queued, _payload, operation_id, generation = (
        active_replacement_with_queued_execution(
            api_client, session_factory, monkeypatch, "issue176-real-guard"
        )
    )
    # A cancelled Execution is an independently valid ACK_NOOP delivery on
    # the same Worker; no synthetic Claim decision or runner is used.
    healthy_adapter = _rabbit_adapter(api_client, worker, "issue176-other-adapter")
    healthy = _execution(api_client, int(healthy_adapter["id"]))
    healthy_dispatch = _dispatch(session_factory, int(healthy["id"]))
    with session_factory() as session:
        execution_service.cancel_execution(session, int(healthy["id"]))
    decisions: list[tuple[int, str, str]] = []
    deliveries: list[dict] = []

    class ObservedConsumer(V3Consumer):
        def _on_delivery(self, epoch, ticket, channel, method, properties, body):
            deliveries.append(
                {
                    "execution_id": json.loads(body)["execution_id"],
                    "delivery_count": (properties.headers or {}).get("x-delivery-count", 0),
                    "redelivered": method.redelivered,
                }
            )
            return super()._on_delivery(epoch, ticket, channel, method, properties, body)

    class ApiClaimClient:
        def claim_v3(self, worker_id, dispatch, *, timeout_seconds):
            response = api_client.post(
                f"/api/workers/{worker_id}/v3/claim", json=dispatch, headers=WORKER_HEADERS
            )
            assert response.status_code == 200, response.text
            value = response.json()
            decisions.append((dispatch["execution_id"], value["decision"], value["reason"]))
            return value

    queue = rabbitmq.topology_names(int(worker["id"])).queue
    dlq = f"dlr.issue176.{uuid.uuid4().hex}.dlq"
    connection = pika.BlockingConnection(pika.URLParameters(str(BROKER_URL)))
    channel = connection.channel()
    consumer = ObservedConsumer(
        ConsumerConfig(
            worker_id=int(worker["id"]),
            queue=queue,
            execution_slots=2,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        ApiClaimClient(),  # type: ignore[arg-type]
        connection_parameters=pika.URLParameters(str(BROKER_URL)),
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
    )
    thread = threading.Thread(target=consumer.run, daemon=True)
    try:
        channel.queue_declare(queue=dlq, durable=True, arguments={"x-queue-type": "quorum"})
        channel.queue_declare(
            queue=queue,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": 5,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": dlq,
            },
        )
        channel.confirm_delivery()
        for dispatch in [_dispatch(session_factory, int(queued["id"])), healthy_dispatch]:
            channel.basic_publish(
                exchange="",
                routing_key=queue,
                body=json.dumps(dispatch),
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
        with session_factory.begin() as session:
            row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == int(queued["id"]))
            )
            assert row is not None
            row.status = "published"
            row.published_at = datetime.now(UTC)
        thread.start()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            connection.process_data_events(time_limit=0.1)
        dlq_count = channel.queue_declare(queue=dlq, passive=True).method.message_count
        print(
            json.dumps(
                {
                    "kind": "real_broker_real_control_unit_resource_envelope",
                    "window_seconds": 12,
                    "connection_epochs": consumer._epoch_counter,
                    "deliveries": deliveries,
                    "decisions": decisions,
                    "dlq_count": dlq_count,
                }
            )
        )
        assert (int(healthy["id"]), "ACK_NOOP", "cancelled") in decisions
        assert (int(queued["id"]), "ACK_NOOP", "cache_dispatch_deferred") in decisions
        assert consumer._epoch_counter == 1
        assert dlq_count == 0
        with session_factory() as session:
            row = session.get(Execution, int(queued["id"]))
            assert row is not None and row.status == "queued" and row.attempt_count == 0
            dispatch_row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == row.id)
            )
            assert dispatch_row is not None and dispatch_row.status == "pending"
            assert dispatch_row.last_error_code == "cache_reclamation_in_progress"
            ready = outbox.lease_due_outbox(
                session, "issue176-test", now=datetime.now(UTC) + timedelta(seconds=2)
            )
            assert all(lease.execution_id != int(queued["id"]) for lease in ready)
        finished = api_client.post(
            f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
            json={"generation": generation, "outcome": "aborted"},
            headers=WORKER_HEADERS,
        )
        assert finished.status_code == 200, finished.text
        with session_factory() as session:
            leases = outbox.lease_due_outbox(session, "issue176-test")
            assert [lease.execution_id for lease in leases] == [int(queued["id"])]
            assert leases[0].payload_json == _dispatch(session_factory, int(queued["id"]))
    finally:
        consumer.request_stop()
        if thread.is_alive():
            thread.join(timeout=5)
        assert not thread.is_alive()
        if channel.is_open:
            channel.queue_delete(queue=queue)
            channel.queue_delete(queue=dlq)
        if connection.is_open:
            connection.close()
