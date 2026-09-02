"""Explicit administrator endpoints for the Issue #130 migration rehearsal."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.security import require_admin_principal
from dlr.control.services import migration as migration_service

router = APIRouter(dependencies=[Depends(require_admin_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]


class MigrationLimitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(default=100, ge=1, le=1_000)


@router.get("/api/admin/reliable-runtime/inventory", response_model=dict[str, Any])
def migration_inventory(session: DbSession) -> dict[str, Any]:
    return migration_service.inventory(session)


@router.post("/api/admin/reliable-runtime/migration/dry-run", response_model=dict[str, Any])
def migration_dry_run(session: DbSession) -> dict[str, Any]:
    return migration_service.dry_run(session)


@router.post(
    "/api/admin/reliable-runtime/migration/legacy-pending",
    response_model=dict[str, Any],
)
def migrate_legacy_pending(payload: MigrationLimitBody, session: DbSession) -> dict[str, Any]:
    return migration_service.migrate_legacy_pending(session, limit=payload.limit)


@router.post(
    "/api/admin/reliable-runtime/migration/legacy-running-drain",
    response_model=dict[str, Any],
)
def legacy_running_drain(session: DbSession) -> dict[str, Any]:
    return migration_service.legacy_running_drain_status(session)
