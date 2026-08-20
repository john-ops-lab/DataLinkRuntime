"""M5.9 Wave A migration upgrade and account table contract tests."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_9_wave_a"
PREVIOUS_REVISION = "0020_m5_8_006_ks_settings"
FINAL_REVISION = "0021_m5_9_wave_a_accounts"


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


def test_upgrade_creates_account_models_without_a_superadmin_row(legacy_engine: Engine) -> None:
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)

    with legacy_engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 0
        assert connection.scalar(text("SELECT count(*) FROM user_sessions")) == 0
        user_columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
                )
            )
        }
        assert user_columns == {
            "id",
            "username",
            "password_hash",
            "role",
            "enabled",
            "must_change_password",
            "created_at",
            "updated_at",
        }
        connection.execute(
            text(
                "INSERT INTO users (username, password_hash, role, must_change_password) "
                "VALUES ('migration-admin', 'scrypt$placeholder', 'admin', true)"
            )
        )
        user_id = connection.scalar(text("SELECT id FROM users WHERE username = 'migration-admin'"))
        connection.execute(
            text(
                "INSERT INTO user_sessions (user_id, session_hash, expires_at) "
                "VALUES (:user_id, :session_hash, now() + interval '1 hour')"
            ),
            {"user_id": user_id, "session_hash": "a" * 64},
        )
        connection.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
        assert connection.scalar(text("SELECT count(*) FROM user_sessions")) == 0
