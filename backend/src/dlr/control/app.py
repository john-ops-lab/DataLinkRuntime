"""FastAPI application factory for the Control Node."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dlr.control.api import (
    adapters,
    ai,
    auth,
    credentials,
    events,
    executions,
    health,
    package_sources,
    schedules,
    webhooks,
    workers,
)
from dlr.control.services.schedule import scheduler_loop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the M5.2 lightweight Schedule polling loop while the app serves.

    PostgreSQL is the only scheduling state source; the loop is a plain
    background task and no external scheduler framework is introduced.
    """
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
    app = FastAPI(title="DLR Control", version="0.0.1", lifespan=lifespan)
    app.include_router(health.router)
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
