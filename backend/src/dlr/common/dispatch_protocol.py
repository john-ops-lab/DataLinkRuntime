"""The non-sensitive dispatch wire contract, independent of Control settings."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

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


class DispatchMessage(BaseModel):
    """The only body shape a RabbitMQ dispatch may carry."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = DISPATCH_SCHEMA_VERSION
    message_id: uuid.UUID
    execution_id: StrictInt = Field(gt=0)
    dispatch_generation: StrictInt = Field(ge=1)
    adapter_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java", "typescript", "go"]
    resource_class: str = Field(min_length=1, max_length=64)
    target_worker_id: StrictInt = Field(gt=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != DISPATCH_SCHEMA_VERSION:
            raise ValueError("invalid dispatch schema version")
        return value


def deserialize_dispatch_message(payload: bytes | str | Mapping[str, Any]) -> DispatchMessage:
    """Parse a dispatch and reject missing or unknown fields before execution."""
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
