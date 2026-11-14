"""Experimental transaction fence for shared-token separation; never mounted.

It coordinates cooperating callers only. A matching Control declaration and
lease do not prove that old binaries, manual DB writers, or a different
configuration cannot still write. Production stays blocked until those writers
are physically fenced and every relevant operation is integrated.
The exact issued, transaction-bound handle is an internal misuse guard, not a
security boundary against arbitrary Python code or proof of prior lock order.
"""

from __future__ import annotations

import hmac
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.session import SessionTransaction

from dlr.common.config import settings
from dlr.common.schema_gate import try_control_gate
from dlr.common.worker_protocol_v4 import is_worker_credential
from dlr.control.services import control_cluster_identity, worker_credentials
from dlr.control.services.worker_token_separation_v4 import (
    assert_no_persisted_collision,
    check_candidate,
)

_NAMESPACE = 0x444C5254
_KEY = 0x544F4B31
_FENCE_REGISTRY_KEY = object()


class TokenFenceUnavailable(Exception):
    """Fixed non-sensitive failure; the caller must roll back."""


def _database_fence(session: Session) -> None:
    """Check the single-Control database role and absence of old sessions.

    Deployment must keep the new role's credentials exclusive to upgraded
    Controls, and trusted database administrators must fence role, grant and
    replication changes while v4 runs. This query cannot prove either fact.
    """
    role = settings.worker_v4_database_role
    admins = settings.worker_v4_database_admin_roles
    if (
        settings.worker_remote_v4_enabled is not True
        or type(role) is not str
        or not role
        or "\x00" in role
        or len(role.encode("utf-8")) > 63
        or type(admins) is not list
        or len(admins) > 16
        or any(
            type(name) is not str or not name or "\x00" in name or len(name.encode("utf-8")) > 63
            for name in admins
        )
        or role in admins
        or len(set(admins)) != len(admins)
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise TokenFenceUnavailable("token separation unavailable")
    # Dynamic activity data may otherwise be cached for the transaction.
    session.execute(text("SELECT pg_catalog.pg_stat_clear_snapshot()"))
    facts = (
        session.execute(
            text(
                """
            SELECT current_user = session_user AS same_role,
                   current_user = :role AS expected_role,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_roles AS r
                       JOIN pg_catalog.pg_database AS d ON d.datname = current_database()
                       WHERE (r.rolname = current_user OR
                              pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
                         AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR
                              r.rolreplication OR r.rolbypassrls OR r.oid = d.datdba)
                   ) AS unsafe_role,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_roles AS r
                       WHERE r.rolcanlogin AND r.rolname <> current_user
                         AND r.rolname <> ALL(CAST(:admins AS text[]))
                         AND pg_catalog.has_database_privilege(
                             r.oid, current_database(), 'CONNECT'
                         )
                   ) AS other_login_can_connect,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_stat_activity AS a
                       WHERE a.datname = current_database()
                         -- backend_type can be hidden, and a dropped role
                         -- can leave a live session without a visible name.
                         -- The session's login OID remains authoritative.
                         AND a.usesysid IS NOT NULL
                         AND a.usesysid <> (
                             SELECT r.oid FROM pg_catalog.pg_roles AS r
                             WHERE r.rolname = current_user
                         )
                   ) AS other_session,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_prepared_xacts AS x
                       WHERE x.database = current_database()
                   ) AS prepared_transaction
            """
            ),
            {"role": role, "admins": admins},
        )
        .mappings()
        .one()
    )
    if (
        facts["same_role"] is not True
        or facts["expected_role"] is not True
        or facts["unsafe_role"] is not False
        or facts["other_login_can_connect"] is not False
        or facts["other_session"] is not False
        or facts["prepared_transaction"] is not False
    ):
        raise TokenFenceUnavailable("token separation unavailable")


@dataclass(frozen=True)
class Fence:
    _session: Session = field(repr=False)
    _transaction: SessionTransaction = field(repr=False)
    _declaration: control_cluster_identity.ControlDeclaration = field(repr=False)
    _instance_id: UUID
    _instance_token: str = field(repr=False)
    _epoch: UUID
    _mode: Literal["legacy", "operation", "issuance"]

    def check(self) -> None:
        """Revalidate in the same transaction immediately before its commit."""
        try:
            registry = self._session.info.get(_FENCE_REGISTRY_KEY)
            if (
                registry is None
                or registry.get(id(self)) is not self
                or self._session.get_transaction() is not self._transaction
                or not self._transaction.is_active
                or self._session.in_nested_transaction()
            ):
                raise TokenFenceUnavailable("token separation unavailable")
            current = settings.worker_token
            declared = self._declaration.worker_token
            current_admin = settings.admin_token
            declared_admin = self._declaration.admin_token
            if (
                not isinstance(current, str)
                or not hmac.compare_digest(current.encode(), declared.encode())
                or not isinstance(current_admin, str)
                or not hmac.compare_digest(current_admin.encode(), declared_admin.encode())
                or is_worker_credential(current)
                or is_worker_credential(current_admin)
            ):
                raise TokenFenceUnavailable("token separation unavailable")
            epoch = control_cluster_identity.assert_live_control(
                self._session,
                self._declaration,
                instance_id=self._instance_id,
                instance_token=self._instance_token,
            )
            if epoch != self._epoch:
                raise TokenFenceUnavailable("token separation unavailable")
            _database_fence(self._session)
            assert_no_persisted_collision(self._session, current)
            assert_no_persisted_collision(self._session, current_admin)
        except Exception:
            raise TokenFenceUnavailable("token separation unavailable") from None

    def check_candidate(self, secret: str) -> None:
        self.check()
        if self._mode != "issuance":
            raise TokenFenceUnavailable("token separation unavailable")
        try:
            check_candidate(self._declaration.worker_token, secret)
        except Exception:
            raise TokenFenceUnavailable("token separation unavailable") from None


def acquire(
    session: Session,
    declaration: control_cluster_identity.ControlDeclaration,
    *,
    instance_id: UUID,
    instance_token: str,
    mode: Literal["legacy", "operation", "issuance"],
) -> Fence:
    """Take schema -> global token -> Control locks before any business locks."""
    try:
        if (
            mode not in {"legacy", "operation", "issuance"}
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            raise TokenFenceUnavailable("token separation unavailable")
        try_control_gate(session)
        # Ordinary operations cannot mint a credential. They may run together
        # while holding the namespace stable against exclusive issuance and
        # revocation; all identity/database checks remain identical.
        fn = (
            "pg_try_advisory_xact_lock"
            if mode == "issuance"
            else "pg_try_advisory_xact_lock_shared"
        )
        if (
            session.scalar(
                text(f"SELECT pg_catalog.{fn}(:ns, :key)"), {"ns": _NAMESPACE, "key": _KEY}
            )
            is not True
        ):
            raise TokenFenceUnavailable("token separation unavailable")
        epoch = control_cluster_identity.assert_live_control(
            session, declaration, instance_id=instance_id, instance_token=instance_token
        )
        transaction = session.get_transaction()
        if transaction is None:
            raise TokenFenceUnavailable("token separation unavailable")
        result = Fence(session, transaction, declaration, instance_id, instance_token, epoch, mode)
        registry = session.info.get(_FENCE_REGISTRY_KEY)
        if registry is None:
            registry = WeakValueDictionary()
            session.info[_FENCE_REGISTRY_KEY] = registry
        registry[id(result)] = result
        result.check()
        return result
    except Exception:
        raise TokenFenceUnavailable("token separation unavailable") from None


@dataclass(frozen=True)
class PreparedRuntimeIssue:
    issued: worker_credentials.IssuedWorkerCredential = field(repr=False)
    fence: Fence = field(repr=False)
    rotation: worker_credentials.RotatedWorkerCredential | None = field(default=None, repr=False)


def issue_fresh_runtime(
    session: Session,
    declaration: control_cluster_identity.ControlDeclaration,
    *,
    instance_id: UUID,
    instance_token: str,
    worker_id: int,
    operation: Literal["exchange", "recover", "rotate"],
    enrollment: str | None = None,
    overlap_seconds: int = 0,
) -> PreparedRuntimeIssue:
    """Prepare one issuance in a caller-owned transaction; never expose pre-commit.

    The caller must roll back on any failure, call Fence.check directly before
    commit in future integrations, and treat a lost commit reply as unknown.
    No live route uses this experimental wrapper.
    """
    try:
        gate = acquire(
            session,
            declaration,
            instance_id=instance_id,
            instance_token=instance_token,
            mode="issuance",
        )
        rotation = None
        if operation == "exchange" and isinstance(enrollment, str):
            issued = worker_credentials.exchange_enrollment(session, worker_id, enrollment)
        elif operation == "recover" and enrollment is None:
            issued = worker_credentials.recover_runtime_credential(session, worker_id)
        elif operation == "rotate" and enrollment is None:
            rotation = worker_credentials.rotate_runtime_credential_with_previous(
                session, worker_id, overlap_seconds=overlap_seconds
            )
            issued = rotation.issued
        else:
            raise TokenFenceUnavailable("token separation unavailable")
        gate.check_candidate(issued.secret)
        gate.check()
        return PreparedRuntimeIssue(issued, gate, rotation)
    except Exception:
        with suppress(Exception):
            session.rollback()
        raise TokenFenceUnavailable("token separation unavailable") from None
