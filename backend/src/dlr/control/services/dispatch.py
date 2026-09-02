"""Minimal, non-sensitive RabbitMQ dispatch message contract."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from dlr.common.config import settings
from dlr.common.jcs import canonicalize

DISPATCH_SCHEMA_VERSION: Literal[1] = 1
DISPATCH_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "message_id",
        "execution_id",
        "dispatch_generation",
        "adapter_id",
        "language",
        "resource_class",
        "target_worker_id",
    }
)
DISPATCH_EXCHANGE = "dlr.execution.dispatch.v1"
INFRASTRUCTURE_DLX = "dlr.execution.infrastructure.dlx"
INFRASTRUCTURE_DLQ = "dlr.execution.infrastructure.dlq"
SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "java"})


class DispatchMessage(BaseModel):
    """The only body shape a RabbitMQ dispatch may carry."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = DISPATCH_SCHEMA_VERSION
    message_id: uuid.UUID
    execution_id: StrictInt = Field(gt=0)
    dispatch_generation: StrictInt = Field(ge=1)
    adapter_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java"]
    resource_class: str = Field(min_length=1, max_length=64)
    target_worker_id: StrictInt = Field(gt=0)


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
    language: Literal["python", "javascript", "java"],
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


def deserialize_dispatch_message(payload: bytes | str | Mapping[str, Any]) -> DispatchMessage:
    """Parse a dispatch and reject unknown schema fields before execution."""

    if isinstance(payload, Mapping):
        raw: object = dict(payload)
    else:
        try:
            raw = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("dispatch message is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("dispatch message must be a JSON object")
    unknown = set(raw) - DISPATCH_ALLOWED_FIELDS
    missing = DISPATCH_ALLOWED_FIELDS - set(raw)
    if unknown or missing:
        raise ValueError("dispatch message fields are not supported")
    return DispatchMessage.model_validate(raw)


def assert_dispatch_message_safe(message: DispatchMessage | Mapping[str, Any]) -> None:
    """Run the structural security audit used by tests and the Outbox path."""

    if isinstance(message, DispatchMessage):
        keys = set(message.model_dump(mode="json"))
    else:
        keys = set(message)
        DispatchMessage.model_validate(message)
    if keys != DISPATCH_ALLOWED_FIELDS:
        raise ValueError("dispatch message contains a forbidden field")
