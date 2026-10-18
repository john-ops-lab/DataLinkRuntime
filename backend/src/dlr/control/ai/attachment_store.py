"""Bounded ephemeral AI files, independent of business artifacts and history.

References are locators, never authorization. API checks edit permission and
this store checks Principal/Adapter/page scope plus each frozen identity.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from dlr.common.config import settings
from dlr.control.ai import attachments
from dlr.control.ai.attachment_documents import (
    DocumentAsset,
    DocumentConversionError,
    DocumentConverterClient,
    DocumentResult,
)
from dlr.control.ai.attachment_reading import (
    AttachmentMedia,
    AttachmentReadResult,
    ScopedAttachmentReader,
    validate_sheet_range,
)
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference, AiPreparedAttachment
from dlr.control.security import Principal

MAX_SCOPES = 128
MAX_TRACKED_SCOPES = MAX_SCOPES * 4
MAX_SCOPES_PER_OWNER = MAX_SCOPES
MAX_METADATA_BYTES = 512 * 1024
MAX_ACTIVE_OPERATIONS = 2
MAX_PENDING_OPERATIONS = 4
MAX_CACHED_READS = 32
_PAGE_FIELDS = {"page", "status", "source_sheet", "image_asset_id", "structure_status"}
_SHEET_FIELDS = {
    "name",
    "rows",
    "columns",
    "hidden",
    "print_area",
    "rendered_page_start",
    "rendered_page_end",
}
Owner = tuple[str, int | None]
Scope = tuple[Owner, int, str]
logger = logging.getLogger("dlr.control.ai.attachments")


def principal_owner(principal: Principal) -> Owner:
    return principal.kind, principal.user_id


def _error(code: str) -> DocumentConversionError:
    return DocumentConversionError(code)


def _bounded_metadata(value: object) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise _error("ai_attachment_too_large")
        result = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise _error("ai_attachment_converter_invalid_result") from None
    if not isinstance(result, dict):
        raise _error("ai_attachment_converter_invalid_result")
    return cast(dict[str, Any], result)


@dataclass
class _Asset:
    content_type: str
    path: Path
    sha256: str
    size_bytes: int
    width: int | None
    height: int | None
    page_count: int | None = None


@dataclass
class _Record:
    scope: Scope
    attachment_ref: str
    filename: str
    content_type: str
    sha256: str
    size_bytes: int
    directory: Path
    expires: float
    charge: int
    representation_revision: str = ""
    descriptor: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, _Asset] = field(default_factory=dict)
    cache: dict[tuple[str, ...], tuple[dict[str, Any], tuple[str, ...]]] = field(
        default_factory=dict
    )
    preparing: bool = True
    closed: bool = False
    pins: int = 0
    jobs: set[str] = field(default_factory=set)
    cancel: threading.Event = field(default_factory=threading.Event)
    rendered_pdf: Path | None = None


class AiAttachmentStore:
    def __init__(
        self,
        root: Path,
        client: DocumentConverterClient,
        *,
        ttl: int = 1800,
        max_bytes: int = 512 * 1024 * 1024,
        derived_max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.root = root
        self.client = client
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.derived_max_bytes = derived_max_bytes
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._closed_scopes: dict[Scope, float] = {}
        self._used_bytes = 0
        self._active = threading.BoundedSemaphore(MAX_ACTIVE_OPERATIONS)
        self._pending = threading.BoundedSemaphore(MAX_PENDING_OPERATIONS)
        owner_fd: int | None = None
        created_directory: Path | None = None
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if root.is_symlink():
                raise _error("ai_attachment_store_unavailable")
            root.chmod(0o700)
            owner_fd = os.open(root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _error("ai_attachment_store_busy") from None
            # Only this function's marked, inactive namespace is eligible for orphan cleanup.
            for child in root.glob("instance-*"):
                marker = child / ".dlr-ai-owner.json"
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and marker.is_file()
                    and not marker.is_symlink()
                ):
                    try:
                        owned = json.loads(marker.read_text()) == {"schema": "dlr-ai-ephemeral-v1"}
                    except (OSError, ValueError):
                        owned = False
                    if owned:
                        shutil.rmtree(child)
            self.directory = root / ("instance-" + secrets.token_hex(16))
            self.directory.mkdir(mode=0o700)
            created_directory = self.directory
            marker = self.directory / ".dlr-ai-owner.json"
            marker.write_text('{"schema":"dlr-ai-ephemeral-v1"}')
            marker.chmod(0o600)
        except Exception as error:
            # A failed constructor never transfers ownership to the store singleton.
            if created_directory is not None:
                with suppress(OSError):
                    shutil.rmtree(created_directory)
            if owner_fd is not None:
                os.close(owner_fd)
            if isinstance(error, OSError):
                raise _error("ai_attachment_store_unavailable") from None
            raise
        self._owner_fd = owner_fd

    def _scope(self, owner: Owner, adapter_id: int, scope_id: UUID | str) -> Scope:
        return owner, adapter_id, str(scope_id)

    def _check_scope_capacity_locked(self, scope: Scope) -> None:
        # An admitted active scope already owns its future close slot. Counting
        # the union avoids charging pins/closed records twice and lets release
        # finish even when every slot is occupied. Unknown closes use the same
        # owner quota, so changing Adapter/page IDs cannot fill the global pool.
        tracked = self._closed_scopes.keys() | {record.scope for record in self._records.values()}
        if scope in tracked:
            return
        if (
            len(tracked) >= MAX_TRACKED_SCOPES
            or sum(selected[0] == scope[0] for selected in tracked) >= MAX_SCOPES_PER_OWNER
        ):
            raise _error("ai_attachment_store_full")

    def _delete_locked(self, record: _Record) -> None:
        if record.pins or record.preparing or record.jobs:
            return
        if record.attachment_ref in self._records:
            try:
                shutil.rmtree(record.directory)
            except FileNotFoundError:
                pass
            except OSError:
                # Keep the closed record and its conservative charge for a later sweep.
                logger.warning("AI attachment cleanup will retry")
                return
            self._records.pop(record.attachment_ref)
            self._used_bytes -= record.charge

    def sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._closed_scopes = {
                scope: expiry for scope, expiry in self._closed_scopes.items() if expiry > now
            }
            for record in list(self._records.values()):
                if record.closed or (
                    record.expires <= now and not record.pins and not record.preparing
                ):
                    record.closed = True
                    record.cancel.set()
                    self._delete_locked(record)

    def _get_locked(self, scope: Scope, reference: str) -> _Record:
        record = self._records.get(reference)
        if record is None or record.scope != scope:
            raise _error("ai_attachment_expired")
        if record.closed or (record.expires <= time.monotonic() and not record.pins):
            raise _error("ai_attachment_expired")
        if record.preparing:
            raise _error("ai_attachment_not_ready")
        return record

    def _public(self, record: _Record) -> AiPreparedAttachment:
        descriptor = record.descriptor
        return AiPreparedAttachment(
            attachment_ref=record.attachment_ref,
            sha256=record.sha256,
            representation_revision=record.representation_revision,
            filename=record.filename,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            status="ready" if descriptor.get("status") == "success" else "partial",
            page_count=descriptor.get("page_count", 0),
            pages=[
                {k: v for k, v in page.items() if k in _PAGE_FIELDS}
                for page in descriptor.get("pages", [])
            ],
            sheets=[
                {k: v for k, v in sheet.items() if k in _SHEET_FIELDS}
                for sheet in descriptor.get("sheets", [])
            ],
            coverage=descriptor.get("coverage", {}),
            warnings=descriptor.get("warnings", []),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=max(0, record.expires - time.monotonic())),
        )

    def get(
        self, owner: Owner, adapter_id: int, scope_id: UUID | str, reference: str
    ) -> AiPreparedAttachment:
        self.sweep()
        with self._lock:
            return self._public(
                self._get_locked(self._scope(owner, adapter_id, scope_id), reference)
            )

    def _read_bytes(self, path: Path, *, limit: int, sha256: str | None = None) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with io.FileIO(fd, closefd=True) as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise OSError("not a regular attachment")
                raw = file.read(limit + 1)
        except OSError:
            raise _error("ai_attachment_expired") from None
        if len(raw) > limit or (sha256 is not None and hashlib.sha256(raw).hexdigest() != sha256):
            raise _error("ai_attachment_hash_mismatch")
        return raw

    def _write_bytes(self, path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)

    @contextmanager
    def _operation(self, record: _Record, deadline: float) -> Iterator[str]:
        if not self._pending.acquire(blocking=False):
            raise _error("ai_attachment_converter_busy")
        active = False
        job_id = secrets.token_hex(20)
        try:
            while not active:
                if record.cancel.is_set():
                    raise _error("ai_attachment_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error("ai_attachment_parse_timeout")
                active = self._active.acquire(timeout=min(0.05, remaining))
            with self._lock:
                if record.closed:
                    raise _error("ai_attachment_cancelled")
                record.jobs.add(job_id)
            yield job_id
        finally:
            try:
                with self._lock:
                    record.jobs.discard(job_id)
                    if record.closed:
                        self._delete_locked(record)
            finally:
                if active:
                    self._active.release()
                self._pending.release()

    def _accept_result(self, record: _Record, result: DocumentResult) -> tuple[str, ...]:
        if result.source_sha256 != record.sha256 or result.status == "failure":
            raise _error("ai_attachment_parse_failed")
        if (
            record.representation_revision
            and result.representation_revision != record.representation_revision
        ):
            raise _error("ai_attachment_hash_mismatch")
        if (
            not isinstance(result.pages, list)
            or not all(isinstance(page, dict) for page in result.pages)
            or not isinstance(result.sheets, list)
            or not all(isinstance(sheet, dict) for sheet in result.sheets)
            or not isinstance(result.coverage, dict)
            or not isinstance(result.data, dict)
            or not isinstance(result.warnings, list)
            or not all(isinstance(warning, str) for warning in result.warnings)
        ):
            raise _error("ai_attachment_converter_invalid_result")
        descriptor = _bounded_metadata(
            {
                "status": result.status,
                "page_count": result.page_count,
                "pages": result.pages,
                "sheets": result.sheets,
                "coverage": result.coverage,
                "warnings": result.warnings,
                "data": result.data,
            }
        )
        if not 0 <= result.page_count <= 1000:
            raise _error("ai_attachment_too_large")
        assets = tuple(result.assets)
        if len({asset.asset_id for asset in assets}) != len(assets):
            raise _error("ai_attachment_converter_invalid_result")
        try:
            AiAttachmentReference(
                attachment_ref=record.attachment_ref,
                sha256=record.sha256,
                representation_revision=result.representation_revision,
            )
        except ValueError:
            raise _error("ai_attachment_converter_invalid_result") from None
        # Validate pixels, bytes and digests before accepting any returned asset.
        for asset in assets:
            self._media(record, asset, page=None)
        with self._lock:
            if record.closed or record.cancel.is_set():
                raise _error("ai_attachment_cancelled")
            new_assets = [asset for asset in assets if asset.asset_id not in record.assets]
            if any(
                record.assets[asset.asset_id].sha256 != asset.sha256
                for asset in assets
                if asset.asset_id in record.assets
            ):
                raise _error("ai_attachment_hash_mismatch")
            increment = sum(asset.size_bytes for asset in new_assets)
            if record.preparing:
                increment += len(json.dumps(descriptor, ensure_ascii=False).encode("utf-8"))
            if result.rendered_pdf is not None and record.rendered_pdf is None:
                increment += len(result.rendered_pdf)
            if record.charge - record.size_bytes + increment > self.derived_max_bytes:
                raise _error("ai_attachment_too_large")
            if self._used_bytes + increment > self.max_bytes:
                raise _error("ai_attachment_store_full")
            # Generated local names never contain a user filename or external path.
            pending_assets: dict[str, _Asset] = {}
            written: list[Path] = []
            pdf_path: Path | None = None
            try:
                for asset in new_assets:
                    path = record.directory / ("asset-" + secrets.token_hex(16))
                    written.append(path)
                    self._write_bytes(path, asset.data)
                    pending_assets[asset.asset_id] = _Asset(
                        asset.content_type,
                        path,
                        asset.sha256,
                        asset.size_bytes,
                        asset.width,
                        asset.height,
                        asset.page_count,
                    )
                if result.rendered_pdf is not None and record.rendered_pdf is None:
                    pdf_path = record.directory / "rendered-pdf"
                    written.append(pdf_path)
                    self._write_bytes(pdf_path, result.rendered_pdf)
            except OSError:
                for path in written:
                    path.unlink(missing_ok=True)
                raise _error("ai_attachment_store_unavailable") from None
            record.assets.update(pending_assets)
            if pdf_path is not None:
                record.rendered_pdf = pdf_path
            self._used_bytes += increment
            record.charge += increment
            record.expires = time.monotonic() + self.ttl
            if record.preparing:
                record.representation_revision = result.representation_revision
                record.descriptor = descriptor
            return tuple(asset.asset_id for asset in assets)

    def _media(
        self, record: _Record, asset: DocumentAsset | _Asset, *, page: int | None
    ) -> AttachmentMedia:
        raw = (
            asset.data
            if isinstance(asset, DocumentAsset)
            else self._read_bytes(
                asset.path,
                limit=self.derived_max_bytes,
                sha256=asset.sha256,
            )
        )
        try:
            return AttachmentMedia(
                content_type=asset.content_type,
                data_base64=base64.b64encode(raw).decode("ascii"),
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                attachment_ref=record.attachment_ref,
                width=asset.width,
                height=asset.height,
                page_count=getattr(asset, "page_count", None),
                page=page,
            )
        except ValueError:
            raise _error("ai_attachment_converter_invalid_result") from None

    def prepare(
        self,
        owner: Owner,
        adapter_id: int,
        scope_id: UUID | str,
        attachment: AiAttachment,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> AiPreparedAttachment:
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        if cancel_event.is_set():
            raise _error("ai_attachment_cancelled")
        try:
            filename = attachments.sanitize_filename(attachment.filename)
            _category, content_type = attachments.classify(filename, attachment.content_type)
            data = attachments.decode_data(attachment.data_base64)
            attachments._sniff(content_type, data)
        except attachments.AttachmentError as error:
            raise _error(error.code) from None
        scope = self._scope(owner, adapter_id, scope_id)
        self.sweep()
        with self._lock:
            if scope in self._closed_scopes:
                raise _error("ai_attachment_cancelled")
            scopes = {record.scope for record in self._records.values()}
            if scope not in scopes and len(scopes) >= MAX_SCOPES:
                raise _error("ai_attachment_store_full")
            self._check_scope_capacity_locked(scope)
            selected = [
                record
                for record in self._records.values()
                if record.scope == scope and not record.closed
            ]
            if len(selected) >= attachments.MAX_ATTACHMENTS:
                raise _error("ai_attachment_count_exceeded")
            if (
                sum(record.size_bytes for record in selected) + len(data)
                > attachments.MAX_TOTAL_BYTES
            ):
                raise _error("ai_attachment_total_too_large")
            if self._used_bytes + len(data) > self.max_bytes:
                raise _error("ai_attachment_store_full")
            reference = secrets.token_urlsafe(32)
            directory = self.directory / secrets.token_hex(20)
            try:
                directory.mkdir(mode=0o700)
            except OSError:
                raise _error("ai_attachment_store_unavailable") from None
            record = _Record(
                scope,
                reference,
                filename,
                content_type,
                hashlib.sha256(data).hexdigest(),
                len(data),
                directory,
                time.monotonic() + self.ttl,
                len(data),
                cancel=cancel_event,
            )
            self._records[reference] = record
            self._used_bytes += len(data)
            try:
                self._write_bytes(directory / "original", data)
            except OSError:
                record.preparing = False
                record.closed = True
                record.cancel.set()
                self._delete_locked(record)
                raise _error("ai_attachment_store_unavailable") from None
        try:
            with self._operation(record, deadline) as job_id:
                result = self.client.prepare(
                    filename,
                    content_type,
                    data,
                    job_id=job_id,
                    deadline=deadline,
                    cancel=record.cancel,
                )
                self._accept_result(record, result)
            with self._lock:
                record.preparing = False
                if record.closed or record.cancel.is_set():
                    raise _error("ai_attachment_cancelled")
                try:
                    return self._public(record)
                except ValueError:
                    raise _error("ai_attachment_converter_invalid_result") from None
        except Exception:
            with self._lock:
                record.closed = True
                record.preparing = False
                record.cancel.set()
                self._delete_locked(record)
            raise

    def release(
        self, owner: Owner, adapter_id: int, scope_id: UUID | str, reference: str | None = None
    ) -> None:
        scope = self._scope(owner, adapter_id, scope_id)
        jobs: list[str] = []
        self.sweep()
        with self._lock:
            if reference is None:
                self._check_scope_capacity_locked(scope)
                self._closed_scopes[scope] = time.monotonic() + self.ttl
            for record in list(self._records.values()):
                if record.scope == scope and (
                    reference is None or record.attachment_ref == reference
                ):
                    record.closed = True
                    record.cancel.set()
                    jobs.extend(record.jobs)
                    self._delete_locked(record)
        # No external call under the capacity lock. Cancel acknowledges actual reaping.
        for job_id in jobs:
            self.client.cancel(job_id)

    @contextmanager
    def reader(
        self,
        owner: Owner,
        adapter_id: int,
        scope_id: UUID | str,
        references: list[AiAttachmentReference],
        *,
        inline_bytes: int = 0,
        permission_check: Callable[[], object] | None = None,
    ) -> Iterator[ScopedAttachmentReader]:
        scope = self._scope(owner, adapter_id, scope_id)
        self.sweep()
        with self._lock:
            records = [
                self._get_locked(scope, reference.attachment_ref) for reference in references
            ]
            if len({record.attachment_ref for record in records}) != len(records):
                raise _error("ai_attachment_reference_invalid")
            if (
                sum(record.size_bytes for record in records) + inline_bytes
                > attachments.MAX_TOTAL_BYTES
            ):
                raise _error("ai_attachment_total_too_large")
            for record, reference in zip(records, references, strict=True):
                if (
                    reference.sha256 != record.sha256
                    or reference.representation_revision != record.representation_revision
                ):
                    raise _error("ai_attachment_hash_mismatch")
            for record in records:
                record.pins += 1
                record.expires = time.monotonic() + self.ttl
        try:
            yield _Reader(
                self,
                scope,
                {reference.attachment_ref: reference for reference in references},
                permission_check,
            )
        finally:
            with self._lock:
                for record in records:
                    record.pins -= 1
                    if record.closed:
                        self._delete_locked(record)

    def close(self) -> None:
        scopes = {record.scope for record in self._records.values()}
        for owner, adapter_id, scope_id in scopes:
            self.release(owner, adapter_id, scope_id)
        with self._lock:
            for record in self._records.values():
                record.closed = True
            self.sweep()
        if self._records:
            raise _error("ai_attachment_store_busy")
        shutil.rmtree(self.directory)
        fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
        os.close(self._owner_fd)


class _Reader:
    def __init__(
        self,
        store: AiAttachmentStore,
        scope: Scope,
        references: dict[str, AiAttachmentReference],
        permission_check: Callable[[], object] | None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.references = references
        self.permission_check = permission_check

    def _record(self, reference: str) -> _Record:
        if self.permission_check is not None:
            self.permission_check()
        identity = self.references.get(reference)
        if identity is None:
            raise _error("ai_attachment_reference_invalid")
        with self.store._lock:
            record = self.store._get_locked(self.scope, reference)
            if (
                identity.sha256 != record.sha256
                or identity.representation_revision != record.representation_revision
            ):
                raise _error("ai_attachment_hash_mismatch")
            return record

    def overview(self) -> dict[str, object]:
        return {
            "attachments": [
                self.store._public(self._record(reference)).model_dump(mode="json")
                for reference in self.references
            ],
            "coverage": {"directory_only": True, "content_requires_read": True},
        }

    def _read(
        self,
        reference: str,
        *,
        timeout_seconds: float,
        page: int | None = None,
        view: str = "both",
        sheet: str | None = None,
        range_a1: str | None = None,
    ) -> AttachmentReadResult:
        record = self._record(reference)
        if timeout_seconds <= 0:
            raise _error("ai_attachment_parse_timeout")
        deadline = time.monotonic() + min(60, timeout_seconds)
        data = self.store._read_bytes(
            record.directory / "original", limit=attachments.MAX_FILE_BYTES, sha256=record.sha256
        )
        key = (
            ("page", str(page), view)
            if page is not None
            else ("sheet", sheet or "", range_a1 or "")
        )
        with self.store._lock:
            cached = record.cache.get(key)
            if cached is None and len(record.cache) >= MAX_CACHED_READS:
                raise _error("ai_attachment_too_large")
        if cached is None:
            with self.store._operation(record, deadline) as job_id:
                if page is not None:
                    if isinstance(page, bool) or not 1 <= page <= record.descriptor.get(
                        "page_count", 0
                    ):
                        raise _error("ai_attachment_range_invalid")
                    if view not in {"text", "image", "both", "pdf"}:
                        raise _error("ai_attachment_range_invalid")
                    pdf = (
                        self.store._read_bytes(
                            record.rendered_pdf, limit=self.store.derived_max_bytes
                        )
                        if record.rendered_pdf is not None
                        else None
                    )
                    result = self.store.client.read_page(
                        record.filename,
                        record.content_type,
                        data,
                        page=page,
                        view=view,
                        rendered_pdf=pdf,
                        job_id=job_id,
                        deadline=deadline,
                        cancel=record.cancel,
                    )
                else:
                    assert sheet is not None and range_a1 is not None
                    validate_sheet_range(range_a1)
                    if not any(
                        item.get("name") == sheet for item in record.descriptor.get("sheets", [])
                    ):
                        raise _error("ai_attachment_range_invalid")
                    result = self.store.client.read_sheet(
                        record.filename,
                        record.content_type,
                        data,
                        sheet=sheet,
                        range_a1=range_a1,
                        job_id=job_id,
                        deadline=deadline,
                        cancel=record.cancel,
                    )
                ids = self.store._accept_result(record, result)
                content = _bounded_metadata(
                    {
                        **result.data,
                        "attachment_ref": reference,
                        "source_sha256": record.sha256,
                        "representation_revision": record.representation_revision,
                        **(
                            {"page": page}
                            if page is not None
                            else {"sheet": sheet, "range": range_a1}
                        ),
                        "coverage": result.coverage,
                        "warnings": result.warnings,
                    }
                )
                with self.store._lock:
                    if record.closed:
                        raise _error("ai_attachment_cancelled")
                    existing = record.cache.get(key)
                    if existing is not None:
                        cached = existing
                    else:
                        increment = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
                        if (
                            record.charge - record.size_bytes + increment
                            > self.store.derived_max_bytes
                        ):
                            raise _error("ai_attachment_too_large")
                        if self.store._used_bytes + increment > self.store.max_bytes:
                            raise _error("ai_attachment_store_full")
                        record.charge += increment
                        self.store._used_bytes += increment
                        cached = content, ids
                        record.cache[key] = cached
        # Cached results still revalidate the original and media digests above/on read.
        self._record(reference)
        content, ids = cached
        return AttachmentReadResult(
            data=cast(dict[str, object], json.loads(json.dumps(content))),
            media=tuple(
                self.store._media(record, record.assets[asset_id], page=page) for asset_id in ids
            ),
        )

    def read_page(
        self, attachment_ref: str, page: int, view: str, *, timeout_seconds: float
    ) -> AttachmentReadResult:
        return self._read(attachment_ref, page=page, view=view, timeout_seconds=timeout_seconds)

    def read_sheet(
        self, attachment_ref: str, sheet: str, range_a1: str, *, timeout_seconds: float
    ) -> AttachmentReadResult:
        return self._read(
            attachment_ref, sheet=sheet, range_a1=range_a1, timeout_seconds=timeout_seconds
        )


_default_store: AiAttachmentStore | None = None
_default_lock = threading.Lock()


def get_attachment_store() -> AiAttachmentStore:
    global _default_store
    if not settings.ai_document_converter_url:
        raise _error("ai_attachment_converter_unavailable")
    with _default_lock:
        if _default_store is None:
            _default_store = AiAttachmentStore(
                settings.ai_attachment_root,
                DocumentConverterClient(settings.ai_document_converter_url),
                ttl=settings.ai_attachment_ttl_seconds,
                max_bytes=settings.ai_attachment_store_max_bytes,
                derived_max_bytes=settings.ai_attachment_derived_max_bytes,
            )
        return _default_store


def close_attachment_store() -> None:
    global _default_store
    with _default_lock:
        if _default_store is not None:
            _default_store.close()
            _default_store = None


async def attachment_gc_loop() -> None:
    try:
        while True:
            if _default_store is not None:
                try:
                    await asyncio.to_thread(_default_store.sweep)
                except OSError:
                    logger.warning("AI attachment cleanup will retry")
            await asyncio.sleep(30)
    finally:
        await asyncio.to_thread(close_attachment_store)
