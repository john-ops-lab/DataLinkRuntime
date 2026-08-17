"""M5.5.15 dependency defaults are additive, idempotent and non-destructive."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_5_15"
PREVIOUS_REVISION = "0012_m5_5_9_active_name_unique"
FINAL_REVISION = "0016_m5_5_15_dep_sources"

CANONICAL = {
    "pypi": {
        "domestic": ("阿里云 PyPI 镜像", "https://mirrors.aliyun.com/pypi/simple/"),
        "official": ("官方 PyPI", "https://pypi.org/simple/"),
    },
    "npm": {
        "domestic": ("npmmirror npm 镜像", "https://registry.npmmirror.com/"),
        "official": ("npm 官方源", "https://registry.npmjs.org/"),
    },
    "maven": {
        "domestic": ("阿里云 Maven 公共仓库", "https://maven.aliyun.com/repository/public"),
        "official": ("Maven Central", "https://repo1.maven.org/maven2/"),
    },
}


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


def _rows(engine: Engine) -> list[tuple[str, str, str, bool]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT name, kind, index_url, is_default "
                    "FROM package_sources ORDER BY kind, index_url"
                )
            )
        )


def test_upgrade_seeds_two_defaults_per_kind(previous_engine: Engine) -> None:
    _upgrade(_alembic_config(_base_url().set(database=MIGRATION_DATABASE)), FINAL_REVISION)

    rows = _rows(previous_engine)
    assert len(rows) == 6
    for kind, defaults in CANONICAL.items():
        matching = [row for row in rows if row[1] == kind]
        assert {(row[0], row[2]) for row in matching} == set(defaults.values())
        domestic_url = defaults["domestic"][1]
        assert next(row[3] for row in matching if row[2] == domestic_url) is True
        assert next(row[3] for row in matching if row[2] != domestic_url) is False


def test_upgrade_adds_defaults_without_overwriting_custom_rows(previous_engine: Engine) -> None:
    with previous_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO package_sources (name, kind, index_url, is_default) "
                "VALUES ('company-pypi', 'pypi', 'https://pypi.example.com/simple/', true)"
            )
        )
    config = _alembic_config(_base_url().set(database=MIGRATION_DATABASE))
    _upgrade(config, FINAL_REVISION)

    rows = _rows(previous_engine)
    custom = next(row for row in rows if row[0] == "company-pypi")
    assert custom == ("company-pypi", "pypi", "https://pypi.example.com/simple/", True)
    assert len(rows) == 7
    assert sum(row[3] for row in rows if row[1] == "pypi") == 1

    # Downgrade + upgrade is also additive: no canonical or user row is removed
    # and a second upgrade does not create duplicate system rows.
    _upgrade(config, PREVIOUS_REVISION)
    _upgrade(config, FINAL_REVISION)
    assert _rows(previous_engine) == rows
