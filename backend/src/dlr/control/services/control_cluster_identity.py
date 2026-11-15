"""Control authority/lease primitives used by optional process diagnostics.

Trusted deployment declarations and explicit local initialization are required.
Matching fingerprints or an unexpired lease are not evidence of physical host
isolation, shared storage, stopped work, capacity, or permission to serve.
There is no implicit bootstrap or authority update; callers own commit/rollback.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from dlr.common.schema_gate import try_control_gate
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control.services import control_auth_proof

LEASE_SECONDS = 30
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


class _RedactedBytes(bytes):
    """Avoid logging private comparator bytes in ORM SQL diagnostics."""

    def __repr__(self) -> str:
        return "<redacted>"


class ControlIdentityUnavailable(Exception):
    """Generic, non-sensitive authority or lease failure."""


@dataclass(frozen=True)
class ControlDeclaration:
    cluster_id: UUID
    image_digest: str = field(repr=False)
    managed_fingerprint: str = field(repr=False)
    builtin_fingerprint: str = field(repr=False)
    attachment_fingerprint: str = field(repr=False)
    capacity_fingerprint: str = field(repr=False)
    master_key: str = field(repr=False)
    admin_token: str = field(repr=False)
    worker_token: str = field(repr=False)
    worker_proof_public_key: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DeclaredCompatibility:
    """Comparisons of declarations only; none grants a runtime capability."""

    instance_id: UUID
    lease_until: datetime
    managed_match: bool
    builtin_match: bool
    attachment_match: bool
    capacity_match: bool


@dataclass(frozen=True)
class ExpiredControl:
    """The caller must commit this transaction to make expiry a durable fence."""

    instance_id: UUID


def _record_expired(session: Session, instance_id: UUID) -> ExpiredControl:
    session.execute(
        text("UPDATE control_process_instances SET revoked = true WHERE instance_id = :id"),
        {"id": instance_id},
    )
    return ExpiredControl(instance_id)


def _invalid() -> ControlIdentityUnavailable:
    return ControlIdentityUnavailable("control identity unavailable")


def _validated(declaration: ControlDeclaration) -> control_auth_proof.ControlAuthProof:
    if (
        type(declaration) is not ControlDeclaration
        or not isinstance(declaration.cluster_id, UUID)
        or not isinstance(declaration.image_digest, str)
        or _DIGEST.fullmatch(declaration.image_digest) is None
        or any(
            not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None
            for value in (
                declaration.managed_fingerprint,
                declaration.builtin_fingerprint,
                declaration.attachment_fingerprint,
                declaration.capacity_fingerprint,
            )
        )
        or (
            declaration.worker_proof_public_key is not None
            and (
                type(declaration.worker_proof_public_key) is not bytes
                or len(declaration.worker_proof_public_key) != 32
            )
        )
    ):
        raise _invalid()
    try:
        return control_auth_proof.create_auth_proof(
            cluster_id=declaration.cluster_id,
            master_key=declaration.master_key,
            admin_token=declaration.admin_token,
            worker_token=declaration.worker_token,
        )
    except control_auth_proof.ControlAuthUnavailable:
        raise _invalid() from None


def _current_schema(session: Session) -> None:
    try_control_gate(session)
    versions = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if versions != [SCHEMA_REVISION]:
        raise _invalid()


def provision_authority(
    session: Session, declaration: ControlDeclaration, *, authority_epoch: UUID
) -> None:
    """One explicit, caller-authorized first write; never replace an authority.

    The local initialization CLI is the explicit caller. Deployment must
    first freeze trusted image, auth and independent namespace/capacity inputs;
    it must stop writes and define a separate audited update operation.
    """
    proof = _validated(declaration)
    if not isinstance(authority_epoch, UUID):
        raise _invalid()
    try:
        _current_schema(session)
        session.execute(
            text("""
                INSERT INTO control_cluster_authority
                  (singleton, cluster_id, authority_epoch, schema_revision, image_digest,
                   master_check, admin_check, worker_check, worker_proof_public_key,
                   managed_fingerprint, builtin_fingerprint, attachment_fingerprint,
                   capacity_fingerprint)
                VALUES (1, :cluster_id, :authority_epoch, :schema_revision, :image_digest,
                        :master_check, :admin_check, :worker_check, :worker_proof_public_key,
                        :managed_fingerprint, :builtin_fingerprint, :attachment_fingerprint,
                        :capacity_fingerprint)
                """),
            {
                "cluster_id": declaration.cluster_id,
                "authority_epoch": authority_epoch,
                "schema_revision": SCHEMA_REVISION,
                "image_digest": declaration.image_digest,
                "master_check": _RedactedBytes(proof.master_check),
                "admin_check": _RedactedBytes(proof.admin_check),
                "worker_check": _RedactedBytes(proof.worker_check),
                "worker_proof_public_key": declaration.worker_proof_public_key,
                "managed_fingerprint": declaration.managed_fingerprint,
                "builtin_fingerprint": declaration.builtin_fingerprint,
                "attachment_fingerprint": declaration.attachment_fingerprint,
                "capacity_fingerprint": declaration.capacity_fingerprint,
            },
        )
    except Exception:
        raise _invalid() from None


def _authority(
    session: Session, declaration: ControlDeclaration, *, exclusive: bool = False
) -> RowMapping:
    _validated(declaration)
    _current_schema(session)
    authority = (
        session.execute(
            text(
                "SELECT * FROM control_cluster_authority WHERE singleton = 1 "
                + ("FOR UPDATE" if exclusive else "FOR SHARE")
            )
        )
        .mappings()
        .one_or_none()
    )
    if authority is None or (
        authority["cluster_id"] != declaration.cluster_id
        or authority["schema_revision"] != SCHEMA_REVISION
        or not hmac.compare_digest(authority["image_digest"], declaration.image_digest)
    ):
        raise _invalid()
    stored_key = authority["worker_proof_public_key"]
    declared_key = declaration.worker_proof_public_key
    if (stored_key is None) != (declared_key is None) or (
        stored_key is not None
        and declared_key is not None
        and not hmac.compare_digest(stored_key, declared_key)
    ):
        raise _invalid()
    proof = control_auth_proof.ControlAuthProof(
        authority["master_check"], authority["admin_check"], authority["worker_check"]
    )
    if not control_auth_proof.verify_auth_proof(
        proof,
        cluster_id=declaration.cluster_id,
        master_key=declaration.master_key,
        admin_token=declaration.admin_token,
        worker_token=declaration.worker_token,
    ):
        raise _invalid()
    return authority


def _matches(
    authority: RowMapping, instance_id: UUID, expires: datetime, declaration: ControlDeclaration
) -> DeclaredCompatibility:
    return DeclaredCompatibility(
        instance_id=instance_id,
        lease_until=expires,
        managed_match=hmac.compare_digest(
            authority["managed_fingerprint"], declaration.managed_fingerprint
        ),
        builtin_match=hmac.compare_digest(
            authority["builtin_fingerprint"], declaration.builtin_fingerprint
        ),
        attachment_match=hmac.compare_digest(
            authority["attachment_fingerprint"], declaration.attachment_fingerprint
        ),
        capacity_match=hmac.compare_digest(
            authority["capacity_fingerprint"], declaration.capacity_fingerprint
        ),
    )


def _identity_parameters(declaration: ControlDeclaration, instance_id: UUID) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "image_digest": declaration.image_digest,
        "managed_fingerprint": declaration.managed_fingerprint,
        "builtin_fingerprint": declaration.builtin_fingerprint,
        "attachment_fingerprint": declaration.attachment_fingerprint,
        "capacity_fingerprint": declaration.capacity_fingerprint,
    }


def new_instance_token() -> str:
    """Generate an in-memory process capability; never place it in logs/argv."""
    return secrets.token_hex(32)


def _token_digest(token: str) -> bytes:
    if not isinstance(token, str) or _FINGERPRINT.fullmatch(token) is None:
        raise _invalid()
    return hashlib.sha256(bytes.fromhex(token)).digest()


def _existing_is_same(
    row: RowMapping, declaration: ControlDeclaration, authority: RowMapping, token_digest: bytes
) -> bool:
    return bool(
        row["cluster_id"] == declaration.cluster_id
        and row["authority_epoch"] == authority["authority_epoch"]
        and hmac.compare_digest(row["instance_token_digest"], token_digest)
        and all(
            hmac.compare_digest(row[name], getattr(declaration, name))
            for name in (
                "image_digest",
                "managed_fingerprint",
                "builtin_fingerprint",
                "attachment_fingerprint",
                "capacity_fingerprint",
            )
        )
    )


def register_control(
    session: Session,
    declaration: ControlDeclaration,
    *,
    instance_id: UUID,
    instance_token: str,
    single_process: bool = False,
) -> DeclaredCompatibility | ExpiredControl:
    """Register once; retry does not renew and observed expiry must be committed."""
    if not isinstance(instance_id, UUID):
        raise _invalid()
    token_digest = _token_digest(instance_token)
    try:
        # Serialize dedicated-stage admission with registration and renewal.
        # Take the authority lock first, matching every existing lease path.
        authority = _authority(session, declaration, exclusive=single_process)
        if single_process and session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM control_process_instances "
                "WHERE instance_id <> :id AND NOT revoked "
                "AND lease_until > pg_catalog.clock_timestamp())"
            ),
            {"id": instance_id},
        ):
            raise _invalid()
        row = (
            session.execute(
                text("SELECT * FROM control_process_instances WHERE instance_id = :id FOR UPDATE"),
                {"id": instance_id},
            )
            .mappings()
            .one_or_none()
        )
        now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if row is not None:
            if row["revoked"] or not _existing_is_same(row, declaration, authority, token_digest):
                raise _invalid()
            if row["lease_until"] <= now:
                return _record_expired(session, instance_id)
            return _matches(authority, instance_id, row["lease_until"], declaration)
        expires = now + timedelta(seconds=LEASE_SECONDS)
        session.execute(
            text("""
                INSERT INTO control_process_instances
                  (instance_id, cluster_id, authority_epoch, instance_token_digest, image_digest,
                   managed_fingerprint, builtin_fingerprint, attachment_fingerprint,
                   capacity_fingerprint,
                   lease_until, created_at)
                VALUES (:instance_id, :cluster_id, :authority_epoch, :instance_token_digest,
                        :image_digest, :managed_fingerprint, :builtin_fingerprint,
                        :attachment_fingerprint,
                        :capacity_fingerprint, :lease_until, :created_at)
                """),
            {
                **_identity_parameters(declaration, instance_id),
                "cluster_id": declaration.cluster_id,
                "authority_epoch": authority["authority_epoch"],
                "instance_token_digest": _RedactedBytes(token_digest),
                "lease_until": expires,
                "created_at": now,
            },
        )
        return _matches(authority, instance_id, expires, declaration)
    except Exception:
        raise _invalid() from None


def _update_control(
    session: Session,
    declaration: ControlDeclaration,
    instance_id: UUID,
    instance_token: str,
    *,
    revoke: bool,
) -> DeclaredCompatibility | ExpiredControl:
    if not isinstance(instance_id, UUID):
        raise _invalid()
    token_digest = _token_digest(instance_token)
    try:
        authority = _authority(session, declaration)
        row = (
            session.execute(
                text("SELECT * FROM control_process_instances WHERE instance_id = :id FOR UPDATE"),
                {"id": instance_id},
            )
            .mappings()
            .one_or_none()
        )
        now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if (
            row is None
            or row["revoked"]
            or not _existing_is_same(row, declaration, authority, token_digest)
        ):
            raise _invalid()
        if row["lease_until"] <= now:
            return _record_expired(session, instance_id)
        expires = now if revoke else now + timedelta(seconds=LEASE_SECONDS)
        session.execute(
            text(
                "UPDATE control_process_instances SET lease_until=:expires, revoked=:revoked "
                "WHERE instance_id=:id"
            ),
            {"expires": expires, "revoked": revoke, "id": instance_id},
        )
        return _matches(authority, instance_id, expires, declaration)
    except Exception:
        raise _invalid() from None


def renew_control(
    session: Session, declaration: ControlDeclaration, *, instance_id: UUID, instance_token: str
) -> DeclaredCompatibility | ExpiredControl:
    """Renew a live lease, or stage a permanent expiry fence for caller commit."""
    return _update_control(session, declaration, instance_id, instance_token, revoke=False)


def fence_control(
    session: Session, declaration: ControlDeclaration, *, instance_id: UUID, instance_token: str
) -> DeclaredCompatibility | ExpiredControl:
    """Fence identity; this does not prove that external work stopped."""
    return _update_control(session, declaration, instance_id, instance_token, revoke=True)


def assert_live_control(
    session: Session, declaration: ControlDeclaration, *, instance_id: UUID, instance_token: str
) -> UUID:
    """Lock and validate a registered instance without renewing its lease.

    Returns its authority epoch only as an internal binding for isolated
    protocol testing. The caller must recheck after any waits and must not use
    it to bypass unfinished capacity, storage, topology or startup gates.
    """
    if not isinstance(instance_id, UUID):
        raise _invalid()
    token_digest = _token_digest(instance_token)
    try:
        authority = _authority(session, declaration)
        row = (
            session.execute(
                text("SELECT * FROM control_process_instances WHERE instance_id=:id FOR SHARE"),
                {"id": instance_id},
            )
            .mappings()
            .one_or_none()
        )
        now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if (
            row is None
            or row["revoked"]
            or row["lease_until"] <= now
            or not _existing_is_same(row, declaration, authority, token_digest)
        ):
            raise _invalid()
        epoch = row["authority_epoch"]
        if not isinstance(epoch, UUID):
            raise _invalid()
        return epoch
    except Exception:
        raise _invalid() from None


def assert_capacity_declaration_matches(
    session: Session, declaration: ControlDeclaration, *, active_fingerprint: str
) -> None:
    """Reject divergent capacity declarations; this is not capacity evidence."""
    if (
        not isinstance(active_fingerprint, str)
        or _FINGERPRINT.fullmatch(active_fingerprint) is None
    ):
        raise _invalid()
    try:
        authority = _authority(session, declaration)
        if not (
            hmac.compare_digest(declaration.capacity_fingerprint, authority["capacity_fingerprint"])
            and hmac.compare_digest(declaration.capacity_fingerprint, active_fingerprint)
        ):
            raise _invalid()
    except Exception:
        raise _invalid() from None
