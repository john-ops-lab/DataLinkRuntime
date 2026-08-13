"""Pydantic schemas for the Adapter Schedule configuration API (M5.2)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ScheduleUpsert(BaseModel):
    """Request body for PUT /api/adapters/{adapter_id}/schedule.

    PUT is create-or-full-replace: every field is mandatory and the saved
    Schedule becomes exactly the submitted configuration. ``input`` follows
    the Execution input contract: any JSON value, including JSON null
    (omitted).
    """

    enabled: bool
    cron: str
    timezone: str
    input: Any = None


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
    updated_at: datetime
