"""M5.5.9 fresh-schema assertions: active-only Adapter name uniqueness."""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Engine


def test_fresh_schema_has_active_only_name_uniqueness(test_engine: Engine) -> None:
    with test_engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        index_definition = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = 'uq_adapters_active_name'"
            )
        )
        legacy_constraint = connection.scalar(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'adapters'::regclass AND conname = 'uq_adapters_name'"
            )
        )

    assert revision == "0018_m5_6_2_execution_locale"
    assert index_definition is not None
    assert "UNIQUE" in index_definition
    assert "archived_at IS NULL" in index_definition
    assert legacy_constraint is None


def test_database_allows_reusing_a_soft_deleted_name(test_engine: Engine) -> None:
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO adapters (name, description, language, adapter_type, run_mode, "
                "created_at, updated_at) "
                "VALUES ('reusable', '', 'python', 'task', 'manual', now(), now())"
            )
        )
        connection.execute(text("UPDATE adapters SET archived_at = now() WHERE name = 'reusable'"))
        connection.execute(
            text(
                "INSERT INTO adapters (name, description, language, adapter_type, run_mode, "
                "created_at, updated_at) "
                "VALUES ('reusable', '', 'python', 'task', 'manual', now(), now())"
            )
        )


def test_database_rejects_two_active_adapters_with_the_same_name(
    test_engine: Engine,
) -> None:
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO adapters (name, description, language, adapter_type, run_mode, "
                "created_at, updated_at) "
                "VALUES ('taken', '', 'python', 'task', 'manual', now(), now())"
            )
        )
        try:
            connection.execute(
                text(
                    "INSERT INTO adapters (name, description, language, adapter_type, run_mode, "
                    "created_at, updated_at) "
                    "VALUES ('taken', '', 'python', 'task', 'manual', now(), now())"
                )
            )
        except sa.exc.IntegrityError as exc:
            # The partial unique index is the concurrent create final defense.
            assert "uq_adapters_active_name" in str(exc.orig)
        else:
            raise AssertionError("duplicate active name was accepted by the database")
