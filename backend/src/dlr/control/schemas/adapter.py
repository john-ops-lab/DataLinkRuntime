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
    language: Literal["python", "javascript", "java"] = "python"

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class AdapterUpdate(BaseModel):
    """Request body for PATCH /api/adapters/{adapter_id}.

    Only metadata and the production Worker pointer are editable; version
    pointers, language and timestamps are intentionally not part of this
    schema. Sending ``production_worker_id: null`` explicitly clears it
    (omitting the field leaves it unchanged).
    """

    name: str | None = None
    description: str | None = None
    production_worker_id: int | None = None

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
    """Adapter representation returned by the API.

    ``production_version_id`` is the version locked by the current production
    entry (set by Start, cleared by Stop). ``production_version_seq`` is its
    display number. ``running_version_id`` / ``running_execution_id`` are
    derived from the Adapter's active Production Execution (at most one,
    enforced by the DB); both are None whenever production has no
    pending/running Execution. The ``published_version_seq`` /
    ``running_version_seq`` provide the Adapter-local display numbers without
    forcing Catalog callers to fetch every version list. The
    ``last_production_*`` fields retain the latest Production Execution's
    minimal identity/status after it becomes terminal, so an open production
    entry with no active child process can be distinguished from a failed or
    timed-out run.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    language: str
    latest_version_id: int | None
    published_version_id: int | None
    published_version_seq: int | None = None
    # M5.1: locked production version; set by Start, cleared by Stop.
    production_version_id: int | None
    production_version_seq: int | None = None
    production_worker_id: int | None
    production_state: str
    archived_at: datetime | None
    running_version_id: int | None = None
    running_version_seq: int | None = None
    running_execution_id: int | None = None
    last_production_execution_id: int | None = None
    last_production_execution_status: str | None = None
    last_production_version_id: int | None = None
    last_production_version_seq: int | None = None
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


class ProductionStopRequest(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/production/stop.

    ``wait`` only closes the production entry (the active Execution runs to
    completion); ``terminate`` additionally cancels the active Execution.
    """

    mode: Literal["wait", "terminate"] = "wait"


class CloneRequest(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/clone.

    The clone copies the working copy (latest version) as its v1 plus the
    credential binding references; it starts unpublished and not running.
    """

    name: str
    description: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class PublishGateLastTest(BaseModel):
    """The most recent test run of the target version on the production Worker."""

    execution_id: int
    status: str
    ended_at: datetime | None


class PublishGateResponse(BaseModel):
    """Publish gate evaluation for one target version (M3.2).

    ``reason`` is None exactly when ``allowed`` is true; stable reason codes:
    ``no_production_worker``, ``not_tested_on_production_worker``,
    ``last_test_not_succeeded``.
    """

    allowed: bool
    reason: str | None = None
    last_test: PublishGateLastTest | None = None
