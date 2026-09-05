"""Issue #132 additive Adapter template-provenance migration contracts."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import IntegrityError

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_issue132"
PREVIOUS_REVISION = "0031_issue130_b2_runtime"
FINAL_REVISION = "0032_issue132_templates"


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


def test_upgrade_from_0031_preserves_adapters_and_enforces_paired_origin(
    legacy_engine: Engine,
) -> None:
    with legacy_engine.begin() as connection:
        old_id = connection.scalar(
            text(
                "INSERT INTO adapters (name, language, adapter_type) "
                "VALUES ('pre-template-adapter', 'python', 'task') RETURNING id"
            )
        )

    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("adapters")}
    assert {"template_scenario_slug", "template_version"}.issubset(columns)
    with legacy_engine.connect() as connection:
        preserved = connection.execute(
            text(
                "SELECT template_scenario_slug, template_version "
                "FROM adapters WHERE id = :adapter_id"
            ),
            {"adapter_id": old_id},
        ).one()
        assert preserved == (None, None)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == FINAL_REVISION

    with pytest.raises(IntegrityError), legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO adapters "
                "(name, language, adapter_type, template_scenario_slug) "
                "VALUES ('half-origin-slug', 'python', 'task', 'rest-single-request')"
            )
        )
    with pytest.raises(IntegrityError), legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO adapters "
                "(name, language, adapter_type, template_version) "
                "VALUES ('half-origin-version', 'python', 'task', '1.0.0')"
            )
        )

    with legacy_engine.begin() as connection:
        created = connection.execute(
            text(
                "INSERT INTO adapters "
                "(name, language, adapter_type, template_scenario_slug, template_version) "
                "VALUES ('paired-origin', 'javascript', 'task', "
                "'rest-single-request', '1.0.0') "
                "RETURNING template_scenario_slug, template_version"
            )
        ).one()
        assert created == ("rest-single-request", "1.0.0")


def test_fresh_head_has_nullable_paired_template_columns(test_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(test_engine).get_columns("adapters")}
    assert {"template_scenario_slug", "template_version"}.issubset(columns)
    checks = {item["name"] for item in inspect(test_engine).get_check_constraints("adapters")}
    assert "ck_adapters_template_provenance_pair" in checks

    with test_engine.begin() as connection:
        values = connection.execute(
            text(
                "INSERT INTO adapters (name, language, adapter_type) "
                "VALUES ('fresh-null-origin', 'java', 'task') "
                "RETURNING template_scenario_slug, template_version"
            )
        ).one()
        assert values == (None, None)


def test_0032_downgrade_and_reupgrade_preserve_adapter_but_drop_origin(
    legacy_engine: Engine,
) -> None:
    config = _alembic_config(legacy_engine.url)
    _upgrade(config, FINAL_REVISION)
    with legacy_engine.begin() as connection:
        adapter_id = connection.scalar(
            text(
                "INSERT INTO adapters "
                "(name, language, adapter_type, template_scenario_slug, template_version) "
                "VALUES ('downgrade-origin', 'python', 'task', 'csv-to-json', '1.0.0') "
                "RETURNING id"
            )
        )

    _downgrade(config, PREVIOUS_REVISION)
    downgraded_columns = {
        column["name"] for column in inspect(legacy_engine).get_columns("adapters")
    }
    assert "template_scenario_slug" not in downgraded_columns
    assert "template_version" not in downgraded_columns
    with legacy_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT name FROM adapters WHERE id = :adapter_id"),
                {"adapter_id": adapter_id},
            )
            == "downgrade-origin"
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            PREVIOUS_REVISION
        )

    _upgrade(config, FINAL_REVISION)
    with legacy_engine.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT template_scenario_slug, template_version "
                "FROM adapters WHERE id = :adapter_id"
            ),
            {"adapter_id": adapter_id},
        ).one()
        assert restored == (None, None)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            FINAL_REVISION
        )
