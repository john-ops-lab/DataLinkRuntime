"""Pydantic schemas for the worker-internal API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

MAX_WORKER_NAME_LENGTH = 128
SUPPORTED_CAPABILITIES = frozenset({"python", "javascript", "java"})


def _validate_name(value: object) -> str:
    """Trim an incoming worker name and enforce the length contract."""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    if len(stripped) > MAX_WORKER_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_WORKER_NAME_LENGTH} characters")
    return stripped


class WorkerRegister(BaseModel):
    """Request body for POST /api/workers/register."""

    name: str
    # Defaults to Python for compatibility with M2 Worker clients.
    capabilities: list[str] = Field(default_factory=lambda: ["python"])
    # Missing protocol_version is the rolling-deployment v1 contract.
    protocol_version: StrictInt = Field(default=1, ge=1, le=3)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("capabilities must not be empty")
        normalized = list(dict.fromkeys(value))
        unknown = set(normalized) - SUPPORTED_CAPABILITIES
        if unknown:
            raise ValueError(f"unsupported capabilities: {', '.join(sorted(unknown))}")
        return normalized


class WorkerResponse(BaseModel):
    """Worker representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    last_heartbeat: datetime
    capabilities: list[str]
    protocol_version: StrictInt = Field(ge=1, le=3)


class TaskInputFile(BaseModel):
    """Public Worker metadata for one leased input file.

    The storage key and Control-side path intentionally have no schema field.
    """

    id: int
    ordinal: int
    mount_name: str
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str | None


class TaskPayload(BaseModel):
    """Everything a Worker needs to run one Execution.

    Version content always comes from the immutable AdapterVersion snapshot,
    never from a browser working copy.

    M3.2 adds ``secrets``: the decrypted env_key -> value map of the bound
    credentials, resolved at claim time so the Worker only ever sees the
    secrets this Execution needs (injected as ``DLR_SECRET_<env_key>``).
    """

    execution_id: int
    adapter_id: int
    version_id: int
    language: str
    code: str
    requirements: str
    runtime_config: dict[str, Any]
    input: Any
    latest_version_id: int | None
    execution_timeout_seconds: int
    secrets: dict[str, str] = Field(default_factory=dict)
    # Default source URL resolved by Adapter language at claim time (auth may
    # be embedded); None means the Worker uses its language-specific fallback.
    index_url: str | None = None
    # Captured at Execution creation; never read again from deployment state.
    locale: str = "zh-CN"
    protocol_version: StrictInt = Field(default=1, ge=1, le=3)
    claim_deadline_at: datetime | None = None
    execution_deadline_at: datetime | None = None
    recovery_grace_seconds_snapshot: int | None = None
    workspace_cleanup_attempt_timeout_seconds_snapshot: int | None = None
    workspace_cleanup_total_timeout_seconds_snapshot: int | None = None
    input_files: list[TaskInputFile] = Field(default_factory=list)
    # Raw values exist only in the in-memory v2 claim response.
    claim_token: str | None = None
    cleanup_token: str | None = None


class CleanupTaskPayload(BaseModel):
    """A Worker task that removes one deleted Adapter's private runtime tree."""

    kind: Literal["adapter_cleanup"] = "adapter_cleanup"
    cleanup_id: int
    adapter_id: int


class CleanupResult(BaseModel):
    """Secret-free completion report for an adapter cleanup task."""

    success: bool
