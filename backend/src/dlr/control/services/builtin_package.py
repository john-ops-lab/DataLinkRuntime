"""Capacity-serialized library mutations with durable reservations and physical deletion."""

import fcntl
import os
import shutil
import tarfile
import uuid
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.common.builtin_packages import KINDS, PackageValidationError, inspect_package, safe_path
from dlr.common.config import settings
from dlr.control.models.builtin_package import (
    BuiltinPackage,
    BuiltinPackageSettings,
    BuiltinPackageUpload,
)
from dlr.control.models.execution import Execution
from dlr.control.models.platform import PackageSource
from dlr.control.services.adapter import domain_error


def root() -> Path:
    directory = Path(settings.builtin_package_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def storage_path(key: str, suffix: str = ".blob") -> Path:
    if str(uuid.UUID(key)) != key:
        raise domain_error(409, "builtin_storage_invalid", "Invalid library storage identity")
    return root() / f"{key}{suffix}"


def sync_directory() -> None:
    descriptor = os.open(root(), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_lock(key: str, *, shared: bool = False) -> BinaryIO:
    stream = storage_path(key, ".lock").open("a+b")
    try:
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        stream.close()
        raise domain_error(
            409, "builtin_package_busy", "The file is being uploaded or downloaded"
        ) from None
    return stream


def lock_library(session: Session) -> BuiltinPackageSettings:
    session.execute(insert(BuiltinPackageSettings).values(id=1).on_conflict_do_nothing())
    row = session.scalar(
        select(BuiltinPackageSettings).where(BuiltinPackageSettings.id == 1).with_for_update()
    )
    assert row is not None
    return row


def usage(session: Session) -> dict[str, int]:
    saved = int(session.scalar(select(func.coalesce(func.sum(BuiltinPackage.size_bytes), 0))) or 0)
    reserved = int(
        session.scalar(select(func.coalesce(func.sum(BuiltinPackageUpload.size_bytes), 0))) or 0
    )
    config = session.get(BuiltinPackageSettings, 1)
    return {
        "used_bytes": saved,
        "reserved_bytes": reserved,
        "quota_bytes": config.quota_bytes if config else 1073741824,
    }


def set_quota(session: Session, quota: int) -> dict[str, int]:
    config = lock_library(session)
    current = usage(session)
    if quota < current["used_bytes"] + current["reserved_bytes"]:
        raise domain_error(
            409, "builtin_capacity_in_use", "Capacity cannot be lower than saved and reserved bytes"
        )
    config.quota_bytes = quota
    session.commit()
    return usage(session)


def reserve_upload(
    session: Session, *, kind: str, filename: str, size_bytes: int, repository_path: str = ""
) -> BuiltinPackageUpload:
    try:
        safe_path(filename)
        if "/" in filename:
            raise PackageValidationError("invalid filename")
        if kind in {"maven", "goproxy"}:
            safe_path(repository_path)
    except PackageValidationError as error:
        raise domain_error(422, "builtin_package_invalid", str(error)) from None
    config = lock_library(session)
    current = usage(session)
    if current["used_bytes"] + current["reserved_bytes"] + size_bytes > config.quota_bytes:
        raise domain_error(409, "builtin_capacity_exceeded", "Builtin library capacity exceeded")
    if (
        shutil.disk_usage(root()).free - current["reserved_bytes"] - size_bytes
        < settings.builtin_package_min_free_bytes
    ):
        raise domain_error(409, "builtin_disk_space_low", "Insufficient Control disk headroom")
    upload = BuiltinPackageUpload(
        id=str(uuid.uuid4()),
        kind=kind,
        filename=filename,
        size_bytes=size_bytes,
        repository_path=repository_path,
    )
    session.add(upload)
    session.commit()
    return upload


def begin_upload(
    session: Session, upload_id: str
) -> tuple[BuiltinPackageUpload, BinaryIO, BinaryIO]:
    lock_library(session)
    upload = session.get(BuiltinPackageUpload, upload_id)
    if upload is None:
        raise domain_error(404, "builtin_upload_not_found", "Upload reservation not found")
    if (
        session.scalar(select(BuiltinPackage.id).where(BuiltinPackage.storage_key == upload.id))
        is not None
    ):
        raise domain_error(
            409,
            "builtin_storage_invalid",
            "Upload reservation conflicts with saved content; verify restore consistency",
        )
    guard = file_lock(upload.id)
    try:
        # A rename followed by a crashed transaction can leave the reserved blob.
        # Drop it before retrying so one reservation never covers two copies.
        storage_path(upload.id).unlink(missing_ok=True)
        stream = storage_path(upload.id, ".part").open("wb")
    except OSError:
        guard.close()
        raise
    session.commit()
    return upload, guard, stream


def finish_upload(session: Session, upload_id: str, digest: str) -> dict[str, Any]:
    upload = session.get(BuiltinPackageUpload, upload_id)
    if upload is None:
        raise domain_error(404, "builtin_upload_not_found", "Upload reservation not found")
    temporary = storage_path(upload.id, ".part")
    try:
        if temporary.stat().st_size != upload.size_bytes:
            raise PackageValidationError("upload size does not match reservation")
        inspected = inspect_package(temporary, upload.kind, upload.filename, upload.repository_path)
        for key, limit in (("name", 256), ("version", 128), ("environment", 512)):
            if len(inspected[key]) > limit:
                raise PackageValidationError("package metadata exceeds field limits")
    except (
        ValueError,
        OSError,
        EOFError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zlib.error,
        RuntimeError,
        NotImplementedError,
    ) as error:
        raise domain_error(422, "builtin_package_invalid", str(error)) from None
    lock_library(session)
    existing = session.scalar(
        select(BuiltinPackage).where(
            BuiltinPackage.kind == upload.kind,
            or_(
                BuiltinPackage.repository_path == inspected["repository_path"],
                (BuiltinPackage.name == inspected["name"])
                & (BuiltinPackage.version == inspected["version"])
                & (BuiltinPackage.environment == inspected["environment"]),
            ),
        )
    )
    if existing is not None:
        if existing.sha256 != digest or existing.status != "uploaded":
            raise domain_error(
                409,
                "builtin_package_conflict",
                "Different content already exists for this package identity",
            )
        temporary.unlink()
        session.delete(upload)
        session.commit()
        return {"file": response(existing), "already_exists": True}
    item = BuiltinPackage(
        kind=upload.kind,
        name=inspected["name"],
        version=inspected["version"],
        environment=inspected["environment"],
        filename=upload.filename,
        repository_path=inspected["repository_path"],
        package_metadata=inspected["metadata"],
        size_bytes=upload.size_bytes,
        sha256=digest,
        storage_key=upload.id,
        status="uploaded",
    )
    # The durable reservation continues to charge bytes if Control dies before commit.
    os.replace(temporary, storage_path(upload.id))
    sync_directory()
    session.add(item)
    session.delete(upload)
    session.commit()
    session.refresh(item)
    return {"file": response(item), "already_exists": False}


def cancel_upload(session: Session, upload_id: str) -> None:
    lock_library(session)
    upload = session.get(BuiltinPackageUpload, upload_id)
    if upload is None:
        session.commit()
        return
    if (
        session.scalar(select(BuiltinPackage.id).where(BuiltinPackage.storage_key == upload.id))
        is not None
    ):
        raise domain_error(
            409,
            "builtin_storage_invalid",
            "Upload reservation conflicts with saved content; verify restore consistency",
        )
    guard = file_lock(upload.id)
    try:
        # Never release uncertain bytes without successfully removing both possible locations.
        storage_path(upload.id, ".part").unlink(missing_ok=True)
        storage_path(upload.id).unlink(missing_ok=True)
        sync_directory()
        session.delete(upload)
        session.commit()
    finally:
        guard.close()


def response(item: BuiltinPackage) -> dict[str, Any]:
    return {
        key: getattr(item, key)
        for key in (
            "id",
            "kind",
            "name",
            "version",
            "environment",
            "filename",
            "repository_path",
            "size_bytes",
            "sha256",
            "status",
            "created_at",
        )
    }


def snapshot(session: Session, language: str, *, force: bool = False) -> dict[str, Any] | None:
    kind = KINDS[language]
    if not force:
        selected = session.scalar(
            select(PackageSource).where(
                PackageSource.kind == kind, PackageSource.is_default.is_(True)
            )
        )
        if selected is None or selected.index_url != f"dlr-builtin://{kind}":
            return None
    lock_library(session)
    files = session.scalars(
        select(BuiltinPackage)
        .where(BuiltinPackage.kind == kind, BuiltinPackage.status == "uploaded")
        .order_by(BuiltinPackage.id)
    ).all()
    return {
        "kind": kind,
        "files": [
            {
                "id": item.id,
                "filename": item.filename,
                "repository_path": item.repository_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "metadata": item.package_metadata,
            }
            for item in files
        ],
    }


def delete_package(session: Session, file_id: int) -> None:
    lock_library(session)
    item = session.get(BuiltinPackage, file_id)
    if item is None:
        raise domain_error(404, "builtin_package_not_found", "Package not found")
    occupied = session.scalar(
        select(Execution.id)
        .where(
            Execution.builtin_package_snapshot.contains({"files": [{"id": file_id}]}),
            or_(
                Execution.status.in_(("queued", "running", "retry_wait")),
                Execution.workspace_cleanup_status.is_distinct_from("completed"),
            ),
        )
        .limit(1)
    )
    if occupied is not None:
        raise domain_error(
            409,
            "builtin_package_in_use",
            "File is held by unfinished tasks or unconfirmed installation cleanup",
            {"execution_id": occupied},
        )
    guard = file_lock(item.storage_key)
    try:
        item.status = "deleting"
        session.commit()
        try:
            storage_path(item.storage_key).unlink(missing_ok=True)
            sync_directory()
        except OSError:
            raise domain_error(
                409,
                "builtin_delete_failed",
                "Physical deletion failed; check Control storage and retry",
            ) from None
        lock_library(session)
        session.delete(item)
        session.commit()
    finally:
        guard.close()


def open_download(session: Session, file_id: int) -> tuple[BuiltinPackage, BinaryIO, BinaryIO]:
    lock_library(session)
    item = session.get(BuiltinPackage, file_id)
    if item is None or item.status != "uploaded":
        raise domain_error(404, "builtin_package_not_found", "Package not found or being deleted")
    guard = file_lock(item.storage_key, shared=True)
    try:
        stream = storage_path(item.storage_key).open("rb")
        if os.fstat(stream.fileno()).st_size != item.size_bytes:
            stream.close()
            raise OSError("library size mismatch")
    except OSError:
        guard.close()
        raise domain_error(
            409,
            "builtin_content_unavailable",
            "Package content unavailable; verify storage and backup",
        ) from None
    session.commit()
    return item, guard, stream


def download_chunks(stream: BinaryIO, guard: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := stream.read(64 * 1024):
            yield chunk
    finally:
        stream.close()
        guard.close()
