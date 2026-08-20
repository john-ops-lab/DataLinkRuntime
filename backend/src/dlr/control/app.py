"""FastAPI application factory for the Control Node."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from dlr.control import db
from dlr.control.api import (
    adapters,
    ai,
    auth,
    credentials,
    events,
    executions,
    health,
    knowledge_sources,
    locale,
    package_sources,
    schedules,
    webhooks,
    workers,
)
from dlr.control.services import accounts as account_service
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
    try:
        yield
    finally:
        task.cancel()
        bootstrap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await bootstrap_task


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
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
        if request.url.path.startswith("/api/auth/account/"):
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
    app.include_router(adapters.router)
    app.include_router(ai.router)
    app.include_router(credentials.router)
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
