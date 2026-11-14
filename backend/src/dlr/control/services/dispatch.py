"""Minimal, non-sensitive RabbitMQ dispatch message contract."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from dlr.common.config import settings
from dlr.common.dispatch_protocol import DISPATCH_ALLOWED_FIELDS as DISPATCH_ALLOWED_FIELDS
from dlr.common.dispatch_protocol import DISPATCH_SCHEMA_VERSION as DISPATCH_SCHEMA_VERSION
from dlr.common.dispatch_protocol import DispatchMessage as DispatchMessage
from dlr.common.dispatch_protocol import (
    deserialize_dispatch_message as deserialize_dispatch_message,
)
from dlr.common.jcs import canonicalize

DISPATCH_EXCHANGE = "dlr.execution.dispatch.v1"
INFRASTRUCTURE_DLX = "dlr.execution.infrastructure.dlx"
INFRASTRUCTURE_DLQ = "dlr.execution.infrastructure.dlq"
SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "java", "typescript", "go"})


def worker_routing_key(worker_id: int) -> str:
    """Return the stable direct-exchange routing key for one target Worker."""

    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id <= 0:
        raise ValueError("target worker id must be a positive integer")
    return f"worker.{worker_id}"


def build_dispatch_message(
    *,
    execution_id: int,
    dispatch_generation: int,
    adapter_id: int,
    language: Literal["python", "javascript", "java", "typescript", "go"],
    resource_class: str,
    target_worker_id: int,
    message_id: uuid.UUID | None = None,
) -> DispatchMessage:
    """Build a validated message without reading code, input or secrets."""

    return DispatchMessage(
        message_id=message_id or uuid.uuid4(),
        execution_id=execution_id,
        dispatch_generation=dispatch_generation,
        adapter_id=adapter_id,
        language=language,
        resource_class=resource_class,
        target_worker_id=target_worker_id,
    )


def serialize_dispatch_message(message: DispatchMessage) -> bytes:
    """Serialize with RFC 8785 rather than an approximate JSON sort."""

    payload = canonicalize(message.model_dump(mode="json"))
    if len(payload) > settings.rabbitmq_dispatch_message_max_bytes:
        raise ValueError("dispatch message exceeds the configured size limit")
    return payload


def assert_dispatch_message_safe(message: DispatchMessage | Mapping[str, Any]) -> None:
    """Run the structural security audit used by tests and the Outbox path."""

    if isinstance(message, DispatchMessage):
        keys = set(message.model_dump(mode="json"))
    else:
        keys = set(message)
        DispatchMessage.model_validate(message)
    if keys != DISPATCH_ALLOWED_FIELDS:
        raise ValueError("dispatch message contains a forbidden field")
