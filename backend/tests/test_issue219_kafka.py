"""Kafka owner/assignment protocol against PostgreSQL; optional isolated real Kafka."""

import base64
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

import pytest
from kafka import KafkaAdminClient, KafkaProducer, TopicPartition
from kafka.admin import NewTopic
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Credential,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    Worker,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_kafka as kafka
from dlr.control.services.secrets import decrypt_fields, encrypt_fields
from dlr.control.services.subscription_source import (
    ApplicationBufferBudget,
    SourceFailure,
    SourceRunContext,
)

P0 = kafka.Partition("events", 0)
P1 = kafka.Partition("events", 1)
IDENTITY = kafka.SourceIdentity("cluster", {"events": str(uuid.uuid4())}, (P0, P1))


@pytest.fixture
def context(api_client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    config = {
        "source_type": "kafka",
        "cluster_id": "cluster",
        "bootstrap_servers": ["broker.invalid:9092"],
        "topics": ["events"],
        "group_id": "dedicated-group",
        "initial_position": "earliest",
        "security_protocol": "PLAINTEXT",
    }
    with session_factory.begin() as session:
        worker = Worker(name="kafka-worker", status="online", capabilities=["python"])
        adapter = Adapter(
            name="kafka-adapter", language="python", adapter_type="task", run_mode="manual"
        )
        session.add_all([worker, adapter])
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        adapter.latest_version_id = revision.id
        adapter.runtime_worker_id = worker.id
        sub = EventSubscription(
            adapter_id=adapter.id,
            name="Kafka",
            source_type="kafka",
            source_epoch="epoch-1",
            desired_state="enabled",
        )
        session.add(sub)
        session.flush()
        cv = SubscriptionConfigVersion(
            subscription_id=sub.id, version=1, source_epoch="epoch-1", config=config
        )
        session.add(cv)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=cv.id,
            revision_id=revision.id,
            worker_id=worker.id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        lease = delivery.acquire_lease(session, sub.id, "kafka-owner")
        return SourceRunContext(
            sub.id,
            lease.owner_id,
            lease.generation,
            activation.id,
            1,
            sub.source_epoch,
            config,
            {},
            threading.Event(),
            session_factory,
            ApplicationBufferBudget().scope(),
            lambda: None,
        )


def offsets(begin=0, end=20, committed=None):
    return kafka.SourceOffsets(
        {P0: begin, P1: begin}, {P0: end, P1: end}, {P0: committed, P1: committed}
    )


def initialize(context, source_offsets=None, parts=(P0,), identity=IDENTITY):
    with context.session_factory.begin() as session:
        return kafka.initialize_assignment(
            session, context, identity, source_offsets or offsets(), parts
        )


def record(offset, partition=0, value=b"\x00\xff", key=b"", headers=None):
    return SimpleNamespace(
        topic="events",
        partition=partition,
        offset=offset,
        timestamp=1000,
        timestamp_type=0,
        key=key,
        value=value,
        headers=headers if headers is not None else [("same", b"a"), ("same", None), ("same", b"")],
    )


def test_initial_latest_and_new_partition_starts_are_persisted_once(context):
    context.source_config["initial_position"] = "latest"
    first = initialize(context, offsets(end=10))[P0]
    again = initialize(context, offsets(end=30))[P0]
    added = initialize(context, offsets(end=40), (P0, P1))
    assert first.offset == again.offset == added[P0].offset == 10
    assert added[P1].offset == 40
    assert (
        added[P0].assignment_generation > again.assignment_generation > first.assignment_generation
    )


@pytest.mark.parametrize("failure", ["ahead", "retention", "truncation", "uuid"])
def test_assignment_blocks_unproven_positions_and_source_changes(context, failure):
    initialize(context, offsets(begin=5, end=20))
    source_offsets, identity = offsets(begin=5, end=20), IDENTITY
    expected = "source_history_gap"
    if failure == "ahead":
        source_offsets, expected = offsets(begin=5, committed=6), "source_checkpoint_ahead"
    elif failure == "retention":
        source_offsets = offsets(begin=6)
    elif failure == "truncation":
        source_offsets = offsets(end=4)
    else:
        identity = replace(IDENTITY, topic_ids={"events": str(uuid.uuid4())})
        expected = "source_identity_changed"
    with pytest.raises(SourceFailure, match=expected):
        initialize(context, source_offsets, identity=identity)


def test_explicit_new_epoch_authorizes_initial_group_rewind_until_ack(context):
    with context.session_factory.begin() as session:
        original = session.scalar(select(SubscriptionConfigVersion))
        sub = session.get(EventSubscription, context.subscription_id)
        sub.source_epoch = "epoch-2"
        sub.config_version = 2
        session.add(
            SubscriptionConfigVersion(
                subscription_id=sub.id, version=2, source_epoch="epoch-2", config=original.config
            )
        )
    updated = replace(context, source_epoch="epoch-2", config_version=2)
    first = initialize(updated, offsets(committed=15))[P0]
    assert first.offset == 0 and first.reset_pending
    # A crash between initialized-DB commit and broker ACK must not lose authorization.
    retry = initialize(updated, offsets(committed=15))[P0]
    with updated.session_factory.begin() as session:
        assert kafka.confirm_checkpoint(session, updated, retry)
    with pytest.raises(SourceFailure, match="source_checkpoint_ahead"):
        initialize(updated, offsets(committed=15))


def test_existing_foreign_group_cannot_authorize_first_earliest_rewind(context):
    with pytest.raises(SourceFailure, match="source_checkpoint_ahead"):
        initialize(context, offsets(committed=1))
    with context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0


def test_old_assignment_and_old_owner_ack_cannot_update_new_checkpoint(context):
    old = initialize(context)[P0]
    new = initialize(context)[P0]
    with context.session_factory.begin() as session:
        assert not kafka.confirm_checkpoint(session, context, old)
        assert kafka.confirm_checkpoint(session, context, new)
        lease = session.get(SubscriptionLease, context.subscription_id)
        lease.generation += 1
    with (
        pytest.raises(SourceFailure, match="subscription_owner_stale"),
        context.session_factory.begin() as session,
    ):
        kafka.confirm_checkpoint(session, context, new)


def test_ack_cannot_advance_beyond_durable_custody(context):
    plan = initialize(context)[P0]
    with context.session_factory.begin() as session:
        assert not kafka.confirm_checkpoint(session, context, replace(plan, offset=100))
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.confirmed_position == {}


def test_binary_null_tombstone_and_duplicate_headers_are_lossless(context):
    for value in [b"\x00\xff", b"", None]:
        envelope = kafka.build_envelope(context, IDENTITY, record(0, value=value))
        kafka.validate_envelope(envelope)
        encoded = envelope["data"]["value"]
        assert (None if encoded is None else base64.b64decode(encoded)) == value
        assert envelope["source"]["attributes"]["key_base64"] == ""
        assert envelope["source"]["attributes"]["headers"] == [
            {"name": "same", "value_base64": "YQ=="},
            {"name": "same", "value_base64": None},
            {"name": "same", "value_base64": ""},
        ]


class FakeTransport:
    def __init__(self, context, records=(), fail_commit=None, revoke_commit=None):
        self.context = context
        self.records = list(records)
        self.listener = None
        self.initialized = False
        self.closed = False
        self.commits = []
        self.seeks = []
        self.paused = set()
        self.fail_commit = fail_commit
        self.revoke_commit = revoke_commit
        self.member_list = ("member",)
        self.identity = IDENTITY
        self.poll_count = 0

    def metadata(self):
        return self.identity

    def members(self):
        return self.member_list if self.listener else ()

    def member_id(self):
        return "member"

    def subscribe(self, listener):
        self.listener = listener

    def poll(self):
        self.poll_count += 1
        if not self.initialized:
            self.initialized = True
            self.listener.on_partitions_assigned([TopicPartition("events", 0)])
            return {}
        if self.records and P0 not in self.paused:
            return {P0: [self.records.pop(0)]}
        self.context.stop_event.set()
        return {}

    def offsets(self, partitions):
        return offsets()

    def seek(self, partition, offset):
        self.seeks.append((partition, offset))

    def pause(self, partitions):
        self.paused.update(partitions)

    def resume(self, partitions):
        self.paused.difference_update(partitions)

    def commit(self, source_offsets):
        self.commits.append(source_offsets)
        if self.fail_commit is not None and len(self.commits) == self.fail_commit:
            raise TimeoutError("private broker password must not escape")
        if self.revoke_commit is not None and len(self.commits) == self.revoke_commit:
            self.listener.on_partitions_revoked([TopicPartition("events", 0)])

    def close(self):
        self.closed = True


def test_owner_loop_safe_offset_holes_do_not_invent_missing_records(context):
    transport = FakeTransport(context, [record(10), record(14)])
    kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert transport.closed
    assert [x[P0] for x in transport.commits] == [0, 11, 15]
    with context.session_factory() as session:
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.position["offset"] == cp.confirmed_position["offset"] == "15"
        receipts = session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)).all()
        assert [r.envelope["source"]["position"]["offset"] for r in receipts] == ["10", "14"]
    assert context.buffer.used_bytes == 0


def test_commit_failure_preserves_receipt_and_safe_checkpoint(context):
    transport = FakeTransport(context, [record(0)], fail_commit=2)
    with pytest.raises(SourceFailure, match="source_temporarily_unavailable") as error:
        kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert error.value.disposition == "backoff" and transport.closed
    with context.session_factory() as session:
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.position["offset"] == "1" and cp.confirmed_position["offset"] == "0"
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 1
    # Redelivery at the same offset is folded into the original retained receipt.
    context.stop_event.clear()
    restarted = FakeTransport(context, [record(0)])
    kafka.KafkaSourceRunner(lambda *args: restarted).run(context)
    with context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 1


def test_revoke_during_source_commit_discards_old_generation_ack(context):
    transport = FakeTransport(context, [record(0)], revoke_commit=2)
    kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    with context.session_factory() as session:
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.position["offset"] == "1"
        assert cp.confirmed_position["offset"] == "0"


def test_revoke_between_t1_and_commit_rolls_back_receipt(context, monkeypatch):
    plan = initialize(context)[P0]
    tracker = kafka.AssignmentTracker()
    tracker.on_partitions_assigned([TopicPartition("events", 0)])
    token, _ = tracker.snapshot()
    actual = delivery.receive_event

    def raced(*args, **kwargs):
        result = actual(*args, **kwargs)
        tracker.on_partitions_revoked([TopicPartition("events", 0)])
        return result

    monkeypatch.setattr(delivery, "receive_event", raced)
    assert (
        kafka.KafkaSourceRunner._receive(context, tracker, token, IDENTITY, P0, plan, record(0))
        is None
    )
    with context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.scalar(select(SourceCheckpoint)).position["offset"] == "0"


def test_old_assignment_cannot_publish_new_generation_poison_diagnosis(context):
    old = initialize(context)[P0]
    tracker = kafka.AssignmentTracker()
    tracker.on_partitions_assigned([TopicPartition("events", 0)])
    token, _ = tracker.snapshot()
    tracker.on_partitions_revoked([TopicPartition("events", 0)])
    assert not kafka.KafkaSourceRunner._reject(
        context,
        IDENTITY,
        P0,
        record(0),
        "event_payload_too_large",
        old.assignment_generation,
        tracker,
        token,
    )
    with context.session_factory() as session:
        assert session.scalar(select(SourceCheckpoint)).blocked_unaccepted is None


def test_oversize_is_bounded_diagnosis_without_receipt_or_source_ack(context):
    transport = FakeTransport(context, [record(0, value=b"x" * (512 * 1024 + 1))])
    kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    with context.session_factory() as session:
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.blocked_unaccepted["code"] == "event_payload_too_large"
        assert len(json.dumps(cp.blocked_unaccepted)) < 2048
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
    assert [x[P0] for x in transport.commits] == [0]


def test_external_member_and_uuid_change_block_before_receive(context):
    transport = FakeTransport(context, [record(0)])
    original = transport.metadata

    def changed():
        if len(transport.commits) == 1:
            return replace(IDENTITY, topic_ids={"events": str(uuid.uuid4())})
        return original()

    transport.metadata = changed
    with pytest.raises(SourceFailure, match="source_identity_changed"):
        kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert transport.closed
    with context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
    transport = FakeTransport(context)
    transport.member_list = ("member", "external")
    with pytest.raises(SourceFailure, match="source_group_conflict"):
        kafka.KafkaSourceRunner(lambda *args: transport).run(context)


def test_native_close_failure_retains_unconfirmed_resource_state(context):
    transport = FakeTransport(context)

    def cannot_close():
        raise SourceFailure("source_close_unconfirmed", closed=False)

    transport.close = cannot_close
    with pytest.raises(SourceFailure) as error:
        kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert not error.value.closed


def test_sdk_io_thread_callback_only_updates_bounded_tokens():
    tracker = kafka.AssignmentTracker()
    thread = threading.Thread(
        target=lambda: tracker.on_partitions_assigned(
            [TopicPartition("events", p) for p in range(16)]
        )
    )
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    token, partitions = tracker.snapshot()
    assert token == 1 and len(partitions) == 16
    tracker.on_partitions_assigned([TopicPartition("events", 17)])
    with pytest.raises(SourceFailure, match="source_assignment_invalid"):
        tracker.snapshot()


def test_new_assignment_no_offset_error_enters_initialization_without_auto_reset(context):
    class FirstPollWithoutCommit(FakeTransport):
        def poll(self):
            if not self.initialized:
                super().poll()
                raise kafka.NoOffsetForPartitionError("no broker offset yet")
            return super().poll()

    transport = FirstPollWithoutCommit(context, [record(0)])
    kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert [item[P0] for item in transport.commits] == [0, 1]


def test_capacity_pause_resumes_from_db_after_explicit_pending_disposition(context):
    config = {**context.source_config, "limits": {"pending_count": 1}}
    context = replace(context, source_config=config)
    with context.session_factory.begin() as session:
        cv = session.scalar(select(SubscriptionConfigVersion))
        cv.config = config

    class RecoveringTransport(FakeTransport):
        recovered = False

        def poll(self):
            if self.paused and not self.recovered:
                self.recovered = True
                with context.session_factory.begin() as session:
                    first = session.scalar(select(EventReceipt))
                    delivery.skip_event(session, first.id, "release-pending", None, "verified skip")
                self.records.append(record(1))
                return {}
            if self.paused:
                time.sleep(0.01)
                return {}
            return super().poll()

    transport = RecoveringTransport(context, [record(0), record(1)])
    kafka.KafkaSourceRunner(lambda *args: transport).run(context)
    assert [item[P0] for item in transport.commits] == [0, 1, 2]
    assert (P0, 1) in transport.seeks
    with context.session_factory() as session:
        receipts = session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)).all()
        assert [r.state for r in receipts] == ["skipped", "pending"]
        assert session.scalar(select(SourceCheckpoint)).blocked_unaccepted is None


def test_real_sdk_constructor_failure_and_debug_logging_do_not_leak(context, monkeypatch, caplog):
    # Logging setup may attach shared handlers directly to a non-propagating
    # SDK parent or leaf; a NullHandler alone does not consume those handlers.
    sdk_root = logging.getLogger("kafka")
    sdk_leaf = logging.getLogger("kafka.consumer.fetcher")
    for sdk_logger in (sdk_root, sdk_leaf):
        monkeypatch.setattr(sdk_logger, "handlers", [*sdk_logger.handlers, caplog.handler])
    root_handlers = tuple(logging.getLogger().handlers)
    capture_was_closed = caplog.handler._closed
    # An unused loopback socket provides immediate refusal, without external DNS.
    with socket.socket() as refused:
        refused.bind(("127.0.0.1", 0))
        endpoint = "127.0.0.1:" + str(refused.getsockname()[1])
        config = {
            **context.source_config,
            "bootstrap_servers": [endpoint],
            "security_protocol": "SASL_SSL",
            "sasl_mechanism": "SCRAM-SHA-256",
        }
        monkeypatch.setitem(kafka.CLIENT_BUDGET, "bootstrap_timeout_ms", 100)
        baseline = {t.ident for t in threading.enumerate()}
        caplog.set_level(logging.DEBUG)
        with pytest.raises(SourceFailure) as failure:
            kafka.KafkaPythonTransport(
                config,
                {"username": "private-user", "password": "private-password-219"},
                "dlr-failure-test",
            )
        logging.getLogger("kafka.consumer.fetcher").debug("record=%r", b"private-raw-value-219")
        logging.getLogger("dlr.control.subscription_runtime").warning("%s", failure.value.code)
        assert tuple(logging.getLogger().handlers) == root_handlers
        assert caplog.handler._closed == capture_was_closed
        assert caplog.handler not in sdk_root.handlers
        assert caplog.handler not in sdk_leaf.handlers
        assert all(
            not record.name.startswith("kafka.") and record.name != "kafka"
            for record in caplog.records
        )
        assert failure.value.code in caplog.text
        assert "private-password-219" not in caplog.text
        assert "private-raw-value-219" not in caplog.text
        assert "private-user" not in caplog.text
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            extra = [
                t for t in threading.enumerate() if t.ident not in baseline and "kafka" in t.name
            ]
            if not extra:
                break
            time.sleep(0.01)
        assert not extra


@pytest.mark.skipif(
    not os.environ.get("DLR_KAFKA_TEST_CONFIG"), reason="isolated Kafka not configured"
)
@pytest.mark.parametrize("initial_position", ["earliest", "latest"])
def test_live_kafka_two_partitions_restart_and_binary_custody(context, initial_position):
    # Only resources with a random task-owned prefix are created/deleted.
    from pathlib import Path

    connection = json.loads(Path(os.environ["DLR_KAFKA_TEST_CONFIG"]).read_text())
    connection.pop("log_level", None)
    if "bootstrap.servers" in connection:
        connection["bootstrap_servers"] = connection.pop("bootstrap.servers")
    admin = KafkaAdminClient(**connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000)
    name = "dlr-issue219-test-" + uuid.uuid4().hex[:12]
    producer = None
    thread = None
    try:
        admin.create_topics(
            [NewTopic(name, num_partitions=2, replication_factor=1)], timeout_ms=5000
        )
        cluster_id = admin.describe_cluster()["cluster_id"]
        config = {
            **context.source_config,
            "cluster_id": cluster_id,
            "topics": [name],
            "bootstrap_servers": connection["bootstrap_servers"],
            "group_id": name + "-group",
            "initial_position": initial_position,
        }
        if isinstance(config["bootstrap_servers"], str):
            config["bootstrap_servers"] = [config["bootstrap_servers"]]
        live = replace(context, source_config=config)
        producer = KafkaProducer(**connection, request_timeout_ms=5000, max_block_ms=5000)
        for partition, value in [(0, b"\x00\xfffirst"), (0, None), (1, b"")]:
            producer.send(
                name, partition=partition, key=b"", value=value, headers=[("same", b"a")]
            ).get(5)
        failures = []
        connected = threading.Event()
        live = replace(live, connected=connected.set)

        def receive():
            try:
                kafka.KafkaSourceRunner().run(live)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=receive)
        thread.start()
        assert connected.wait(15), [
            (str(error), type(error.__context__).__name__) for error in failures
        ]
        if initial_position == "latest":
            # Stop after persisted initialization but before any new receipt,
            # then publish between owners: restarting must not re-evaluate latest.
            live.stop_event.set()
            thread.join(15)
            assert not thread.is_alive() and not failures
            with live.session_factory() as session:
                assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
                starts = {
                    cp.shard: cp.position["offset"]
                    for cp in session.scalars(select(SourceCheckpoint))
                }
                assert sorted(starts.values()) == ["1", "2"]
            for partition, value in [(0, b"\x00\xfffirst"), (0, None), (1, b"")]:
                producer.send(name, partition=partition, key=b"", value=value).get(5)
            live.stop_event.clear()
            thread = threading.Thread(target=receive)
            thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and thread.is_alive():
            with live.session_factory() as session:
                count = session.scalar(select(func.count()).select_from(EventReceipt))
            if count == 3:
                break
            time.sleep(0.05)
        live.stop_event.set()
        thread.join(15)
        assert not thread.is_alive()
        assert not failures, [
            (type(error).__name__, str(error), type(error.__context__).__name__)
            for error in failures
        ]
        with live.session_factory() as session:
            receipts = session.scalars(select(EventReceipt)).all()
            assert len(receipts) == 3
            values = [r.envelope["data"]["value"] for r in receipts]
            assert sorted(v for v in values if v is not None) == [
                "",
                base64.b64encode(b"\x00\xfffirst").decode(),
            ]
            assert None in values
            cps = session.scalars(select(SourceCheckpoint)).all()
            assert len(cps) == 2
            assert all(
                cp.position["topic_id"] and cp.position["cluster_id"] == cluster_id for cp in cps
            )
    finally:
        context.stop_event.set()
        if thread is not None:
            thread.join(15)
        if producer is not None:
            producer.close(timeout=5)
        admin.delete_topics([name], timeout_ms=5000)
        admin.delete_groups([name + "-group"])
        admin.close()


@pytest.mark.skipif(
    not os.environ.get("DLR_KAFKA_AUTH_TEST_CONFIG"), reason="isolated TLS/SCRAM not configured"
)
@pytest.mark.parametrize("case", ["scram256", "scram512", "wrong_password", "untrusted_ca"])
def test_live_tls_scram_typed_failure_and_native_reclamation(context, case):
    from pathlib import Path

    private = json.loads(Path(os.environ["DLR_KAFKA_AUTH_TEST_CONFIG"]).read_text())
    config = {
        **context.source_config,
        "cluster_id": private["cluster_id"],
        "bootstrap_servers": private["bootstrap_servers"],
        "topics": ["dlr219-connection-check"],
        "group_id": "dlr219-connection-check",
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "SCRAM-SHA-512" if case == "scram512" else "SCRAM-SHA-256",
        "ca_pem": None if case == "untrusted_ca" else Path(private["ca_file"]).read_text(),
    }
    fields = {
        "username": private["username"],
        "password": "deliberately-wrong-password"
        if case == "wrong_password"
        else private["password"],
    }
    baseline = {t.ident for t in threading.enumerate()}
    if case in {"scram256", "scram512"}:
        transport = kafka.KafkaPythonTransport(config, fields, "dlr219-live-auth-test")
        transport.close()
    else:
        expected = (
            "source_authentication_failed" if case == "wrong_password" else "source_tls_failed"
        )
        with pytest.raises(SourceFailure, match=expected) as failure:
            kafka.KafkaPythonTransport(config, fields, "dlr219-live-auth-test")
        assert failure.value.disposition == "blocked" and failure.value.closed
    assert not [
        t for t in threading.enumerate() if t.ident not in baseline and "kafka-io" in t.name
    ]


@pytest.mark.skipif(
    not os.environ.get("DLR_KAFKA_TEST_CONFIG"), reason="isolated Kafka not configured"
)
def test_live_external_group_retention_gap_and_topic_uuid_recreation(context):
    from pathlib import Path

    connection = json.loads(Path(os.environ["DLR_KAFKA_TEST_CONFIG"]).read_text())
    connection.pop("log_level", None)
    if "bootstrap.servers" in connection:
        connection["bootstrap_servers"] = connection.pop("bootstrap.servers")
    admin = KafkaAdminClient(**connection, request_timeout_ms=3000, bootstrap_timeout_ms=3000)
    name = "dlr-issue219-test-" + uuid.uuid4().hex[:12]
    transport = producer = foreign = None
    try:
        admin.create_topics(
            [NewTopic(name, num_partitions=1, replication_factor=1)], timeout_ms=3000
        )
        config = {
            **context.source_config,
            "cluster_id": admin.describe_cluster()["cluster_id"],
            "bootstrap_servers": connection["bootstrap_servers"],
            "topics": [name],
            "group_id": name + "-group",
        }
        if isinstance(config["bootstrap_servers"], str):
            config["bootstrap_servers"] = [config["bootstrap_servers"]]
        live = replace(context, source_config=config)
        transport = kafka.KafkaPythonTransport(config, {}, "dlr-live-source")
        identity = transport.metadata()
        partition = identity.partitions[0]
        source_offsets = transport.offsets((partition,))
        with live.session_factory.begin() as session:
            kafka.initialize_assignment(session, live, identity, source_offsets, (partition,))
        foreign = kafka.KafkaConsumer(
            **connection, **kafka.CLIENT_BUDGET, group_id=config["group_id"]
        )
        foreign.subscribe([name])
        deadline = time.monotonic() + 8
        while not foreign.assignment() and time.monotonic() < deadline:
            with suppress(kafka.NoOffsetForPartitionError):
                foreign.poll(timeout_ms=200)
        assert foreign.assignment()
        with pytest.raises(SourceFailure, match="source_group_conflict"):
            kafka.KafkaSourceRunner().test_connection(config, {})
        foreign.close(autocommit=False, timeout_ms=3000)
        foreign = None
        producer = KafkaProducer(**connection, request_timeout_ms=3000, max_block_ms=3000)
        producer.send(name, partition=0, value=b"expired").get(3)
        admin.delete_records({TopicPartition(name, 0): 1}, timeout_ms=3000)
        actual = transport.offsets((partition,))
        assert actual.beginning[partition] == 1
        with (
            pytest.raises(SourceFailure, match="source_history_gap"),
            live.session_factory.begin() as session,
        ):
            kafka.initialize_assignment(session, live, identity, actual, (partition,))
        old_id = identity.topic_ids[name]
        admin.delete_topics([name], timeout_ms=3000)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                admin.create_topics(
                    [NewTopic(name, num_partitions=1, replication_factor=1)], timeout_ms=3000
                )
                identity = transport.metadata()
                if identity.topic_ids[name] != old_id:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        assert identity.topic_ids[name] != old_id
        actual = transport.offsets((partition,))
        with (
            pytest.raises(SourceFailure, match="source_identity_changed"),
            live.session_factory.begin() as session,
        ):
            kafka.initialize_assignment(session, live, identity, actual, (partition,))
        with live.session_factory() as session:
            cp = session.scalar(select(SourceCheckpoint))
            assert cp.position["topic_id"] == old_id
            assert cp.source_epoch == context.source_epoch
    finally:
        if foreign is not None:
            foreign.close(autocommit=False, timeout_ms=3000)
        if transport is not None:
            transport.close()
        if producer is not None:
            producer.close(timeout=3)
        admin.delete_topics([name], timeout_ms=3000)
        admin.delete_groups([name + "-group"])
        admin.close()


@pytest.mark.skipif(
    not os.environ.get("DLR_KAFKA_AUTH_ROTATOR"),
    reason="isolated credential rotation not configured",
)
def test_live_credential_rotation_preserves_epoch_and_latest_checkpoint(context):
    from pathlib import Path

    private_path = Path(os.environ["DLR_KAFKA_AUTH_TEST_CONFIG"])
    private = json.loads(private_path.read_text())
    name = "dlr219-rotation-" + uuid.uuid4().hex[:12]
    fields = {"username": private["username"], "password": private["password"]}
    config = {
        **context.source_config,
        "cluster_id": private["cluster_id"],
        "bootstrap_servers": private["bootstrap_servers"],
        "topics": [name],
        "group_id": name + "-group",
        "initial_position": "latest",
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "SCRAM-SHA-256",
        "ca_pem": Path(private["ca_file"]).read_text(),
    }
    # The source principal deliberately has no Delete permission. Record exact
    # owned resources for the fixture administrator's bounded cleanup helper.
    inventory = private_path.parent / "b2-owned-resources.json"
    owned = json.loads(inventory.read_text()) if inventory.exists() else []
    owned.append({"topic": name, "group": config["group_id"]})
    inventory.write_text(json.dumps(owned, indent=2))
    inventory.chmod(0o600)
    failures = []
    thread = None
    admin_transport = kafka.KafkaPythonTransport(config, fields, "dlr219-rotation-admin")
    try:
        admin_transport.admin.create_topics(
            [NewTopic(name, num_partitions=1, replication_factor=1)], timeout_ms=3000
        )
        producer = KafkaProducer(
            **kafka.connection_options(kafka.KafkaSourceConfig.model_validate(config), fields),
            max_block_ms=3000,
        )
        try:
            producer.send(name, partition=0, value=b"before-first-latest").get(3)
        finally:
            producer.close(timeout=3)
        with context.session_factory.begin() as session:
            credential = Credential(
                name="source-auth", type="password", ciphertext=encrypt_fields(fields)
            )
            session.add(credential)
            session.flush()
            credential_id = credential.id
            cv = session.scalar(select(SubscriptionConfigVersion))
            cv.config = {**config, "credential_id": credential_id}
            cv.source_credential_id = credential_id
        connected = threading.Event()
        live = replace(
            context,
            source_config={**config, "credential_id": credential_id},
            credential_fields=dict(fields),
            connected=connected.set,
        )

        def receive():
            try:
                kafka.KafkaSourceRunner().run(live)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=receive)
        thread.start()
        assert connected.wait(15), [
            (str(error), type(error.__context__).__name__) for error in failures
        ]
        live.stop_event.set()
        thread.join(15)
        assert not thread.is_alive() and not failures
        admin_transport.close()
        with live.session_factory() as session:
            saved = session.scalar(select(SourceCheckpoint))
            original_id = saved.id
            original_uuid = saved.position["topic_id"]
            assert saved.position["offset"] == "1"
            assert saved.source_epoch == live.source_epoch
        # Explicitly authorized, task-owned fixture helper; stdout never enters logs.
        rotated = subprocess.run(
            [sys.executable, os.environ["DLR_KAFKA_AUTH_ROTATOR"]],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert rotated.returncode == 0, "isolated credential rotator failed"
        rejected = False
        for _ in range(3):
            try:
                stale = kafka.KafkaPythonTransport(config, fields, "dlr219-rotation-old-password")
                stale.close()
            except SourceFailure as error:
                if error.code == "source_authentication_failed":
                    assert error.disposition == "blocked"
                    rejected = True
                    break
                assert error.disposition == "backoff", error.code
            # AlterUserScramCredentials completion does not make each broker's
            # applied SCRAM metadata or an in-flight handshake atomic with CLI exit.
            time.sleep(0.2)
        assert rejected, "old credential was not rejected after bounded rotation propagation"
        replacement = json.loads(private_path.read_text())
        new_fields = {"username": replacement["username"], "password": replacement["password"]}
        with live.session_factory.begin() as session:
            credential = session.get(Credential, credential_id)
            credential.ciphertext = encrypt_fields(new_fields)
            resolved = decrypt_fields(credential.ciphertext)
        producer = KafkaProducer(
            **kafka.connection_options(kafka.KafkaSourceConfig.model_validate(config), resolved),
            max_block_ms=3000,
        )
        try:
            producer.send(name, partition=0, value=b"after-rotation").get(3)
        finally:
            producer.close(timeout=3)
        live = replace(live, credential_fields=resolved)
        live.stop_event.clear()
        connected.clear()
        thread = threading.Thread(target=receive)
        thread.start()
        deadline = time.monotonic() + 15
        count = 0
        while thread.is_alive() and time.monotonic() < deadline:
            with live.session_factory() as session:
                count = session.scalar(select(func.count()).select_from(EventReceipt))
            if count == 1:
                break
            time.sleep(0.05)
        live.stop_event.set()
        thread.join(15)
        assert not thread.is_alive() and not failures
        assert count == 1
        with live.session_factory() as session:
            receipt = session.scalar(select(EventReceipt))
            saved = session.scalar(select(SourceCheckpoint))
            assert receipt.envelope["source"]["position"]["offset"] == "1"
            assert base64.b64decode(receipt.envelope["data"]["value"]) == b"after-rotation"
            assert saved.id == original_id and saved.source_epoch == context.source_epoch
            assert saved.position["topic_id"] == original_uuid and saved.position["offset"] == "2"
            cv = session.scalar(select(SubscriptionConfigVersion))
            assert cv.source_credential_id == credential_id
    finally:
        context.stop_event.set()
        if thread is not None:
            thread.join(15)
        admin_transport.close()
