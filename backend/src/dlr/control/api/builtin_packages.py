"""Administrator library API; package managers never receive these credentials."""

import hashlib
import os
import shutil
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only
from starlette.concurrency import run_in_threadpool

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models.builtin_package import BuiltinPackage, BuiltinPackageUpload
from dlr.control.schemas.execution import ExecutionResponse
from dlr.control.security import require_admin_principal, require_worker_token
from dlr.control.services import builtin_package as library
from dlr.control.services.adapter import domain_error
from dlr.control.services.worker import validate_claim_for_route

router = APIRouter(prefix="/api/builtin-packages", dependencies=[Depends(require_admin_principal)])
worker_router = APIRouter(dependencies=[Depends(require_worker_token)])
DbSession = Annotated[Session, Depends(db.get_session)]


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pypi", "npm", "maven", "goproxy"]
    filename: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(gt=0, le=16 * 1024**3, strict=True)
    repository_path: str = Field(default="", max_length=512)


class QuotaRequest(BaseModel):
    quota_bytes: int = Field(gt=0, le=16 * 1024**4, strict=True)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: int = Field(gt=0, strict=True)
    worker_id: int = Field(gt=0, strict=True)


class CheckSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    adapter_id: int
    version_id: int
    created_at: datetime
    target_worker_id: int | None
    status: str
    error: str | None
    error_code: str | None


@router.get("")
def list_library(session: DbSession, kind: str | None = None, q: str = "") -> dict[str, Any]:
    query = select(BuiltinPackage).order_by(
        BuiltinPackage.created_at.desc(), BuiltinPackage.id.desc()
    )
    if kind:
        query = query.where(BuiltinPackage.kind == kind)
    if q:
        query = query.where(
            BuiltinPackage.name.icontains(q, autoescape=True)
            | BuiltinPackage.filename.icontains(q, autoescape=True)
        )
    return {
        "files": [library.response(item) for item in session.scalars(query)],
        "capacity": library.usage(session),
        "uploads": [
            {
                "id": row.id,
                "filename": row.filename,
                "size_bytes": row.size_bytes,
                "created_at": row.created_at,
            }
            for row in session.scalars(
                select(BuiltinPackageUpload).order_by(BuiltinPackageUpload.created_at)
            )
        ],
    }


@router.patch("/capacity")
def update_capacity(payload: QuotaRequest, session: DbSession) -> dict[str, int]:
    return library.set_quota(session, payload.quota_bytes)


@router.post("/uploads", status_code=201)
def reserve_upload(payload: UploadRequest, session: DbSession) -> dict[str, Any]:
    upload = library.reserve_upload(session, **payload.model_dump())
    return {"id": upload.id, "size_bytes": upload.size_bytes}


@router.put("/uploads/{upload_id}")
async def upload_content(upload_id: str, request: Request, session: DbSession) -> dict[str, Any]:
    upload, guard, stream = await run_in_threadpool(library.begin_upload, session, upload_id)
    total = 0
    digest = hashlib.sha256()
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > upload.size_bytes:
                raise domain_error(413, "builtin_upload_too_large", "Upload exceeds reserved size")
            if (
                shutil.disk_usage(library.root()).free - len(chunk)
                < settings.builtin_package_min_free_bytes
            ):
                raise domain_error(
                    409, "builtin_disk_space_low", "Insufficient Control disk headroom"
                )
            await run_in_threadpool(stream.write, chunk)
            digest.update(chunk)
        await run_in_threadpool(stream.flush)
        await run_in_threadpool(os.fsync, stream.fileno())
        stream.close()
        return await run_in_threadpool(
            library.finish_upload, session, upload_id, digest.hexdigest()
        )
    finally:
        stream.close()
        guard.close()
        # Failed/interrupted reservations stay charged until explicit cancel/retry.
        # No timeout can prove a stream is no longer active; file_lock does.


@router.delete("/uploads/{upload_id}", status_code=204)
def cancel_upload(upload_id: str, session: DbSession) -> Response:
    library.cancel_upload(session, upload_id)
    return Response(status_code=204)


def _download(session: Session, file_id: int) -> StreamingResponse:
    item, guard, stream = library.open_download(session, file_id)
    return StreamingResponse(
        library.download_chunks(stream, guard),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(item.size_bytes),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(item.filename)}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/sources/{kind}")
def choose_builtin_source(
    kind: Literal["pypi", "npm", "maven", "goproxy"], session: DbSession
) -> dict[str, Any]:
    from dlr.control.models.platform import PackageSource
    from dlr.control.services.package_source import (
        _available_default_name,
        _clear_other_defaults,
        package_source_response,
    )

    library.lock_library(session)
    source = session.scalar(
        select(PackageSource).where(
            PackageSource.kind == kind, PackageSource.index_url == f"dlr-builtin://{kind}"
        )
    )
    if source is None:
        source = PackageSource(
            name=_available_default_name(session, f"DLR builtin source ({kind})"),
            kind=kind,
            index_url=f"dlr-builtin://{kind}",
            is_default=False,
        )
        session.add(source)
        session.flush()
    _clear_other_defaults(session, kind, keep_id=source.id)
    source.is_default = True
    session.commit()
    return package_source_response(session, source).model_dump(mode="json")


@router.get("/{file_id}/content")
def download_package(file_id: int, session: DbSession) -> StreamingResponse:
    return _download(session, file_id)


@router.delete("/{file_id}", status_code=204)
def delete_package(file_id: int, session: DbSession) -> Response:
    library.delete_package(session, file_id)
    return Response(status_code=204)


@router.post("/checks", status_code=202, response_model=ExecutionResponse)
def create_check(payload: CheckRequest, session: DbSession) -> ExecutionResponse:
    from dlr.control.models.adapter import Adapter
    from dlr.control.services.reliable_execution import accept_execution

    adapter = session.get(Adapter, payload.adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    execution = accept_execution(
        session,
        adapter,
        trigger="manual",
        runtime_input=None,
        input_source_type="none",
        input_config_revision=1,
        input_snapshot={"source_type": "none", "dependency_check": True},
        dependency_check=True,
        worker_id_override=payload.worker_id,
    )
    session.commit()
    session.refresh(execution)
    return ExecutionResponse.model_validate(execution)


@router.get("/checks/recent", response_model=list[CheckSummary])
def list_checks(session: DbSession) -> list[CheckSummary]:
    from dlr.control.models.execution import Execution

    return [
        CheckSummary.model_validate(row)
        for row in session.scalars(
            select(Execution)
            .options(
                load_only(
                    Execution.id,
                    Execution.adapter_id,
                    Execution.version_id,
                    Execution.created_at,
                    Execution.target_worker_id,
                    Execution.status,
                    Execution.error,
                    Execution.error_code,
                )
            )
            .where(Execution.dependency_check.is_(True))
            .order_by(Execution.id.desc())
            .limit(30)
        )
    ]


@worker_router.get(
    "/api/workers/{worker_id}/executions/{execution_id}/builtin-packages/{file_id}/content"
)
def download_for_worker(
    worker_id: int,
    execution_id: int,
    file_id: int,
    session: DbSession,
    claim_token: Annotated[str | None, Header(alias="X-DLR-Claim-Token")] = None,
) -> StreamingResponse:
    execution = validate_claim_for_route(session, worker_id, execution_id, claim_token)
    snapshot = execution.builtin_package_snapshot or {}
    files = snapshot.get("files", [])
    if not isinstance(files, list) or not any(
        isinstance(item, dict) and item.get("id") == file_id for item in files
    ):
        raise domain_error(
            403, "builtin_package_not_leased", "File is not in this task's dependency snapshot"
        )
    return _download(session, file_id)
