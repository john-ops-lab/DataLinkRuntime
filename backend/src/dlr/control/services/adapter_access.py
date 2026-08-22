"""Adapter ownership and read/edit authorization for account principals.

The deployment superadmin and account admins bypass Adapter ACL. Ordinary
account users see only their owned Adapters and explicit grants. This module
is deliberately a small Adapter-specific matrix rather than a general RBAC
engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterPermission, Execution, User
from dlr.control.schemas.adapter import (
    AdapterPermissionCandidate,
    AdapterPermissionResponse,
)
from dlr.control.services.adapter import domain_error

if TYPE_CHECKING:
    from dlr.control.security import Principal

AccessLevel = Literal["admin", "owner", "edit", "read"]
AccessAction = Literal["read", "edit", "delete", "manage", "owner"]


@dataclass(frozen=True)
class AdapterAccess:
    """The resolved capability level for one principal and Adapter."""

    adapter: Adapter
    level: AccessLevel


def _is_admin(principal: Principal) -> bool:
    return principal.kind == "superadmin" or principal.role == "admin"


def owner_user_id_for_create(principal: Principal) -> int | None:
    """Return an owner only for an ordinary account user creation."""
    if principal.kind == "account" and principal.role == "user":
        return principal.user_id
    return None


def _adapter_not_found() -> NoReturn:
    raise domain_error(404, "adapter_not_found", "Adapter not found")


def _level(session: Session, adapter: Adapter, principal: Principal) -> AccessLevel | None:
    if _is_admin(principal):
        return "admin"
    if principal.user_id is None:
        return None
    if adapter.owner_user_id == principal.user_id:
        return "owner"
    permission = session.scalar(
        select(AdapterPermission.permission).where(
            AdapterPermission.adapter_id == adapter.id,
            AdapterPermission.user_id == principal.user_id,
        )
    )
    if permission is None:
        return None
    return cast(AccessLevel, permission)


def _forbidden(action: AccessAction, level: AccessLevel) -> None:
    if action == "edit":
        raise domain_error(
            403,
            "adapter_read_only",
            "Read permission allows viewing only; edit permission is required",
        )
    if action == "delete":
        raise domain_error(
            403,
            "adapter_delete_forbidden",
            "Only the Adapter owner or an administrator may delete an Adapter",
        )
    if action == "manage":
        raise domain_error(
            403,
            "adapter_permission_management_forbidden",
            "Only the Adapter owner or an administrator may manage permissions",
        )
    if action == "owner":
        raise domain_error(
            403,
            "adapter_owner_required",
            "Only the Adapter owner or an administrator may change this configuration",
        )
    raise domain_error(
        403,
        "adapter_permission_denied",
        f"Adapter access level '{level}' cannot perform this action",
    )


def require_adapter_access(
    session: Session,
    adapter_id: int,
    principal: Principal,
    action: AccessAction = "read",
    *,
    for_update: bool = False,
) -> AdapterAccess:
    """Resolve and enforce one Adapter capability.

    A user without any relationship receives the same 404 as an unknown
    Adapter, so direct ID guessing cannot be used as an existence oracle.
    Known read-only shares receive a stable 403 for attempted writes.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=for_update)
    if adapter is None or adapter.archived_at is not None:
        _adapter_not_found()
    level = _level(session, adapter, principal)
    if level is None:
        _adapter_not_found()
    if action == "read":
        return AdapterAccess(adapter=adapter, level=level)
    if action == "edit" and level in {"admin", "owner", "edit"}:
        return AdapterAccess(adapter=adapter, level=level)
    if action in {"delete", "manage", "owner"} and level in {"admin", "owner"}:
        return AdapterAccess(adapter=adapter, level=level)
    _forbidden(action, level)
    raise AssertionError("unreachable")


def list_visible_adapters(session: Session, principal: Principal) -> list[Adapter]:
    """List every active Adapter visible to the principal."""
    if _is_admin(principal):
        query = select(Adapter).where(Adapter.archived_at.is_(None))
    else:
        if principal.user_id is None:
            return []
        shared_ids = select(AdapterPermission.adapter_id).where(
            AdapterPermission.user_id == principal.user_id
        )
        query = select(Adapter).where(
            or_(
                Adapter.owner_user_id == principal.user_id,
                Adapter.id.in_(shared_ids),
            ),
            Adapter.archived_at.is_(None),
        )
    return list(session.scalars(query.order_by(Adapter.updated_at.desc(), Adapter.id.desc())).all())


def require_execution_access(
    session: Session,
    execution_id: int,
    principal: Principal,
    action: AccessAction = "read",
) -> Execution:
    """Resolve an Execution through its Adapter before exposing its details."""
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    require_adapter_access(session, execution.adapter_id, principal, action)
    return execution


def _locked_for_management(session: Session, adapter_id: int, principal: Principal) -> Adapter:
    access = require_adapter_access(session, adapter_id, principal, "manage", for_update=True)
    if access.adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    return access.adapter


def list_permissions(
    session: Session, adapter_id: int, principal: Principal
) -> list[AdapterPermissionResponse]:
    _locked_for_management(session, adapter_id, principal)
    rows = session.execute(
        select(AdapterPermission, User)
        .join(User, User.id == AdapterPermission.user_id)
        .where(AdapterPermission.adapter_id == adapter_id)
        .order_by(User.username, User.id)
    ).all()
    return [
        AdapterPermissionResponse(
            user_id=user.id,
            username=user.username,
            enabled=user.enabled,
            permission=cast(Literal["read", "edit"], grant.permission),
        )
        for grant, user in rows
    ]


def list_permission_candidates(
    session: Session, adapter_id: int, principal: Principal
) -> list[AdapterPermissionCandidate]:
    """Return only account metadata needed by the Adapter sharing picker.

    This is intentionally not an alias for the account-management API. Owners
    may discover ordinary-user grantees for their own Adapter; administrators
    may also see account-admin rows so the UI can explain that those accounts
    already bypass Adapter ACL. No password, Session or lifecycle fields are
    selected here.
    """
    adapter = require_adapter_access(session, adapter_id, principal, "manage").adapter
    query = select(User.id, User.username, User.role, User.enabled)
    if adapter.owner_user_id is not None:
        query = query.where(User.id != adapter.owner_user_id)
    if not _is_admin(principal):
        query = query.where(User.role == "user")
    users = session.execute(query.order_by(User.username, User.id)).all()
    return [
        AdapterPermissionCandidate(
            id=user_id,
            username=username,
            role=cast(Literal["admin", "user"], role),
            enabled=enabled,
        )
        for user_id, username, role, enabled in users
    ]


def response_metadata(
    session: Session, adapter: Adapter, principal: Principal
) -> tuple[AccessLevel, str | None]:
    """Resolve safe relationship labels for an already-authorized response."""
    level = _level(session, adapter, principal)
    if level is None:
        _adapter_not_found()
    owner_username = None
    if adapter.owner_user_id is not None:
        owner_username = session.scalar(
            select(User.username).where(User.id == adapter.owner_user_id)
        )
    return level, owner_username


def set_permission(
    session: Session,
    adapter_id: int,
    user_id: int,
    permission: Literal["read", "edit"],
    principal: Principal,
) -> AdapterPermissionResponse:
    adapter = _locked_for_management(session, adapter_id, principal)
    user = session.get(User, user_id)
    if user is None:
        raise domain_error(404, "account_not_found", "Account not found")
    if adapter.owner_user_id == user_id:
        raise domain_error(
            409,
            "adapter_owner_permission_forbidden",
            "The Adapter owner does not need an explicit permission grant",
        )
    grant = session.scalar(
        select(AdapterPermission)
        .where(
            AdapterPermission.adapter_id == adapter_id,
            AdapterPermission.user_id == user_id,
        )
        .with_for_update()
    )
    if grant is None:
        grant = AdapterPermission(
            adapter_id=adapter_id,
            user_id=user_id,
            permission=permission,
        )
        session.add(grant)
    else:
        grant.permission = permission
    session.commit()
    return AdapterPermissionResponse(
        user_id=user.id,
        username=user.username,
        enabled=user.enabled,
        permission=permission,
    )


def revoke_permission(
    session: Session,
    adapter_id: int,
    user_id: int,
    principal: Principal,
) -> None:
    _locked_for_management(session, adapter_id, principal)
    grant = session.scalar(
        select(AdapterPermission)
        .where(
            AdapterPermission.adapter_id == adapter_id,
            AdapterPermission.user_id == user_id,
        )
        .with_for_update()
    )
    if grant is not None:
        session.delete(grant)
        session.commit()
