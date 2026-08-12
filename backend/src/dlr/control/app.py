"""FastAPI application factory for the Control Node."""

from fastapi import FastAPI

from dlr.control.api import (
    adapters,
    auth,
    credentials,
    events,
    executions,
    health,
    package_sources,
    workers,
)


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
    app = FastAPI(title="DLR Control", version="0.0.1")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(adapters.router)
    app.include_router(credentials.router)
    app.include_router(package_sources.router)
    app.include_router(executions.router)
    app.include_router(workers.router)
    app.include_router(workers.admin_router)
    app.include_router(events.router)
    return app
