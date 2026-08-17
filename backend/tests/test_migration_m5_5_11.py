"""M5.5.11 migration compatibility: the Adapter-level single-run execution
timeout column is added with the 300s default and the 1..86400 check
constraint."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_5_11"
LEGACY_REVISION = "0014_m5_5_7_access_key_fields"
FINAL_REVISION = "0015_m5_5_11_execution_timeout"


def _base_url() -> URL:
    return make_url(settings.database_url).set(database=None)


def _alembic_config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def _upgrade(config: Config, revision: str) -> None:
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url


@pytest.fixture()
def legacy_engine() -> Iterator[Engine]:
    """A dedicated database upgraded only to the pre-M5.5.11 revision."""
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()

    _upgrade(_alembic_config(url), LEGACY_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def test_upgrade_backfills_timeout_default_and_enforces_bounds(legacy_engine: Engine) -> None:
    with legacy_engine.begin() as connection:
        existing_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters (name, language, adapter_type) "
                    "VALUES ('legacy-timeout', 'python', 'task') RETURNING id"
                )
            )
        )

    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)

    with legacy_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT timeout_seconds FROM adapters WHERE id = :id"),
                {"id": existing_id},
            )
            == 300
        )

    # The check constraint is active: out-of-range values are rejected (a
    # fresh connection keeps the transaction state unambiguous).
    from sqlalchemy.exc import IntegrityError

    with (
        legacy_engine.connect() as connection,
        connection.begin(),
        pytest.raises(IntegrityError),
    ):
        connection.execute(
            text(
                "INSERT INTO adapters (name, language, adapter_type, timeout_seconds) "
                "VALUES ('too-long', 'python', 'task', 86401)"
            )
        )
