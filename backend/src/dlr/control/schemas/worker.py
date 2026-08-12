"""Pydantic schemas for the worker-internal API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_WORKER_NAME_LENGTH = 128


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
    # M2 workers only offer the Python runtime.
    capabilities: list[str] = Field(default_factory=lambda: ["python"])

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("capabilities must not be empty")
        return value


class WorkerResponse(BaseModel):
    """Worker representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    last_heartbeat: datetime
    capabilities: list[str]


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
    code: str
    requirements: str
    runtime_config: dict[str, Any]
    input: Any
    latest_version_id: int | None
    published_version_id: int | None
    execution_timeout_seconds: int
    secrets: dict[str, str] = Field(default_factory=dict)
