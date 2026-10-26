"""Database engine, session factory and connectivity probe for the Control Node."""

import logging
from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dlr.common.config import settings

logger = logging.getLogger("dlr.control.db")

# Ingress owns a separate eight-connection pool. All management, recovery and
# maintenance work shares this finite reserve, without hidden overflow.
engine: Engine = create_engine(
    settings.database_url,
    pool_size=4,
    max_overflow=0,
    pool_timeout=1,
    connect_args={
        "connect_timeout": 5,
        "options": "-c statement_timeout=5000 -c lock_timeout=1000",
    },
)
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
