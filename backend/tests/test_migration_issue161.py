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

PREVIOUS_REVISION = "0041_issue161_cache_guards"
LEGACY_REVISION = "0043_issue161_legacy_replacement"
FINAL_REVISION = "0044_issue161_cache_admin"


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


def test_upgrade_adds_independent_operation_table_and_backfills_active_guard() -> None:
    with _isolated_schema("issue161_upgrade", PREVIOUS_REVISION) as (engine, database):
        operation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO worker_cache_guards "
                    "(worker_id, version_id, adapter_id, generation, operation_id, phase) "
                    "VALUES (101, 202, 303, 4, :operation_id, 'acquired')"
                ),
                {"operation_id": operation_id},
            )
        _upgrade(database, "head")
        schema = inspect(engine)
        assert "worker_cache_operations" in schema.get_table_names()
        assert "worker_cache_management_operations" in schema.get_table_names()
        assert "worker_cache_snapshots" in schema.get_table_names()
        assert "worker_cache_snapshot_items" in schema.get_table_names()
        assert schema.get_foreign_keys("worker_cache_operations") == []
        assert {
            item["name"] for item in schema.get_check_constraints("worker_cache_operations")
        } >= {
            "ck_worker_cache_operations_generation_positive",
            "ck_worker_cache_operations_phase",
            "ck_worker_cache_operations_kind",
            "ck_worker_cache_operations_cleanup_pair",
            "ck_worker_cache_operations_replacement_context",
        }
        assert {column["name"] for column in schema.get_columns("worker_cache_operations")} >= {
            "operation_kind",
            "cleanup_id",
            "cleanup_claim_attempt",
            "observed_identity",
            "replacement_context",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                FINAL_REVISION
            )
            operation = connection.execute(
                text(
                    "SELECT operation_id, worker_id, version_id, adapter_id, generation, phase, "
                    "operation_kind, cleanup_id, replacement_context "
                    "FROM worker_cache_operations"
                )
            ).one()
            assert operation == (operation_id, 101, 202, 303, 4, "acquired", "gc", None, None)


def test_downgrade_refuses_unfinished_operation_then_allows_terminal() -> None:
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
            connection.execute(
                text(
                    "INSERT INTO worker_cache_operations "
                    "(operation_id, worker_id, version_id, adapter_id, generation, phase) "
                    "VALUES (:operation_id, 101, 202, 303, 1, 'acquired')"
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
            connection.execute(
                text(
                    "UPDATE worker_cache_operations SET phase = 'aborted', finished_at = now() "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
        _downgrade(database, PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                PREVIOUS_REVISION
            )
            assert connection.scalar(text("SELECT to_regclass('worker_cache_operations')")) is None
            assert connection.scalar(text("SELECT to_regclass('worker_cache_guards')")) is not None


@pytest.mark.parametrize("operation_kind", ["cleanup", "replacement"])
def test_0043_downgrade_refuses_unfinished_specialized_operation(
    operation_kind: str,
) -> None:
    with _isolated_schema(f"issue161_0043_{operation_kind}", "head") as (engine, database):
        operation_id = uuid.uuid4()
        cleanup_id = 404 if operation_kind == "cleanup" else None
        claim_attempt = 2 if operation_kind == "cleanup" else None
        replacement_context = '{"attempt_id": 505}' if operation_kind == "replacement" else None
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO worker_cache_operations "
                    "(operation_id, worker_id, version_id, adapter_id, generation, phase, "
                    "operation_kind, cleanup_id, cleanup_claim_attempt, replacement_context) "
                    "VALUES (:operation_id, 101, 202, 303, 1, 'acquired', :operation_kind, "
                    ":cleanup_id, :claim_attempt, CAST(:replacement_context AS jsonb))"
                ),
                {
                    "operation_id": operation_id,
                    "operation_kind": operation_kind,
                    "cleanup_id": cleanup_id,
                    "claim_attempt": claim_attempt,
                    "replacement_context": replacement_context,
                },
            )

        with pytest.raises(RuntimeError, match="cleanup or replacement"):
            _downgrade(database, "0042_issue161_cache_operations")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                FINAL_REVISION
            )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE worker_cache_operations SET phase = 'aborted', finished_at = now() "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
        _downgrade(database, "0042_issue161_cache_operations")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0042_issue161_cache_operations"
            )


def test_0044_downgrade_refuses_active_management_operation() -> None:
    with _isolated_schema("issue161_0044_admin", "head") as (engine, database):
        operation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workers (id, name, status, capabilities, protocol_version) "
                    "VALUES (707, 'cache-admin-worker', 'online', '[]'::jsonb, 3)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO worker_cache_management_operations "
                    "(operation_id, worker_id, kind, status, idempotency_key, request_hash, "
                    "request_payload, actor) VALUES (:operation_id, 707, 'preview', 'pending', "
                    "'migration-test', :request_hash, '{}'::jsonb, '{}'::jsonb)"
                ),
                {"operation_id": operation_id, "request_hash": "a" * 64},
            )
        with pytest.raises(RuntimeError, match="unfinished"):
            _downgrade(database, LEGACY_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE worker_cache_management_operations SET status='completed', "
                    "finished_at=now() WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        _downgrade(database, LEGACY_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                LEGACY_REVISION
            )
