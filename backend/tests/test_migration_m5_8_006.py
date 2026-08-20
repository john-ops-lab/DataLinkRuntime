"""M5.8-006 migration adds an empty, credential-referencing singleton."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_8_006"
PREVIOUS_REVISION = "0019_m5_6_3_preset_ids"
FINAL_REVISION = "0020_m5_8_006_ks_settings"


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


def _downgrade(config: Config, revision: str) -> None:
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.downgrade(config, revision)
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url


@pytest.fixture()
def previous_engine() -> Iterator[Engine]:
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()

    _upgrade(_alembic_config(url), PREVIOUS_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def test_upgrade_creates_empty_singleton_with_credential_fk(previous_engine: Engine) -> None:
    _upgrade(
        _alembic_config(_base_url().set(database=MIGRATION_DATABASE)),
        FINAL_REVISION,
    )

    with previous_engine.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM knowledge_source_settings")) == 0
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'knowledge_source_settings'"
                )
            )
        }
        assert columns == {
            "id",
            "source_id",
            "enabled",
            "credential_id",
            "created_at",
            "updated_at",
        }

        credential_id = conn.scalar(
            text(
                "INSERT INTO credentials (name, type, ciphertext) "
                "VALUES ('migration-ima-credential', 'access_key', 'ciphertext-sentinel') "
                "RETURNING id"
            )
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_source_settings (id, source_id, enabled, credential_id) "
                "VALUES (1, 'ima', true, :credential_id)"
            ),
            {"credential_id": credential_id},
        )
        assert (
            conn.scalar(text("SELECT credential_id FROM knowledge_source_settings WHERE id = 1"))
            == credential_id
        )

        conn.execute(
            text("DELETE FROM credentials WHERE id = :credential_id"),
            {"credential_id": credential_id},
        )
        assert (
            conn.scalar(text("SELECT credential_id FROM knowledge_source_settings WHERE id = 1"))
            is None
        )


def test_downgrade_removes_table_without_seeding_on_reupgrade(previous_engine: Engine) -> None:
    config = _alembic_config(_base_url().set(database=MIGRATION_DATABASE))
    _upgrade(config, FINAL_REVISION)
    _downgrade(config, PREVIOUS_REVISION)

    with previous_engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.knowledge_source_settings')")) is None

    _upgrade(config, FINAL_REVISION)
    with previous_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM knowledge_source_settings")) == 0
