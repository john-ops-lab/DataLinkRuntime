"""Bounded, secret-free batch intent and observation contracts."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BatchAction = Literal["start", "stop"]
BatchMode = Literal["manual", "schedule", "webhook", "subscription"]
BatchItemAction = Literal[
    "run_once",
    "cancel_executions",
    "enable_schedule",
    "disable_schedule",
    "enable_webhook",
    "disable_webhook",
    "enable_subscription",
    "disable_subscription",
]
BatchStatus = Literal[
    "pending", "accepted", "stopping", "succeeded", "no_change", "failed", "unknown"
]


class BatchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: int = Field(gt=0, strict=True)
    expected_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    action: BatchAction
    targets: list[BatchTarget] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_targets(self) -> "BatchCreate":
        if len({target.adapter_id for target in self.targets}) != len(self.targets):
            raise ValueError("Batch targets must be distinct")
        return self


class BatchRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def distinct_targets(self) -> "BatchRetry":
        if any(isinstance(value, bool) or value <= 0 for value in self.adapter_ids):
            raise ValueError("Adapter IDs must be positive integers")
        if len(set(self.adapter_ids)) != len(self.adapter_ids):
            raise ValueError("Retry targets must be distinct")
        return self


class BatchExecution(BaseModel):
    id: int
    status: str
    cancel_requested: bool


class BatchItemResponse(BaseModel):
    adapter_id: int
    adapter_name: str | None = None
    mode: BatchMode | None = None
    action: BatchItemAction | None = None
    status: BatchStatus
    error_code: str | None = None
    retryable: bool = False
    execution_ids: list[int] = Field(default_factory=list)
    executions: list[BatchExecution] = Field(default_factory=list)
    subscription_id: int | None = None
    subscription_state: str | None = None


class BatchResponse(BaseModel):
    id: uuid.UUID
    action: BatchAction
    created_at: datetime
    items: list[BatchItemResponse]
