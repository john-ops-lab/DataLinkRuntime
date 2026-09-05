"""Fresh runtime schema and explicit refusal of unsupported old installations."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from dlr.common.config import settings

HEAD_REVISION = "0033_unified_execution"


def _upgrade(database: str, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url",
        make_url(settings.database_url)
        .set(database=database)
        .render_as_string(hide_password=False),
    )
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


@contextmanager
def _isolated_schema(suffix: str, revision: str) -> Iterator[tuple[Engine, str]]:
    database = f"dlr_unify_migration_{suffix}"
    base = make_url(settings.database_url)
    maintenance = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = create_engine(base.set(database=database))
    try:
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))
            connection.execute(text(f"CREATE DATABASE {database}"))
        _upgrade(database, revision)
        yield engine, database
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))
        maintenance.dispose()


def test_fresh_head_has_only_reliable_execution_defaults_and_constraints() -> None:
    with _isolated_schema("fresh", "head") as (engine, _database), engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert connection.scalar(text("SELECT to_regclass('uq_executions_active_adapter')")) is None
        defaults = dict(
            connection.execute(
                text(
                    "SELECT column_name, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'executions' "
                    "AND column_name IN ('status', 'dispatch_backend', 'dispatch_generation')"
                )
            ).all()
        )
        assert "queued" in defaults["status"]
        assert "rabbitmq" in defaults["dispatch_backend"]
        assert defaults["dispatch_generation"] == "1"
        constraints = dict(
            connection.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid IN ('executions'::regclass, 'workers'::regclass) "
                    "AND contype = 'c'"
                )
            ).all()
        )
        assert "legacy" not in constraints["ck_executions_dispatch_backend"]
        assert "rabbitmq" in constraints["ck_executions_dispatch_backend"]
        assert "protocol_version = 3" in constraints["ck_workers_protocol_version"]
        assert "dispatch_generation >= 1" in constraints["ck_executions_dispatch_generation"]
        for table in ("execution_outbox", "execution_attempts", "adapter_execution_slots"):
            assert connection.scalar(text("SELECT to_regclass(:table)"), {"table": table}) == table


@pytest.mark.parametrize("legacy_kind", ["worker", "execution"])
def test_upgrade_rejects_old_runtime_data_without_modifying_it(legacy_kind: str) -> None:
    with _isolated_schema(legacy_kind, "0032_issue132_templates") as (engine, database):
        with engine.begin() as connection:
            if legacy_kind == "worker":
                connection.execute(
                    text(
                        "INSERT INTO workers (name, status, capabilities) "
                        "VALUES ('old-worker', 'offline', '[\"python\"]'::jsonb)"
                    )
                )
            else:
                adapter_id = connection.scalar(
                    text(
                        "INSERT INTO adapters (name, language, adapter_type, run_mode) "
                        "VALUES ('old-adapter', 'python', 'task', 'manual') RETURNING id"
                    )
                )
                version_id = connection.scalar(
                    text(
                        "INSERT INTO adapter_versions (adapter_id, seq, code) "
                        "VALUES (:adapter_id, 1, 'pass') RETURNING id"
                    ),
                    {"adapter_id": adapter_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO executions (adapter_id, version_id, trigger, input, "
                        "input_source_type, input_config_revision, input_snapshot) "
                        "VALUES (:adapter_id, :version_id, 'manual', '{}'::jsonb, "
                        "'json', 1, '{}'::jsonb)"
                    ),
                    {"adapter_id": adapter_id, "version_id": version_id},
                )
        with pytest.raises(RuntimeError, match="legacy"):
            _upgrade(database, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0032_issue132_templates"
            )
            table = "workers" if legacy_kind == "worker" else "executions"
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
