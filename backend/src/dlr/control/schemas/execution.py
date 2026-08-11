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
