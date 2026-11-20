"""Cluster-scoped temporary files with database fences and physical reader pins."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
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
from dlr.control.ai.attachment_store import (
    _PAGE_FIELDS,
    _SHEET_FIELDS,
    MAX_ACTIVE_OPERATIONS,
    MAX_METADATA_BYTES,
    MAX_SCOPES,
    MAX_SCOPES_PER_OWNER,
    MAX_TRACKED_SCOPES,
    Owner,
    _bounded_metadata,
)
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference, AiPreparedAttachment
from dlr.control.services import control_ha_capacity, storage_backends, storage_maintenance
from dlr.control.services.shared_posix import (
    open_directory,
    open_regular,
    publish_record,
    read_record,
)

_CATALOG_LOCK = 3817796903147
_PAGE_SIZE = 16
_ORIGINAL_OVERHEAD = 4096
_SELECT = """
    SELECT o.* FROM control_attachment_objects o
    JOIN control_attachment_scopes s USING (namespace_id,principal_key,adapter_id,scope_id)
    WHERE o.namespace_id=:namespace AND o.principal_key=:principal AND o.adapter_id=:adapter
      AND o.scope_id=:scope AND o.reference=:ref AND NOT s.closed
      AND o.state IN ('pending','ready') AND o.expires_at > pg_catalog.clock_timestamp()
"""


def _error(code: str, *, reason: str | None = None) -> DocumentConversionError:
    return DocumentConversionError(code, reason=reason)


@dataclass(frozen=True)
class _Scope:
    principal: str
    adapter: int
    scope: UUID

    @classmethod
    def create(cls, owner: Owner, adapter: int, scope: UUID | str) -> _Scope:
        key = hashlib.sha256(json.dumps(owner, separators=(",", ":")).encode()).hexdigest()
        return cls(key, adapter, UUID(str(scope)))


@dataclass(frozen=True)
class _Job:
    identity: UUID
    converter_id: UUID
    generation: int | None


def _read(directory: int, name: str, limit: int, *, sha256: str | None = None) -> bytes:
    with os.fdopen(open_regular(directory, name, os.O_RDONLY), "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or (sha256 is not None and hashlib.sha256(data).hexdigest() != sha256):
        raise _error("ai_attachment_hash_mismatch")
    return data


def _write(directory: int, name: str, data: bytes) -> None:
    with os.fdopen(
        open_regular(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def _media(reference: str, asset: DocumentAsset, page: int | None) -> AttachmentMedia:
    return AttachmentMedia(
        content_type=asset.content_type,
        data_base64=base64.b64encode(asset.data).decode("ascii"),
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        attachment_ref=reference,
        width=asset.width,
        height=asset.height,
        page_count=asset.page_count,
        page=page,
    )


class SharedAttachmentStore:
    def __init__(self) -> None:
        slot, url = settings.control_ha_slot, settings.ai_document_converter_url
        if slot is None or not url:
            raise _error("ai_attachment_converter_unavailable")
        self.slot = slot
        self.client = DocumentConverterClient(url)
        try:
            self.namespace = storage_backends.active_namespace("ai_attachments")
        except OSError:
            raise _error("ai_attachment_store_unavailable") from None

    def _params(self, scope: _Scope, reference: str = "") -> dict[str, Any]:
        return {
            "namespace": self.namespace.namespace_id,
            "principal": scope.principal,
            "adapter": scope.adapter,
            "scope": scope.scope,
            "ref": reference,
        }

    @contextmanager
    def _catalog(self) -> Iterator[Session]:
        with db.SessionLocal.begin() as session:
            control_ha_capacity.assert_local_control(session)
            session.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"), {"lock": _CATALOG_LOCK}
            )
            if storage_backends.binding_generation(session, self.namespace) is None:
                raise _error("ai_attachment_store_unavailable")
            yield session
            control_ha_capacity.recheck_local_control(session)

    @contextmanager
    def _pin(self, identity: UUID, *, exclusive: bool = False, job: bool = False) -> Iterator[int]:
        stripe = hashlib.sha256(identity.bytes).hexdigest()[:2]
        with (
            self.namespace.open(slot=self.slot) as root,
            open_directory(root, ".job-locks" if job else ".reader-locks", create=True) as locks,
        ):
            descriptor = open_regular(locks, stripe, os.O_RDWR | os.O_CREAT)
            try:
                try:
                    fcntl.flock(
                        descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                    )
                except BlockingIOError:
                    raise _error("ai_attachment_store_busy") from None
                current = os.stat(stripe, dir_fd=locks, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if (current.st_ino, current.st_dev) != (opened.st_ino, opened.st_dev):
                    raise _error("ai_attachment_store_unavailable")
                yield root
            finally:
                os.close(descriptor)

    def _lookup(self, scope: _Scope, reference: str, *, ready: bool = False) -> RowMapping:
        with db.SessionLocal() as session:
            if storage_backends.binding_generation(session, self.namespace) is None:
                raise _error("ai_attachment_store_unavailable")
            row = (
                session.execute(text(_SELECT), self._params(scope, reference))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _error("ai_attachment_expired")
        if ready and row.state != "ready":
            raise _error("ai_attachment_not_ready")
        return row

    def _charge(self, session: Session) -> int:
        # Old unknown jobs also count until a physical stop can be proved.
        return int(
            session.scalar(
                text("""
            SELECT coalesce((SELECT sum(charge) FROM control_attachment_objects),0)
                 + coalesce((SELECT sum(charge) FROM control_attachment_jobs),0)
        """)
            )
            or 0
        )

    def _reserve(self, scope: _Scope, data: bytes) -> tuple[str, UUID]:
        if not data:
            raise _error("ai_attachment_invalid")
        identity, reference = uuid4(), secrets.token_urlsafe(32)
        digest = hashlib.sha256(data).hexdigest()
        charge = len(data) + _ORIGINAL_OVERHEAD
        params = {
            **self._params(scope, reference),
            "id": identity,
            "size": len(data),
            "sha256": digest,
            "charge": charge,
            "ttl": settings.ai_attachment_ttl_seconds,
        }
        with self._catalog() as session:
            existing = session.execute(
                text("""
                SELECT closed FROM control_attachment_scopes
                WHERE namespace_id=:namespace AND principal_key=:principal
                  AND adapter_id=:adapter AND scope_id=:scope
            """),
                params,
            ).scalar_one_or_none()
            if existing is True:
                raise _error("ai_attachment_cancelled", reason="scope_closed")
            if existing is None:
                global_scopes, tracked_scopes, principal_scopes = session.execute(
                    text("""
                    SELECT count(*) FILTER(WHERE NOT closed), count(*),
                           count(*) FILTER(WHERE principal_key=:principal)
                    FROM control_attachment_scopes WHERE namespace_id=:namespace
                """),
                    params,
                ).one()
                if (
                    global_scopes >= MAX_SCOPES
                    or tracked_scopes >= MAX_TRACKED_SCOPES
                    or principal_scopes >= MAX_SCOPES_PER_OWNER
                ):
                    raise _error("ai_attachment_store_full")
            files, size = session.execute(
                text("""
                SELECT count(*), coalesce(sum(size_bytes),0) FROM control_attachment_objects
                WHERE namespace_id=:namespace AND principal_key=:principal AND adapter_id=:adapter
                  AND scope_id=:scope AND state <> 'closed'
            """),
                params,
            ).one()
            if files >= attachments.MAX_ATTACHMENTS:
                raise _error("ai_attachment_count_exceeded")
            if size + len(data) > attachments.MAX_TOTAL_BYTES:
                raise _error("ai_attachment_total_too_large")
            if self._charge(session) + charge > settings.ai_attachment_store_max_bytes:
                raise _error("ai_attachment_store_full")
            now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
            assert now is not None
            params["expires"] = now + timedelta(seconds=params["ttl"])
            params["resume"] = now + timedelta(
                seconds=settings.ai_attachment_prepare_timeout_seconds
            )
            session.execute(
                text("""
                INSERT INTO control_attachment_scopes
                    (namespace_id,principal_key,adapter_id,scope_id,expires_at)
                VALUES (:namespace,:principal,:adapter,:scope,:expires)
                ON CONFLICT (namespace_id,principal_key,adapter_id,scope_id)
                DO UPDATE SET expires_at=greatest(
                    control_attachment_scopes.expires_at,excluded.expires_at)
            """),
                params,
            )
            session.execute(
                text("""
                INSERT INTO control_attachment_objects (reference,namespace_id,principal_key,
                    adapter_id,scope_id,object_id,sha256,size_bytes,charge,expires_at,resume_after,state)
                VALUES (:ref,:namespace,:principal,:adapter,:scope,:id,:sha256,:size,
                        :charge,:expires,:resume,'allocating')
            """),
                params,
            )
        return reference, identity

    def _begin_job(
        self, scope: _Scope, ref: str, operation: Literal["prepare", "read"], deadline: float
    ) -> _Job:
        identity, converter_id = uuid4(), self.client.identity()
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise _error("ai_attachment_parse_timeout")
        charge = settings.ai_attachment_derived_max_bytes
        with self._catalog() as session:
            params = self._params(scope, ref)
            row = session.execute(text(_SELECT), params).mappings().one_or_none()
            if row is None:
                raise _error("ai_attachment_expired")
            if row.state != ("pending" if operation == "prepare" else "ready"):
                raise _error("ai_attachment_not_ready")
            assigned = session.scalar(
                text("""
                SELECT count(*) FROM control_attachment_jobs WHERE control_slot=:slot
            """),
                {"slot": self.slot},
            )
            if assigned is None or assigned >= MAX_ACTIVE_OPERATIONS:
                raise _error("ai_attachment_converter_busy")
            if self._charge(session) + charge > settings.ai_attachment_store_max_bytes:
                raise _error("ai_attachment_store_full")
            generation = None
            if operation == "prepare":
                busy = session.scalar(
                    text("""
                    SELECT EXISTS (SELECT 1 FROM control_attachment_jobs WHERE reference=:ref
                      AND parse_generation=:generation
                      AND lease_until > pg_catalog.clock_timestamp())
                """),
                    {"ref": ref, "generation": row.parse_generation},
                )
                if busy:
                    raise _error("ai_attachment_converter_busy")
                generation = row.parse_generation + 1
                session.execute(
                    text("""
                    UPDATE control_attachment_objects SET parse_generation=:generation
                    WHERE reference=:ref
                """),
                    {"ref": ref, "generation": generation},
                )
            session.execute(
                text("""
                INSERT INTO control_attachment_jobs
                    (job_id,reference,converter_id,control_slot,parse_generation,lease_until,charge)
                VALUES (:id,:ref,:converter,:slot,:generation,
                    least(:expires,pg_catalog.clock_timestamp()+make_interval(secs=>:seconds)),
                    :charge)
            """),
                {
                    "id": identity,
                    "ref": ref,
                    "converter": converter_id,
                    "slot": self.slot,
                    "generation": generation,
                    "expires": row.expires_at,
                    "seconds": seconds,
                    "charge": charge,
                },
            )
        return _Job(identity, converter_id, generation)

    def _converter(self, instance: UUID) -> DocumentConverterClient:
        return DocumentConverterClient(self.client.url, instance_id=instance)

    def _verify_job(self, session: Session, scope: _Scope, ref: str, job: _Job) -> RowMapping:
        row = session.execute(text(_SELECT), self._params(scope, ref)).mappings().one_or_none()
        valid = session.scalar(
            text("""
            SELECT lease_until > pg_catalog.clock_timestamp() FROM control_attachment_jobs
            WHERE job_id=:job AND reference=:ref AND converter_id=:converter
        """),
            {"job": job.identity, "ref": ref, "converter": job.converter_id},
        )
        if (
            row is None
            or not valid
            or (job.generation is not None and row.parse_generation != job.generation)
        ):
            raise _error("ai_attachment_cancelled")
        return row

    def _delete_job(self, session: Session, job: UUID) -> None:
        session.execute(text("DELETE FROM control_attachment_jobs WHERE job_id=:job"), {"job": job})

    def _finish_failed_job(self, identity: UUID, job: _Job) -> None:
        # The caller still holds the converter's local transport custody lock.
        with db.SessionLocal() as session:
            needed = session.scalar(
                text("""
                SELECT (o.result_id IS DISTINCT FROM j.job_id) FROM control_attachment_jobs j
                JOIN control_attachment_objects o USING(reference)
                WHERE j.job_id=:job AND o.object_id=:id AND o.namespace_id=:namespace
            """),
                {"job": job.identity, "id": identity, "namespace": self.namespace.namespace_id},
            )
        if needed is not True:
            return
        self._converter(job.converter_id).cancel(job.identity.hex)
        with self.namespace.open(slot=self.slot) as root:
            self._remove_result(root, identity, "g-" + job.identity.hex)
        with self._catalog() as session:
            self._delete_job(session, job.identity)

    @staticmethod
    def _remove_result(root: int, identity: UUID, result: str) -> None:
        with suppress(FileNotFoundError), open_directory(root, "o-" + identity.hex) as directory:
            shutil.rmtree(result, dir_fd=directory)
            os.fsync(directory)

    def _parse(
        self, scope: _Scope, row: RowMapping, rooted: int, deadline: float, cancel: threading.Event
    ) -> AiPreparedAttachment:
        identity = row.object_id
        with open_directory(rooted, "o-" + identity.hex) as directory:
            header = read_record(directory, "source.json")
            data = _read(directory, "original", row.size_bytes, sha256=row.sha256)
            job = self._begin_job(scope, row.reference, "prepare", deadline)
            with self._pin(job.identity, exclusive=True, job=True):
                client = self._converter(job.converter_id)
                try:
                    with db.SessionLocal() as session:
                        self._verify_job(session, scope, row.reference, job)
                    result = client.prepare(
                        header["filename"],
                        header["content_type"],
                        data,
                        job_id=job.identity.hex,
                        deadline=deadline,
                        cancel=cancel,
                    )
                    client.cancel(job.identity.hex)
                    if cancel.is_set() or result.status == "failure":
                        raise _error("ai_attachment_parse_failed")
                    return self._publish(scope, row, directory, header, result, job)
                except Exception:
                    # A failed commit may have published this exact generation.
                    with db.SessionLocal() as session:
                        published = session.scalar(
                            text("""
                            SELECT result_id=:job FROM control_attachment_objects
                            WHERE reference=:ref
                        """),
                            {"ref": row.reference, "job": job.identity},
                        )
                    if published is not True:
                        self._finish_failed_job(identity, job)
                    raise

    def _publish(
        self,
        scope: _Scope,
        row: RowMapping,
        directory: int,
        header: dict[str, str],
        result: DocumentResult,
        job: _Job,
    ) -> AiPreparedAttachment:
        if result.source_sha256 != row.sha256:
            raise _error("ai_attachment_hash_mismatch")
        prepared = AiPreparedAttachment(
            attachment_ref=row.reference,
            filename=header["filename"],
            content_type=header["content_type"],
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            representation_revision=result.representation_revision,
            status="ready" if result.status == "success" else "partial",
            page_count=result.page_count,
            pages=[{k: v for k, v in page.items() if k in _PAGE_FIELDS} for page in result.pages],
            sheets=[
                {k: v for k, v in sheet.items() if k in _SHEET_FIELDS} for sheet in result.sheets
            ],
            coverage=result.coverage,
            warnings=result.warnings,
            expires_at=row.expires_at,
        )
        serialized = prepared.model_dump_json().encode()
        pdf = result.rendered_pdf or b""
        derived = len(serialized) + len(pdf)
        if (
            len(serialized) > MAX_METADATA_BYTES
            or derived > settings.ai_attachment_derived_max_bytes
        ):
            raise _error("ai_attachment_too_large")
        with open_directory(directory, "g-" + job.identity.hex, create=True) as target:
            _write(target, "prepared.json", serialized)
            if pdf:
                _write(target, "rendered-pdf", pdf)
        with self._catalog() as session:
            current = self._verify_job(session, scope, row.reference, job)
            if current.state != "pending":
                raise _error("ai_attachment_cancelled")
            session.execute(
                text("""
                UPDATE control_attachment_objects
                SET state='ready',result_id=:result,charge=charge+:size
                WHERE reference=:ref
            """),
                {"result": job.identity, "size": derived, "ref": row.reference},
            )
            self._delete_job(session, job.identity)
        return prepared

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
        cancel = cancel_event if cancel_event is not None else threading.Event()
        if cancel.is_set():
            raise _error("ai_attachment_cancelled")
        try:
            filename = attachments.sanitize_filename(attachment.filename)
            _, content_type = attachments.classify(filename, attachment.content_type)
            data = attachments.decode_data(attachment.data_base64)
            attachments._sniff(content_type, data)
        except attachments.AttachmentError as error:
            raise _error(error.code) from None
        scope = _Scope.create(owner, adapter_id, scope_id)
        reference, identity = self._reserve(scope, data)
        with self._pin(identity, exclusive=True) as root:
            with db.SessionLocal() as session:
                allowed = session.scalar(
                    text("""
                    SELECT state='allocating' AND expires_at > pg_catalog.clock_timestamp()
                    FROM control_attachment_objects WHERE reference=:ref
                """),
                    {"ref": reference},
                )
            if not allowed or cancel.is_set():
                raise _error("ai_attachment_cancelled")
            with open_directory(root, "o-" + identity.hex, create=True) as directory:
                _write(directory, "original", data)
                publish_record(
                    directory, "source.json", {"filename": filename, "content_type": content_type}
                )
            with self._catalog() as session:
                updated = session.scalar(
                    text("""
                    UPDATE control_attachment_objects SET state='pending'
                    WHERE reference=:ref AND state='allocating'
                      AND expires_at > pg_catalog.clock_timestamp()
                    RETURNING 1
                """),
                    {"ref": reference},
                )
                if updated != 1:
                    raise _error("ai_attachment_cancelled")
        row = self._lookup(scope, reference)
        with self._pin(row.object_id) as root:
            return self._parse(scope, row, root, deadline, cancel)

    def _prepared(self, root: int, row: RowMapping) -> AiPreparedAttachment:
        with (
            open_directory(root, "o-" + row.object_id.hex) as directory,
            open_directory(directory, "g-" + row.result_id.hex) as result,
        ):
            prepared = AiPreparedAttachment.model_validate_json(
                _read(result, "prepared.json", MAX_METADATA_BYTES)
            )
        if (prepared.attachment_ref, prepared.sha256, prepared.size_bytes, prepared.expires_at) != (
            row.reference,
            row.sha256,
            row.size_bytes,
            row.expires_at,
        ):
            raise _error("ai_attachment_hash_mismatch")
        return prepared

    def get(
        self, owner: Owner, adapter_id: int, scope_id: UUID | str, reference: str
    ) -> AiPreparedAttachment:
        scope = _Scope.create(owner, adapter_id, scope_id)
        row = self._lookup(scope, reference, ready=True)
        with self._pin(row.object_id) as root:
            return self._prepared(root, self._lookup(scope, reference, ready=True))

    def release(
        self, owner: Owner, adapter_id: int, scope_id: UUID | str, reference: str | None = None
    ) -> None:
        scope = _Scope.create(owner, adapter_id, scope_id)
        params = {
            **self._params(scope, reference or ""),
            "close_scope": reference is None,
            "ttl": settings.ai_attachment_ttl_seconds,
        }
        with self._catalog() as session:
            if reference is None:
                existing, principal = session.execute(
                    text("""
                    SELECT count(*), count(*) FILTER (WHERE principal_key=:principal)
                    FROM control_attachment_scopes WHERE namespace_id=:namespace
                """),
                    params,
                ).one()
                known = session.scalar(
                    text("""
                    SELECT 1 FROM control_attachment_scopes WHERE namespace_id=:namespace
                      AND principal_key=:principal AND adapter_id=:adapter AND scope_id=:scope
                """),
                    params,
                )
                if known is None and (
                    existing >= MAX_TRACKED_SCOPES or principal >= MAX_SCOPES_PER_OWNER
                ):
                    raise _error("ai_attachment_store_full")
                session.execute(
                    text("""
                    INSERT INTO control_attachment_scopes
                        (namespace_id,principal_key,adapter_id,scope_id,expires_at,closed)
                    VALUES (:namespace,:principal,:adapter,:scope,
                            pg_catalog.clock_timestamp()+make_interval(secs=>:ttl),true)
                    ON CONFLICT (namespace_id,principal_key,adapter_id,scope_id)
                    DO UPDATE SET closed=true, expires_at=greatest(
                        control_attachment_scopes.expires_at,excluded.expires_at)
                """),
                    params,
                )
            jobs = session.execute(
                text("""
                WITH closed AS (
                    UPDATE control_attachment_objects SET state='closed'
                    WHERE namespace_id=:namespace AND principal_key=:principal
                      AND adapter_id=:adapter
                      AND scope_id=:scope AND (:close_scope OR reference=:ref) RETURNING reference
                )
                SELECT job_id,converter_id FROM control_attachment_jobs JOIN closed USING(reference)
            """),
                params,
            ).all()
        for job in jobs:
            self._converter(job.converter_id).cancel(job.job_id.hex)

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
        scope = _Scope.create(owner, adapter_id, scope_id)
        pinned: dict[str, tuple[int, AiAttachmentReference]] = {}
        total = inline_bytes
        with ExitStack() as stack:
            for reference in references:
                ref = reference.attachment_ref
                if ref in pinned:
                    raise _error("ai_attachment_reference_invalid")
                row = self._lookup(scope, ref, ready=True)
                root = stack.enter_context(self._pin(row.object_id))
                prepared = self._prepared(root, self._lookup(scope, ref, ready=True))
                if (prepared.sha256, prepared.representation_revision) != (
                    reference.sha256,
                    reference.representation_revision,
                ):
                    raise _error("ai_attachment_hash_mismatch")
                pinned[ref] = root, reference
                total += prepared.size_bytes
            if total > attachments.MAX_TOTAL_BYTES:
                raise _error("ai_attachment_total_too_large")
            yield _SharedReader(self, scope, pinned, permission_check)

    def sweep(self) -> None:
        with db.SessionLocal() as session:
            page = storage_maintenance.claim(session, self.namespace)
            if page is None:
                return
            next_key = ""
            if page.shard == 0:
                keys = self._reconcile_jobs(session, page)
                if len(keys) == 4:
                    next_key = keys[-1]
            elif page.shard == 1:
                keys = self._reclaim_objects(session, page)
                if len(keys) == _PAGE_SIZE:
                    next_key = keys[-1]
            else:
                next_key = self._resume_parse(session, page)
            storage_maintenance.finish(
                session,
                page,
                shard=page.shard if next_key else (page.shard + 1) % 3,
                after_key=next_key,
            )

    def _reconcile_jobs(self, session: Session, page: storage_maintenance.Page) -> list[str]:
        params = {
            "namespace": self.namespace.namespace_id,
            "after": UUID(page.after_key or "0" * 32),
        }
        rows = session.execute(
            text("""
            SELECT j.job_id,j.converter_id,o.object_id FROM control_attachment_jobs j
            JOIN control_attachment_objects o USING(reference)
            WHERE o.namespace_id=:namespace AND j.job_id > :after
              AND (o.state='closed' OR o.expires_at <= pg_catalog.clock_timestamp()
                   OR j.lease_until <= pg_catalog.clock_timestamp())
            ORDER BY j.job_id LIMIT 4
        """),
            params,
        ).all()
        session.rollback()
        for row in rows:
            if not storage_maintenance.current(session, page):
                break
            session.rollback()
            with (
                suppress(OSError, DocumentConversionError),
                self._pin(row.job_id, exclusive=True, job=True),
            ):
                self._finish_failed_job(row.object_id, _Job(row.job_id, row.converter_id, None))
        return [str(row.job_id) for row in rows]

    def _reclaim_objects(self, session: Session, page: storage_maintenance.Page) -> list[str]:
        params = {
            "namespace": self.namespace.namespace_id,
            "limit": _PAGE_SIZE,
            "after": page.after_key,
        }
        rows = session.execute(
            text("""
            SELECT reference,object_id FROM control_attachment_objects
            WHERE namespace_id=:namespace AND reference > :after
              AND (state='closed' OR expires_at <= pg_catalog.clock_timestamp())
            ORDER BY reference LIMIT :limit
        """),
            params,
        ).all()
        session.rollback()
        for row in rows:
            if not storage_maintenance.current(session, page):
                break
            session.rollback()
            with suppress(OSError, DocumentConversionError):
                self._expire(row.reference, row.object_id)
        with self._catalog() as catalog:
            catalog.execute(
                text("""
                DELETE FROM control_attachment_scopes s
                WHERE (namespace_id,principal_key,adapter_id,scope_id) IN (
                  SELECT namespace_id,principal_key,adapter_id,scope_id
                  FROM control_attachment_scopes x WHERE x.namespace_id=:namespace
                    AND x.expires_at <= pg_catalog.clock_timestamp() AND NOT EXISTS (
                      SELECT 1 FROM control_attachment_objects o WHERE
                        (o.namespace_id,o.principal_key,o.adapter_id,o.scope_id) =
                        (x.namespace_id,x.principal_key,x.adapter_id,x.scope_id))
                  LIMIT :limit
                )
            """),
                params,
            )
        return [str(row.reference) for row in rows]

    def _resume_parse(self, session: Session, page: storage_maintenance.Page) -> str:
        candidate = (
            session.execute(
                text("""
            SELECT o.* FROM control_attachment_objects o JOIN control_attachment_scopes s
            USING (namespace_id,principal_key,adapter_id,scope_id)
            WHERE namespace_id=:namespace AND o.reference > :after
              AND o.state='pending' AND NOT s.closed
              AND (o.parse_generation > 0 OR o.resume_after <= pg_catalog.clock_timestamp())
              AND o.expires_at > pg_catalog.clock_timestamp() AND NOT EXISTS (
                SELECT 1 FROM control_attachment_jobs j WHERE j.reference=o.reference
                  AND j.parse_generation=o.parse_generation
                  AND j.lease_until > pg_catalog.clock_timestamp())
            ORDER BY o.reference LIMIT 1
        """),
                {"namespace": self.namespace.namespace_id, "after": page.after_key},
            )
            .mappings()
            .one_or_none()
        )
        session.rollback()
        if candidate is None:
            return ""
        scope = _Scope(candidate.principal_key, candidate.adapter_id, candidate.scope_id)
        # Commit scan progress before waiting for converter I/O.
        storage_maintenance.finish(session, page, shard=2, after_key=candidate.reference)
        with suppress(OSError, DocumentConversionError), self._pin(candidate.object_id) as root:
            self._parse(
                scope,
                candidate,
                root,
                time.monotonic() + settings.ai_attachment_prepare_timeout_seconds,
                threading.Event(),
            )
        return str(candidate.reference)

    def _expire(self, reference: str, identity: UUID) -> None:
        with self._pin(identity, exclusive=True) as root:
            with self._catalog() as session:
                eligible = session.scalar(
                    text("""
                    UPDATE control_attachment_objects o SET state='closed'
                    WHERE reference=:ref AND object_id=:id AND namespace_id=:namespace
                      AND (state='closed' OR expires_at <= pg_catalog.clock_timestamp())
                      AND NOT EXISTS (SELECT 1 FROM control_attachment_jobs j
                                      WHERE j.reference=o.reference)
                    RETURNING 1
                """),
                    {"ref": reference, "id": identity, "namespace": self.namespace.namespace_id},
                )
            if eligible != 1:
                return
            with suppress(FileNotFoundError):
                shutil.rmtree("o-" + identity.hex, dir_fd=root)
            os.fsync(root)
            with self._catalog() as session:
                session.execute(
                    text("""
                    DELETE FROM control_attachment_objects
                    WHERE reference=:ref AND object_id=:id AND state='closed'
                """),
                    {"ref": reference, "id": identity},
                )


class _SharedReader:
    def __init__(
        self,
        store: SharedAttachmentStore,
        scope: _Scope,
        pinned: dict[str, tuple[int, AiAttachmentReference]],
        permission: Callable[[], object] | None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.pinned = pinned
        self.permission = permission

    def _current(self, reference: str) -> tuple[int, RowMapping, AiPreparedAttachment]:
        if self.permission is not None:
            self.permission()
        selected = self.pinned.get(reference)
        if selected is None:
            raise _error("ai_attachment_reference_invalid")
        root, frozen = selected
        row = self.store._lookup(self.scope, reference, ready=True)
        prepared = self.store._prepared(root, row)
        if (prepared.sha256, prepared.representation_revision) != (
            frozen.sha256,
            frozen.representation_revision,
        ):
            raise _error("ai_attachment_hash_mismatch")
        return root, row, prepared

    def overview(self) -> dict[str, object]:
        return {
            "attachments": [self._current(ref)[2].model_dump(mode="json") for ref in self.pinned],
            "coverage": {"directory_only": True, "content_requires_read": True},
        }

    def read_page(
        self, attachment_ref: str, page: int, view: str, *, timeout_seconds: float
    ) -> AttachmentReadResult:
        if type(page) is not int or view not in {"text", "image", "both", "pdf"}:
            raise _error("ai_attachment_range_invalid")
        return self._read(attachment_ref, timeout_seconds, page=page, view=view)

    def read_sheet(
        self, attachment_ref: str, sheet: str, range_a1: str, *, timeout_seconds: float
    ) -> AttachmentReadResult:
        validate_sheet_range(range_a1)
        return self._read(attachment_ref, timeout_seconds, sheet=sheet, range_a1=range_a1)

    def _read(
        self,
        ref: str,
        timeout_seconds: float,
        *,
        page: int | None = None,
        view: str = "both",
        sheet: str | None = None,
        range_a1: str | None = None,
    ) -> AttachmentReadResult:
        if timeout_seconds <= 0:
            raise _error("ai_attachment_parse_timeout")
        deadline = time.monotonic() + min(60, timeout_seconds)
        root, row, prepared = self._current(ref)
        if (page is not None and not 1 <= page <= prepared.page_count) or (
            sheet is not None and not any(item.get("name") == sheet for item in prepared.sheets)
        ):
            raise _error("ai_attachment_range_invalid")
        pdf = None
        with open_directory(root, "o-" + row.object_id.hex) as directory:
            data = _read(directory, "original", row.size_bytes, sha256=row.sha256)
            if page is not None:
                with (
                    open_directory(directory, "g-" + row.result_id.hex) as derivative,
                    suppress(FileNotFoundError),
                ):
                    pdf = _read(
                        derivative, "rendered-pdf", settings.ai_attachment_derived_max_bytes
                    )
        job = self.store._begin_job(self.scope, ref, "read", deadline)
        with self.store._pin(job.identity, exclusive=True, job=True):
            client = self.store._converter(job.converter_id)
            try:
                with db.SessionLocal() as session:
                    self.store._verify_job(session, self.scope, ref, job)
                position: dict[str, str | int]
                if page is not None:
                    result = client.read_page(
                        prepared.filename,
                        prepared.content_type,
                        data,
                        page=page,
                        view=view,
                        rendered_pdf=pdf,
                        job_id=job.identity.hex,
                        deadline=deadline,
                        cancel=None,
                    )
                    position = {"page": page}
                else:
                    assert sheet is not None and range_a1 is not None
                    result = client.read_sheet(
                        prepared.filename,
                        prepared.content_type,
                        data,
                        sheet=sheet,
                        range_a1=range_a1,
                        job_id=job.identity.hex,
                        deadline=deadline,
                        cancel=None,
                    )
                    position = {"sheet": sheet, "range": range_a1}
                client.cancel(job.identity.hex)
                self._current(ref)
                if result.status == "failure":
                    raise _error("ai_attachment_parse_failed")
                if (result.source_sha256, result.representation_revision) != (
                    prepared.sha256,
                    prepared.representation_revision,
                ):
                    raise _error("ai_attachment_hash_mismatch")
                payload = _bounded_metadata(
                    {
                        **result.data,
                        **position,
                        "attachment_ref": ref,
                        "source_sha256": prepared.sha256,
                        "representation_revision": prepared.representation_revision,
                        "coverage": result.coverage,
                        "warnings": result.warnings,
                    }
                )
                if (
                    sum(asset.size_bytes for asset in result.assets)
                    > settings.ai_attachment_derived_max_bytes
                ):
                    raise _error("ai_attachment_too_large")
                answer = AttachmentReadResult(
                    data=cast(dict[str, object], payload),
                    media=tuple(_media(ref, asset, page) for asset in result.assets),
                )
                with self.store._catalog() as session:
                    self.store._verify_job(session, self.scope, ref, job)
                    self.store._delete_job(session, job.identity)
                return answer
            except Exception:
                self.store._finish_failed_job(row.object_id, job)
                raise
