"""Manage Pool configuration without opening production Pool intake."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.worker_pool import (
    EnvironmentRule,
    Language,
    PoolCreate,
    PoolDetail,
    PoolMemberUpdate,
    PoolUpdate,
    PoolView,
)
from dlr.control.security import Principal, require_admin_principal, require_principal
from dlr.control.services import worker_pool

router = APIRouter(tags=["worker-pools"])
DbSession = Annotated[Session, Depends(db.get_session)]
AdminPrincipal = Annotated[Principal, Depends(require_admin_principal)]
AccountPrincipal = Annotated[Principal, Depends(require_principal)]
PathId = Annotated[int, Path(ge=1, le=2**63 - 1)]


@router.get("/api/worker-pools", response_model=list[PoolView])
def list_pools(principal: AccountPrincipal, session: DbSession) -> list[PoolView]:
    return worker_pool.list_pools(session)


@router.post("/api/worker-pools", response_model=PoolDetail, status_code=201)
def create_pool(payload: PoolCreate, principal: AdminPrincipal, session: DbSession) -> PoolDetail:
    return worker_pool.create_pool(session, payload, principal)


@router.get("/api/worker-pools/{pool_id}", response_model=PoolDetail)
def read_pool(pool_id: PathId, principal: AdminPrincipal, session: DbSession) -> PoolDetail:
    return worker_pool.pool_detail(session, pool_id)


@router.patch("/api/worker-pools/{pool_id}", response_model=PoolDetail)
def update_pool(
    pool_id: PathId, payload: PoolUpdate, principal: AdminPrincipal, session: DbSession
) -> PoolDetail:
    return worker_pool.update_pool(session, pool_id, payload, principal)


@router.put("/api/worker-pools/{pool_id}/members/{worker_id}", response_model=PoolDetail)
def update_member(
    pool_id: PathId,
    worker_id: PathId,
    payload: PoolMemberUpdate,
    principal: AdminPrincipal,
    session: DbSession,
) -> PoolDetail:
    return worker_pool.update_member(session, pool_id, worker_id, payload, principal)


@router.delete("/api/worker-pools/{pool_id}/revisions/{revision}", response_model=PoolDetail)
def delete_revision(
    pool_id: PathId,
    revision: PathId,
    expected_version: Annotated[int, Query(ge=1, le=2**63 - 1)],
    principal: AdminPrincipal,
    session: DbSession,
) -> PoolDetail:
    return worker_pool.delete_revision(session, pool_id, revision, expected_version, principal)


@router.get("/api/worker-pools/{pool_id}/revisions/{revision}/member-preview")
def preview_members(
    pool_id: PathId,
    revision: PathId,
    language: Language,
    principal: AdminPrincipal,
    session: DbSession,
) -> list[dict[str, object]]:
    return worker_pool.preview_members(session, pool_id, revision, language)


@router.get("/api/worker-environments", response_model=list[EnvironmentRule])
def list_environments(principal: AdminPrincipal, session: DbSession) -> list[EnvironmentRule]:
    return worker_pool.list_environments(session)


@router.post("/api/worker-environments", response_model=EnvironmentRule, status_code=201)
def create_environment(
    payload: EnvironmentRule, principal: AdminPrincipal, session: DbSession
) -> EnvironmentRule:
    return worker_pool.create_environment(session, payload)
