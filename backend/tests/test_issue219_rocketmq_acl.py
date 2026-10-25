"""Opt-in real RocketMQ 5.3.3 ACL 2.0; no broker setup or secret fixtures in Git."""

import json
import os
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import pytest
from rocketmq.grpc_protocol import definition_pb2 as d
from rocketmq.grpc_protocol import service_pb2 as s

from dlr.control.services.subscription_rocketmq import RocketMQTransport, rpc_failure, status
from dlr.control.services.subscription_source import SourceFailure

pytestmark = pytest.mark.skipif(
    not os.environ.get("DLR_ROCKETMQ_ACL_FIXTURE_FILE"), reason="isolated real ACL broker opt-in"
)


@dataclass(frozen=True)
class AclFixture:
    private: dict = field(repr=False)

    @property
    def config(self):
        return {
            "source_type": "rocketmq",
            "cluster_id": self.private["cluster_id"],
            "server": f"grpc://{self.private['host']}:{self.private['port']}",
            "topic": self.private["topic"],
            "group_id": self.private["group"],
        }

    def credentials(self, case="consumer"):
        value = dict(self.private["accounts"]["consumer"])
        if case == "wrong_password":
            value["access_key_secret"] += "_incorrect"
        elif case == "unknown_account":
            value["access_key_id"] += "_not_created"
        elif case == "missing_account":
            return {}
        elif case in ("producer", "no_topic", "no_group"):
            value = dict(self.private["accounts"][case])
        return value


@pytest.fixture
def acl_fixture():
    private = json.loads(Path(os.environ["DLR_ROCKETMQ_ACL_FIXTURE_FILE"]).read_text())
    assert private["version"] == "5.3.3" and private["acl_version"] == "2.0"
    return AclFixture(private)


DENIALS = [
    ("wrong_password", "source_authentication_failed"),
    ("unknown_account", "source_authentication_failed"),
    ("missing_account", "source_authentication_failed"),
    # RocketMQ 5.3.3 ResponseBuilder maps both AuthenticationException and
    # AuthorizationException to UNAUTHORIZED. Never infer a kind from raw text.
    ("no_topic", "source_authentication_failed"),
    ("no_group", "source_authentication_failed"),
]


@pytest.mark.parametrize("case,code", DENIALS)
def test_real_acl_rejects_unauthenticated_or_unauthorized_consumer(acl_fixture, case, code):
    if case == "no_topic":
        # This same signature can access the granted Group; only Topic access is missing.
        probe = RocketMQTransport(acl_fixture.config, acl_fixture.credentials(), diagnostic=False)
        try:
            probe.credentials = acl_fixture.credentials(case)
            probe.heartbeat()
        finally:
            probe.close()
    if case == "no_group":
        # Routing checks the Topic only. A successful diagnostic cannot prove Group access.
        client = RocketMQTransport(
            acl_fixture.config, acl_fixture.credentials(case), diagnostic=True
        )
        assert client.telemetry is None
        client.close()
    started = time.monotonic()
    with pytest.raises(SourceFailure) as caught:
        RocketMQTransport(acl_fixture.config, acl_fixture.credentials(case), diagnostic=False)
    assert caught.value.code == code and str(caught.value) == code
    assert caught.value.disposition == "blocked" and caught.value.closed
    assert time.monotonic() - started < 8


def test_real_acl_sub_only_account_receives_and_acks_after_denied_requests(acl_fixture, caplog):
    config = acl_fixture.config
    producer = RocketMQTransport(config, acl_fixture.credentials("producer"), diagnostic=True)
    consumer = rejected = None
    try:
        body = b'\x00\xff{"business":"real-acl","id":"' + uuid.uuid4().hex.encode() + b'"}'
        props = d.SystemProperties(
            message_id=uuid.uuid4().hex.upper(),
            message_type=d.NORMAL,
            body_encoding=d.IDENTITY,
            born_host="127.0.0.1",
        )
        props.born_timestamp.GetCurrentTime()
        props.body_digest.type = d.CRC32
        props.body_digest.checksum = format(zlib.crc32(body), "08X")
        native = d.Message(
            topic=d.Resource(name=config["topic"]), body=body, system_properties=props
        )
        response = producer.stub.SendMessage(
            s.SendMessageRequest(messages=[native]), metadata=producer.metadata(), timeout=3
        )
        status(response.status)
        consumer = RocketMQTransport(config, acl_fixture.credentials(), diagnostic=False)
        # Reuse a real channel/route, but sign every read with the rejected account.
        # The Broker must deny ReceiveMessage itself, before any message is delivered.
        rejected = RocketMQTransport(config, acl_fixture.credentials(), diagnostic=True)
        for case, code in DENIALS:
            rejected.credentials = acl_fixture.credentials(case)
            request = s.ReceiveMessageRequest(
                group=d.Resource(name=config["group_id"]),
                message_queue=rejected.queues[0],
                batch_size=1,
                auto_renew=False,
            )
            request.filter_expression.type, request.filter_expression.expression = d.TAG, "*"
            request.invisible_duration.seconds = 30
            request.long_polling_timeout.seconds = 5
            frames = rejected.stub.ReceiveMessage(request, metadata=rejected.metadata(), timeout=8)
            try:
                with pytest.raises(SourceFailure) as caught:
                    try:
                        for frame in frames:
                            if frame.HasField("message"):
                                pytest.fail("Rejected account received a real message")
                            if frame.HasField("status"):
                                status(frame.status, empty=True)
                    except grpc.RpcError as error:
                        raise rpc_failure(error) from None
                assert caught.value.code == code
            finally:
                frames.cancel()

        item = None
        for _ in range(4):
            item = consumer.poll()
            if item:
                break
        assert item is not None and item.body == body and item.message_id == props.message_id
        # A Topic Sub grant does not allow publishing.
        with pytest.raises(SourceFailure, match="^source_authentication_failed$"):
            response = consumer.stub.SendMessage(
                s.SendMessageRequest(messages=[native]), metadata=consumer.metadata(), timeout=3
            )
            status(response.status)
        for case, code in DENIALS:
            rejected.credentials = acl_fixture.credentials(case)
            with pytest.raises(SourceFailure) as caught:
                rejected.ack(item)
            assert caught.value.code == code
        consumer.ack(item)
        if any(
            account["access_key_secret"] in caplog.text
            for account in acl_fixture.private["accounts"].values()
        ):
            pytest.fail("Connector logging exposed a fixture credential")
    finally:
        for client in (rejected, consumer, producer):
            if client is not None:
                started = time.monotonic()
                client.close()
                assert time.monotonic() - started < 2
