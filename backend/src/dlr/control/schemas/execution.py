"""Pydantic schemas for Execution management and result reporting."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionCreate(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/executions.

    Any JSON value is a valid input, including JSON null (omitted). M5.4.1
    always runs the latest saved Revision; historical Revision selection is
    not accepted.
    """

    model_config = ConfigDict(extra="forbid")

    input: Any = None


class ExecutionResponse(BaseModel):
    """Full current state of one Execution."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    adapter_id: int
    version_id: int
    worker_id: int | None
    target_worker_id: int | None
    trigger: str
    # M5.2: the planned point for trigger=schedule; null for other triggers.
    scheduled_for: datetime | None
    status: str
    dispatch_backend: Literal["rabbitmq"] = "rabbitmq"
    dispatch_generation: int = 0
    queued_at: datetime | None = None
    next_attempt_at: datetime | None = None
    attempt_count: int = 0
    max_attempts_snapshot: int = 1
    retry_policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    resource_profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    resource_class: str | None = None
    target_worker_id_snapshot: int | None = None
    logical_input_bytes: int = 0
    idempotency_record_id: int | None = None
    last_error_code: str | None = None
    admission_released_at: datetime | None = None
    replay_of_execution_id: int | None = None
    cancel_requested: bool
    input: Any
    input_source_type: str
    input_config_revision: int
    input_snapshot: dict[str, Any]
    timeout_seconds_snapshot: int | None = None
    recovery_grace_seconds_snapshot: int | None = None
    workspace_cleanup_attempt_timeout_seconds_snapshot: int | None = None
    workspace_cleanup_total_timeout_seconds_snapshot: int | None = None
    claim_deadline_at: datetime | None = None
    execution_deadline_at: datetime | None = None
    workspace_cleanup_status: Literal["pending", "completed", "deferred"] | None = None
    workspace_cleanup_error_code: str | None = None
    output: Any = None
    output_size: int | None
    output_truncated: bool
    output_preview: str | None
    stdout: str
    stdout_truncated: bool
    stderr: str
    stderr_truncated: bool
    error: str | None
    error_code: str | None
    locale: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None


class ExecutionResultReport(BaseModel):
    """Terminal result reported by the Worker.

    Control re-validates every big-field contract before persisting, so a
    misbehaving client can never store oversized or half-broken payloads.
    """

    status: Literal["succeeded", "failed", "timeout", "cancelled"]
    output: Any = None
    output_size: int | None = None
    output_truncated: bool = False
    output_preview: str | None = None
    stdout: str = ""
    stdout_truncated: bool = False
    stderr: str = ""
    stderr_truncated: bool = False
    error: str | None = None
    error_code: str | None = Field(default=None, max_length=64)
    workspace_cleanup_status: Literal["completed", "deferred"] | None = None
    workspace_cleanup_error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_workspace_cleanup_result(self) -> "ExecutionResultReport":
        """Keep cleanup state machine values stable and unambiguous."""
        if self.workspace_cleanup_status == "deferred":
            if self.workspace_cleanup_error_code != "workspace_cleanup_failed":
                raise ValueError("deferred workspace cleanup requires workspace_cleanup_failed")
        elif self.workspace_cleanup_error_code is not None:
            raise ValueError("workspace cleanup error code requires deferred status")
        return self


class ProgressReport(BaseModel):
    """Best-effort stdout/stderr chunks reported while an Execution runs.

    Progress never changes Execution status, output, error or timing fields;
    the M2 final result remains the authoritative source of truth.
    """

    stdout_chunk: str = ""
    stderr_chunk: str = ""


class WorkspaceCleanupReceipt(BaseModel):
    """C0 wire shape for the later deferred-cleanup acknowledgement."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"] = "completed"


class ProgressAck(BaseModel):
    """Acknowledgement of one progress upload (M3.2).

    Carries the cancel flag so the owning Worker learns about a cancel
    request on its normal live-log round trip, without an extra endpoint.
    """

    cancel_requested: bool


class ExecutionSummary(BaseModel):
    """Lightweight history row; never carries input/output/stdout/stderr."""

    id: int
    adapter_id: int
    version_id: int
    version_seq: int
    worker_id: int | None
    worker_name: str | None
    trigger: str
    # M5.2: the planned point for trigger=schedule; null for other triggers.
    scheduled_for: datetime | None
    status: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None


class ExecutionHistoryPage(BaseModel):
    """One cursor page of an Adapter's execution history (newest first).

    ``next_before_id`` is present only when a next page exists, so clients
    never show a useless "load more" action at the end of the history.
    """

    items: list[ExecutionSummary]
    next_before_id: int | None = None
