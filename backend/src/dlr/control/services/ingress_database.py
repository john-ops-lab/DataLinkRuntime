"""One finite intake pool shared by HTTP and native source owners."""

from __future__ import annotations

import threading

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

_lock = threading.Lock()
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def intake_sessions(url: str | URL) -> sessionmaker[Session]:
    global _engine, _factory
    with _lock:
        if _factory is None:
            _engine = create_engine(
                url,
                pool_size=8,
                max_overflow=0,
                pool_timeout=1,
                pool_pre_ping=True,
                connect_args={
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=5000 -c lock_timeout=1000",
                    "keepalives": 1,
                    "keepalives_idle": 2,
                    "keepalives_interval": 1,
                    "keepalives_count": 3,
                    "tcp_user_timeout": 5000,
                },
            )
            _factory = sessionmaker(bind=_engine, expire_on_commit=False)
        return _factory


def close_intake_database() -> None:
    """Only after native owners and HTTP jobs have actually finished."""
    global _engine, _factory
    with _lock:
        engine, _engine, _factory = _engine, None, None
    if engine is not None:
        engine.dispose()
