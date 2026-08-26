"""Issue #127 B0 fresh, upgrade, catalog, and cleanup migration gates."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url

from dlr.common.config import settings

MIGRATION_DATABASE = "dlr_i127_b0_schema_datalinkruntime_128_migration"
FRESH_DATABASE = "dlr_i127_b0_schema_datalinkruntime_128_fresh"
PREVIOUS_REVISION = "0027_issue127_a1_execution_input"
FINAL_REVISION = "0028_issue127_b0_managed_input"

B0_TABLES = {
    "managed_input_settings",
    "managed_input_capacity",
    "managed_input_upload_reservations",
    "managed_input_artifacts",
    "adapter_input_artifact_bindings",
    "artifact_deletion_jobs",
}

B0_INDEXES = {
    "ix_managed_input_upload_reservations_adapter_id",
    "ix_managed_input_upload_reservations_status_expires_at",
    "ix_managed_input_artifacts_adapter_id",
    "ix_managed_input_artifacts_created_by_user_id",
    "ix_managed_input_artifacts_status_expires_at",
    "ix_managed_input_artifacts_status_delete_lease_until",
    "ix_managed_input_artifacts_status_created_at",
    "ix_adapter_input_artifact_bindings_artifact_id",
    "ix_adapter_input_artifact_bindings_adapter_revision",
    "ix_artifact_deletion_jobs_status_lease_until",
    "ix_artifact_deletion_jobs_former_adapter_id",
}

B0_CHECKS = {
    "ck_managed_input_settings_singleton",
    "ck_managed_input_settings_default_retention_seconds",
    "ck_managed_input_settings_max_file_bytes",
    "ck_managed_input_settings_platform_quota_bytes",
    "ck_managed_input_settings_adapter_quota_bytes",
    "ck_managed_input_settings_adapter_quota_le_platform",
    "ck_managed_input_settings_max_custom_retention_seconds",
    "ck_managed_input_settings_custom_retention_ge_default",
    "ck_managed_input_settings_min_free_space_bytes",
    "ck_managed_input_settings_staged_ttl_seconds",
    "ck_managed_input_capacity_singleton",
    "ck_managed_input_capacity_actual_bytes",
    "ck_managed_input_capacity_reserved_bytes",
    "ck_managed_input_upload_reservations_status",
    "ck_managed_input_upload_reservations_reserved_bytes",
    "ck_managed_input_artifacts_status",
    "ck_managed_input_artifacts_size_bytes",
    "ck_managed_input_artifacts_retention_mode",
    "ck_managed_input_artifacts_delete_attempts",
    "ck_adapter_input_artifact_bindings_revision_positive",
    "ck_adapter_input_artifact_bindings_ordinal",
    "ck_artifact_deletion_jobs_status",
    "ck_artifact_deletion_jobs_size_bytes",
    "ck_artifact_deletion_jobs_charged_bytes",
    "ck_artifact_deletion_jobs_attempts",
}

B0_UNIQUES = {
    "uq_managed_input_upload_reservations_session",
    "uq_managed_input_upload_reservations_id_adapter",
    "uq_managed_input_artifacts_storage_key",
    "uq_managed_input_artifacts_upload_reservation",
    "uq_managed_input_artifacts_id_adapter",
    "uq_adapter_input_artifact_bindings_adapter_ordinal",
    "uq_artifact_deletion_jobs_storage_key",
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


def _downgrade(config: Config, revision: str) -> None:
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.downgrade(config, revision)
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url


def _create_database(name: str) -> None:
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            connection.execute(text(f"CREATE DATABASE {name}"))
    finally:
        maintenance.dispose()


def _drop_database(name: str) -> None:
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        maintenance.dispose()


@pytest.fixture()
def legacy_engine() -> Iterator[Engine]:
    url = _base_url().set(database=MIGRATION_DATABASE)
    _create_database(MIGRATION_DATABASE)
    engine: Engine | None = None
    try:
        _upgrade(_alembic_config(url), PREVIOUS_REVISION)
        engine = create_engine(url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(MIGRATION_DATABASE)


@pytest.fixture()
def fresh_engine() -> Iterator[Engine]:
    url = _base_url().set(database=FRESH_DATABASE)
    _create_database(FRESH_DATABASE)
    engine: Engine | None = None
    try:
        _upgrade(_alembic_config(url), FINAL_REVISION)
        engine = create_engine(url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(FRESH_DATABASE)


def test_fresh_upgrade_seeds_singletons_and_capacity_consistently(fresh_engine: Engine) -> None:
    inspector = inspect(fresh_engine)
    assert B0_TABLES.issubset(set(inspector.get_table_names()))

    with fresh_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == FINAL_REVISION
        settings_row = (
            connection.execute(
                text(
                    "SELECT id, default_retention_seconds, max_file_bytes, platform_quota_bytes, "
                    "adapter_quota_bytes, allow_manual_delete, max_custom_retention_seconds, "
                    "min_free_space_bytes, staged_ttl_seconds "
                    "FROM managed_input_settings"
                )
            )
            .mappings()
            .one()
        )
        assert dict(settings_row) == {
            "id": 1,
            "default_retention_seconds": 86_400,
            "max_file_bytes": 104_857_600,
            "platform_quota_bytes": 10 * 1024 * 1024 * 1024,
            "adapter_quota_bytes": 1024 * 1024 * 1024,
            "allow_manual_delete": True,
            "max_custom_retention_seconds": 2_592_000,
            "min_free_space_bytes": 1024 * 1024 * 1024,
            "staged_ttl_seconds": 3_600,
        }
        assert connection.execute(
            text("SELECT id, actual_bytes, reserved_bytes FROM managed_input_capacity")
        ).mappings().all() == [{"id": 1, "actual_bytes": 0, "reserved_bytes": 0}]
        assert connection.scalar(text("SELECT count(*) FROM managed_input_settings")) == 1
        assert connection.scalar(text("SELECT count(*) FROM managed_input_capacity")) == 1


def test_fixed_base_upgrade_preserves_legacy_rows_and_starts_empty_b0(
    legacy_engine: Engine,
) -> None:
    with legacy_engine.begin() as connection:
        adapter_id = connection.scalar(
            text(
                "INSERT INTO adapters (name, language, adapter_type, run_mode) "
                "VALUES ('b0-legacy-adapter', 'python', 'task', 'manual') RETURNING id"
            )
        )
        assert adapter_id is not None

    _upgrade(_alembic_config(legacy_engine.url), FINAL_REVISION)

    with legacy_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == FINAL_REVISION
        assert connection.scalar(text("SELECT count(*) FROM adapters")) == 1
        assert connection.scalar(text("SELECT count(*) FROM managed_input_artifacts")) == 0
        assert (
            connection.scalar(text("SELECT count(*) FROM managed_input_upload_reservations")) == 0
        )
        assert connection.scalar(text("SELECT count(*) FROM adapter_input_artifact_bindings")) == 0
        assert connection.scalar(text("SELECT count(*) FROM artifact_deletion_jobs")) == 0
        assert (
            connection.scalar(text("SELECT actual_bytes FROM managed_input_capacity WHERE id = 1"))
            == 0
        )


def test_repeated_upgrade_and_test_only_downgrade_are_reproducible(
    fresh_engine: Engine,
) -> None:
    config = _alembic_config(fresh_engine.url)
    with fresh_engine.connect() as connection:
        initial_settings = (
            connection.execute(text("SELECT * FROM managed_input_settings WHERE id = 1"))
            .mappings()
            .one()
        )
        initial_capacity = (
            connection.execute(text("SELECT * FROM managed_input_capacity WHERE id = 1"))
            .mappings()
            .one()
        )

    _upgrade(config, FINAL_REVISION)
    with fresh_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT * FROM managed_input_settings WHERE id = 1"))
            .mappings()
            .one()
            == initial_settings
        )
        assert (
            connection.execute(text("SELECT * FROM managed_input_capacity WHERE id = 1"))
            .mappings()
            .one()
            == initial_capacity
        )

    _downgrade(config, PREVIOUS_REVISION)
    with fresh_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS_REVISION
        )
        for table in B0_TABLES:
            assert (
                connection.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": table})
                is None
            )

    _upgrade(config, FINAL_REVISION)
    with fresh_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM managed_input_settings")) == 1
        assert connection.scalar(text("SELECT count(*) FROM managed_input_capacity")) == 1


def test_catalog_has_b0_constraints_and_gc_ttl_indexes(fresh_engine: Engine) -> None:
    inspector = inspect(fresh_engine)
    index_names = {index["name"] for table in B0_TABLES for index in inspector.get_indexes(table)}
    check_names = {
        constraint["name"]
        for table in B0_TABLES
        for constraint in inspector.get_check_constraints(table)
    }
    unique_names = {
        constraint["name"]
        for table in B0_TABLES
        for constraint in inspector.get_unique_constraints(table)
    }
    assert index_names >= B0_INDEXES
    assert check_names >= B0_CHECKS
    assert unique_names >= B0_UNIQUES

    assert inspector.get_pk_constraint("managed_input_settings")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("managed_input_capacity")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("managed_input_upload_reservations")[
        "constrained_columns"
    ] == ["id"]
    assert inspector.get_pk_constraint("managed_input_artifacts")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("adapter_input_artifact_bindings")[
        "constrained_columns"
    ] == ["adapter_id", "artifact_id"]
    assert inspector.get_pk_constraint("artifact_deletion_jobs")["constrained_columns"] == ["id"]

    foreign_keys = {
        (
            foreign_key["name"],
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for table in B0_TABLES
        for foreign_key in inspector.get_foreign_keys(table)
    }
    assert (
        "fk_managed_input_artifacts_upload_reservation_adapter",
        ("upload_reservation_id", "adapter_id"),
        "managed_input_upload_reservations",
        ("id", "adapter_id"),
    ) in foreign_keys
    assert (
        "fk_adapter_input_artifact_bindings_artifact_adapter",
        ("artifact_id", "adapter_id"),
        "managed_input_artifacts",
        ("id", "adapter_id"),
    ) in foreign_keys
    assert (
        "fk_adapter_input_artifact_bindings_input_config",
        ("adapter_id",),
        "adapter_input_configs",
        ("adapter_id",),
    ) in foreign_keys
    assert not inspector.get_foreign_keys("artifact_deletion_jobs")

    with fresh_engine.connect() as connection:
        index_definitions = dict(
            connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = ANY(:names)"
                ),
                {"names": list(B0_INDEXES)},
            ).all()
        )
    assert "status, expires_at" in index_definitions["ix_managed_input_artifacts_status_expires_at"]
    assert (
        "status, delete_lease_until"
        in index_definitions["ix_managed_input_artifacts_status_delete_lease_until"]
    )
    assert "status, created_at" in index_definitions["ix_managed_input_artifacts_status_created_at"]
    assert (
        "status, expires_at"
        in index_definitions["ix_managed_input_upload_reservations_status_expires_at"]
    )
    assert (
        "status, delete_lease_until"
        in index_definitions["ix_artifact_deletion_jobs_status_lease_until"]
    )
