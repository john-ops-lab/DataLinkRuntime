"""Pydantic schemas for the Adapter Schedule configuration API (M5.2)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ScheduleMisfirePolicy = Literal["coalesce_latest", "queue_every_occurrence", "skip_while_busy"]
ScheduleOutcome = Literal["enqueued", "coalesced", "skipped", "expired"]


class ScheduleOutcomeResponse(BaseModel):
    """Safe, bounded audit facts shown with the Schedule configuration."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    first_scheduled_for: datetime
    last_scheduled_for: datetime
    occurrence_count: int
    outcome: ScheduleOutcome
    reason: str | None


class ScheduleUpsert(BaseModel):
    """Request body for PUT /api/adapters/{adapter_id}/schedule.

    PUT is create-or-full-replace: every field is mandatory and the saved
    Schedule becomes exactly the submitted configuration. ``input`` follows
    the Execution input contract: any JSON value, including JSON null. During
    the compatibility window an omitted ``input`` preserves the saved Adapter
    input, while an explicit JSON null replaces it with JSON null.
    """

    enabled: bool
    cron: str
    timezone: str
    input: Any = None
    # Optional during the rolling deployment window: omission preserves the
    # server-side value instead of resetting an existing policy.
    misfire_policy: ScheduleMisfirePolicy | None = None
    max_catchup_count: int | None = Field(default=None, ge=1, le=1000)
    max_catchup_age_seconds: int | None = Field(default=None, ge=60, le=604800)

    @field_validator(
        "misfire_policy", "max_catchup_count", "max_catchup_age_seconds", mode="before"
    )
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("schedule policy fields cannot be null when supplied")
        return value


class ScheduleResponse(BaseModel):
    """Current Schedule configuration of one Adapter.

    ``next_run_at`` is the scheduler cursor in UTC: the next planned point
    while enabled, ``null`` while disabled (the fixed M5.2 semantics).
    """

    model_config = ConfigDict(from_attributes=True)

    adapter_id: int
    enabled: bool
    cron: str
    timezone: str
    input: Any
    next_run_at: datetime | None
    last_blocked_reason: str | None
    last_blocked_detail: dict[str, Any] | None
    last_blocked_at: datetime | None
    last_processed_due_at: datetime | None
    misfire_policy: ScheduleMisfirePolicy
    max_catchup_count: int
    max_catchup_age_seconds: int
    recent_outcomes: list[ScheduleOutcomeResponse] = Field(default_factory=list)
    updated_at: datetime
