"""Pydantic request/response schemas for Adapter management."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ADAPTER_NAME_LENGTH = 128

# M5.5.11 single-run execution timeout contract (seconds, backend-authoritative).
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300
MIN_EXECUTION_TIMEOUT_SECONDS = 1
MAX_EXECUTION_TIMEOUT_SECONDS = 24 * 60 * 60  # 24 hours; no "unlimited" option.

AdapterAccessLevel = Literal["admin", "owner", "edit", "read"]


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
    # M5.5.11: default single-run execution timeout (5 minutes).
    timeout_seconds: int = Field(
        default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        ge=MIN_EXECUTION_TIMEOUT_SECONDS,
        le=MAX_EXECUTION_TIMEOUT_SECONDS,
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class AdapterUpdate(BaseModel):
    """Request body for PATCH /api/adapters/{adapter_id}.

    Only metadata, the Task run mode, the runtime Worker pointer and the
    M5.5.11 single-run execution timeout are editable;
    adapter type, language, Revision pointers and timestamps are intentionally absent.
    Sending ``runtime_worker_id: null`` explicitly clears it
    (omitting the field leaves it unchanged).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    runtime_worker_id: int | None = None
    run_mode: Literal["manual", "schedule"] | None = None
    timeout_seconds: int | None = Field(
        default=None,
        ge=MIN_EXECUTION_TIMEOUT_SECONDS,
        le=MAX_EXECUTION_TIMEOUT_SECONDS,
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_name(value)

    @field_validator("run_mode", mode="before")
    @classmethod
    def reject_null_run_mode(cls, value: object) -> object:
        if value is None:
            raise ValueError("run_mode must be manual or schedule")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def reject_null_timeout_seconds(cls, value: object) -> object:
        """An explicit null cannot mean "unchanged"; require a real value."""
        if value is None:
            raise ValueError("timeout_seconds must be between 1 and 86400 seconds (max 24 hours)")
        return value


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
    run_mode: str
    timeout_seconds: int
    owner_user_id: int | None
    # M5.9 Wave D: the caller's effective Adapter relationship. This is
    # derived server-side for UI gating; the ACL service remains authoritative.
    access_level: AdapterAccessLevel | None = None
    # Safe display metadata only; system-owned Adapters keep this null.
    owner_username: str | None = None
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


class AdapterPermissionUpsert(BaseModel):
    """One explicit read/edit ACL grant for an account user."""

    model_config = ConfigDict(extra="forbid")

    permission: Literal["read", "edit"]


class AdapterPermissionResponse(BaseModel):
    """Secret-free ACL metadata returned to an owner or administrator."""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    username: str
    enabled: bool
    permission: Literal["read", "edit"]


class AdapterPermissionCandidate(BaseModel):
    """Minimal account metadata used only by the Adapter sharing picker."""

    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    role: Literal["admin", "user"]
    enabled: bool
