"""Pydantic schemas for Execution management and result reporting."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ExecutionCreate(BaseModel):
    """Request body for POST /api/adapters/{adapter_id}/executions.

    Any JSON value is a valid input, including JSON null (omitted).
    ``version_id`` omitted/null runs the Adapter's latest version.
    """

    input: Any = None
    version_id: int | None = None


class ExecutionResponse(BaseModel):
    """Full current state of one Execution."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    adapter_id: int
    version_id: int
    worker_id: int | None
    trigger: str
    status: str
    input: Any
    output: Any = None
    output_size: int | None
    output_truncated: bool
    output_preview: str | None
    stdout: str
    stdout_truncated: bool
    stderr: str
    stderr_truncated: bool
    error: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None


class ExecutionResultReport(BaseModel):
    """Terminal result reported by the Worker.

    Control re-validates every big-field contract before persisting, so a
    misbehaving client can never store oversized or half-broken payloads.
    """

    status: Literal["succeeded", "failed", "timeout"]
    output: Any = None
    output_size: int | None = None
    output_truncated: bool = False
    output_preview: str | None = None
    stdout: str = ""
    stdout_truncated: bool = False
    stderr: str = ""
    stderr_truncated: bool = False
    error: str | None = None


class ProgressReport(BaseModel):
    """Best-effort stdout/stderr chunks reported while an Execution runs.

    Progress never changes Execution status, output, error or timing fields;
    the M2 final result remains the authoritative source of truth.
    """

    stdout_chunk: str = ""
    stderr_chunk: str = ""


class ExecutionSummary(BaseModel):
    """Lightweight history row; never carries input/output/stdout/stderr."""

    id: int
    adapter_id: int
    version_id: int
    version_seq: int
    worker_id: int | None
    worker_name: str | None
    trigger: str
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
