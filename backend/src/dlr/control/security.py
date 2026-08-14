"""Static bearer-token authentication for the Control Node (M2).

Two platform-wide shared tokens protect all management and worker APIs:

- ``DLR_ADMIN_TOKEN`` for Adapter/Revision/Execution management.
- ``DLR_WORKER_TOKEN`` for the worker-internal API.

Tokens live only in configuration: they are never persisted and never
logged. When a token is not configured, protected endpoints answer 503
instead of silently running open; a wrong or missing token yields 401.
"""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException

from dlr.common.config import settings

AuthorizationHeader = Annotated[str | None, Header()]


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


def require_admin_token(authorization: AuthorizationHeader = None) -> None:
    """FastAPI dependency protecting admin-facing endpoints.

    The explicit ``None`` default matters: without it a completely absent
    Authorization header fails FastAPI validation with 422 instead of
    reaching the token check and yielding 401.
    """
    _require_token(authorization, settings.admin_token)


def require_worker_token(authorization: AuthorizationHeader = None) -> None:
    """FastAPI dependency protecting worker-internal endpoints."""
    _require_token(authorization, settings.worker_token)
