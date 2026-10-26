"""FastAPI application factory for the Control Node."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from dlr.common.config import settings, validate_deployment_configuration
from dlr.common.platform_logging import configure_platform_logging
from dlr.control import db
from dlr.control.ai.attachment_documents import DocumentConversionError
from dlr.control.ai.attachment_store import attachment_gc_loop
from dlr.control.ai.tool_audit import configure_ai_tool_audit_logging
from dlr.control.api import (
    adapter_batches,
    adapters,
    ai,
    ai_attachments,
    auth,
    builtin_packages,
    cache_admin,
    credentials,
    events,
    executions,
    health,
    ingress,
    input_configs,
    knowledge_sources,
    locale,
    managed_input,
    package_sources,
    portable,
    schedules,
    subscriptions,
    templates,
    users,
    webhooks,
    workers,
)
from dlr.control.security import require_csrf
from dlr.control.services import accounts as account_service
from dlr.control.services.admission import admission_reconciler_loop
from dlr.control.services.attempt import attempt_reconciler_loop
from dlr.control.services.http_ingress_admission import HttpIngressAdmission
from dlr.control.services.infrastructure_dlq import infrastructure_dlq_loop
from dlr.control.services.managed_input_gc import artifact_gc_loop, orphan_audit_loop
from dlr.control.services.outbox import outbox_relay_loop
from dlr.control.services.rabbitmq import topology_bootstrap_loop
from dlr.control.services.retention import retention_loop
from dlr.control.services.schedule import scheduler_loop
from dlr.control.services.secrets import bootstrap_demo_credentials
from dlr.control.services.subscription_management import (
    subscription_maintenance_loop,
    subscription_quota_audit_loop,
)
from dlr.control.services.subscription_runtime import get_runtime
from dlr.control.template_catalog import get_template_catalog

logger = logging.getLogger("dlr.control.app")


class _EntryBoundary:
    """Assign account mode only from the private reverse-proxy URI prefix.

    The account Nginx server rewrites public ``/api/...`` requests to
    ``/__dlr_account/api/...``. That prefix is not proxied by the token
    server and Control itself is not published as a host port, so this
    boundary does not trust a client-controlled request header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        account_prefix = "/__dlr_account"
        path = scope["path"]
        if path == account_prefix or path.startswith(f"{account_prefix}/"):
            if path == account_prefix:
                response = JSONResponse(status_code=404, content={"detail": "Not found"})
                await response(scope, receive, send)
                return
            scope["dlr_entry_mode"] = "account"
            scope["path"] = path[len(account_prefix) :]
            raw_path = scope.get("raw_path")
            if isinstance(raw_path, bytes):
                scope["raw_path"] = raw_path[len(account_prefix) :]
        else:
            scope["dlr_entry_mode"] = "token"
        # Account writes use the existing cookie/header double-submit check.
        # Public Webhook and durable ingress retain their own Bearer auth;
        # token entry keeps bypassing CSRF even when account cookies exist.
        is_public_hook = scope["path"].startswith(("/api/hooks/", "/api/ingress/"))
        if (
            scope["dlr_entry_mode"] == "account"
            and not is_public_hook
            and scope["method"].upper() not in {"GET", "HEAD", "OPTIONS"}
        ):
            try:
                require_csrf(Request(scope, receive=receive))
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
    """Run lightweight Schedule, retention, GC and stale Execution loops.

    PostgreSQL is the only Control-plane state source; these loops are plain
    background tasks and no external scheduler framework is introduced.
    """
    bootstrap_task = asyncio.create_task(_demo_bootstrap_loop())
    ai_attachment_task = asyncio.create_task(attachment_gc_loop())
    task = asyncio.create_task(scheduler_loop())
    retention_task = asyncio.create_task(retention_loop())
    artifact_gc_task = asyncio.create_task(artifact_gc_loop())
    orphan_audit_task = asyncio.create_task(orphan_audit_loop())
    admission_task = asyncio.create_task(admission_reconciler_loop())
    attempt_task = asyncio.create_task(attempt_reconciler_loop())
    infrastructure_dlq_task = asyncio.create_task(infrastructure_dlq_loop())
    subscription_task = asyncio.create_task(subscription_maintenance_loop())
    quota_audit_task = asyncio.create_task(subscription_quota_audit_loop())
    source_runtime = get_runtime()
    source_runtime.start()
    rabbitmq_tasks: tuple[asyncio.Task[None], ...] = ()
    if settings.rabbitmq_url:
        rabbitmq_tasks = (
            asyncio.create_task(topology_bootstrap_loop()),
            asyncio.create_task(outbox_relay_loop()),
        )
    try:
        yield
    finally:
        native_closed = await source_runtime.aclose()
        from dlr.control.api.ingress import flush_ingress_resources, shutdown_ingress_resources
        from dlr.control.services.ingress_database import close_intake_database

        flush_ingress_resources()
        await asyncio.to_thread(shutdown_ingress_resources)
        await asyncio.to_thread(close_intake_database, native_closed=native_closed)
        for background_task in (
            task,
            ai_attachment_task,
            bootstrap_task,
            retention_task,
            artifact_gc_task,
            orphan_audit_task,
            admission_task,
            attempt_task,
            infrastructure_dlq_task,
            subscription_task,
            quota_audit_task,
            *rabbitmq_tasks,
        ):
            background_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await ai_attachment_task
        with contextlib.suppress(asyncio.CancelledError):
            await bootstrap_task
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await artifact_gc_task
        with contextlib.suppress(asyncio.CancelledError):
            await orphan_audit_task
        with contextlib.suppress(asyncio.CancelledError):
            await admission_task
        with contextlib.suppress(asyncio.CancelledError):
            await attempt_task
        with contextlib.suppress(asyncio.CancelledError):
            await infrastructure_dlq_task
        with contextlib.suppress(asyncio.CancelledError):
            await subscription_task
        with contextlib.suppress(asyncio.CancelledError):
            await quota_audit_task
        for background_task in rabbitmq_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await background_task


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
    validate_deployment_configuration(settings)
    configure_platform_logging("control")
    configure_ai_tool_audit_logging()
    # Parse and cross-validate every catalog metadata resource at
    # startup. Variant source bodies remain lazy and are hashed when selected;
    # release gates validate all 51 before packaging.
    get_template_catalog()
    app = FastAPI(title="DLR Control", version="0.0.1", lifespan=lifespan)

    @app.exception_handler(DocumentConversionError)
    async def _ai_attachment_error(request: Request, exc: DocumentConversionError) -> JSONResponse:
        status = {
            "ai_attachment_expired": 410,
            "ai_attachment_hash_mismatch": 409,
            "ai_attachment_not_ready": 409,
            "ai_attachment_cancelled": 409,
            "ai_attachment_converter_busy": 429,
            "ai_attachment_store_full": 429,
            "ai_attachment_converter_unavailable": 503,
            "ai_attachment_store_unavailable": 503,
            "ai_attachment_store_busy": 503,
            "ai_attachment_parse_timeout": 504,
            "ai_attachment_cancel_failed": 503,
        }.get(exc.code, 422)
        detail: dict[str, object] = {
            "code": exc.code,
            "message": "AI attachment operation could not be completed",
        }
        if exc.code == "ai_attachment_cancelled" and exc.reason == "scope_closed":
            detail["params"] = {"reason": "scope_closed"}
        return JSONResponse(
            status_code=status,
            content={"detail": detail},
        )

    app.add_middleware(_EntryBoundary)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Do not echo rejected request values on secret-bearing surfaces.

        In particular, a malformed client must not make FastAPI's generic
        validation payload reflect an access_key_secret or a value rejected by
        the Template Gallery. Other API routes retain FastAPI's default
        validation response for compatibility.
        """
        if request.url.path.startswith("/api/event-subscriptions"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "subscription_config_invalid",
                        "message": "Event subscription request is invalid",
                    }
                },
            )
        if request.url.path.startswith("/api/portable/") or (
            request.url.path.startswith("/api/adapters/")
            and request.url.path.endswith(("/portable-preview", "/templates"))
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "portable_package_invalid",
                        "message": "Invalid or unsupported DLR package",
                    }
                },
            )
        if request.url.path.startswith("/api/adapters/") and "/ai/attachments" in request.url.path:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "ai_attachment_invalid",
                        "message": "Invalid AI attachment request",
                    }
                },
            )
        if request.url.path.startswith("/api/templates/"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "template_request_invalid",
                        "message": "Template request is invalid",
                    }
                },
            )
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
        matched_route = request.scope.get("route")
        matched_route_path = getattr(matched_route, "path", None)
        is_package_source_write = (
            request.method == "POST" and request.url.path == "/api/package-sources"
        ) or (
            request.method == "PATCH"
            and matched_route_path == "/api/package-sources/{package_source_id}"
        )
        if is_package_source_write:
            field = next(
                (
                    location[-1]
                    for error in exc.errors()
                    if (location := error.get("loc"))
                    and location[-1] in {"name", "kind", "index_url", "is_default", "credential_id"}
                ),
                "request",
            )
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "package_source_request_invalid",
                        "message": "Package source request is invalid",
                        "params": {"field": field, "reason": "request_validation"},
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
    app.include_router(adapter_batches.router)
    app.include_router(templates.router)
    app.include_router(portable.router)
    app.include_router(input_configs.router)
    app.include_router(managed_input.router)
    app.include_router(managed_input.capability_router)
    app.include_router(managed_input.upload_router)
    app.include_router(managed_input.adapter_router)
    app.include_router(ai.router)
    app.include_router(ai.adapter_router)
    app.include_router(ai_attachments.router)
    app.include_router(credentials.router)
    app.include_router(credentials.adapter_router)
    app.include_router(package_sources.router)
    app.include_router(builtin_packages.router)
    app.include_router(builtin_packages.worker_router)
    app.include_router(knowledge_sources.router)
    app.include_router(schedules.router)
    app.include_router(subscriptions.router)
    app.include_router(ingress.router)
    app.include_router(webhooks.router)
    # M5.4.3: the external Webhook ingress has its own Bearer authentication
    # and must never require the admin token.
    app.include_router(webhooks.public_router)
    app.include_router(executions.router)
    app.include_router(workers.router)
    app.include_router(workers.admin_router)
    app.include_router(cache_admin.router)
    app.include_router(events.router)
    # Last registered user middleware is outermost: reject only known-full
    # ingress resources before entry checks and route work.
    app.add_middleware(HttpIngressAdmission, busy=ingress.admission_capacity_busy)
    return app
