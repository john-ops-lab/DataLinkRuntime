"""M5.1 migration compatibility: a real 0005 database survives 0006.

Provisions a dedicated database at the legacy ``0005_m4_ai_editor`` revision,
seeds it with legacy running Adapters and legacy ``trigger='production'``
Executions exactly as M3.2 stored them, then upgrades to head and verifies the
``production_version_id`` backfill:

- active legacy production Execution wins,
- without an active one the latest legacy production Execution wins,
- without any production Execution the published version is used,
- non-running Adapters keep NULL.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_test_migration_m5_1"
LEGACY_REVISION = "0005_m4_ai_editor"


def _base_url() -> URL:
    """Configured database URL without the database name component."""
    return make_url(settings.database_url).set(database=None)


def _alembic_config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def _upgrade(config: Config, revision: str) -> None:
    # env.py lets DATABASE_URL override the migration target; keep the
    # migration pointed at the migration database during this run.
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url


@pytest.fixture()
def legacy_engine() -> Iterator[Engine]:
    """A fresh database upgraded to the legacy 0005 revision."""
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()

    _upgrade(_alembic_config(url), LEGACY_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def test_upgrade_from_0005_recovers_production_version_for_running_adapters(
    legacy_engine: Engine,
) -> None:
    adapter_ids: dict[str, int] = {}
    version_ids: dict[str, int] = {}
    with legacy_engine.begin() as conn:
        for name in (
            "legacy-active-exec",
            "legacy-fallback-latest",
            "legacy-fallback-published",
            "legacy-stopped",
            "legacy-idle",
        ):
            adapter_ids[name] = int(
                conn.scalar(
                    text(
                        "INSERT INTO adapters (name, language) VALUES (:n, 'python') RETURNING id"
                    ),
                    {"n": name},
                )
            )

        def add_version(adapter_name: str, seq: int) -> int:
            return int(
                conn.scalar(
                    text(
                        "INSERT INTO adapter_versions (adapter_id, seq, code) "
                        "VALUES (:a, :s, 'print(1)') RETURNING id"
                    ),
                    {"a": adapter_ids[adapter_name], "s": seq},
                )
            )

        def add_production_execution(adapter_name: str, version_id: int, status: str) -> None:
            conn.execute(
                text(
                    "INSERT INTO executions (adapter_id, version_id, trigger, status, input) "
                    "VALUES (:a, :v, 'production', :s, '{}'::jsonb)"
                ),
                {"a": adapter_ids[adapter_name], "v": version_id, "s": status},
            )

        def configure(
            adapter_name: str,
            *,
            published: int | None = None,
            production_state: str = "idle",
        ) -> None:
            conn.execute(
                text(
                    "UPDATE adapters SET latest_version_id = :latest, "
                    "published_version_id = :published, production_state = :state "
                    "WHERE id = :id"
                ),
                {
                    "latest": published,
                    "published": published,
                    "state": production_state,
                    "id": adapter_ids[adapter_name],
                },
            )

        # Running with an active legacy production Execution: it wins.
        version_ids["active-old"] = add_version("legacy-active-exec", 1)
        version_ids["active-locked"] = add_version("legacy-active-exec", 2)
        configure(
            "legacy-active-exec",
            published=version_ids["active-old"],
            production_state="running",
        )
        add_production_execution("legacy-active-exec", version_ids["active-old"], "succeeded")
        add_production_execution("legacy-active-exec", version_ids["active-locked"], "running")

        # Running without an active Execution: the latest (by id) legacy
        # production Execution wins over the published pointer. The newest
        # Execution runs the NON-published version so the two sources are
        # distinguishable.
        version_ids["fallback-published"] = add_version("legacy-fallback-latest", 1)
        version_ids["fallback-latest"] = add_version("legacy-fallback-latest", 2)
        configure(
            "legacy-fallback-latest",
            published=version_ids["fallback-published"],
            production_state="running",
        )
        add_production_execution(
            "legacy-fallback-latest", version_ids["fallback-published"], "succeeded"
        )
        add_production_execution(
            "legacy-fallback-latest", version_ids["fallback-latest"], "succeeded"
        )

        # Running without any production Execution: published version fallback.
        version_ids["published-only"] = add_version("legacy-fallback-published", 1)
        configure(
            "legacy-fallback-published",
            published=version_ids["published-only"],
            production_state="running",
        )

        # Stopped with production history: stays NULL.
        version_ids["stopped"] = add_version("legacy-stopped", 1)
        configure("legacy-stopped", published=version_ids["stopped"], production_state="stopped")
        add_production_execution("legacy-stopped", version_ids["stopped"], "succeeded")

        # Idle and never published: stays NULL.
        configure("legacy-idle")

    # The real upgrade under test is deliberately bounded to 0006. Later
    # migrations may retire these compatibility columns.
    _upgrade(
        _alembic_config(_base_url().set(database=MIGRATION_DATABASE)),
        "0006_m5_1_production_entry",
    )

    with legacy_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT name, production_state, production_version_id FROM adapters ORDER BY name")
        ).all()
    recovered = {row.name: row.production_version_id for row in rows}

    assert recovered["legacy-active-exec"] == version_ids["active-locked"], (
        "an active legacy production Execution must win the backfill"
    )
    assert recovered["legacy-fallback-latest"] == version_ids["fallback-latest"], (
        "without an active Execution the newest legacy Execution wins over the published pointer"
    )
    assert recovered["legacy-fallback-published"] == version_ids["published-only"], (
        "without production Executions the published version is recovered"
    )
    assert recovered["legacy-stopped"] is None, "non-running Adapters keep NULL"
    assert recovered["legacy-idle"] is None, "idle Adapters keep NULL"
