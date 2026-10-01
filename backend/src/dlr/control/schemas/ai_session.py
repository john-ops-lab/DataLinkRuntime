"""Public, visible-only AI session responses."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiSessionSummary(_Schema):
    id: uuid.UUID
    adapter_id: int
    revision: int
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class AiSessionMessage(_Schema):
    sequence: int
    turn_id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    source_revision: int
    generation: int
    request_status: Literal["pending", "completed", "failed", "cancelled"] | None


class AiSessionDetail(AiSessionSummary):
    messages: list[AiSessionMessage]
    summary_covered_through: int
    summary_valid: bool


class AiSessionList(_Schema):
    sessions: list[AiSessionSummary] = Field(default_factory=list)
