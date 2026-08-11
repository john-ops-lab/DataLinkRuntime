"""Database engine, session factory and connectivity probe for the Control Node."""

import logging
from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dlr.common.config import settings

logger = logging.getLogger("dlr.control.db")

engine: Engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base shared by all Control Node models."""


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Return True when the database is reachable.

    Failures are logged with diagnostics on the server side; the health API
    intentionally exposes only the boolean result.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # a health probe must never raise
        logger.exception("database health check failed")
        return False
    return True
