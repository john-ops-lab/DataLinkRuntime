"""M5.5.8 migration compatibility: canonical dependency-source defaults.

The fresh-deployment seed must create exactly the three canonical default
sources on an empty table, and must never touch an existing deployment's
package sources (whether they keep their own defaults or deliberately
removed every source).
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_5_8"
PREVIOUS_REVISION = "0012_m5_5_9_active_name_unique"

CANONICAL = {
    "pypi": "https://mirrors.aliyun.com/pypi/simple/",
    "npm": "https://registry.npmmirror.com/",
    "maven": "https://maven.aliyun.com/repository/public",
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
    """A fresh database at the revision before the M5.5.8 seed."""
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


def _source_rows(engine: Engine) -> list[tuple[str, str, str, bool]]:
    with engine.connect() as conn:
        return [
            (name, kind, index_url, is_default)
            for name, kind, index_url, is_default in conn.execute(
                text("SELECT name, kind, index_url, is_default FROM package_sources ORDER BY kind")
            )
        ]


def test_fresh_deployment_seeds_three_default_sources(previous_engine: Engine) -> None:
    assert _source_rows(previous_engine) == []

    _upgrade(
        _alembic_config(_base_url().set(database=MIGRATION_DATABASE)),
        "0013_m5_5_8_dep_source_defaults",
    )

    rows = _source_rows(previous_engine)
    assert len(rows) == 3
    by_kind = {kind: (name, index_url, is_default) for name, kind, index_url, is_default in rows}
    for kind, url in CANONICAL.items():
        name, index_url, is_default = by_kind[kind]
        assert index_url == url, kind
        assert is_default is True, kind
        assert name, "seeded sources carry a readable name"


def test_upgrade_keeps_existing_sources_untouched(previous_engine: Engine) -> None:
    with previous_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO package_sources (name, kind, index_url, is_default) "
                "VALUES ('company-mirror', 'pypi', 'https://pypi.example.com/simple/', true)"
            )
        )

    _upgrade(
        _alembic_config(_base_url().set(database=MIGRATION_DATABASE)),
        "0013_m5_5_8_dep_source_defaults",
    )

    rows = _source_rows(previous_engine)
    assert len(rows) == 1
    assert rows[0] == (
        "company-mirror",
        "pypi",
        "https://pypi.example.com/simple/",
        True,
    ), "an existing deployment keeps exactly its own sources"


def test_upgrade_with_deliberately_removed_sources_stays_empty(previous_engine: Engine) -> None:
    """A deployment that cleared every source stays cleared after upgrading."""
    _upgrade(
        _alembic_config(_base_url().set(database=MIGRATION_DATABASE)),
        "0013_m5_5_8_dep_source_defaults",
    )
    assert len(_source_rows(previous_engine)) == 3

    with previous_engine.begin() as conn:
        conn.execute(text("DELETE FROM package_sources"))

    # Downgrade + upgrade again must not resurrect removed defaults.
    config = _alembic_config(_base_url().set(database=MIGRATION_DATABASE))
    _upgrade(config, PREVIOUS_REVISION)
    _upgrade(config, "0013_m5_5_8_dep_source_defaults")
    assert _source_rows(previous_engine) == []
