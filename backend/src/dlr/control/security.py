"""Authentication and Principal dependencies for the Control Node.

The legacy bearer-token contract remains the default entry mode. Account
sessions are accepted only after the account Web reverse proxy has rewritten
the request through the private ``/__dlr_account`` path; the browser cannot
select that path because it is not exposed by either public Nginx server.
"""

import secrets
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.services.accounts import AccountSession, find_session

AuthorizationHeader = Annotated[str | None, Header()]
PrincipalKind = Literal["superadmin", "account"]
AccountRole = Literal["admin", "user"]
ENTRY_MODE_SCOPE_KEY = "dlr_entry_mode"
TOKEN_ENTRY_MODE: Literal["token"] = "token"
ACCOUNT_ENTRY_MODE: Literal["account"] = "account"
DbSession = Annotated[Session, Depends(db.get_session)]


@dataclass(frozen=True)
class Principal:
    """The common identity shape used by all management API dependencies."""

    kind: PrincipalKind
    role: AccountRole | None = None
    user_id: int | None = None
    username: str | None = None
    must_change_password: bool = False


SUPERADMIN_PRINCIPAL = Principal(kind="superadmin")


def entry_mode(request: Request) -> str:
    """Return the server-assigned entry mode, never a client header value."""
    return str(request.scope.get(ENTRY_MODE_SCOPE_KEY, TOKEN_ENTRY_MODE))


def _entry_error(expected: str) -> HTTPException:
    if expected == ACCOUNT_ENTRY_MODE:
        return HTTPException(
            status_code=401,
            detail={
                "code": "account_entry_required",
                "message": "Account authentication is available on the account entry only",
            },
        )
    return HTTPException(
        status_code=401,
        detail={
            "code": "token_entry_required",
            "message": "Token authentication is available on the token entry only",
        },
    )


def require_entry(request: Request, expected: Literal["token", "account"]) -> None:
    """Reject credentials arriving through the wrong server-side entry."""
    if entry_mode(request) != expected:
        raise _entry_error(expected)


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _require_token(authorization: str | None, configured: str | None) -> None:
    if configured is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "auth_not_configured",
                "message": "Authentication token is not configured on the server",
            },
        )
    provided = _bearer_token(authorization)
    if provided is None or not secrets.compare_digest(provided, configured):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "Invalid or missing bearer token"},
        )


def require_admin_token(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> None:
    """Protect the legacy superadmin-only endpoints."""
    require_entry(request, TOKEN_ENTRY_MODE)
    _require_token(authorization, settings.admin_token)


def require_worker_token(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> None:
    """Protect worker-internal endpoints and keep them off the account entry."""
    require_entry(request, TOKEN_ENTRY_MODE)
    _require_token(authorization, settings.worker_token)


def _account_principal(account_session: AccountSession) -> Principal:
    user = account_session.user
    return Principal(
        kind="account",
        role=user.role,  # type: ignore[arg-type]
        user_id=user.id,
        username=user.username,
        must_change_password=user.must_change_password,
    )


def require_account_session(
    request: Request,
    session: DbSession,
) -> AccountSession:
    """Load a live account session for account auth endpoints."""
    require_entry(request, ACCOUNT_ENTRY_MODE)
    matched = find_session(session, request.cookies.get("dlr_account_session"))
    if matched is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "account_session_required", "message": "Account session is required"},
        )
    return matched


def require_principal(
    request: Request,
    session: DbSession,
    authorization: AuthorizationHeader = None,
) -> Principal:
    """Authenticate the unified superadmin/account Principal for app APIs."""
    if entry_mode(request) == TOKEN_ENTRY_MODE:
        _require_token(authorization, settings.admin_token)
        return SUPERADMIN_PRINCIPAL

    if entry_mode(request) != ACCOUNT_ENTRY_MODE:
        raise _entry_error(ACCOUNT_ENTRY_MODE)
    matched = find_session(session, request.cookies.get("dlr_account_session"))
    if matched is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "account_session_required", "message": "Account session is required"},
        )
    if matched.user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_password_change_required",
                "message": "Change the account password before using the application",
            },
        )
    return _account_principal(matched)


def require_csrf(request: Request) -> None:
    """Double-submit CSRF protection for cookie-authenticated account writes."""
    cookie_token = request.cookies.get("dlr_account_csrf")
    header_token = request.headers.get("X-CSRF-Token")
    if (
        not cookie_token
        or not header_token
        or not secrets.compare_digest(cookie_token, header_token)
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "account_csrf_invalid", "message": "Invalid CSRF protection token"},
        )


def is_https_request(request: Request) -> bool:
    """Honor direct HTTPS and the trusted reverse proxy's overwritten scheme."""
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
