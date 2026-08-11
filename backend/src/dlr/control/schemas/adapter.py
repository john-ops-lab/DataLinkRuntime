"""Pydantic request/response schemas for Adapter management."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ADAPTER_NAME_LENGTH = 128


def _validate_name(value: object) -> str:
    """Trim an incoming adapter name and enforce the M1 length contract."""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    if len(stripped) > MAX_ADAPTER_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_ADAPTER_NAME_LENGTH} characters")
    return stripped


class AdapterCreate(BaseModel):
    """Request body for POST /api/adapters."""

    name: str
    description: str = ""
    # M1 only supports Python adapters; any other value yields a 422.
    language: Literal["python"] = "python"

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class AdapterUpdate(BaseModel):
    """Request body for PATCH /api/adapters/{adapter_id}.

    Only metadata fields are editable; pointers, language and timestamps are
    intentionally not part of this schema.
    """

    name: str | None = None
    description: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_name(value)


class VersionCreate(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/versions.

    Semantics: Save new version. M1 performs no Python syntax checking and
    does not parse requirements.
    """

    code: str
    requirements: str = ""
    # Must be a JSON object; arrays, scalars and null are rejected with 422.
    runtime_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", mode="before")
    @classmethod
    def validate_code(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("code must be a string")
        if not value.strip():
            raise ValueError("code must not be blank")
        return value


class AdapterResponse(BaseModel):
    """Adapter representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    language: str
    latest_version_id: int | None
    published_version_id: int | None
    created_at: datetime
    updated_at: datetime


class VersionSummary(BaseModel):
    """Version list entry (no content fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    adapter_id: int
    seq: int
    created_at: datetime


class VersionDetail(VersionSummary):
    """Full immutable version snapshot."""

    code: str
    requirements: str
    runtime_config: dict[str, Any]
