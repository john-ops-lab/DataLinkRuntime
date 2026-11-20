"""Capacity-serialized library mutations with durable reservations and physical deletion."""

import asyncio
import fcntl
import hashlib
import os
import shutil
import tarfile
import uuid
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dlr.common.builtin_packages import KINDS, PackageValidationError, inspect_package, safe_path
from dlr.common.config import settings
from dlr.control import db
from dlr.control.models import ExecutionAttempt
from dlr.control.models.builtin_package import (
    BuiltinPackage,
    BuiltinPackageSettings,
    BuiltinPackageUpload,
)
from dlr.control.models.execution import Execution
from dlr.control.models.platform import PackageSource
from dlr.control.services import control_ha_capacity, storage_backends, storage_maintenance
from dlr.control.services.adapter import domain_error
from dlr.control.services.shared_posix import open_directory, open_regular


def root() -> Path:
    directory = Path(settings.builtin_package_root)
    if settings.control_ha_slot is not None:
        with _shared_root():
            return directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def storage_path(key: str, suffix: str = ".blob") -> Path:
    if str(uuid.UUID(key)) != key:
        raise domain_error(409, "builtin_storage_invalid", "Invalid library storage identity")
    return root() / f"{key}{suffix}"


@contextmanager
def _shared_root() -> Iterator[int]:
    namespace = storage_backends.active_namespace("builtin_packages")
    with namespace.open(slot=settings.control_ha_slot) as directory:
        yield directory


def free_bytes() -> int:
    if settings.control_ha_slot is None:
        return shutil.disk_usage(root()).free
    with _shared_root() as directory:
        capacity = os.fstatvfs(directory)
        return capacity.f_bavail * capacity.f_frsize


def _material_name(key: str, suffix: str = ".blob") -> str:
    try:
        if str(uuid.UUID(key)) != key:
            raise ValueError
    except (ValueError, TypeError):
        raise domain_error(
            409, "builtin_storage_invalid", "Invalid library storage identity"
        ) from None
    return f"{key}{suffix}"


def open_blob(key: str) -> BinaryIO:
    if settings.control_ha_slot is None:
        return os.fdopen(
            os.open(storage_path(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        )
    with _shared_root() as directory:
        return os.fdopen(open_regular(directory, _material_name(key), os.O_RDONLY), "rb")


def _shared_discard(key: str, *, part: bool = False) -> None:
    with _shared_root() as directory:
        name = _material_name(key, ".part" if part else ".blob")
        with suppress(FileNotFoundError):
            os.close(open_regular(directory, name, os.O_RDONLY))
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)


def sync_directory() -> None:
    descriptor = os.open(root(), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_lock(key: str, *, shared: bool = False) -> BinaryIO:
    if settings.control_ha_slot is None:
        stream = storage_path(key, ".lock").open("a+b")
    else:
        lock_name = hashlib.sha256(_material_name(key).encode("ascii")).hexdigest()[:2]
        with (
            _shared_root() as directory,
            open_directory(directory, ".library-locks", create=True) as locks,
        ):
            stream = os.fdopen(
                open_regular(locks, lock_name, os.O_RDWR | os.O_CREAT),
                "r+b",
            )
    try:
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise domain_error(
            409, "builtin_package_busy", "The file is being uploaded or downloaded"
        ) from None
    except BaseException:
        stream.close()
        raise
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
    free = free_bytes() if settings.control_ha_slot is not None else None
    if settings.control_ha_slot is not None:
        control_ha_capacity.assert_local_control(session)
    config = lock_library(session)
    current = usage(session)
    if current["used_bytes"] + current["reserved_bytes"] + size_bytes > config.quota_bytes:
        raise domain_error(409, "builtin_capacity_exceeded", "Builtin library capacity exceeded")
    if (free if free is not None else free_bytes()) - current[
        "reserved_bytes"
    ] - size_bytes < settings.builtin_package_min_free_bytes:
        raise domain_error(409, "builtin_disk_space_low", "Insufficient Control disk headroom")
    upload = BuiltinPackageUpload(
        id=str(uuid.uuid4()),
        kind=kind,
        filename=filename,
        size_bytes=size_bytes,
        repository_path=repository_path,
    )
    session.add(upload)
    control_ha_capacity.recheck_local_control(session)
    session.commit()
    return upload


def begin_upload(
    session: Session, upload_id: str
) -> tuple[BuiltinPackageUpload, BinaryIO, BinaryIO]:
    if settings.control_ha_slot is not None:
        return _begin_shared_upload(session, upload_id)
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


def _begin_shared_upload(
    session: Session, upload_id: str
) -> tuple[BuiltinPackageUpload, BinaryIO, BinaryIO]:
    reservation = session.get(BuiltinPackageUpload, upload_id)
    available = reservation is not None and reservation.cancelled_at is None
    session.rollback()
    if not available:
        raise domain_error(404, "builtin_upload_not_found", "Upload reservation not found")
    guard = file_lock(upload_id)
    try:
        control_ha_capacity.assert_local_control(session)
        lock_library(session)
        upload = session.get(BuiltinPackageUpload, upload_id)
        if upload is None or upload.cancelled_at is not None:
            raise domain_error(404, "builtin_upload_not_found", "Upload reservation not found")
        _assert_reserved_blob(session, upload_id)
        upload.writer_started_at = session.scalar(select(func.clock_timestamp()))
        control_ha_capacity.recheck_local_control(session)
        session.commit()
        _shared_discard(upload_id)
        _shared_discard(upload_id, part=True)
        with _shared_root() as directory:
            stream = os.fdopen(
                open_regular(
                    directory,
                    _material_name(upload_id, ".part"),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                ),
                "wb",
            )
        return upload, guard, stream
    except BaseException:
        guard.close()
        raise


def _assert_reserved_blob(session: Session, upload_id: str) -> None:
    if (
        session.scalar(select(BuiltinPackage.id).where(BuiltinPackage.storage_key == upload_id))
        is not None
    ):
        raise domain_error(
            409, "builtin_storage_invalid", "Upload reservation conflicts with saved content"
        )


def check_shared_upload(upload_id: str) -> None:
    with db.SessionLocal() as session:
        row = session.get(BuiltinPackageUpload, upload_id)
        if row is None or row.cancelled_at is not None:
            raise domain_error(409, "builtin_upload_cancelled", "Upload was cancelled")


def _publish_shared_blob(upload_id: str) -> None:
    with _shared_root() as directory:
        temporary = _material_name(upload_id, ".part")
        destination = _material_name(upload_id)
        try:
            os.stat(destination, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise domain_error(409, "builtin_storage_invalid", "Package destination already exists")
        with os.fdopen(open_regular(directory, temporary, os.O_RDONLY), "rb") as stream:
            os.fsync(stream.fileno())
        os.rename(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)


def finish_upload(session: Session, upload_id: str, digest: str) -> dict[str, Any]:
    upload = session.get(BuiltinPackageUpload, upload_id)
    if upload is None or upload.cancelled_at is not None:
        raise domain_error(404, "builtin_upload_not_found", "Upload reservation not found")
    shared = settings.control_ha_slot is not None
    if shared:
        session.commit()
    temporary = storage_path(upload.id, ".part") if not shared else None
    try:
        if shared:
            with _shared_root() as directory:
                fd = open_regular(directory, _material_name(upload_id, ".part"), os.O_RDONLY)
                try:
                    if os.fstat(fd).st_size != upload.size_bytes:
                        raise PackageValidationError("upload size does not match reservation")
                    inspected = inspect_package(
                        Path(f"/proc/self/fd/{fd}"),
                        upload.kind,
                        upload.filename,
                        upload.repository_path,
                    )
                finally:
                    os.close(fd)
        else:
            assert temporary is not None
            if temporary.stat().st_size != upload.size_bytes:
                raise PackageValidationError("upload size does not match reservation")
            inspected = inspect_package(
                temporary, upload.kind, upload.filename, upload.repository_path
            )
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
    if shared:
        _publish_shared_blob(upload_id)
        control_ha_capacity.assert_local_control(session)
    lock_library(session)
    if shared:
        current = session.get(BuiltinPackageUpload, upload_id, populate_existing=True)
        if current is None or current.cancelled_at is not None:
            raise domain_error(409, "builtin_upload_cancelled", "Upload was cancelled")
        upload = current
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
        if shared:
            upload.cancelled_at = session.scalar(select(func.clock_timestamp()))
            result = response(existing)
            control_ha_capacity.recheck_local_control(session)
            session.commit()
            _finish_cancelled(session, upload_id)
            return {"file": result, "already_exists": True}
        if temporary is not None:
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
    if temporary is not None:
        os.replace(temporary, storage_path(upload.id))
        sync_directory()
    session.add(item)
    session.delete(upload)
    control_ha_capacity.recheck_local_control(session)
    session.commit()
    session.refresh(item)
    return {"file": response(item), "already_exists": False}


def cancel_upload(session: Session, upload_id: str) -> None:
    if settings.control_ha_slot is not None:
        _cancel_shared_upload(session, upload_id)
        return
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


def _cancel_shared_upload(session: Session, upload_id: str, *, locked: bool = False) -> None:
    control_ha_capacity.assert_local_control(session)
    lock_library(session)
    upload = session.get(BuiltinPackageUpload, upload_id)
    if upload is None:
        session.rollback()
        return
    _assert_reserved_blob(session, upload_id)
    if upload.cancelled_at is None:
        upload.cancelled_at = session.scalar(select(func.clock_timestamp()))
    control_ha_capacity.recheck_local_control(session)
    session.commit()
    if locked:
        _finish_cancelled(session, upload_id)
        return
    try:
        guard = file_lock(upload_id)
    except HTTPException as error:
        if isinstance(error.detail, dict) and error.detail.get("code") == "builtin_package_busy":
            return
        raise
    with guard:
        _finish_cancelled(session, upload_id)


def _finish_cancelled(session: Session, upload_id: str) -> None:
    control_ha_capacity.assert_local_control(session)
    upload = session.get(BuiltinPackageUpload, upload_id, populate_existing=True)
    if upload is None or upload.cancelled_at is None:
        session.rollback()
        return
    _assert_reserved_blob(session, upload_id)
    session.rollback()
    _shared_discard(upload_id, part=True)
    _shared_discard(upload_id)
    control_ha_capacity.assert_local_control(session)
    lock_library(session)
    upload = session.get(BuiltinPackageUpload, upload_id, populate_existing=True)
    if upload is not None and upload.cancelled_at is not None:
        _assert_reserved_blob(session, upload_id)
        session.delete(upload)
    control_ha_capacity.recheck_local_control(session)
    session.commit()


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
    if settings.control_ha_slot is None:
        _delete_package(session, file_id, None)
        return
    key = session.scalar(select(BuiltinPackage.storage_key).where(BuiltinPackage.id == file_id))
    session.rollback()
    if key is None:
        raise domain_error(404, "builtin_package_not_found", "Package not found")
    with file_lock(key):
        _delete_package(session, file_id, key)


def _delete_package(session: Session, file_id: int, shared_key: str | None) -> None:
    if shared_key is not None:
        control_ha_capacity.assert_local_control(session)
    lock_library(session)
    item = session.get(BuiltinPackage, file_id)
    if item is None or (shared_key is not None and item.storage_key != shared_key):
        raise domain_error(404, "builtin_package_not_found", "Package not found")
    occupied = session.scalar(
        select(Execution.id)
        .where(
            Execution.builtin_package_snapshot.contains({"files": [{"id": file_id}]}),
            or_(
                Execution.status.in_(("queued", "running", "retry_wait")),
                Execution.workspace_cleanup_status.is_distinct_from("completed"),
                select(ExecutionAttempt.id)
                .where(
                    ExecutionAttempt.execution_id == Execution.id,
                    ExecutionAttempt.worker_identity_id.is_not(None),
                    ExecutionAttempt.cleanup_settled_at.is_(None),
                )
                .exists(),
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
    guard = file_lock(item.storage_key) if shared_key is None else None
    try:
        item.status = "deleting"
        control_ha_capacity.recheck_local_control(session)
        session.commit()
        try:
            if shared_key is not None:
                _shared_discard(shared_key)
            else:
                storage_path(item.storage_key).unlink(missing_ok=True)
                sync_directory()
        except OSError:
            raise domain_error(
                409,
                "builtin_delete_failed",
                "Physical deletion failed; check Control storage and retry",
            ) from None
        if shared_key is not None:
            control_ha_capacity.assert_local_control(session)
        lock_library(session)
        session.delete(item)
        control_ha_capacity.recheck_local_control(session)
        session.commit()
    finally:
        if guard is not None:
            guard.close()


def open_download(session: Session, file_id: int) -> tuple[BuiltinPackage, BinaryIO, BinaryIO]:
    if settings.control_ha_slot is not None:
        return _open_shared_download(session, file_id)
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


def _open_shared_download(
    session: Session, file_id: int
) -> tuple[BuiltinPackage, BinaryIO, BinaryIO]:
    item = session.get(BuiltinPackage, file_id)
    if item is None or item.status != "uploaded":
        raise domain_error(404, "builtin_package_not_found", "Package not found or being deleted")
    key = item.storage_key
    session.commit()
    guard = file_lock(key, shared=True)
    try:
        item = session.get(BuiltinPackage, file_id, populate_existing=True)
        if item is None or item.status != "uploaded" or key != item.storage_key:
            raise domain_error(
                404, "builtin_package_not_found", "Package not found or being deleted"
            )
        session.commit()
        stream = open_blob(key)
        if os.fstat(stream.fileno()).st_size != item.size_bytes:
            stream.close()
            raise OSError("library size mismatch")
        return item, guard, stream
    except OSError:
        guard.close()
        raise domain_error(
            503, "builtin_content_unavailable", "Package content unavailable"
        ) from None
    except BaseException:
        guard.close()
        raise


def download_chunks(stream: BinaryIO, guard: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := stream.read(64 * 1024):
            yield chunk
    finally:
        stream.close()
        guard.close()


def reconcile_once() -> None:
    namespace = storage_backends.active_namespace("builtin_packages")
    with db.SessionLocal() as session:
        page = storage_maintenance.claim(session, namespace)
        if page is None:
            return
        limit = 25
        uploads = page.shard == 0
        if uploads:
            now = session.scalar(select(func.clock_timestamp()))
            assert now is not None
            candidates = list(
                session.scalars(
                    select(BuiltinPackageUpload.id)
                    .where(
                        BuiltinPackageUpload.id > page.after_key,
                        or_(
                            BuiltinPackageUpload.cancelled_at.is_not(None),
                            BuiltinPackageUpload.writer_started_at < now - timedelta(minutes=10),
                        ),
                    )
                    .order_by(BuiltinPackageUpload.id)
                    .limit(limit)
                )
            )
        else:
            candidates = list(
                session.scalars(
                    select(BuiltinPackage.storage_key)
                    .where(
                        BuiltinPackage.storage_key > page.after_key,
                        BuiltinPackage.status == "deleting",
                    )
                    .order_by(BuiltinPackage.storage_key)
                    .limit(limit)
                )
            )
        session.rollback()
        for key in candidates:
            if not storage_maintenance.current(session, page):
                return
            session.rollback()
            try:
                if uploads:
                    with file_lock(key):
                        _cancel_shared_upload(session, key, locked=True)
                else:
                    file_id = session.scalar(
                        select(BuiltinPackage.id).where(
                            BuiltinPackage.storage_key == key,
                            BuiltinPackage.status == "deleting",
                        )
                    )
                    if file_id is not None:
                        delete_package(session, file_id)
            except (HTTPException, OSError):
                session.rollback()
        more = len(candidates) == limit
        storage_maintenance.finish(
            session,
            page,
            shard=page.shard if more else 1 - page.shard,
            after_key=candidates[-1] if more else "",
        )


async def reconcile_loop() -> None:
    while True:
        if storage_backends.available("builtin_packages"):
            with suppress(OSError, HTTPException, RuntimeError, SQLAlchemyError):
                await asyncio.to_thread(reconcile_once)
        await asyncio.sleep(5)
