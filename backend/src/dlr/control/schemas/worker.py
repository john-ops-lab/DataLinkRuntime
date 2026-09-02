"""Pydantic schemas for the worker-internal API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

MAX_WORKER_NAME_LENGTH = 128
SUPPORTED_CAPABILITIES = frozenset({"python", "javascript", "java"})
REQUIRED_ISOLATION_CAPABILITIES = frozenset(
    {
        "cgroup_v2",
        "mount_namespace",
        "pid_namespace",
        "memory_hard_limit",
        "pids_hard_limit",
        "tmpfs_hard_limit",
        "bounded_output",
        "preflight_passed",
        "cpu_hard_limit",
        "swap_hard_limit",
        "nofile_hard_limit",
        "no_new_privileges",
        "cgroup_kill",
        "adapter_control_plane_hidden",
        "sandbox_cleanup",
    }
)


def isolation_capabilities_ready(value: object) -> bool:
    """Return true only for an explicitly proven complete matrix.

    Protocol v3 registration is useful for diagnosis, but protocol alone is
    never treated as a sandbox capability. Unknown matrix keys are harmless;
    all required keys must be literal ``True``.
    """

    if not isinstance(value, dict):
        return False
    return all(value.get(key) is True for key in REQUIRED_ISOLATION_CAPABILITIES)


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
    protocol_version: StrictInt | None = Field(default=1, ge=1, le=3)
    # A v3 Worker may report an incomplete matrix and remain registered for
    # diagnostics. Control persists the fact but keeps the execution gate
    # false until every required capability is explicitly true.
    isolation_capabilities: dict[str, bool] = Field(default_factory=dict)

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

    @field_validator("protocol_version", mode="before")
    @classmethod
    def normalize_missing_protocol(cls, value: object) -> int:
        # Missing and explicit JSON null are the rolling v1 contract. Make the
        # rejection explicit here so a future Pydantic coercion change cannot
        # turn bool/float/string input into a protocol capability.
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("protocol_version must be an integer")
        if not 1 <= value <= 3:
            raise ValueError("protocol_version must be between 1 and 3")
        return value

    @field_validator("isolation_capabilities", mode="before")
    @classmethod
    def validate_isolation_capabilities(cls, value: object) -> dict[str, bool]:
        if value is None:
            return {}
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not key or not isinstance(flag, bool)
            for key, flag in value.items()
        ):
            raise ValueError("isolation_capabilities must be an object of boolean flags")
        return dict(value)


class WorkerResponse(BaseModel):
    """Worker representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    last_heartbeat: datetime
    capabilities: list[str]
    protocol_version: StrictInt = Field(ge=1, le=3)
    isolation_capabilities: dict[str, bool] = Field(default_factory=dict)
    isolation_preflight_status: str = "unknown"
    isolation_preflight_at: datetime | None = None
    rabbitmq_execution_v3: bool = False


class WorkerHeartbeat(BaseModel):
    """Optional v3 capability refresh carried by a heartbeat."""

    model_config = ConfigDict(extra="forbid")

    isolation_capabilities: dict[str, bool] | None = None

    @field_validator("isolation_capabilities", mode="before")
    @classmethod
    def validate_matrix(cls, value: object) -> dict[str, bool] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not key or not isinstance(flag, bool)
            for key, flag in value.items()
        ):
            raise ValueError("isolation_capabilities must be an object of boolean flags")
        return dict(value)


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
