"""Closed wire schemas for the Issue #130 Batch 2 runtime contract."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

from dlr.common.managed_input import controlled_input_mount_name
from dlr.control.schemas.worker import TaskInputFile
from dlr.control.schemas.worker_pool import RoutePreview, RouteSnapshot

Decision = Literal["EXECUTE", "ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"]
AttemptStatus = Literal[
    "succeeded", "failed", "timed_out", "cancelled", "worker_lost", "resource_exceeded"
]
IncidentDispositionAction = Literal["recover", "terminate"]
IncidentDispositionReason = Literal[
    "capacity_repaired", "routing_repaired", "operator_cancel", "verified_terminal"
]
IncidentDispositionOutcome = Literal[
    "dispatch_already_pending",
    "recovery_dispatched",
    "cancellation_requested",
    "execution_terminal",
    "stale_incident_ignored",
    "incident_generation_conflict",
    "incident_dispatch_identity_invalid",
    "incident_execution_unsupported",
    "incident_dispatch_identity_unverifiable",
    "incident_stale_generation",
    "incident_execution_active",
    "incident_cancellation_pending",
    "incident_execution_not_queued",
    "incident_dispatch_settled",
    "incident_dispatch_inflight",
    "incident_materials_unavailable",
    "outbox_backlog_full",
]


class IncidentDispositionBody(BaseModel):
    """Closed operator intent; actor identity is always taken from Principal."""

    model_config = ConfigDict(extra="forbid")

    action: IncidentDispositionAction
    expected_generation: StrictInt = Field(ge=1, le=2**53 - 1)
    reason_code: IncidentDispositionReason


class IncidentDispositionReceipt(BaseModel):
    """Non-sensitive durable audit receipt returned by POST and list APIs."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    incident_id: int
    execution_id: int
    idempotency_key: uuid.UUID
    actor_kind: Literal["superadmin", "account"]
    user_id: int | None
    action: IncidentDispositionAction
    reason_code: IncidentDispositionReason
    outcome: IncidentDispositionOutcome
    code: str = Field(min_length=1, max_length=64)
    from_generation: int | None = Field(default=None, ge=1)
    to_generation: int | None = Field(default=None, ge=1)
    from_outbox_id: uuid.UUID | None = None
    to_outbox_id: uuid.UUID | None = None
    execution_status: str = Field(min_length=1, max_length=32)
    created_at: datetime


class IncidentDispositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: IncidentDispositionReceipt
    incident_status: Literal["open", "resolved", "ignored"]
    execution_status: str = Field(min_length=1, max_length=32)
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86_400)


class IncidentDispositionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[IncidentDispositionReceipt]
    next_before_id: uuid.UUID | None = None


class InfrastructureIncidentSummary(BaseModel):
    """Bounded Incident facts plus current advisory disposition capabilities."""

    model_config = ConfigDict(extra="forbid")

    id: int
    execution_id: int
    dispatch_generation: int | None = Field(default=None, ge=1)
    message_id: uuid.UUID | None = None
    kind: str = Field(min_length=1, max_length=64)
    status: Literal["open", "resolved", "ignored"]
    attempts: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    disposition_count: int = Field(ge=0)
    recovery_dispatch_count: int = Field(ge=0)
    last_error: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    recent_disposition: IncidentDispositionReceipt | None = None
    dispositions_url: str
    recover_available: bool
    recover_reason: str | None = None
    terminate_available: bool
    terminate_reason: str | None = None


class AttemptClaimBody(BaseModel):
    """Minimal dispatch facts echoed by the Worker to Control."""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    message_id: str = Field(min_length=1, max_length=128)
    execution_id: StrictInt = Field(gt=0)
    dispatch_generation: StrictInt = Field(ge=1)
    adapter_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java", "typescript", "go"]
    resource_class: str = Field(min_length=1, max_length=64)
    target_worker_id: StrictInt = Field(gt=0)


class ResourceProfile(BaseModel):
    """Immutable profile required by v3 before local execution side effects."""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = Field(ge=1)
    resource_class: str = Field(min_length=1, max_length=64)
    backend: Literal["cgroup_v2"]
    cpu_cores: float = Field(gt=0, allow_inf_nan=False)
    memory_bytes: StrictInt = Field(gt=0)
    pids: StrictInt = Field(gt=0)
    tmp_bytes: StrictInt = Field(gt=0)
    nofile: StrictInt = Field(gt=0)
    execution_timeout_seconds: StrictInt = Field(gt=0)
    claim_timeout_seconds: StrictInt = Field(gt=0)
    recovery_grace_seconds: StrictInt = Field(gt=0)
    workspace_cleanup_attempt_timeout_seconds: StrictInt = Field(gt=0)
    workspace_cleanup_total_timeout_seconds: StrictInt = Field(gt=0)
    stream_max_bytes: StrictInt = Field(gt=0)
    output_max_bytes: StrictInt = Field(gt=0)
    output_preview_max_bytes: StrictInt = Field(gt=0)

    @field_validator("cpu_cores", mode="before")
    @classmethod
    def validate_cpu(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("resource profile cpu_cores must be finite")
        return float(value)

    @model_validator(mode="after")
    def validate_budgets(self) -> ResourceProfile:
        if (
            self.workspace_cleanup_attempt_timeout_seconds
            > self.workspace_cleanup_total_timeout_seconds
        ):
            raise ValueError("workspace cleanup attempt budget exceeds total budget")
        if self.workspace_cleanup_total_timeout_seconds >= self.recovery_grace_seconds:
            raise ValueError("workspace cleanup budget must be below recovery grace")
        return self


class _TaskPayloadBase(BaseModel):
    """Common immutable task fields returned after a successful Claim."""

    model_config = ConfigDict(extra="forbid")

    @model_serializer(mode="wrap")
    def compatible_wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        # Keep ordinary tasks readable by the previous fixed-v3 Worker.
        if self.builtin_package_snapshot is None:
            data.pop("builtin_package_snapshot", None)
        if not self.dependency_check:
            data.pop("dependency_check", None)
        return data

    dispatch_backend: Literal["rabbitmq"] = "rabbitmq"
    # Concrete wire versions replace these fields in place. This keeps the
    # existing v3 field order and serializer output unchanged.
    protocol_version: int
    execution_id: StrictInt = Field(gt=0)
    attempt_id: StrictInt = Field(gt=0)
    attempt_no: StrictInt = Field(gt=0)
    fencing_token: StrictInt = Field(gt=0)
    lease_expires_at: datetime
    lease_seconds: StrictInt = Field(gt=0)
    renew_seconds: StrictInt = Field(gt=0)
    claim_token: str = Field(min_length=1, max_length=512)
    cleanup_token: str = Field(min_length=1, max_length=512)
    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java", "typescript", "go"]
    code: str
    requirements: str
    runtime_config: dict[str, Any]
    input: Any
    latest_version_id: StrictInt | None = Field(default=None, gt=0)
    execution_timeout_seconds: StrictInt = Field(gt=0)
    secrets: dict[str, str] = Field(default_factory=dict)
    builtin_package_snapshot: dict[str, Any] | None = None
    dependency_check: bool = False
    index_url: str | None = None
    locale: Literal["zh-CN", "en"] = "zh-CN"
    resource_profile: ResourceProfile
    credential_bindings: list[dict[str, Any]] = Field(default_factory=list)
    input_source_type: Literal["none", "json", "webhook", "managed_files"]
    input_snapshot: dict[str, Any]
    input_files: list[Any] = Field(default_factory=list)
    recovery_grace_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)
    workspace_cleanup_attempt_timeout_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)
    workspace_cleanup_total_timeout_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_lease_intervals(self) -> Self:
        if self.renew_seconds * 3 >= self.lease_seconds:
            raise ValueError("renew interval must be less than one third of lease")
        return self


class V3TaskPayload(_TaskPayloadBase):
    """Current v3 wire shape; validation and defaults remain unchanged."""

    protocol_version: Literal[3] = 3
    input_files: list[TaskInputFile] = Field(default_factory=list)


class V4TaskInputFile(BaseModel):
    """Closed, path-free descriptor for an immutable leased input file."""

    model_config = ConfigDict(extra="forbid")

    id: StrictInt = Field(gt=0, le=2**63 - 1)
    ordinal: StrictInt = Field(ge=0, le=7)
    mount_name: str
    original_filename: str
    content_type: str
    size_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_mount_name(self) -> Self:
        if self.mount_name != controlled_input_mount_name(self.ordinal, self.original_filename):
            raise ValueError("input mount name must match the frozen filename and ordinal")
        return self


class V4TaskPayload(_TaskPayloadBase):
    """A future v4 payload; defining this schema does not enable v4 Claim."""

    protocol_version: Literal[4]
    execution_id: StrictInt = Field(gt=0, le=2**63 - 1)
    attempt_id: StrictInt = Field(gt=0, le=2**63 - 1)
    attempt_no: StrictInt = Field(gt=0, le=2**31 - 1)
    fencing_token: StrictInt = Field(gt=0, le=2**63 - 1)
    adapter_id: StrictInt = Field(gt=0, le=2**63 - 1)
    version_id: StrictInt = Field(gt=0, le=2**63 - 1)
    worker_identity_id: uuid.UUID
    instance_id: uuid.UUID
    instance_generation: StrictInt = Field(gt=0, le=2**63 - 1)
    latest_version_id: StrictInt | None = Field(gt=0, le=2**63 - 1)
    input_files: list[V4TaskInputFile] = Field(default_factory=list, max_length=8)
    recovery_grace_seconds_snapshot: StrictInt = Field(gt=0)
    workspace_cleanup_attempt_timeout_seconds_snapshot: StrictInt = Field(gt=0)
    workspace_cleanup_total_timeout_seconds_snapshot: StrictInt = Field(gt=0)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def validate_protocol_version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("protocol_version must be the integer 4")
        return value

    @field_validator("worker_identity_id", "instance_id")
    @classmethod
    def validate_identity(cls, value: uuid.UUID) -> uuid.UUID:
        if value.int == 0:
            raise ValueError("worker identity must not be nil")
        return value

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code must not be blank")
        return value

    @model_validator(mode="after")
    def validate_v4_snapshots_and_files(self) -> Self:
        profile = self.resource_profile
        if (
            self.execution_timeout_seconds != profile.execution_timeout_seconds
            or self.recovery_grace_seconds_snapshot != profile.recovery_grace_seconds
            or self.workspace_cleanup_attempt_timeout_seconds_snapshot
            != profile.workspace_cleanup_attempt_timeout_seconds
            or self.workspace_cleanup_total_timeout_seconds_snapshot
            != profile.workspace_cleanup_total_timeout_seconds
            or profile.output_preview_max_bytes > profile.output_max_bytes
        ):
            raise ValueError("task budget snapshots must match the resource profile")
        if any(item.ordinal != ordinal for ordinal, item in enumerate(self.input_files)):
            raise ValueError("input file ordinals must start at zero and be consecutive")
        if self.input_source_type != "managed_files":
            if self.input_files:
                raise ValueError("only managed input may carry file descriptors")
            return self
        frozen = self.input_snapshot.get("artifacts")
        revision = self.input_snapshot.get("revision")
        if (
            self.input_snapshot.get("source_type") != "managed_files"
            or type(revision) is not int
            or revision <= 0
            or not isinstance(frozen, list)
            or not 1 <= len(self.input_files) <= 8
            or len(frozen) != len(self.input_files)
            or len({item.id for item in self.input_files}) != len(self.input_files)
        ):
            raise ValueError("managed input does not match its frozen snapshot")
        for item, entry in zip(self.input_files, frozen, strict=True):
            if (
                not isinstance(entry, dict)
                or type(entry.get("ordinal")) is not int
                or type(entry.get("size_bytes")) is not int
                or entry
                != {
                    "ordinal": item.ordinal,
                    "original_filename": item.original_filename,
                    "content_type": item.content_type,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
            ):
                raise ValueError("managed input does not match its frozen snapshot")
        return self


class ClaimDecision(BaseModel):
    """Closed Control decision consumed by the RabbitMQ Worker."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    reason: str = Field(min_length=1, max_length=64)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    attempt_id: int | None = Field(default=None, gt=0)
    cancel_requested: bool = False
    payload: V3TaskPayload | None = None

    @model_validator(mode="after")
    def validate_decision_shape(self) -> ClaimDecision:
        if self.decision == "EXECUTE" and self.payload is None:
            raise ValueError("EXECUTE requires a v3 task payload")
        if self.decision != "EXECUTE" and self.payload is not None:
            raise ValueError("non-EXECUTE decision cannot contain a task payload")
        if self.decision == "DEFER" and self.retry_after_seconds is None:
            raise ValueError("DEFER requires bounded retry_after_seconds")
        if self.decision == "EXECUTE" and self.attempt_id is None:
            raise ValueError("EXECUTE requires attempt_id")
        return self


class V4RenewDecision(ClaimDecision):
    """A v4 renew reports the conservative authority remaining at the DB sample."""

    lease_remaining_ms: StrictInt | None = Field(default=None, ge=1, le=86_400_000)

    @model_validator(mode="after")
    def validate_remaining(self) -> V4RenewDecision:
        if (self.reason == "renewed") != (self.lease_remaining_ms is not None):
            raise ValueError("renewed decision requires remaining authority")
        return self


class AttemptActionBody(BaseModel):
    """Fence and token fields shared by start/renew/progress/result calls."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: StrictInt = Field(gt=0)
    fencing_token: StrictInt = Field(gt=0)
    claim_token: str = Field(min_length=1, max_length=512)


class AttemptRenewBody(AttemptActionBody):
    pass


class AttemptStartBody(AttemptActionBody):
    pass


class AttemptProgressBody(AttemptActionBody):
    stdout_chunk: str = ""
    stderr_chunk: str = ""


class AttemptResultBody(AttemptActionBody):
    status: AttemptStatus
    output: Any = None
    output_size: int | None = Field(default=None, ge=0)
    output_truncated: bool = False
    output_preview: str | None = None
    stdout: str = ""
    stdout_truncated: bool = False
    stderr: str = ""
    stderr_truncated: bool = False
    error: str | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_class: str | None = Field(default=None, max_length=64)
    workspace_cleanup_status: Literal["completed", "deferred"] | None = None
    workspace_cleanup_error_code: str | None = Field(default=None, max_length=64)
    resource_usage: dict[str, Any] | None = None
    cleanup_summary: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_workspace_cleanup_result(self) -> AttemptResultBody:
        if self.workspace_cleanup_status == "deferred":
            if self.workspace_cleanup_error_code != "workspace_cleanup_failed":
                raise ValueError("deferred workspace cleanup requires workspace_cleanup_failed")
        elif self.workspace_cleanup_error_code is not None:
            raise ValueError("workspace cleanup error code requires deferred status")
        return self


class AttemptPrepareFailedBody(AttemptActionBody):
    error_code: str = Field(default="attempt_prepare_failed", max_length=64)
    error_class: str = Field(default="platform_transient", max_length=64)
    error: str | None = None


class AttemptSummary(BaseModel):
    """Safe Attempt detail: never returns either token or code/input."""

    id: int
    execution_id: int
    adapter_id: int
    attempt_no: int
    worker_id: int
    fencing_token: int
    lease_expires_at: datetime
    status: str
    claimed_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    error_code: str | None
    resource_usage_json: dict[str, Any] | None
    output_summary: dict[str, Any] | None
    cleanup_summary: dict[str, Any] | None


class ReliableExecutionDetail(BaseModel):
    """Lightweight reliable detail exposed to authenticated business users."""

    execution_id: int
    dispatch_backend: Literal["rabbitmq"]
    status: str
    attempts: list[AttemptSummary]
    incidents: list[InfrastructureIncidentSummary]
    replay_available: bool
    replay_reason: str | None = None
    route_snapshot: RouteSnapshot | None = None
    replay_route: RoutePreview | None = None


class ReplayResponse(BaseModel):
    execution_id: int
    replay_of_execution_id: int
    route_snapshot: RouteSnapshot | None = None


class HoldPurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=256)
