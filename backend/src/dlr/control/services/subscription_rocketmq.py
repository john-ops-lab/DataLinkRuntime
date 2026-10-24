"""RocketMQ 5.x SimpleConsumer protocol with bounded DLR-owned synchronous gRPC."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import threading
import time
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import grpc
from rocketmq.grpc_protocol import definition_pb2 as d
from rocketmq.grpc_protocol import service_pb2 as s
from rocketmq.grpc_protocol import service_pb2_grpc as g

from dlr.control.schemas.subscription import RocketMQSourceConfig
from dlr.control.services.subscription_queue import Message, QueueSourceRunner, silence_sdk
from dlr.control.services.subscription_source import SourceFailure


def status(value: Any, *, empty: bool = False) -> None:
    if value.code == d.OK or (empty and value.code == d.MESSAGE_NOT_FOUND):
        return
    if value.code == d.UNAUTHORIZED:
        raise SourceFailure("source_authentication_failed")
    if value.code == d.FORBIDDEN:
        raise SourceFailure("source_authorization_failed")
    if value.code == d.TOO_MANY_REQUESTS or value.code >= 50000:
        raise SourceFailure("source_unavailable", "backoff")
    raise SourceFailure("source_protocol_unsupported")


def rpc_failure(error: grpc.RpcError) -> SourceFailure:
    code = error.code()
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return SourceFailure("source_authentication_failed")
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return SourceFailure("source_authorization_failed")
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return SourceFailure("event_payload_too_large")
    return SourceFailure("source_unavailable", "backoff")


def decode_message(config: dict[str, Any], value: Any) -> Message:
    properties = value.system_properties
    if properties.message_type != d.NORMAL or value.topic.name != config["topic"]:
        raise SourceFailure("source_message_type_unsupported")
    if not properties.message_id or not properties.receipt_handle:
        raise SourceFailure("source_message_id_required")
    body = value.body
    if len(body) > 512 * 1024:
        raise SourceFailure("event_payload_too_large")
    digest = properties.body_digest
    checksums = {
        d.CRC32: lambda b: format(zlib.crc32(b), "08X"),
        d.MD5: lambda b: hashlib.md5(b, usedforsecurity=False).hexdigest().upper(),
        d.SHA1: lambda b: hashlib.sha1(b, usedforsecurity=False).hexdigest().upper(),
    }
    if digest.type not in checksums or checksums[digest.type](body) != digest.checksum.upper():
        raise SourceFailure("source_message_digest_invalid")
    if properties.body_encoding == d.GZIP:
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(body, 512 * 1024 + 1)
            if len(decoded) > 512 * 1024 or decoder.unconsumed_tail:
                raise SourceFailure("event_payload_too_large")
            if not decoder.eof or decoder.unused_data:
                raise SourceFailure("source_message_encoding_invalid")
            body = decoded
        except zlib.error:
            raise SourceFailure("source_message_encoding_invalid") from None
    elif properties.body_encoding != d.IDENTITY:
        raise SourceFailure("source_message_encoding_invalid")
    attributes = {
        "cluster_id": config["cluster_id"],
        "topic": config["topic"],
        "group_id": config["group_id"],
        "tag": properties.tag,
        "keys": list(properties.keys),
        "properties": dict(value.user_properties),
    }
    return Message(properties.message_id, body, attributes, properties.receipt_handle)


class RocketMQTransport:
    def __init__(self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool):
        self.config = RocketMQSourceConfig.model_validate(config).model_dump()
        silence_sdk("rocketmq")
        self.credentials = dict(credentials)
        if self.config.get("credential_id") and not (
            credentials.get("access_key_id") and credentials.get("access_key_secret")
        ):
            raise SourceFailure("source_credential_unavailable")
        self.client_id = "dlr-" + uuid.uuid4().hex
        address = urlsplit(self.config["server"])
        target = address.netloc
        options = [
            ("grpc.max_receive_message_length", 2 * 1024**2),
            ("grpc.max_send_message_length", 2 * 1024**2),
            ("grpc.enable_retries", 0),
            ("grpc.http2.bdp_probe", 0),
        ]
        if address.scheme == "grpcs":
            ca = self.config.get("ca_pem")
            trust = grpc.ssl_channel_credentials(ca.encode() if ca else None)
            self.channel = grpc.secure_channel(target, trust, options=options)
        else:
            self.channel = grpc.insecure_channel(target, options=options)
        self.stub = g.MessagingServiceStub(self.channel)
        self.endpoints = d.Endpoints()
        try:
            ip = ipaddress.ip_address(address.hostname)
            self.endpoints.scheme = d.IPv6 if ip.version == 6 else d.IPv4
        except ValueError:
            self.endpoints.scheme = d.DOMAIN_NAME
        self.endpoints.addresses.add(host=address.hostname, port=address.port)
        self.telemetry: Any = None
        self.receive_call: Any = None
        self.telemetry_stop = threading.Event()
        self.telemetry_done = threading.Event()
        self.next_settings = 0.0
        self.next_heartbeat = 0.0
        self.next_route = 0.0
        self.index = 0
        self.queues: list[Any] = []
        try:
            self.route()
            if not diagnostic:
                self.settings()
                self.heartbeat()
        except Exception as error:
            self.close()
            if isinstance(error, grpc.RpcError):
                raise rpc_failure(error) from None
            raise

    def metadata(self) -> list[tuple[str, str]]:
        date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result = [
            ("x-mq-client-id", self.client_id),
            ("x-mq-language", "PYTHON"),
            ("x-mq-protocol", "GRPC_V2"),
            ("x-mq-client-version", "5.1.2"),
            ("x-mq-date-time", date),
            ("x-mq-request-id", str(uuid.uuid4())),
            ("x-mq-namespace", ""),
        ]
        if self.credentials:
            signature = hmac.new(
                self.credentials["access_key_secret"].encode(), date.encode(), hashlib.sha1
            ).hexdigest()
            result.append(
                (
                    "authorization",
                    "MQv2-HMAC-SHA1 Credential="
                    + self.credentials["access_key_id"]
                    + ", SignedHeaders=x-mq-date-time, Signature="
                    + signature,
                )
            )
        return result

    def route(self) -> None:
        response = self.stub.QueryRoute(
            s.QueryRouteRequest(
                topic=d.Resource(name=self.config["topic"]), endpoints=self.endpoints
            ),
            metadata=self.metadata(),
            timeout=3,
        )
        status(response.status)
        if not 0 < len(response.message_queues) <= 16:
            raise SourceFailure("source_partition_limit_exceeded")
        queues = [
            q
            for q in response.message_queues
            if q.permission in (d.READ, d.READ_WRITE) and d.NORMAL in q.accept_message_types
        ]
        if not queues:
            raise SourceFailure("source_message_type_unsupported")
        # Reuse the configured Proxy; never open arbitrary route-provided endpoints.
        self.queues = queues
        self.next_route = time.monotonic() + 30

    def stop_settings(self) -> None:
        if self.telemetry is not None:
            self.telemetry_stop.set()
            self.telemetry.cancel()
            if not self.telemetry_done.wait(1):
                raise SourceFailure("source_close_unconfirmed", closed=False)
            self.telemetry = None

    def settings(self) -> None:
        self.stop_settings()
        self.telemetry_stop = threading.Event()
        self.telemetry_done = threading.Event()
        stop, done = self.telemetry_stop, self.telemetry_done
        settings = d.Settings(client_type=d.SIMPLE_CONSUMER, access_point=self.endpoints)
        settings.request_timeout.seconds = 3
        settings.subscription.group.name = self.config["group_id"]
        entry = settings.subscription.subscriptions.add()
        entry.topic.name = self.config["topic"]
        entry.expression.type, entry.expression.expression = d.TAG, "*"
        settings.subscription.long_polling_timeout.seconds = 5
        settings.user_agent.language, settings.user_agent.version = 6, "5.1.2"
        settings.metric.on = False

        def commands() -> Iterator[Any]:
            try:
                yield s.TelemetryCommand(settings=settings)
                stop.wait(30)
            finally:
                done.set()

        self.telemetry = self.stub.Telemetry(commands(), metadata=self.metadata(), timeout=30)
        timer = threading.Timer(3, self.telemetry.cancel)
        timer.daemon = True
        timer.start()
        try:
            first = next(self.telemetry)
            if not first.HasField("settings"):
                status(first.status)
                raise SourceFailure("source_protocol_unsupported")
        finally:
            timer.cancel()
            timer.join(timeout=1)
        self.next_settings = time.monotonic() + 20

    def heartbeat(self) -> None:
        response = self.stub.Heartbeat(
            s.HeartbeatRequest(
                group=d.Resource(name=self.config["group_id"]), client_type=d.SIMPLE_CONSUMER
            ),
            metadata=self.metadata(),
            timeout=3,
        )
        status(response.status)
        self.next_heartbeat = time.monotonic() + 10

    def diagnostic(self) -> dict[str, Any]:
        return {
            "source_type": "rocketmq",
            "cluster_id": self.config["cluster_id"],
            "topic_count": 1,
            "partition_count": len(self.queues),
        }

    def poll(self) -> Message | None:
        try:
            if time.monotonic() >= self.next_settings:
                self.settings()
            if time.monotonic() >= self.next_route:
                self.route()
            if time.monotonic() >= self.next_heartbeat:
                self.heartbeat()
            request = s.ReceiveMessageRequest(
                group=d.Resource(name=self.config["group_id"]),
                message_queue=self.queues[self.index % len(self.queues)],
                batch_size=1,
                auto_renew=False,
            )
            self.index += 1
            request.filter_expression.type, request.filter_expression.expression = d.TAG, "*"
            request.invisible_duration.seconds = 30
            request.long_polling_timeout.seconds = 5
            self.receive_call = self.stub.ReceiveMessage(
                request, metadata=self.metadata(), timeout=8
            )
            message = None
            for frame_count, frame in enumerate(self.receive_call, 1):
                if frame_count > 4:
                    raise SourceFailure("source_protocol_unsupported")
                if frame.HasField("status"):
                    status(frame.status, empty=True)
                elif frame.HasField("message"):
                    if message is not None:
                        raise SourceFailure("source_protocol_unsupported")
                    message = decode_message(self.config, frame.message)
            return message
        except grpc.RpcError as error:
            raise rpc_failure(error) from None
        finally:
            if self.receive_call is not None:
                self.receive_call.cancel()
                self.receive_call = None

    def ack(self, message: Message) -> None:
        request = s.AckMessageRequest(
            group=d.Resource(name=self.config["group_id"]),
            topic=d.Resource(name=self.config["topic"]),
        )
        request.entries.add(message_id=message.message_id, receipt_handle=message.handle)
        try:
            response = self.stub.AckMessage(request, metadata=self.metadata(), timeout=3)
            status(response.status)
            if len(response.entries) != 1 or response.entries[0].message_id != message.message_id:
                raise SourceFailure("source_ack_failed", "backoff")
            status(response.entries[0].status)
        except grpc.RpcError as error:
            raise rpc_failure(error) from None

    def close(self) -> None:
        failure = None
        try:
            if self.receive_call is not None:
                self.receive_call.cancel()
            self.stop_settings()
        except SourceFailure as error:
            failure = error
        finally:
            self.channel.close()
            self.credentials.clear()
        if failure:
            raise failure


class RocketMQSourceRunner(QueueSourceRunner):
    def open(
        self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool
    ) -> RocketMQTransport:
        return RocketMQTransport(config, credentials, diagnostic=diagnostic)
