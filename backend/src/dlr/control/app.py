"""FastAPI application factory for the Control Node."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from dlr.common.config import settings, validate_deployment_configuration
from dlr.common.platform_logging import configure_platform_logging
from dlr.control import db
from dlr.control.ai.tool_audit import configure_ai_tool_audit_logging
from dlr.control.api import (
    adapters,
    ai,
    auth,
    credentials,
    events,
    executions,
    health,
    input_configs,
    knowledge_sources,
    locale,
    managed_input,
    package_sources,
    schedules,
    users,
    webhooks,
    workers,
)
from dlr.control.security import require_csrf
from dlr.control.services import accounts as account_service
from dlr.control.services.retention import retention_loop
from dlr.control.services.schedule import scheduler_loop
from dlr.control.services.secrets import bootstrap_demo_credentials

logger = logging.getLogger("dlr.control.app")


async def _demo_bootstrap_loop() -> None:
    """Create the demo Credentials once the database is migrated.

    The Control process starts before Alembic on fresh deployments, so a
    single startup attempt can run against an empty schema. Retry until the
    bootstrap succeeds, then stop; failures are logged once so an unhealthy
    database does not spam the service log.
    """
    logged = False
    while True:
        try:
            with db.SessionLocal() as session:
                account_service.bootstrap_default_admin(session)
                bootstrap_demo_credentials(session)
            return
        except Exception:
            if not logged:
                logger.warning(
                    "demo credential bootstrap unavailable (database not migrated yet?); "
                    "will keep retrying until it succeeds"
                )
                logged = True
            await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the M5.2 lightweight Schedule polling loop while the app serves.

    PostgreSQL is the only scheduling state source; the loop is a plain
    background task and no external scheduler framework is introduced.
    """
    bootstrap_task = asyncio.create_task(_demo_bootstrap_loop())
    task = asyncio.create_task(scheduler_loop())
    retention_task = asyncio.create_task(retention_loop())
    try:
        yield
    finally:
        task.cancel()
        bootstrap_task.cancel()
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await bootstrap_task
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
    validate_deployment_configuration(settings)
    configure_platform_logging("control")
    configure_ai_tool_audit_logging()
    app = FastAPI(title="DLR Control", version="0.0.1", lifespan=lifespan)

    @app.middleware("http")
    async def _entry_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Assign account mode only from the private reverse-proxy URI prefix.

        The account Nginx server rewrites public ``/api/...`` requests to
        ``/__dlr_account/api/...``. That prefix is not proxied by the token
        server and Control itself is not published as a host port, so this
        boundary does not trust a client-controlled request header.
        """
        account_prefix = "/__dlr_account"
        path = request.scope["path"]
        if path == account_prefix or path.startswith(f"{account_prefix}/"):
            if path == account_prefix:
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            request.scope["dlr_entry_mode"] = "account"
            request.scope["path"] = path[len(account_prefix) :]
            raw_path = request.scope.get("raw_path")
            if isinstance(raw_path, bytes):
                request.scope["raw_path"] = raw_path[len(account_prefix) :]
        else:
            request.scope["dlr_entry_mode"] = "token"
        # Every non-safe request arriving through the account reverse-proxy
        # boundary is cookie-authenticated and must carry the double-submit
        # token. The public Webhook ingress is the exception: it is Bearer
        # token-authenticated by the referenced Credential and is intentionally
        # reachable through the account entry so copied hook URLs keep working.
        # Token superadmin requests deliberately bypass this check so the
        # legacy entry remains compatible even when account cookies exist for
        # the same host on another port.
        is_public_hook = request.scope["path"].startswith("/api/hooks/")
        if (
            request.scope["dlr_entry_mode"] == "account"
            and not is_public_hook
            and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
        ):
            try:
                require_csrf(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Do not echo rejected KnowledgeSource request values.

        In particular, a malformed client must not make FastAPI's generic
        validation payload reflect an access_key_secret. Other API routes
        retain FastAPI's default validation response for compatibility.
        """
        if request.url.path.startswith("/api/knowledge-sources/"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "ks_config_invalid",
                        "message": "Knowledge source configuration is invalid",
                    }
                },
            )
        if request.url.path.startswith("/api/adapters/") and request.url.path.endswith(
            "/input-config"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "input_invalid",
                        "message": "Input configuration request is invalid",
                        "params": {"reason": "request_validation"},
                    }
                },
            )
        if request.url.path == "/api/system/managed-input-settings":
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "managed_input_settings_invalid",
                        "message": "Managed Input settings are invalid",
                    }
                },
            )
        if request.url.path.startswith("/api/auth/account/") or request.url.path.startswith(
            "/api/users"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "account_request_invalid",
                        "message": "Account request is invalid",
                    }
                },
            )
        return await request_validation_exception_handler(request, exc)

    app.include_router(health.router)
    app.include_router(locale.public_router)
    app.include_router(locale.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(adapters.router)
    app.include_router(input_configs.router)
    app.include_router(managed_input.router)
    app.include_router(managed_input.upload_router)
    app.include_router(managed_input.adapter_router)
    app.include_router(ai.router)
    app.include_router(ai.adapter_router)
    app.include_router(credentials.router)
    app.include_router(credentials.adapter_router)
    app.include_router(package_sources.router)
    app.include_router(knowledge_sources.router)
    app.include_router(schedules.router)
    app.include_router(webhooks.router)
    # M5.4.3: the external Webhook ingress has its own Bearer authentication
    # and must never require the admin token.
    app.include_router(webhooks.public_router)
    app.include_router(executions.router)
    app.include_router(workers.router)
    app.include_router(workers.admin_router)
    app.include_router(events.router)
    return app
