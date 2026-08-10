"""Database engine and connectivity probe for the Control Node."""

from sqlalchemy import Engine, create_engine, text

from dlr.common.config import settings

engine: Engine = create_engine(settings.database_url)


def check_database() -> bool:
    """Return True when the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # a health probe must never raise
        return False
    return True
