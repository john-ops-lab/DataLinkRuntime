"""Independent runtime budget/fencing checks; source SDK protocols are tested separately.

The fake native driver uses explicit gates to keep close unfinished. HTTP requests
still cross a real local TCP socket into the Control app. These checks establish
resource isolation, not Kafka throughput or business-target acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import ADMIN_TOKEN
from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionAttempt,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    Worker,
)
from dlr.control.services import rabbitmq
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.event_envelope import event_id
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_runtime import SubscriptionRuntimeManager
from dlr.control.services.subscription_source import (
    ApplicationBufferBudget,
    SourceFailure,
    SourceRunContext,
)

MIB = 1024**2


def eventually(predicate: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Native owner did not reach the expected state")
        time.sleep(0.005)


class GatedSource:
    """A native call observes stop, but cannot close before the test permits it."""

    def __init__(self, *, failure: SourceFailure | None = None) -> None:
        self.contexts: Queue[SourceRunContext] = Queue()
        self.stop_observations: Queue[tuple[int, int, int | None]] = Queue()
        self.close_gates: dict[int, threading.Event] = {}
        self.closed: set[int] = set()
        self.lock = threading.Lock()
        self.failure = failure
        self.probe_entered = threading.Event()
        self.probe_close = threading.Event()
        self.probe_closed = threading.Event()

    def run(self, context: SourceRunContext) -> None:
        gate = threading.Event()
        with self.lock:
            self.close_gates[context.subscription_id] = gate
        assert context.buffer.reserve(1024)
        context.connected()
        self.contexts.put(context)
        if not context.stop_event.wait(15):
            raise SourceFailure("test_native_gate_timeout", closed=False)
        with context.session_factory() as session:
            lease = session.get(SubscriptionLease, context.subscription_id)
            self.stop_observations.put(
                (context.subscription_id, context.generation, lease.generation if lease else None)
            )
        if not gate.wait(15):
            raise SourceFailure("test_close_gate_timeout", closed=False)
        with self.lock:
            self.closed.add(context.subscription_id)
        if self.failure is not None:
            raise self.failure

    def test_connection(
        self, source_config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        self.probe_entered.set()
        if not self.probe_close.wait(15):
            raise SourceFailure("test_probe_gate_timeout", closed=False)
        self.probe_closed.set()
        return {"connected": True}

    def allow_close(self, subscription_id: int) -> None:
        with self.lock:
            self.close_gates[subscription_id].set()

    def allow_all_close(self) -> None:
        self.probe_close.set()
        with self.lock:
            for gate in self.close_gates.values():
                gate.set()


@pytest.fixture
def runtime_world(api_client, session_factory, monkeypatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *args, **kwargs: True)
    managers: list[SubscriptionRuntimeManager] = []
    sources: list[GatedSource] = []

    def subscriptions(
        count: int = 1, source_configuration: dict[str, Any] | None = None
    ) -> list[int]:
        ids: list[int] = []
        with session_factory.begin() as session:
            worker = Worker(
                name="runtime-test-worker-" + uuid.uuid4().hex,
                status="online",
                capabilities=["python"],
                protocol_version=3,
                rabbitmq_execution_v3=True,
            )
            session.add(worker)
            session.flush()
            for _ in range(count):
                adapter = Adapter(
                    name="runtime-budget-" + uuid.uuid4().hex,
                    language="python",
                    adapter_type="task",
                )
                session.add(adapter)
                session.flush()
                revision = AdapterVersion(
                    adapter_id=adapter.id, seq=1, code="return input", requirements=""
                )
                session.add(revision)
                session.flush()
                adapter.latest_version_id, adapter.runtime_worker_id = revision.id, worker.id
                sub = EventSubscription(
                    adapter_id=adapter.id,
                    source_type="kafka",
                    source_epoch=uuid.uuid4().hex,
                    desired_state="enabled",
                    connection_state="starting",
                )
                session.add(sub)
                session.flush()
                config = SubscriptionConfigVersion(
                    subscription_id=sub.id,
                    version=1,
                    source_epoch=sub.source_epoch,
                    config={
                        "source_type": "kafka",
                        "cluster_id": "runtime-fixture",
                        "bootstrap_servers": ["broker.example.test:9092"],
                        "topics": ["runtime-fixture"],
                        "group_id": "runtime-" + uuid.uuid4().hex,
                        "initial_position": "earliest",
                        "security_protocol": "PLAINTEXT",
                        **(source_configuration or {}),
                    },
                )
                session.add(config)
                session.flush()
                activation = SubscriptionActivation(
                    subscription_id=sub.id,
                    config_version_id=config.id,
                    revision_id=revision.id,
                    worker_id=worker.id,
                )
                session.add(activation)
                session.flush()
                # New activations carry their policy before listeners start.
                # The bounded legacy backfill gate is covered separately.
                from dlr.control.services.subscription_activation_policy import ensure_policy

                ensure_policy(session, activation, adapter)
                sub.activation_id = activation.id
                ids.append(sub.id)
        return ids

    def manager(source: GatedSource | None = None, **kwargs: Any) -> SubscriptionRuntimeManager:
        source = source or GatedSource()
        value = SubscriptionRuntimeManager(session_factory, {"kafka": source}, **kwargs)
        managers.append(value)
        sources.append(source)
        return value

    yield {"subscriptions": subscriptions, "manager": manager, "app": api_client.app}
    for value in managers:
        value.stop_all()
    for source in sources:
        source.allow_all_close()
    for value, source in zip(managers, sources, strict=True):
        unconfirmed = source.failure is not None and not source.failure.closed
        if not unconfirmed:
            eventually(lambda value=value: value.active_count == 0)
        asyncio.run(value.aclose(timeout=0.05 if unconfirmed else 1))


def source_envelope(context: SourceRunContext, offset: int = 0) -> dict[str, Any]:
    value = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(context.subscription_id),
            "config_version": context.config_version,
            "source_epoch": context.source_epoch,
        },
        "event": {"id": "", "kind": "message", "object_key": None},
        "source": {
            "type": "kafka",
            "identity": "runtime-fixture",
            "position": {"topic": "runtime-fixture", "partition": "0", "offset": str(offset)},
            "attributes": {
                "cluster_id": "runtime-fixture",
                "topic_incarnation": "runtime-topic-uuid",
                "timestamp_ms": None,
                "timestamp_type": None,
                "key_base64": None,
                "headers": [],
            },
        },
        "received_at": "2026-10-23T00:00:00Z",
        "data": {"encoding": "base64", "content_type": "application/octet-stream", "value": "eA=="},
        "sync": None,
    }
    value["event"]["id"] = event_id(value)
    return value


def receive(session, context: SourceRunContext, offset: int = 0):
    return delivery.receive_event(
        session,
        context.subscription_id,
        context.owner_id,
        context.generation,
        source_envelope(context, offset),
        "runtime-fixture:0",
        {"offset": str(offset + 1)},
    )[0]


def test_global_application_buffer_is_64_mib_and_scope_cleanup_is_symmetric():
    budget = ApplicationBufferBudget()
    scopes = [budget.scope(4 * MIB) for _ in range(16)]
    for scope in scopes:
        assert scope.reserve(4 * MIB)
    assert budget.used_bytes == 64 * MIB
    assert not budget.reserve(1)
    assert not scopes[0].reserve(1)
    scopes[0].release_all()
    scopes[0].release_all()
    assert budget.used_bytes == 60 * MIB
    assert budget.reserve(4 * MIB)
    budget.release(4 * MIB)
    for scope in scopes:
        scope.release_all()
    assert budget.used_bytes == 0


def test_sixteen_native_owners_have_no_waiting_queue_or_worker_slot(runtime_world, session_factory):
    ids = runtime_world["subscriptions"](17)
    source = GatedSource()
    manager = runtime_world["manager"](source)
    for subscription_id in ids[:16]:
        assert manager.start_once(subscription_id)
    contexts = [source.contexts.get(timeout=5) for _ in range(16)]
    assert {context.subscription_id for context in contexts} == set(ids[:16])
    assert manager.active_count == 16
    assert not manager.start_once(ids[16])
    with session_factory() as session:
        assert session.get(SubscriptionLease, ids[16]) is None
        for model in (Execution, ExecutionAttempt, AdapterExecutionSlot):
            assert session.scalar(select(func.count()).select_from(model)) == 0

    async def exercise_control_http_and_default_pool() -> None:
        loop = asyncio.get_running_loop()
        ordinary = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ordinary-runtime-test")
        loop.set_default_executor(ordinary)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        server = uvicorn.Server(
            uvicorn.Config(runtime_world["app"], lifespan="off", log_level="critical", ws="none")
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.001)
            assert (
                await asyncio.wait_for(loop.run_in_executor(None, lambda: "ordinary"), timeout=1)
                == "ordinary"
            )
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            async with httpx.AsyncClient(
                base_url=url,
                trust_env=False,
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                timeout=2,
            ) as client:
                replies = await asyncio.gather(
                    *[client.get("/api/event-subscriptions/capabilities") for _ in range(12)]
                )
            assert all(reply.status_code == 200 for reply in replies)
            assert all(reply.json()["enabled"] for reply in replies)
            assert manager.active_count == 16
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)
            listener.close()
            ordinary.shutdown()

    asyncio.run(exercise_control_http_and_default_pool())
    manager.stop_single(ids[0])
    source.allow_close(ids[0])
    eventually(lambda: manager.active_count == 15)
    with session_factory() as session:
        assert session.get(SubscriptionLease, ids[16]) is None
    assert source.contexts.empty()  # Rejected work was not queued for a newly free permit.
    assert manager.start_once(ids[16])
    assert source.contexts.get(timeout=5).subscription_id == ids[16]


def test_stop_fences_before_native_close_and_releases_only_after_return(
    runtime_world, session_factory
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source, max_listeners=1)
    assert manager.start_once(subscription_id)
    context = source.contexts.get(timeout=5)
    manager.stop_single(subscription_id)
    observed_id, old_generation, fenced_generation = source.stop_observations.get(timeout=5)
    assert observed_id == subscription_id and old_generation == context.generation
    assert fenced_generation > old_generation
    assert manager.active_count == 1 and manager.buffer.used_bytes == 1024
    assert not manager.start_once(subscription_id)
    assert subscription_id not in source.closed
    with session_factory() as session:
        with pytest.raises(HTTPException) as error:
            receive(session, context)
        assert error.value.detail["code"] == "subscription_owner_stale"
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
    source.allow_close(subscription_id)
    eventually(lambda: manager.active_count == 0)
    assert manager.buffer.used_bytes == 0
    assert manager.start_once(subscription_id)
    replacement = source.contexts.get(timeout=5)
    assert replacement.generation > context.generation and replacement.owner_id != context.owner_id
    with session_factory.begin() as session:
        assert receive(session, replacement) is not None


@pytest.mark.parametrize("cancel", [False, True])
def test_probe_timeout_or_cancel_keeps_native_permit(runtime_world, cancel):
    source = GatedSource()
    manager = runtime_world["manager"](source, max_listeners=1)

    async def exercise() -> None:
        task = asyncio.create_task(
            manager.test_connection(1, {"source_type": "kafka"}, {}, timeout=0.1)
        )
        async with asyncio.timeout(2):
            while not source.probe_entered.is_set():
                await asyncio.sleep(0.001)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SourceFailure) as error:
                await task
            assert error.value.code == "source_connection_timeout"
        assert manager.active_count == 1 and not source.probe_closed.is_set()
        with pytest.raises(SourceFailure) as error:
            await manager.test_connection(2, {"source_type": "kafka"}, {}, timeout=0.1)
        assert error.value.code == "subscription_capacity_exceeded"
        source.probe_close.set()
        async with asyncio.timeout(3):
            while manager.active_count:
                await asyncio.sleep(0.005)
        assert source.probe_closed.is_set()
        assert (await manager.test_connection(2, {"source_type": "kafka"}, {}))["connected"]

    asyncio.run(exercise())


@pytest.mark.parametrize("closed", [False, True])
def test_exception_releases_buffer_and_permit_only_when_close_is_confirmed(
    runtime_world, session_factory, closed
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource(failure=SourceFailure("source_network_failed", "backoff", closed=closed))
    manager = runtime_world["manager"](source, max_listeners=1)
    assert manager.start_once(subscription_id)
    source.contexts.get(timeout=5)
    manager.stop_single(subscription_id)
    source.stop_observations.get(timeout=5)
    source.allow_close(subscription_id)
    eventually(lambda: subscription_id in source.closed)
    if closed:
        eventually(lambda: manager.active_count == 0)
        assert manager.buffer.used_bytes == 0
    else:

        def reported_unconfirmed() -> bool:
            with session_factory() as session:
                return (
                    session.get(EventSubscription, subscription_id).last_error_code
                    == "source_close_unconfirmed"
                )

        eventually(reported_unconfirmed)
        assert manager.active_count == 1 and manager.buffer.used_bytes == 1024
        assert not manager.start_once(subscription_id)


def test_lease_renewal_is_independent_and_expired_owner_cannot_write(
    runtime_world, session_factory
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source)
    assert manager.start_once(subscription_id)
    context = source.contexts.get(timeout=5)
    with session_factory.begin() as session:
        session.get(SubscriptionLease, subscription_id).lease_until = database_now(
            session
        ) + timedelta(seconds=1)
    manager.renew_once()
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, subscription_id)
        assert lease.lease_until > database_now(session) + timedelta(seconds=28)
        assert lease.generation == context.generation
        lease.lease_until = database_now(session) - timedelta(seconds=1)
    manager.renew_once()
    assert context.stop_event.is_set()
    assert manager.active_count == 1
    with session_factory() as session:
        with pytest.raises(HTTPException) as error:
            receive(session, context)
        assert error.value.detail["code"] == "subscription_owner_stale"


def test_feature_off_stops_reception_but_keeps_t2_drain(
    runtime_world, session_factory, monkeypatch
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source)
    assert manager.start_once(subscription_id)
    context = source.contexts.get(timeout=5)
    with session_factory.begin() as session:
        receipt = receive(session, context)
        receipt_id = receipt.id
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    manager.reconcile_once()
    assert context.stop_event.is_set() and manager.active_count == 1
    with session_factory() as session:
        with pytest.raises(HTTPException):
            receive(session, context, 1)
        session.rollback()
        pending = session.scalar(
            select(EventDelivery).where(EventDelivery.receipt_id == receipt_id)
        )
        execution = delivery.dispatch_delivery(session, pending.id)
        session.commit()
        assert execution is not None and execution.trigger == "subscription"
        assert session.get(EventReceipt, receipt_id).state == "processing"
    assert not manager.start_once(subscription_id)


def test_aclose_without_supervisor_fences_and_keeps_unfinished_native_permit(runtime_world):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source)
    assert manager.start_once(subscription_id)
    context = source.contexts.get(timeout=5)
    asyncio.run(manager.aclose(timeout=0.1))
    assert context.stop_event.is_set()
    _, old_generation, fenced_generation = source.stop_observations.get(timeout=5)
    assert fenced_generation > old_generation
    assert manager.active_count == 1 and manager.buffer.used_bytes == 1024
    assert subscription_id not in source.closed
    source.allow_close(subscription_id)
    eventually(lambda: manager.active_count == 0)
    assert manager.buffer.used_bytes == 0


def test_db_close_ack_failure_keeps_permit_until_reconciled(
    runtime_world, session_factory, monkeypatch
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source)
    assert manager.start_once(subscription_id)
    source.contexts.get(timeout=5)
    with session_factory.begin() as session:
        session.get(EventSubscription, subscription_id).desired_state = "disabled"
    manager.stop_single(subscription_id)
    attempted = threading.Event()
    acknowledge = management.listener_closed

    def unavailable(*args: Any, **kwargs: Any) -> None:
        attempted.set()
        raise RuntimeError("Injected acknowledgement transaction outage")

    monkeypatch.setattr(management, "listener_closed", unavailable)
    source.allow_close(subscription_id)
    assert attempted.wait(5)
    assert manager.active_count == 1 and manager.buffer.used_bytes == 1024
    assert not manager.start_once(subscription_id)
    monkeypatch.setattr(management, "listener_closed", acknowledge)
    manager.reconcile_once()
    eventually(lambda: manager.active_count == 0)
    assert manager.buffer.used_bytes == 0
    with session_factory() as session:
        assert session.get(EventSubscription, subscription_id).connection_state == "stopped"
        assert session.get(SubscriptionLease, subscription_id).owner_id == ""


def test_shutdown_cannot_leave_an_owner_that_finishes_starting_late(
    runtime_world, session_factory, monkeypatch
):
    subscription_id = runtime_world["subscriptions"]()[0]
    source = GatedSource()
    manager = runtime_world["manager"](source)
    entered = threading.Event()
    proceed = threading.Event()
    results: Queue[bool] = Queue()
    acquire = delivery.acquire_lease

    def gated_acquire(*args: Any, **kwargs: Any):
        entered.set()
        assert proceed.wait(5)
        return acquire(*args, **kwargs)

    monkeypatch.setattr(delivery, "acquire_lease", gated_acquire)
    starting = threading.Thread(target=lambda: results.put(manager.start_once(subscription_id)))
    starting.start()
    try:
        assert entered.wait(5)
        assert manager.active_count == 1
        asyncio.run(manager.aclose(timeout=0.1))
    finally:
        proceed.set()
        starting.join(timeout=5)
    assert not starting.is_alive()
    assert not results.empty()
    try:
        context = source.contexts.get(timeout=0.1)
    except Empty:
        pass  # Cancelling the startup before the native driver opens is also valid.
    else:
        assert context.stop_event.is_set(), "Owner started after shutdown without a stop fence"
        source.allow_close(subscription_id)
    eventually(lambda: manager.active_count == 0)
    with session_factory() as session:
        lease = session.get(SubscriptionLease, subscription_id)
        assert lease is None or lease.owner_id == ""


@pytest.mark.skipif(
    not os.environ.get("DLR_KAFKA_TEST_CONFIG"), reason="isolated Kafka not configured"
)
@pytest.mark.parametrize("fault", ["drop_request", "drop_response", "late_request"])
def test_live_offset_commit_fault_windows(runtime_world, session_factory, fault):
    """Real API 8 failures: source offset is an independent oracle of PG custody."""
    from kafka import KafkaAdminClient, KafkaProducer, TopicPartition
    from kafka.admin import NewTopic

    from dlr.control.models import SourceCheckpoint
    from dlr.control.services.subscription_kafka import KafkaPythonTransport, KafkaSourceRunner

    path = Path(__file__).resolve().parents[2] / "tools/subscriptions/probe_kafka_ack_fault.py"
    spec = importlib.util.spec_from_file_location("issue219_ack_fault_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = json.loads(Path(os.environ["DLR_KAFKA_TEST_CONFIG"]).read_text())
    connection.pop("log_level", None)
    if "bootstrap.servers" in connection:
        connection["bootstrap_servers"] = connection.pop("bootstrap.servers")
    assert connection.get("security_protocol", "PLAINTEXT") == "PLAINTEXT"
    bootstrap = connection["bootstrap_servers"]
    if isinstance(bootstrap, list):
        assert len(bootstrap) == 1
        bootstrap = bootstrap[0]
    host, port = bootstrap.rsplit(":", 1)
    proxy = module.OffsetCommitFaultProxy(host, int(port), fault)
    admin = KafkaAdminClient(**connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000)
    topic = "dlr-issue219-ack-" + uuid.uuid4().hex[:12]
    group = topic + "-group"
    partition = TopicPartition(topic, 0)
    report: dict[str, Any] = {
        "fault": fault,
        "passed": False,
        "phase": "create_topic",
        "product_sha256": {
            value.__name__: hashlib.sha256(Path(inspect.getfile(value)).read_bytes()).hexdigest()
            for value in (KafkaSourceRunner, SubscriptionRuntimeManager)
        },
    }
    created = False
    producer = manager = None

    class RealSource:
        failure = None

        def __init__(self) -> None:
            self.contexts: Queue[SourceRunContext] = Queue()
            self.runner = KafkaSourceRunner()

        def run(self, context: SourceRunContext) -> None:
            self.contexts.put(context)
            self.runner.run(context)

        def test_connection(self, config, credentials):
            return self.runner.test_connection(config, credentials)

        def allow_all_close(self) -> None:
            proxy.armed.clear()
            proxy.allow_request.set()

    try:
        admin.create_topics(
            [NewTopic(topic, num_partitions=1, replication_factor=1)], timeout_ms=5000
        )
        created = True
        cluster_id = str(admin.describe_cluster()["cluster_id"])
        subscription_id = runtime_world["subscriptions"](
            source_configuration={
                "cluster_id": cluster_id,
                "bootstrap_servers": [f"127.0.0.1:{proxy.port}"],
                "topics": [topic],
                "group_id": group,
            }
        )[0]
        source = RealSource()
        manager = runtime_world["manager"](source)

        def facts() -> dict[str, Any]:
            with session_factory() as session:
                checkpoint = session.scalar(
                    select(SourceCheckpoint).where(
                        SourceCheckpoint.subscription_id == subscription_id
                    )
                )
                sub = session.get(EventSubscription, subscription_id)
                count = session.scalar(
                    select(func.count())
                    .select_from(EventReceipt)
                    .where(EventReceipt.subscription_id == subscription_id)
                )
                return {
                    "safe": int(checkpoint.position["offset"]) if checkpoint else None,
                    "confirmed": int(checkpoint.confirmed_position.get("offset", -1))
                    if checkpoint
                    else None,
                    "receipt_count": count,
                    "execution_count": session.scalar(select(func.count()).select_from(Execution)),
                    "connection_state": sub.connection_state,
                    "last_error_code": sub.last_error_code,
                }

        def broker_offset() -> int | None:
            values = admin.list_group_offsets({group: [partition]})[group]
            return values[partition].offset if partition in values else None

        report["phase"] = "initial_durable_start"
        assert manager.start_once(subscription_id)
        context = source.contexts.get(timeout=5)
        eventually(lambda: facts()["connection_state"] == "listening", timeout=20)
        assert facts()["safe"] == facts()["confirmed"] == broker_offset() == 0
        proxy.armed.set()
        producer = KafkaProducer(**connection, request_timeout_ms=5000, max_block_ms=5000)
        produced = producer.send(topic, partition=0, key=b"ack-id", value=b"ack-window").get(5)
        assert produced.offset == 0
        report["phase"] = "commit_request_intercepted_after_t1"
        assert proxy.request_seen.wait(15)
        accepted = facts()
        assert accepted["safe"] == accepted["receipt_count"] == 1
        assert accepted["confirmed"] == 0
        if fault == "late_request":
            assert broker_offset() == 0
            manager.stop_single(subscription_id)
            assert context.stop_event.is_set() and manager.active_count == 1
            with session_factory() as session:
                lease = session.get(SubscriptionLease, subscription_id)
                assert lease.generation > context.generation
                with pytest.raises(HTTPException) as error:
                    receive(session, context, 1)
                assert error.value.detail["code"] == "subscription_owner_stale"
            proxy.allow_request.set()
        report["phase"] = "native_closed_after_source_rpc"
        eventually(lambda: manager.active_count == 0, timeout=15)
        failed = facts()
        failed["broker_committed"] = broker_offset()
        report["after_fault"] = failed
        assert failed["safe"] == failed["receipt_count"] == 1
        assert failed["execution_count"] == 0
        assert failed["confirmed"] == 0
        assert failed["broker_committed"] == (0 if fault == "drop_request" else 1)
        assert proxy.response_seen.is_set() == (fault != "drop_request")
        if fault != "late_request":
            assert failed["connection_state"] == "reconnecting"
            assert failed["last_error_code"] == "source_temporarily_unavailable"
        proxy.armed.clear()
        report["phase"] = "restart_from_existing_custody"
        assert manager.start_once(subscription_id)
        replacement = source.contexts.get(timeout=5)
        assert replacement.generation > context.generation
        eventually(lambda: facts()["connection_state"] == "listening", timeout=20)
        recovered = facts()
        recovered["broker_committed"] = broker_offset()
        report["after_restart"] = recovered
        assert recovered["safe"] == recovered["confirmed"] == recovered["broker_committed"] == 1
        assert recovered["receipt_count"] == 1
        assert recovered["execution_count"] == 0
        report["passed"] = True
        report["scope"] = (
            "Real product owner/PG custody and Kafka API8; no T2/Worker/target delivery."
        )
        report["transport_class"] = KafkaPythonTransport.__name__
    finally:
        cleanup_errors = []
        proxy.armed.clear()
        proxy.allow_request.set()
        if manager is not None:
            manager.stop_all()
            try:
                eventually(lambda: manager.active_count == 0, timeout=15)
            except AssertionError:
                cleanup_errors.append("native_close_not_confirmed")
        proxy.close()
        if producer is not None:
            producer.close(timeout=5)
        if created:
            try:
                admin.delete_groups([group])
                admin.delete_topics([topic], timeout_ms=5000)
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        admin.close()
        report["cleanup_complete"] = not cleanup_errors
        report["cleanup_errors"] = cleanup_errors
        if directory := os.environ.get("DLR_KAFKA_FAULT_EVIDENCE_DIR"):
            output = Path(directory)
            output.mkdir(parents=True, exist_ok=True)
            (output / f"api8-{fault}.json").write_text(json.dumps(report, indent=2) + "\n")
        if report["passed"]:
            assert not cleanup_errors
