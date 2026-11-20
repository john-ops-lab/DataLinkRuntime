"""Offline, resumable file copy and generation-checked namespace selection."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dlr.common.config import settings
from dlr.common.schema_gate import acquire_migration_gate
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control.services import control_image_transition_v4 as transition
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.control_declaration_v4 import read_declaration
from dlr.control.services.shared_posix import (
    PURPOSES,
    SharedNamespace,
    SharedStorageUnavailable,
    open_directory,
    open_regular,
    open_root,
)
from dlr.control.services.storage_backends import policy_sha256

_PAGE_SIZE = 64
_CHUNK_SIZE = 256 * 1024
_MANAGED = """
    SELECT storage_key AS key, size_bytes AS size, sha256, false AS pending, false AS optional
    FROM managed_input_artifacts WHERE status NOT IN ('UPLOADING', 'DELETED')
    UNION ALL
    SELECT a.storage_key, r.reserved_bytes, NULL, true, true FROM managed_input_artifacts a
    JOIN managed_input_upload_reservations r ON r.id=a.upload_reservation_id
    WHERE a.status='UPLOADING' AND r.status='ACTIVE'
    UNION ALL
    SELECT storage_key, charged_bytes, NULL, false, true FROM artifact_deletion_jobs
    WHERE status <> 'DELETED'
"""
_BUILTIN = """
    SELECT storage_key AS key, size_bytes AS size, sha256, false AS pending, false AS optional
    FROM builtin_packages
    UNION ALL
    SELECT id, size_bytes, NULL, true, true FROM builtin_package_uploads
"""


def _owner(session: Session) -> UUID:
    acquire_migration_gate(session)
    transition._owner_fence(session, settings)
    deployment = read_declaration(settings)
    row = (
        session.execute(text("SELECT * FROM control_cluster_authority WHERE singleton=1 FOR SHARE"))
        .mappings()
        .one()
    )
    if not transition._authority_matches(row, deployment, SCHEMA_REVISION):
        raise SharedStorageUnavailable("Storage migration authority unavailable")
    return deployment.authority.cluster_id


@contextmanager
def _entry(
    root: int, managed: bool, key: str, part: bool, *, create: bool
) -> Iterator[tuple[int, str]]:
    if not managed:
        if str(UUID(key)) != key:
            raise SharedStorageUnavailable("Invalid library identity")
        yield root, key + (".part" if part else ".blob")
        return
    LocalFileArtifactStore.validate_storage_key(key)
    with (
        open_directory(root, "parts" if part else "objects", create=create) as directory,
        open_directory(directory, key[:2], create=create) as prefix,
    ):
        yield prefix, key + (".part" if part else "")


def _legacy_file(directory: int, name: str) -> BinaryIO:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
    )
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
        os.close(descriptor)
        raise SharedStorageUnavailable("Invalid source file")
    return os.fdopen(descriptor, "rb")


def _read_content(source: BinaryIO, limit: int, target: BinaryIO | None = None) -> tuple[int, str]:
    before = os.fstat(source.fileno())
    digest = hashlib.sha256()
    total = 0
    while data := source.read(min(_CHUNK_SIZE, limit - total + 1)):
        total += len(data)
        if total > limit:
            raise SharedStorageUnavailable("Unexpected source size")
        digest.update(data)
        if target is not None:
            target.write(data)
    after = os.fstat(source.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or total != after.st_size:
        raise SharedStorageUnavailable("Source changed during copy")
    return total, digest.hexdigest()


def _copy_one(
    source: BinaryIO, target: int, name: str, size: int, sha256: str | None
) -> tuple[int, str]:
    def validate(facts: tuple[int, str]) -> tuple[int, str]:
        if sha256 is not None and facts != (size, sha256):
            raise SharedStorageUnavailable("Frozen file did not match its manifest")
        return facts

    try:
        previous = open_regular(target, name, os.O_RDONLY)
    except FileNotFoundError:
        temporary = f".copy-{uuid4().hex}.part"
        try:
            with os.fdopen(
                open_regular(target, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
            ) as destination:
                facts = validate(_read_content(source, size, destination))
                destination.flush()
                os.fsync(destination.fileno())
            os.rename(temporary, name, src_dir_fd=target, dst_dir_fd=target)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=target)
    else:
        with os.fdopen(previous, "rb") as existing:
            facts = validate(_read_content(source, size))
            if _read_content(existing, size) != facts:
                raise SharedStorageUnavailable("Existing destination differs")
            os.fsync(existing.fileno())
    os.fsync(target)
    return facts


def _copy_catalog(
    factory: sessionmaker[Session],
    source: int,
    target: int,
    *,
    managed: bool,
    manifest: BinaryIO | None,
) -> tuple[str, int]:
    checksum = hashlib.sha256()
    copied = 0
    after = ""
    query = _MANAGED if managed else _BUILTIN

    def record(entry: list[Any]) -> None:
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        checksum.update(line)
        if manifest is not None:
            manifest.write(line)

    while True:
        with factory() as session:
            _owner(session)
            rows = session.execute(
                text(f"SELECT * FROM ({query}) r WHERE key > :after ORDER BY key LIMIT :limit"),
                {"after": after, "limit": _PAGE_SIZE},
            ).all()
        if not rows:
            return checksum.hexdigest(), copied
        for row in rows:
            if row.key <= after:
                raise SharedStorageUnavailable("Duplicate file responsibility")
            after = row.key
            if not row.pending and not row.optional and row.sha256 is None:
                raise SharedStorageUnavailable("Missing frozen metadata")
            for part in (False, True) if row.pending else (False,):
                with ExitStack() as entries:
                    try:
                        origin, source_name = entries.enter_context(
                            _entry(source, managed, row.key, part, create=False)
                        )
                        stream = entries.enter_context(_legacy_file(origin, source_name))
                    except FileNotFoundError:
                        if not row.optional:
                            raise SharedStorageUnavailable("Missing a referenced object") from None
                        record([row.key, part, None])
                        continue
                    destination, target_name = entries.enter_context(
                        _entry(target, managed, row.key, part, create=True)
                    )
                    facts = _copy_one(stream, destination, target_name, row.size, row.sha256)
                    record([row.key, part, *facts])
                    copied += 1
        if manifest is not None:
            manifest.flush()
            os.fsync(manifest.fileno())


def migrate(
    factory: sessionmaker[Session],
    namespace: SharedNamespace,
    origin: Path | None,
    expected: int,
    *,
    manifest: BinaryIO | None = None,
) -> dict[str, str | int]:
    slot = settings.control_ha_slot
    if slot is None or expected < 0:
        raise SharedStorageUnavailable("Storage migration requires a Control slot")
    with factory() as session:
        if _owner(session) != namespace.deployment_id:
            raise SharedStorageUnavailable("Wrong deployment")
        prior = session.execute(
            text(
                "SELECT namespace_id,generation FROM control_storage_bindings "
                "WHERE purpose=:purpose"
            ),
            {"purpose": namespace.purpose},
        ).one_or_none()
    if expected != (prior.generation if prior is not None else 0):
        raise SharedStorageUnavailable("Storage generation changed")
    if prior is not None and prior.namespace_id == namespace.namespace_id:
        raise SharedStorageUnavailable("Destination is already authoritative")
    with ExitStack() as stack:
        target = stack.enter_context(namespace.open(slot=slot))
        lock = open_regular(target, ".dlr-migration.lock", os.O_RDWR | os.O_CREAT)
        stack.callback(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        count = 0
        if namespace.purpose == "ai_attachments":
            if origin is not None:
                raise SharedStorageUnavailable("Temporary content is never copied")
            checksum = hashlib.sha256(b"temporary references invalidated").hexdigest()
        else:
            if (
                origin is None
                or origin.is_relative_to(namespace.root)
                or namespace.root.is_relative_to(origin)
            ):
                raise SharedStorageUnavailable("Source and destination must be independent")
            source = stack.enter_context(
                open_root(origin, private=False)
                if prior is None
                else SharedNamespace(
                    origin,
                    namespace.purpose,
                    namespace.deployment_id,
                    prior.namespace_id,
                ).open(slot=slot)
            )
            if os.path.samestat(os.fstat(source), os.fstat(target)):
                raise SharedStorageUnavailable("Source and destination identify the same directory")
            checksum, count = _copy_catalog(
                factory,
                source,
                target,
                managed=namespace.purpose == "managed_files",
                manifest=manifest,
            )
        with factory.begin() as session:
            if _owner(session) != namespace.deployment_id:
                raise SharedStorageUnavailable("Wrong deployment")
            params = {
                "purpose": namespace.purpose,
                "deployment": namespace.deployment_id,
                "namespace": namespace.namespace_id,
                "generation": expected,
                "checksum": checksum,
                "policy": policy_sha256(namespace.purpose),
                "previous": prior.namespace_id if prior is not None else None,
            }
            changed = session.execute(
                text("""
                INSERT INTO control_storage_bindings
                    (purpose,deployment_id,namespace_id,previous_namespace_id,
                     generation,state,manifest_sha256,policy_sha256)
                VALUES (:purpose,:deployment,:namespace,:previous,
                        :generation+1,'active',:checksum,:policy)
                ON CONFLICT (purpose) DO UPDATE SET
                    namespace_id=excluded.namespace_id,
                    previous_namespace_id=excluded.previous_namespace_id,
                    generation=excluded.generation, state=excluded.state,
                    manifest_sha256=excluded.manifest_sha256, policy_sha256=excluded.policy_sha256
                WHERE control_storage_bindings.generation=:generation
                    AND control_storage_bindings.deployment_id=:deployment
                    AND control_storage_bindings.namespace_id=:previous
                RETURNING generation
            """),
                params,
            ).scalar_one_or_none()
            if changed != expected + 1:
                raise SharedStorageUnavailable("Storage selection raced")
            session.execute(
                text("""
                UPDATE control_storage_maintenance
                SET shard=0, after_key='', owner=NULL, lease_until=NULL
                WHERE purpose=:purpose
            """),
                params,
            )
    return {
        "status": "selected",
        "generation": expected + 1,
        "verified_objects": count,
        "manifest_sha256": checksum,
    }


def observe(factory: sessionmaker[Session], namespace: SharedNamespace) -> dict[str, str | int]:
    with factory() as session:
        if _owner(session) != namespace.deployment_id:
            raise SharedStorageUnavailable("Storage migration authority unavailable")
        row = session.execute(
            text("""
            SELECT generation,manifest_sha256 FROM control_storage_bindings
            WHERE purpose=:purpose AND deployment_id=:deployment
              AND namespace_id=:namespace AND state='active'
        """),
            {
                "purpose": namespace.purpose,
                "deployment": namespace.deployment_id,
                "namespace": namespace.namespace_id,
            },
        ).one_or_none()
    if row is None:
        return {"status": "not_selected"}
    return {
        "status": "selected",
        "generation": row.generation,
        "manifest_sha256": row.manifest_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("purpose", choices=PURPOSES)
    parser.add_argument("destination", type=Path)
    parser.add_argument("namespace", type=UUID)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--manifest", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--expected-generation", type=int)
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--revoke-temporary", action="store_true")
    args = parser.parse_args()
    engine = create_engine(settings.database_url, poolclass=NullPool)
    try:
        declaration = read_declaration(settings)
        backend = SharedNamespace(
            args.destination, args.purpose, declaration.authority.cluster_id, args.namespace
        )
        factory = sessionmaker(engine)
        with ExitStack() as files:
            if args.observe:
                result = observe(factory, backend)
            elif args.revoke_temporary:
                with factory() as session:
                    if _owner(session) != backend.deployment_id:
                        raise SharedStorageUnavailable("Storage migration authority unavailable")
                backend.revoke_temporary()
                result = {"status": "revoked"}
            else:
                manifest = None
                if args.manifest is not None:
                    fd = os.open(
                        args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    manifest = files.enter_context(os.fdopen(fd, "wb"))
                result = migrate(
                    factory, backend, args.source, args.expected_generation, manifest=manifest
                )
        print(json.dumps(result))
    except Exception:
        print(json.dumps({"status": "arbitration_required_or_unavailable"}))
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
