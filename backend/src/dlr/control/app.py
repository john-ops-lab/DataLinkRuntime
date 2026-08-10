"""FastAPI application factory for the Control Node."""

from fastapi import FastAPI

from dlr.control.api import health


def create_app() -> FastAPI:
    """Create the Control Node FastAPI application."""
    app = FastAPI(title="DLR Control", version="0.0.1")
    app.include_router(health.router)
    return app
