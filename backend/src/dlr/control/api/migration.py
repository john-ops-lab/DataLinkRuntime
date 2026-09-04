"""Explicit administrator endpoints for Issue #130 migration and Cutover."""

from typing import Annotated, Any, Literal

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


class CutoverRetireBody(BaseModel):
    """Explicit operator confirmation for the irreversible index retirement."""

    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["retire-legacy-active-index"]
    expected_schema_revision: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    backup_restore_evidence_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


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


@router.get(
    "/api/admin/reliable-runtime/cutover/preflight",
    response_model=dict[str, Any],
)
def cutover_preflight(session: DbSession) -> dict[str, Any]:
    return migration_service.cutover_preflight(session)


@router.post(
    "/api/admin/reliable-runtime/cutover/retire-legacy-index",
    response_model=dict[str, Any],
)
def retire_legacy_active_index(
    payload: CutoverRetireBody,
    session: DbSession,
) -> dict[str, Any]:
    return migration_service.retire_legacy_active_index(
        session,
        expected_schema_revision=payload.expected_schema_revision,
        backup_restore_evidence_id=payload.backup_restore_evidence_id,
    )


@router.get(
    "/api/admin/reliable-runtime/cutover/invariants",
    response_model=dict[str, Any],
)
def post_cutover_invariants(session: DbSession) -> dict[str, Any]:
    return migration_service.post_cutover_invariants(session)
