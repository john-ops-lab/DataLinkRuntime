"""Pydantic schemas for the worker-internal API."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_WORKER_NAME_LENGTH = 128
SUPPORTED_CAPABILITIES = frozenset({"python", "javascript", "java", "typescript", "go"})
REQUIRED_ISOLATION_CAPABILITIES = frozenset(
    {
        "cgroup_v2",
        "cgroup_namespace_private",
        "mount_namespace",
        "pid_namespace",
        "memory_hard_limit",
        "pids_hard_limit",
        "tmpfs_hard_limit",
        "bounded_output",
        "preflight_passed",
        "resource_envelope_verified",
        "cpu_hard_limit",
        "swap_hard_limit",
        "nofile_hard_limit",
        "no_new_privileges",
        "cgroup_kill",
        "adapter_control_plane_hidden",
        "adapter_mount_blocked",
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
    # The internal wire version is fixed, not a deployment mode.
    protocol_version: StrictInt = Field(ge=3, le=3)
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
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("protocol_version must be an integer")
        if value != 3:
            raise ValueError("unsupported Worker protocol")
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
    builtin_package_snapshot: dict[str, Any] | None = None
    dependency_check: bool = False
    index_url: str | None = None
    # Captured at Execution creation; never read again from deployment state.
    locale: str = "zh-CN"
    protocol_version: StrictInt = Field(default=3, ge=3, le=3)
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
    claim_attempt: int = Field(gt=0)


class CleanupResult(BaseModel):
    """Secret-free completion report for an adapter cleanup task."""

    success: bool
    claim_attempt: int | None = Field(default=None, gt=0)
    error_code: Literal["cache_cleanup_retained", "cache_cleanup_failed"] | None = None


class CacheCleanupContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleanup_id: StrictInt = Field(gt=0)
    claim_attempt: StrictInt = Field(gt=0)


class CacheObservedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    language: Literal["python", "javascript", "java", "typescript", "go"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CacheReplacementContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: StrictInt = Field(gt=0)
    attempt_id: StrictInt = Field(gt=0)
    fencing_token: StrictInt = Field(gt=0)
    claim_token: str = Field(min_length=1, max_length=512)
    old_identity: CacheObservedIdentity
    target_language: Literal["python", "javascript", "java", "typescript", "go"]
    target_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CacheGuardAcquire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    operation_id: uuid.UUID
    cleanup_context: CacheCleanupContext | None = None
    observed_identity: CacheObservedIdentity | None = None
    replacement_context: CacheReplacementContext | None = None


class CacheGuardResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: StrictInt = Field(gt=0)
    outcome: Literal["completed", "aborted"]


class CacheGuardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    worker_id: int
    adapter_id: int
    version_id: int
    generation: int
    operation_id: uuid.UUID | None
    phase: Literal["idle", "acquired"]


class CacheGuardOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    worker_id: int
    adapter_id: int
    version_id: int
    generation: int
    operation_id: uuid.UUID
    phase: Literal["acquired", "completed", "aborted"]
    operation_kind: Literal["gc", "cleanup", "replacement"] = "gc"
    cleanup_id: int | None = None
    cleanup_claim_attempt: int | None = None
    observed_identity: dict[str, Any] | None = None
    replacement_context: dict[str, Any] | None = None


class CacheGuardPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CacheGuardResponse]
    next_after_version_id: int | None = None


class CacheReferenceResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: StrictInt = Field(gt=0)
    attempt_id: StrictInt | None = Field(default=None, gt=0)


class CacheReferenceResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=3, max_length=80)
    adapter_id: int
    version_id: int


class CacheKeyReferenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)


class CacheKeyReferenceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["cache_keys_v1"]
    items: list[CacheKeyReferenceItem] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def reject_duplicate_keys(self) -> "CacheKeyReferenceBatch":
        keys = {(item.adapter_id, item.version_id) for item in self.items}
        if len(keys) != len(self.items):
            raise ValueError("cache keys must be unique")
        return self


CacheReferenceReason = Literal[
    "cache_identity_unknown",
    "cache_operation_in_progress",
    "reference_query_truncated",
    "reference_identity_conflict",
    "execution_queued",
    "execution_running",
    "execution_retry_wait",
    "attempt_history_unknown",
    "attempt_identity_unknown",
    "attempt_active",
    "attempt_cleanup_unknown",
    "attempt_cleanup_incomplete",
    "workspace_cleanup_incomplete",
    "incident_open",
    "recovery_material_active",
    "adapter_cleanup_incomplete",
]


class CacheKeyReferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: int
    version_id: int
    status: Literal["clear", "protected", "unknown"]
    reasons: list[CacheReferenceReason] = Field(max_length=16)


class CacheKeyReferenceBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["cache_keys_v1"] = "cache_keys_v1"
    worker_id: int
    sampled_at: float
    complete: bool
    items: list[CacheKeyReferenceResult]
