"""FastAPI application factory for the Control Node."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dlr.control import db
from dlr.control.api import (
    adapters,
    ai,
    auth,
    credentials,
    events,
    executions,
    health,
    locale,
    package_sources,
    schedules,
    webhooks,
    workers,
)
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
    app.include_router(health.router)
    app.include_router(locale.public_router)
    app.include_router(locale.router)
    app.include_router(auth.router)
    app.include_router(adapters.router)
    app.include_router(ai.router)
    app.include_router(credentials.router)
    app.include_router(package_sources.router)
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
