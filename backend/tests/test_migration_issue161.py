"""Issue #161 stable cache guard migration contracts."""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from dlr.common.config import settings
from test_unified_runtime_migration import _isolated_schema, _upgrade

PREVIOUS_REVISION = "0040_issue152_dispositions"
FINAL_REVISION = "0041_issue161_cache_guards"


def _downgrade(database: str, revision: str) -> None:
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
        command.downgrade(config, revision)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def test_upgrade_adds_independent_empty_guard_table() -> None:
    with _isolated_schema("issue161_upgrade", PREVIOUS_REVISION) as (engine, database):
        _upgrade(database, "head")
        schema = inspect(engine)
        assert "worker_cache_guards" in schema.get_table_names()
        assert schema.get_foreign_keys("worker_cache_guards") == []
        assert {item["name"] for item in schema.get_check_constraints("worker_cache_guards")} >= {
            "ck_worker_cache_guards_generation_nonnegative",
            "ck_worker_cache_guards_operation_phase",
            "ck_worker_cache_guards_phase",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                FINAL_REVISION
            )
            assert connection.scalar(text("SELECT count(*) FROM worker_cache_guards")) == 0


def test_downgrade_refuses_unfinished_guard_then_allows_idle() -> None:
    with _isolated_schema("issue161_downgrade", "head") as (engine, database):
        operation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO worker_cache_guards "
                    "(worker_id, version_id, adapter_id, generation, operation_id, phase) "
                    "VALUES (101, 202, 303, 1, :operation_id, 'acquired')"
                ),
                {"operation_id": operation_id},
            )
        with pytest.raises(RuntimeError, match="unfinished"):
            _downgrade(database, PREVIOUS_REVISION)
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                FINAL_REVISION
            )
            connection.execute(
                text(
                    "UPDATE worker_cache_guards SET phase = 'idle', operation_id = NULL "
                    "WHERE worker_id = 101 AND version_id = 202"
                )
            )
        _downgrade(database, PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                PREVIOUS_REVISION
            )
            assert connection.scalar(text("SELECT to_regclass('worker_cache_guards')")) is None
