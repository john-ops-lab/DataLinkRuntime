"""Closed wire schemas for the Issue #130 Batch 2 runtime contract."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from dlr.control.schemas.worker import TaskInputFile

Decision = Literal["EXECUTE", "ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"]
AttemptStatus = Literal[
    "succeeded", "failed", "timed_out", "cancelled", "worker_lost", "resource_exceeded"
]


class AttemptClaimBody(BaseModel):
    """Minimal dispatch facts echoed by the Worker to Control."""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    message_id: str = Field(min_length=1, max_length=128)
    execution_id: StrictInt = Field(gt=0)
    dispatch_generation: StrictInt = Field(ge=1)
    adapter_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java"]
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


class V3TaskPayload(BaseModel):
    """Complete immutable TaskPayload returned only after a successful Claim."""

    model_config = ConfigDict(extra="forbid")

    dispatch_backend: Literal["rabbitmq"] = "rabbitmq"
    protocol_version: Literal[3] = 3
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
    language: Literal["python", "javascript", "java"]
    code: str
    requirements: str
    runtime_config: dict[str, Any]
    input: Any
    latest_version_id: StrictInt | None = Field(default=None, gt=0)
    execution_timeout_seconds: StrictInt = Field(gt=0)
    secrets: dict[str, str] = Field(default_factory=dict)
    index_url: str | None = None
    locale: Literal["zh-CN", "en"] = "zh-CN"
    resource_profile: ResourceProfile
    credential_bindings: list[dict[str, Any]] = Field(default_factory=list)
    input_source_type: Literal["none", "json", "webhook", "managed_files"]
    input_snapshot: dict[str, Any]
    input_files: list[TaskInputFile] = Field(default_factory=list)
    recovery_grace_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)
    workspace_cleanup_attempt_timeout_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)
    workspace_cleanup_total_timeout_seconds_snapshot: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_lease_intervals(self) -> V3TaskPayload:
        if self.renew_seconds * 3 >= self.lease_seconds:
            raise ValueError("renew interval must be less than one third of lease")
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
    incidents: list[dict[str, Any]]
    replay_available: bool
    replay_reason: str | None = None


class ReplayResponse(BaseModel):
    execution_id: int
    replay_of_execution_id: int


class HoldPurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=256)
