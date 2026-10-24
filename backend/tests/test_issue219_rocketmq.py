"""RocketMQ protocol-specific tests; no official high-level SDK threads are started."""

import gzip
import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rocketmq.grpc_protocol import definition_pb2 as d
from rocketmq.grpc_protocol import service_pb2 as s

from dlr.control.models import EventSubscription
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_queue as queue
from dlr.control.services.event_envelope import validate_envelope
from dlr.control.services.subscription_rocketmq import RocketMQTransport, decode_message, status
from dlr.control.services.subscription_source import SourceFailure
from test_issue219_kafka import context as kafka_fixture
from test_issue219_queue_runners import counts
from test_issue219_queue_runners import rabbit_context as rabbit_fixture

context = kafka_fixture
rabbit_context = rabbit_fixture
CONFIG = {
    "source_type": "rocketmq",
    "cluster_id": "fixture",
    "server": "grpc://proxy.invalid:8081",
    "topic": "events",
    "group_id": "dedicated",
}


def native(body=b"\x00\xff", encoding=d.IDENTITY):
    props = d.SystemProperties(
        message_id="stable-one",
        receipt_handle="temporary-one",
        message_type=d.NORMAL,
        body_encoding=encoding,
    )
    props.body_digest.type = d.CRC32
    props.body_digest.checksum = format(zlib.crc32(body), "08X")
    return d.Message(
        topic=d.Resource(name="events"),
        body=body,
        system_properties=props,
        user_properties={"custom": "original"},
    )


def test_rocket_identity_excludes_handles_attempt_and_compression(rabbit_context):
    ctx = replace(rabbit_context, source_config=CONFIG)
    first = native()
    item = decode_message(CONFIG, first)
    digest, _ = validate_envelope(queue.envelope(ctx, item))
    first.system_properties.receipt_handle = "new-handle"
    first.system_properties.delivery_attempt = 9
    first.system_properties.queue_offset = 123
    other = decode_message(CONFIG, first)
    assert validate_envelope(queue.envelope(ctx, other))[0] == digest
    compressed = decode_message(CONFIG, native(gzip.compress(item.body), d.GZIP))
    assert compressed.body == item.body
    assert validate_envelope(queue.envelope(ctx, compressed))[0] == digest


@pytest.mark.parametrize(
    "change,code",
    [
        ("message_id", "source_message_id_required"),
        ("type", "source_message_type_unsupported"),
        ("digest", "source_message_digest_invalid"),
        ("encoding", "source_message_encoding_invalid"),
        ("bomb", "event_payload_too_large"),
    ],
)
def test_rocket_invalid_native_never_ack(change, code):
    value = native()
    if change == "message_id":
        value.system_properties.message_id = ""
    elif change == "type":
        value.system_properties.message_type = d.FIFO
    elif change == "digest":
        value.system_properties.body_digest.checksum = "BAD"
    elif change == "encoding":
        value.system_properties.body_encoding = 99
    else:
        value = native(gzip.compress(b"x" * (512 * 1024 + 1)), d.GZIP)
    with pytest.raises(SourceFailure, match=code):
        decode_message(CONFIG, value)


def test_rocket_auth_signature_and_bounded_status_codes():
    client = object.__new__(RocketMQTransport)
    client.client_id = "safe-client"
    client.credentials = {"access_key_id": "unit-ak", "access_key_secret": "unit-sk"}
    metadata = dict(client.metadata())
    expected = hmac.new(b"unit-sk", metadata["x-mq-date-time"].encode(), hashlib.sha1).hexdigest()
    assert metadata["authorization"].endswith("Signature=" + expected)
    with pytest.raises(SourceFailure, match="source_authentication_failed"):
        status(d.Status(code=d.UNAUTHORIZED, message="secret must not surface"))


def test_rocket_ack_entry_failure_is_not_success():
    client = object.__new__(RocketMQTransport)
    client.config, client.credentials, client.client_id = CONFIG, {}, "unit"
    result = s.AckMessageResponse(status=d.Status(code=d.OK))
    result.entries.add(message_id="stable-one", status=d.Status(code=d.FORBIDDEN))
    client.stub = SimpleNamespace(AckMessage=lambda *a, **k: result)
    with pytest.raises(SourceFailure, match="source_authorization_failed"):
        client.ack(decode_message(CONFIG, native()))


@pytest.mark.skipif(not os.environ.get("DLR_QUEUE_FIXTURE_FILE"), reason="isolated broker opt-in")
def test_real_rocketmq_t1_redelivery_ack_and_settings_close(rabbit_context):
    private = json.loads(Path(os.environ["DLR_QUEUE_FIXTURE_FILE"]).read_text())["rocketmq"]
    suffix = uuid.uuid4().hex[:12]
    topic, group = "dlr219-native-" + suffix, "dlr219-native-group-" + suffix
    base = [
        "docker",
        "--context",
        "colima-dlr-preview",
        "exec",
        "dlr219-queue-rocketmq",
        "sh",
        "mqadmin",
    ]
    for command in (
        ["updateTopic", "-t", topic, "-r", "1", "-w", "1", "-a", "+message.type=NORMAL"],
        ["updateSubGroup", "-g", group, "-m", "true"],
    ):
        result = subprocess.run(
            [*base, *command, "-n", "dlr219-queue-namesrv:9876", "-b", "127.0.0.1:10911"],
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, "isolated fixture setup failed"
    config = {
        **CONFIG,
        "server": f"grpc://{private['host']}:{private['port']}",
        "topic": topic,
        "group_id": group,
    }
    with rabbit_context.session_factory.begin() as session:
        session.get(EventSubscription, rabbit_context.subscription_id).source_type = "rocketmq"
    ctx = replace(rabbit_context, source_config=config)
    diagnostic = RocketMQTransport(config, {}, diagnostic=True)
    assert diagnostic.telemetry is None
    assert diagnostic.diagnostic()["topic_count"] == 1
    diagnostic.close()

    def open_client():
        client = RocketMQTransport(config, {}, diagnostic=False)
        original = client.stub.ReceiveMessage

        def receive(request, **kwargs):
            request.invisible_duration.seconds = 10  # accelerate this isolated failure-window test
            return original(request, **kwargs)

        client.stub.ReceiveMessage = receive
        return client

    first = open_client()
    value = native()
    value.topic.name = topic
    value.system_properties.message_id = uuid.uuid4().hex.upper()
    value.system_properties.born_timestamp.GetCurrentTime()
    value.system_properties.born_host = "127.0.0.1"
    value.system_properties.receipt_handle = ""
    result = first.stub.SendMessage(
        s.SendMessageRequest(messages=[value]), metadata=first.metadata(), timeout=3
    )
    status(result.status)

    def poll(client):
        for _ in range(6):
            with ctx.session_factory.begin() as session:
                delivery.renew_lease(session, ctx.subscription_id, ctx.owner_id, ctx.generation)
            item = client.poll()
            if item:
                return item
        pytest.fail("source did not deliver within bounded attempts")

    item = poll(first)
    assert item.body == value.body
    first.close()  # no T1 / no ACK
    second = open_client()
    redelivery = poll(second)
    assert redelivery.message_id == item.message_id
    assert queue.take_custody(ctx, redelivery)
    second.close()  # T1 committed, ACK lost
    third = open_client()
    duplicate = poll(third)
    assert duplicate.handle != redelivery.handle
    assert queue.take_custody(ctx, duplicate)
    assert counts(ctx) == 1
    third.ack(duplicate)
    for _ in range(3):
        old_done = third.telemetry_done
        third.settings()
        assert old_done.is_set()
    started = time.monotonic()
    third.close()
    assert third.telemetry_done.is_set() and time.monotonic() - started < 2


def test_rocket_tls_trust_and_signed_diagnostic_without_consumer(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta

    import grpc
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from rocketmq.grpc_protocol import service_pb2_grpc as g

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )

    class Service(g.MessagingServiceServicer):
        calls = 0

        def QueryRoute(self, request, context):
            self.calls += 1
            metadata = dict(context.invocation_metadata())
            signed = hmac.new(
                b"fixture-sk", metadata["x-mq-date-time"].encode(), hashlib.sha1
            ).hexdigest()
            if not metadata.get("authorization", "").endswith("Signature=" + signed):
                return s.QueryRouteResponse(status=d.Status(code=d.UNAUTHORIZED))
            result = s.QueryRouteResponse(status=d.Status(code=d.OK))
            result.message_queues.add(
                topic=d.Resource(name="events"),
                id=0,
                permission=d.READ_WRITE,
                accept_message_types=[d.NORMAL],
            )
            return result

    service = Service()
    pool = ThreadPoolExecutor(max_workers=1)
    server = grpc.server(pool)
    g.add_MessagingServiceServicer_to_server(service, server)
    port = server.add_secure_port("localhost:0", grpc.ssl_server_credentials([(key_pem, pem)]))
    server.start()
    config = {
        **CONFIG,
        "server": f"grpcs://localhost:{port}",
        "credential_id": 1,
        "ca_pem": pem.decode(),
    }
    credentials = {"access_key_id": "fixture-ak", "access_key_secret": "fixture-sk"}
    try:
        client = RocketMQTransport(config, credentials, diagnostic=True)
        assert client.telemetry is None and service.calls == 1
        client.close()
        with pytest.raises(SourceFailure, match="source_authentication_failed"):
            RocketMQTransport(
                config, {**credentials, "access_key_secret": "wrong"}, diagnostic=True
            )
        with pytest.raises(SourceFailure):
            RocketMQTransport({**config, "ca_pem": None}, credentials, diagnostic=True)
    finally:
        server.stop(0).wait(2)
        pool.shutdown(wait=True)
