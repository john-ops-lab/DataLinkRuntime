"""Legacy Token and Wave A account authentication endpoints."""

import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.models.account import User
from dlr.control.schemas.account import (
    AccountLogin,
    AccountLoginResponse,
    AccountPasswordChange,
    AccountPasswordReset,
    AccountSessionResponse,
    AccountStatusResponse,
)
from dlr.control.security import (
    is_https_request,
    require_account_session,
    require_admin_token,
    require_csrf,
    require_entry,
)
from dlr.control.services import accounts as account_service
from dlr.control.services.adapter import domain_error

CSRF_COOKIE_NAME = account_service.CSRF_COOKIE_NAME
SESSION_COOKIE_NAME = account_service.SESSION_COOKIE_NAME

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentAccountSession = Annotated[account_service.AccountSession, Depends(require_account_session)]


def _set_csrf_cookie(response: Response, request: Request) -> None:
    """Issue a non-HttpOnly double-submit token; it is not an auth secret."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        max_age=int(account_service.SESSION_TTL.total_seconds()),
        expires=datetime.now(UTC) + account_service.SESSION_TTL,
        httponly=False,
        secure=is_https_request(request),
        samesite="lax",
        path="/",
    )


def _set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    """Set the only browser-persistent account credential as HttpOnly cookie."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=int(account_service.SESSION_TTL.total_seconds()),
        expires=datetime.now(UTC) + account_service.SESSION_TTL,
        httponly=True,
        secure=is_https_request(request),
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.get("/api/auth/admin/verify")
def verify_admin_token(_: None = Depends(require_admin_token)) -> dict[str, str]:
    """Minimal probe the legacy Web UI uses to validate an admin Token."""
    return {"status": "ok"}


@router.get("/api/auth/account/csrf", response_model=AccountStatusResponse)
def get_account_csrf(request: Request, response: Response) -> AccountStatusResponse:
    """Bootstrap a same-origin CSRF cookie before account login or writes."""
    require_entry(request, "account")
    _set_csrf_cookie(response, request)
    return AccountStatusResponse(status="ok")


@router.post("/api/auth/account/login", response_model=AccountLoginResponse)
def login_account(
    payload: AccountLogin,
    request: Request,
    response: Response,
    session: DbSession,
) -> AccountLoginResponse:
    """Create a server-side account Session after CSRF and password checks."""
    require_entry(request, "account")
    require_csrf(request)
    user = session.scalar(select(User).where(User.username == payload.username))
    if user is None or not account_service.verify_password(
        payload.password.get_secret_value(), user.password_hash
    ):
        raise domain_error(
            401,
            "account_invalid_credentials",
            "Invalid username or password",
        )
    if not user.enabled:
        raise domain_error(403, "account_disabled", "Account is disabled")

    raw_token = account_service.create_session(session, user)
    _set_session_cookie(response, request, raw_token)
    _set_csrf_cookie(response, request)
    return AccountLoginResponse(principal=account_service.principal_response(user))


@router.get("/api/auth/account/me", response_model=AccountSessionResponse)
def get_account_me(current: CurrentAccountSession) -> AccountSessionResponse:
    """Return the current non-secret Principal, including forced-change state."""
    return AccountSessionResponse(principal=account_service.principal_response(current.user))


@router.post("/api/auth/account/logout", response_model=AccountStatusResponse)
def logout_account(
    request: Request,
    response: Response,
    current: CurrentAccountSession,
    session: DbSession,
) -> AccountStatusResponse:
    """Invalidate the current Session on the server and clear browser cookies."""
    require_csrf(request)
    # Delete by primary key through the loaded row; no raw Session token is logged or stored.
    session.delete(current.session)
    session.commit()
    _clear_auth_cookies(response)
    return AccountStatusResponse(status="ok")


@router.post("/api/auth/account/change-password", response_model=AccountStatusResponse)
def change_account_password(
    payload: AccountPasswordChange,
    request: Request,
    response: Response,
    current: CurrentAccountSession,
    session: DbSession,
) -> AccountStatusResponse:
    """Change the password and invalidate every old account Session."""
    require_csrf(request)
    current_password = payload.current_password.get_secret_value()
    new_password = payload.new_password.get_secret_value()
    if not account_service.verify_password(current_password, current.user.password_hash):
        raise domain_error(
            400,
            "account_current_password_invalid",
            "Current password is invalid",
        )
    if account_service.verify_password(new_password, current.user.password_hash):
        raise domain_error(
            422,
            "account_password_reuse",
            "New password must differ from the current password",
        )
    account_service.change_password(session, current, new_password)
    _clear_auth_cookies(response)
    return AccountStatusResponse(status="ok")


@router.post(
    "/api/auth/account/reset",
    response_model=AccountStatusResponse,
    dependencies=[Depends(require_admin_token)],
)
def reset_account_password(
    payload: AccountPasswordReset,
    session: DbSession,
) -> AccountStatusResponse:
    """Narrow superadmin emergency reset; routine user management is Wave B."""
    account_service.reset_password(
        session,
        payload.username,
        payload.new_password.get_secret_value(),
    )
    return AccountStatusResponse(status="ok")
