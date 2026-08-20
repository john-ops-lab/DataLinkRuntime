"""M5.9 Wave C migration contract from the Wave A schema."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import IntegrityError

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_9_wave_c"
PREVIOUS_REVISION = "0021_m5_9_wave_a_accounts"
FINAL_REVISION = "0022_m5_9_wave_c_adapter_acl"


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
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()

    _upgrade(_alembic_config(url), PREVIOUS_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def test_wave_c_upgrade_preserves_system_ownership_and_enforces_acl_contract(
    legacy_engine: Engine,
) -> None:
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    with legacy_engine.begin() as connection:
        adapter_columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'adapters'"
                )
            )
        }
        assert "owner_user_id" in adapter_columns
        connection.execute(
            text(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ('wave-c-owner', 'scrypt$placeholder', 'user')"
            )
        )
        user_id = connection.scalar(text("SELECT id FROM users WHERE username = 'wave-c-owner'"))
        adapter_id = connection.scalar(
            text(
                "INSERT INTO adapters "
                "(name, language, adapter_type, owner_user_id) "
                "VALUES ('wave-c-adapter', 'python', 'task', :user_id) RETURNING id"
            ),
            {"user_id": user_id},
        )
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'adapter_permissions'"
                )
            )
        }
        assert columns == {"adapter_id", "user_id", "permission"}
        connection.execute(
            text(
                "INSERT INTO adapter_permissions (adapter_id, user_id, permission) "
                "VALUES (:adapter_id, :user_id, 'read')"
            ),
            {"adapter_id": adapter_id, "user_id": user_id},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO adapter_permissions (adapter_id, user_id, permission) "
                    "VALUES (:adapter_id, :user_id, 'edit')"
                ),
                {"adapter_id": adapter_id, "user_id": user_id},
            )
