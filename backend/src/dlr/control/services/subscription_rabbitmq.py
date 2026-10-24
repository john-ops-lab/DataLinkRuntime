"""Dedicated RabbitMQ source; never shares the internal dispatch connection or queue."""

from __future__ import annotations

import base64
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pika
from pika import frame

from dlr.control.schemas.subscription import RabbitMQSourceConfig
from dlr.control.services.subscription_queue import Message, QueueSourceRunner, silence_sdk
from dlr.control.services.subscription_source import SourceFailure


def typed_property(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    """AMQP field-table bytes/decimals/timestamps survive JSON without coercion."""
    budget = [4096] if budget is None else budget
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise SourceFailure("source_properties_invalid")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, bytes):
        return {"amqp_type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return {"amqp_type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"amqp_type": "timestamp", "value": value.isoformat()}
    if isinstance(value, int):
        return {"amqp_type": "integer", "value": str(value)}
    if isinstance(value, float):
        import math

        if not math.isfinite(value):
            raise SourceFailure("source_properties_invalid")
        return {"amqp_type": "float", "value": repr(value)}
    if isinstance(value, list):
        return [typed_property(child, depth + 1, budget) for child in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        # A typed wrapper avoids collisions with producer dictionaries containing amqp_type.
        return {
            "amqp_type": "table",
            "value": {
                key: typed_property(child, depth + 1, budget) for key, child in value.items()
            },
        }
    raise SourceFailure("source_properties_invalid")


def guard_content(channel: Any) -> None:
    original = channel._handle_content_frame

    def bounded(value: Any) -> Any:
        # Pika 1.3.2 invokes this before ContentFrameAssembler buffers body fragments.
        if isinstance(value, frame.Header) and value.body_size > 512 * 1024:
            channel._dlr_content_fault = "event_payload_too_large"
            raise SourceFailure("event_payload_too_large")
        return original(value)

    channel._handle_content_frame = bounded


class RabbitMQTransport:
    def __init__(self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool):
        value = RabbitMQSourceConfig.model_validate(config)
        silence_sdk("pika")
        if not credentials.get("username") or not credentials.get("password"):
            raise SourceFailure("source_credential_unavailable")
        address = urlsplit(value.server)
        tls = None
        if address.scheme == "amqps":
            trust = ssl.create_default_context(cadata=value.ca_pem)
            tls = pika.SSLOptions(trust, address.hostname)
        parameters = pika.ConnectionParameters(
            host=address.hostname,
            port=address.port,
            virtual_host=value.vhost,
            credentials=pika.PlainCredentials(credentials["username"], credentials["password"]),
            ssl_options=tls,
            heartbeat=10,
            connection_attempts=1,
            retry_delay=0,
            socket_timeout=3,
            stack_timeout=5,
            blocked_connection_timeout=3,
            frame_max=131072,
        )
        self.connection: Any = None
        self.channel: Any = None
        self.iterator: Any = None
        self.config = value.model_dump()
        try:
            self.connection = pika.BlockingConnection(parameters)
            with self.deadline():
                self.channel = self.connection.channel()
            guard_content(self.channel._impl)
            # Passive declaration performs no mutation. Non-durable queues are unsupported:
            # AMQP passive responses do not expose durable, so require it in source setup.
            with self.deadline():
                self.channel.queue_declare(queue=value.queue, passive=True)
            if not diagnostic:
                with self.deadline():
                    self.channel.basic_qos(prefetch_count=1, global_qos=False)
                self.iterator = self.channel.consume(
                    queue=value.queue, auto_ack=False, exclusive=True, inactivity_timeout=0.2
                )
        except Exception:
            self.close()
            raise

    @contextmanager
    def deadline(self) -> Iterator[None]:
        def abort() -> None:
            self.connection._impl._terminate_stream(
                pika.exceptions.StreamLostError("source operation deadline")
            )

        timer = self.connection.call_later(3, abort)
        try:
            yield
        finally:
            if not self.connection.is_closed:
                self.connection.remove_timeout(timer)

    def diagnostic(self) -> dict[str, Any]:
        return {
            "source_type": "rabbitmq",
            "cluster_id": self.config["cluster_id"],
            "group_available": True,
        }

    def poll(self) -> Message | None:
        try:
            with self.deadline():
                method, properties, body = next(self.iterator)
        except Exception:
            if getattr(self.channel._impl, "_dlr_content_fault", None):
                raise SourceFailure("event_payload_too_large") from None
            raise
        if method is None:
            return None
        if len(body) > 512 * 1024:
            raise SourceFailure("event_payload_too_large")
        if not properties.message_id:
            raise SourceFailure("source_message_id_required")
        values = {
            name: typed_property(getattr(properties, name))
            for name in (
                "content_type",
                "content_encoding",
                "headers",
                "delivery_mode",
                "priority",
                "correlation_id",
                "reply_to",
                "expiration",
                "timestamp",
                "type",
                "user_id",
                "app_id",
                "cluster_id",
            )
        }
        attrs = {
            "cluster_id": self.config["cluster_id"],
            "vhost": self.config["vhost"],
            "queue": self.config["queue"],
            "exchange": method.exchange,
            "routing_key": method.routing_key,
            "properties": values,
        }
        return Message(
            properties.message_id, body, attrs, method.delivery_tag, properties.content_type
        )

    def ack(self, message: Message) -> None:
        with self.deadline():
            self.channel.basic_ack(delivery_tag=message.handle, multiple=False)
            # Flush the asynchronous AMQP ACK within the bounded I/O turn.
            self.connection.process_data_events(time_limit=0.01)

    def close(self) -> None:
        connection = self.connection
        if connection is None or connection.is_closed:
            return
        # Deliberately abort TCP: broker requeues any unacknowledged delivery. A graceful
        # Close/CloseOk handshake can wait indefinitely when the peer stops responding.
        connection._impl._terminate_stream(pika.exceptions.StreamLostError("source closed"))
        deadline = time.monotonic() + 2
        while not connection.is_closed and time.monotonic() < deadline:
            try:
                connection.process_data_events(time_limit=0.05)
            except pika.exceptions.AMQPError:
                break
        if not connection.is_closed:
            raise SourceFailure("source_close_unconfirmed", closed=False)


class RabbitMQSourceRunner(QueueSourceRunner):
    def open(
        self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool
    ) -> RabbitMQTransport:
        return RabbitMQTransport(config, credentials, diagnostic=diagnostic)
