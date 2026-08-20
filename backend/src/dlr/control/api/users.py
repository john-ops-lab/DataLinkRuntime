"""Wave B account-user management endpoints.

The deployment superadmin and account admins share this API. Account users
may only read or rename their own account; lifecycle, role and password-reset
operations stay administrator-only. There is intentionally no delete route in
Wave B, so an owner-transfer policy is not needed before Wave C.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.account import (
    AccountUserCreate,
    AccountUserPasswordReset,
    AccountUserPatch,
    AccountUserResponse,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_principal,
)
from dlr.control.services import accounts as account_service

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
AdminPrincipal = Annotated[Principal, Depends(require_admin_principal)]


def _require_user_access(user_id: int, principal: CurrentPrincipal) -> Principal:
    """Allow admins/superadmin or the account owner to inspect one row."""
    if principal.kind == "superadmin" or principal.role == "admin":
        return principal
    if principal.user_id == user_id:
        return principal
    raise HTTPException(
        status_code=403,
        detail={
            "code": "account_user_self_only",
            "message": "Account users may only access their own account",
        },
    )


UserAccess = Annotated[Principal, Depends(_require_user_access)]


@router.get("/api/users", response_model=list[AccountUserResponse])
def list_users(_: AdminPrincipal, session: DbSession) -> list[AccountUserResponse]:
    """List all account users without password/hash/session fields."""
    return [account_service.user_response(user) for user in account_service.list_users(session)]


@router.post("/api/users", status_code=201, response_model=AccountUserResponse)
def create_user(
    payload: AccountUserCreate,
    _: AdminPrincipal,
    session: DbSession,
) -> AccountUserResponse:
    """Create an enabled admin/user with a forced first password change."""
    user = account_service.create_user(
        session,
        payload.username,
        payload.password.get_secret_value(),
        payload.role,
    )
    return account_service.user_response(user)


@router.get("/api/users/{user_id}", response_model=AccountUserResponse)
def get_user(user_id: int, _: UserAccess, session: DbSession) -> AccountUserResponse:
    """Read one account for an administrator or the account owner."""
    return account_service.user_response(account_service.get_user(session, user_id))


@router.patch("/api/users/{user_id}", response_model=AccountUserResponse)
def patch_user(
    user_id: int,
    payload: AccountUserPatch,
    principal: UserAccess,
    session: DbSession,
) -> AccountUserResponse:
    """Rename an own account; administrators may also change role/status."""
    if principal.kind == "account" and principal.role == "user":
        forbidden_fields = payload.model_fields_set - {"username"}
        if forbidden_fields:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "account_user_profile_forbidden",
                    "message": "Account users cannot change role or enabled state",
                },
            )
    user = account_service.update_user(
        session,
        user_id,
        username=payload.username,
        role=payload.role,
        enabled=payload.enabled,
        fields_set=payload.model_fields_set,
    )
    return account_service.user_response(user)


@router.post("/api/users/{user_id}/reset-password", response_model=AccountUserResponse)
def reset_user_password(
    user_id: int,
    payload: AccountUserPasswordReset,
    _: AdminPrincipal,
    session: DbSession,
) -> AccountUserResponse:
    """Reset a target password, force a change, and invalidate every Session."""
    account_service.reset_password_for_user(
        session,
        user_id,
        payload.new_password.get_secret_value(),
    )
    return account_service.user_response(account_service.get_user(session, user_id))
