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

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    language: Literal["python", "javascript", "java"]
    adapter_type: Literal["task", "webhook"]

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class AdapterUpdate(BaseModel):
    """Request body for PATCH /api/adapters/{adapter_id}.

    Only metadata and the runtime Worker pointer are editable; adapter type,
    language, Revision pointers and timestamps are intentionally absent.
    Sending ``runtime_worker_id: null`` explicitly clears it
    (omitting the field leaves it unchanged).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    runtime_worker_id: int | None = None

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
    """Adapter representation after Publish / Production model removal."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    language: str
    adapter_type: str
    latest_version_id: int | None
    runtime_worker_id: int | None
    runtime_locked: bool = False
    archived_at: datetime | None
    running_execution_id: int | None = None
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


class CloneRequest(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/clone.

    The clone copies the latest Revision as its own Revision 1 plus shared
    immutable Adapter facts and credential binding references. It has no
    Execution and starts stopped.
    """

    name: str
    description: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)
