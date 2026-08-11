"""Database engine and connectivity probe for the Control Node."""

import logging

from sqlalchemy import Engine, create_engine, text

from dlr.common.config import settings

logger = logging.getLogger("dlr.control.db")

engine: Engine = create_engine(settings.database_url)


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
