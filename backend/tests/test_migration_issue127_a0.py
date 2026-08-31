"""Issue #127 A0 expand migration and fail-fast backfill tests."""

import importlib.util
import os
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_i127_a0_datalinkruntime_119_migration"
FRESH_DATABASE = "dlr_i127_a0_datalinkruntime_119_fresh"
PREVIOUS_REVISION = "0025_default_ai_tools_enabled"
FINAL_REVISION = "0026_issue127_a0_input_config"


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


@pytest.fixture()
def fresh_engine() -> Iterator[Engine]:
    url = _base_url().set(database=FRESH_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {FRESH_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {FRESH_DATABASE}"))
    maintenance.dispose()

    # Keep this fixture pinned to the A0 boundary; later changes advance the
    # repository head without changing the A0 migration contract under test.
    _upgrade(_alembic_config(url), FINAL_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {FRESH_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def _migration_module() -> ModuleType:
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "alembic",
        "versions",
        "0026_issue127_a0_input_config.py",
    )
    name = "issue127_a0_migration_for_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _insert_legacy_fixtures(engine: Engine) -> tuple[int, int, int]:
    with engine.begin() as connection:
        manual_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters "
                    "(name, language, adapter_type, run_mode) "
                    "VALUES ('a0-legacy-manual', 'python', 'task', 'manual') RETURNING id"
                )
            )
        )
        scheduled_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters "
                    "(name, language, adapter_type, run_mode) "
                    "VALUES ('a0-legacy-scheduled', 'python', 'task', 'schedule') RETURNING id"
                )
            )
        )
        webhook_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters "
                    "(name, language, adapter_type, run_mode) "
                    "VALUES ('a0-legacy-webhook', 'python', 'webhook', 'manual') RETURNING id"
                )
            )
        )
        connection.execute(
            text(
                "INSERT INTO adapter_schedules "
                "(adapter_id, cron, timezone, input, enabled) "
                "VALUES (:adapter_id, '0 * * * *', 'UTC', '{\"from\":\"schedule\"}', false)"
            ),
            {"adapter_id": scheduled_id},
        )
    return manual_id, scheduled_id, webhook_id


def test_fresh_head_creates_a0_schema(fresh_engine: Engine) -> None:
    inspector = inspect(fresh_engine)

    assert "adapter_input_configs" in inspector.get_table_names()
    assert {
        "adapter_id",
        "source_type",
        "json_value",
        "retention_mode",
        "retention_seconds",
        "revision",
    }.issubset({column["name"] for column in inspector.get_columns("adapter_input_configs")})
    assert {
        "last_blocked_reason",
        "last_blocked_detail",
        "last_blocked_at",
        "last_processed_due_at",
    }.issubset({column["name"] for column in inspector.get_columns("adapter_schedules")})
    assert {
        "ix_adapter_input_configs_source_type",
        "ix_adapter_input_configs_revision",
    }.issubset({index["name"] for index in inspector.get_indexes("adapter_input_configs")})
    with fresh_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == FINAL_REVISION
        assert connection.scalar(text("SELECT count(*) FROM adapter_input_configs")) == 0


def test_fixed_base_upgrade_backfills_and_preserves_legacy_input(
    legacy_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    manual_id, scheduled_id, webhook_id = _insert_legacy_fixtures(legacy_engine)

    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)

    with legacy_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT adapter_id, source_type, json_value, revision "
                    "FROM adapter_input_configs ORDER BY adapter_id"
                )
            )
            .mappings()
            .all()
        )
        assert rows == [
            {
                "adapter_id": manual_id,
                "source_type": "json",
                "json_value": {},
                "revision": 1,
            },
            {
                "adapter_id": scheduled_id,
                "source_type": "json",
                "json_value": {"from": "schedule"},
                "revision": 1,
            },
        ]
        assert connection.scalar(
            text("SELECT input FROM adapter_schedules WHERE adapter_id = :id"),
            {"id": scheduled_id},
        ) == {"from": "schedule"}
        assert (
            connection.scalar(
                text("SELECT count(*) FROM adapter_input_configs WHERE adapter_id = :id"),
                {"id": webhook_id},
            )
            == 0
        )
        schedule_columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'adapter_schedules'"
                )
            )
        }
        assert {
            "last_blocked_reason",
            "last_blocked_detail",
            "last_blocked_at",
            "last_processed_due_at",
        }.issubset(schedule_columns)

    captured = capsys.readouterr().out
    assert "adapters=2" in captured
    assert 'source_type_counts={"json":2}' in captured
    assert "conflicts=0" in captured


def test_backfill_is_idempotent_and_count_stable(legacy_engine: Engine) -> None:
    _insert_legacy_fixtures(legacy_engine)
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    migration = _migration_module()

    with legacy_engine.begin() as connection:
        first = migration.backfill_input_configs(connection)
        second = migration.backfill_input_configs(connection)

    assert first.adapter_count == second.adapter_count == 2
    assert first.source_type_counts == second.source_type_counts == {"json": 2}
    assert first.conflict_count == second.conflict_count == 0
    assert first.inserted_count == 0
    assert second.inserted_count == 0


def test_backfill_fails_fast_for_existing_conflict(legacy_engine: Engine) -> None:
    manual_id, _, _ = _insert_legacy_fixtures(legacy_engine)
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    migration = _migration_module()

    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE adapter_input_configs SET source_type='none', json_value=NULL "
                "WHERE adapter_id=:adapter_id"
            ),
            {"adapter_id": manual_id},
        )
        with pytest.raises(RuntimeError, match=f"adapter_id={manual_id}:input_config_conflict"):
            migration.backfill_input_configs(connection)


def test_backfill_fails_fast_for_duplicate_schedule(legacy_engine: Engine) -> None:
    _, scheduled_id, _ = _insert_legacy_fixtures(legacy_engine)
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    migration = _migration_module()

    with legacy_engine.begin() as connection:
        unique_constraint = connection.scalar(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='adapter_schedules'::regclass "
                "AND contype='u' AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
                "WHERE attrelid='adapter_schedules'::regclass AND attname='adapter_id')]"
            )
        )
        assert unique_constraint is not None
        connection.execute(
            text(f'ALTER TABLE adapter_schedules DROP CONSTRAINT "{unique_constraint}"')
        )
        connection.execute(
            text(
                "INSERT INTO adapter_schedules "
                "(adapter_id, cron, timezone, input, enabled) "
                "VALUES (:adapter_id, '*/5 * * * *', 'UTC', 'null', false)"
            ),
            {"adapter_id": scheduled_id},
        )
        with pytest.raises(
            RuntimeError, match=f"adapter_id={scheduled_id}:duplicate_schedule_rows=2"
        ):
            migration.backfill_input_configs(connection)


def test_backfill_fails_fast_for_orphan_schedule(legacy_engine: Engine) -> None:
    _insert_legacy_fixtures(legacy_engine)
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    migration = _migration_module()

    with legacy_engine.begin() as connection:
        foreign_key = connection.scalar(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='adapter_schedules'::regclass AND contype='f'"
            )
        )
        assert foreign_key is not None
        connection.execute(text(f'ALTER TABLE adapter_schedules DROP CONSTRAINT "{foreign_key}"'))
        connection.execute(
            text(
                "INSERT INTO adapter_schedules "
                "(adapter_id, cron, timezone, input, enabled) "
                "VALUES (999999, '0 * * * *', 'UTC', 'null', false)"
            )
        )
        with pytest.raises(RuntimeError, match="adapter_id=999999:orphan_schedule"):
            migration.backfill_input_configs(connection)


def test_backfill_fails_fast_for_invalid_source(legacy_engine: Engine) -> None:
    manual_id, _, _ = _insert_legacy_fixtures(legacy_engine)
    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)
    migration = _migration_module()

    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE adapter_input_configs "
                "DROP CONSTRAINT ck_adapter_input_configs_source_type"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE adapter_input_configs "
                "DROP CONSTRAINT ck_adapter_input_configs_source_fields"
            )
        )
        connection.execute(
            text(
                "UPDATE adapter_input_configs SET source_type='future_source' "
                "WHERE adapter_id=:adapter_id"
            ),
            {"adapter_id": manual_id},
        )
        with pytest.raises(RuntimeError, match=f"adapter_id={manual_id}:invalid_source"):
            migration.backfill_input_configs(connection)
