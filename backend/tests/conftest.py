"""Shared test fixtures: dedicated PostgreSQL test database and API client.

M1 backend tests run against real PostgreSQL (never SQLite) so JSONB, FK and
migration behavior is exercised for real. The suite provisions a dedicated
``dlr_test`` database, applies the actual Alembic migration chain and resets
table contents between tests.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control import db
from dlr.control.app import create_app
from dlr.control.services import events as events_service

TEST_DATABASE = "dlr_test"

# M2: fixed test tokens. conftest configures them on the settings singleton
# so every protected endpoint is exercised with real authentication in place.
ADMIN_TOKEN = "test-admin-token"
WORKER_TOKEN = "test-worker-token"

settings.admin_token = ADMIN_TOKEN
settings.worker_token = WORKER_TOKEN


def _base_url() -> URL:
    """Configured database URL without the database name component."""
    return make_url(settings.database_url).set(database=None)


def _maintenance_engine() -> Engine:
    return create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    """Provision the test database and apply real migrations once per run."""
    test_url = _base_url().set(database=TEST_DATABASE)

    maintenance = _maintenance_engine()
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    maintenance.dispose()

    alembic_config = Config()
    alembic_config.set_main_option("script_location", "alembic")
    alembic_config.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False))
    # env.py lets DATABASE_URL override the migration target; keep the
    # migration pointed at the test database during this run.
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(alembic_config, "head")
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url

    engine = create_engine(test_url)
    yield engine
    engine.dispose()

    maintenance = _maintenance_engine()
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def _truncate(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE adapters, adapter_versions, workers, executions "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture(scope="session")
def session_factory(test_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture()
def api_client(
    test_engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """API client wired to the test database via dependency override.

    Requests carry the admin bearer token by default, so the M1 suite keeps
    working with M2 authentication in place; worker endpoints are called
    with an explicit Worker token header in their own tests.
    """
    _truncate(test_engine)

    def override_get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    # M3: the SSE event stream owns its own session (the response outlives
    # the request handler), so it cannot go through the dependency override;
    # point its session factory at the test database as well.
    monkeypatch.setattr(events_service, "SessionLocal", session_factory)

    app = create_app()
    app.dependency_overrides[db.get_session] = override_get_session
    yield TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    app.dependency_overrides.clear()
    _truncate(test_engine)
