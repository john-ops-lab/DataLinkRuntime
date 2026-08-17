"""M5.5.7 migration compatibility: legacy access_key binding fields are
rewritten to the standardized access_key_id / access_key_secret names."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_5_7"
LEGACY_REVISION = "0011_m5_4_3_webhook_final_model"
FINAL_REVISION = "0012_m5_5_7_access_key_fields"


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
def legacy_binding_engine() -> Iterator[Engine]:
    """A dedicated database upgraded only to the pre-M5.5.7 revision."""
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


def test_legacy_access_key_bindings_are_rewritten_on_upgrade(
    legacy_binding_engine: Engine,
) -> None:
    with legacy_binding_engine.begin() as connection:
        adapter_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters (name, language, adapter_type) "
                    "VALUES ('legacy-ak-adapter', 'python', 'task') RETURNING id"
                )
            )
        )
        credential_id = int(
            connection.scalar(
                text(
                    "INSERT INTO credentials (name, type, ciphertext) "
                    "VALUES ('legacy-ak', 'access_key', 'legacy-ciphertext') RETURNING id"
                )
            )
        )
        connection.execute(
            text(
                "INSERT INTO adapter_credential_bindings "
                "(adapter_id, env_key, credential_id, field) VALUES "
                "(:adapter_id, 'AK_ID', :credential_id, 'access_key'), "
                "(:adapter_id, 'AK_SK', :credential_id, 'secret_key')"
            ),
            {"adapter_id": adapter_id, "credential_id": credential_id},
        )

    _upgrade(_alembic_config(legacy_binding_engine.url), FINAL_REVISION)

    with legacy_binding_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT env_key, field FROM adapter_credential_bindings "
                "WHERE adapter_id = :adapter_id ORDER BY env_key"
            ),
            {"adapter_id": adapter_id},
        ).all()
    assert rows == [("AK_ID", "access_key_id"), ("AK_SK", "access_key_secret")]
