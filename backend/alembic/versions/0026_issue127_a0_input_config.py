"""Issue #127 A0 input object expand schema and deterministic backfill.

The old Schedule ``input`` column is intentionally retained for the
compatibility window. The new table is the future Adapter-level authority;
this migration only establishes the schema and historical seed rows. Runtime
resolver/legacy mirroring changes belong to the later A1 batch.

Revision ID: 0026_issue127_a0_input_config
Revises: 0025_default_ai_tools_enabled
Create Date: 2026-08-26
"""

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0026_issue127_a0_input_config"
down_revision: str | None = "0025_default_ai_tools_enabled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPES = frozenset({"none", "json", "managed_files", "remote_files"})
RETENTION_MODES = frozenset({"system_default", "custom", "manual_delete"})


@dataclass(frozen=True)
class BackfillStats:
    """Machine-readable summary emitted by the expand backfill."""

    adapter_count: int
    source_type_counts: dict[str, int]
    conflict_count: int
    inserted_count: int


def _json_equal(left: object, right: object) -> bool:
    """Compare JSON values without relying on database driver representations."""
    left_json = json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    right_json = json.dumps(right, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return left_json == right_json


def _summary(stats: BackfillStats) -> str:
    source_counts = json.dumps(stats.source_type_counts, sort_keys=True, separators=(",", ":"))
    return (
        "issue127_a0_input_config_migration "
        f"adapters={stats.adapter_count} source_type_counts={source_counts} "
        f"conflicts={stats.conflict_count} inserted={stats.inserted_count}"
    )


def _raise_conflicts(
    *, adapter_count: int, expected_sources: Counter[str], conflicts: list[str]
) -> None:
    stats = BackfillStats(
        adapter_count=adapter_count,
        source_type_counts=dict(sorted(expected_sources.items())),
        conflict_count=len(conflicts),
        inserted_count=0,
    )
    message = _summary(stats) + " details=" + "; ".join(conflicts)
    print(message)
    raise RuntimeError(message)


def backfill_input_configs(connection: Connection) -> BackfillStats:
    """Validate and idempotently backfill current Task input rows.

    This helper intentionally performs a complete preflight before any insert.
    It is also callable by migration tests after a fixture injects duplicate,
    orphan, conflicting, or invalid-source rows into the expand schema.
    """
    adapter_rows = list(
        connection.execute(sa.text("SELECT id, adapter_type FROM adapters ORDER BY id")).mappings()
    )
    adapters = {int(row["id"]): str(row["adapter_type"]) for row in adapter_rows}
    task_adapter_ids = sorted(adapter_id for adapter_id, kind in adapters.items() if kind == "task")

    schedule_rows = list(
        connection.execute(
            sa.text("SELECT id, adapter_id, input FROM adapter_schedules ORDER BY id")
        ).mappings()
    )
    schedules_by_adapter: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in schedule_rows:
        adapter_id = int(row["adapter_id"])
        schedules_by_adapter[adapter_id].append(row)

    conflicts: list[str] = []
    for adapter_id, rows in sorted(schedules_by_adapter.items()):
        if adapter_id not in adapters:
            conflicts.append(f"adapter_id={adapter_id}:orphan_schedule")
        elif adapters[adapter_id] != "task":
            conflicts.append(f"adapter_id={adapter_id}:schedule_on_non_task")
        if len(rows) > 1:
            conflicts.append(f"adapter_id={adapter_id}:duplicate_schedule_rows={len(rows)}")

    config_rows = list(
        connection.execute(
            sa.text(
                "SELECT adapter_id, source_type, json_value, retention_mode, "
                "retention_seconds, revision, json_value IS NULL AS json_value_sql_null "
                "FROM adapter_input_configs "
                "ORDER BY adapter_id"
            )
        ).mappings()
    )
    config_groups: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in config_rows:
        adapter_id = int(row["adapter_id"])
        config_groups[adapter_id].append(row)
    for adapter_id, rows in sorted(config_groups.items()):
        if adapter_id not in adapters:
            conflicts.append(f"adapter_id={adapter_id}:orphan_input_config")
        elif adapters[adapter_id] != "task":
            conflicts.append(f"adapter_id={adapter_id}:input_config_on_non_task")
        if len(rows) > 1:
            conflicts.append(f"adapter_id={adapter_id}:duplicate_input_configs={len(rows)}")

    expected: dict[int, tuple[str, object]] = {}
    expected_sources: Counter[str] = Counter()
    for adapter_id in task_adapter_ids:
        rows = schedules_by_adapter.get(adapter_id, [])
        if len(rows) == 1:
            source_type, json_value = "json", rows[0]["input"]
        else:
            source_type, json_value = "json", {}
        expected[adapter_id] = source_type, json_value
        expected_sources[source_type] += 1

    for adapter_id, rows in sorted(config_groups.items()):
        if not rows or adapter_id not in adapters:
            continue
        row = rows[0]
        source_type = row["source_type"]
        if source_type not in SOURCE_TYPES:
            conflicts.append(f"adapter_id={adapter_id}:invalid_source={source_type!r}")
        retention_mode = row["retention_mode"]
        if retention_mode not in RETENTION_MODES:
            conflicts.append(f"adapter_id={adapter_id}:invalid_retention_mode={retention_mode!r}")
        elif (
            (
                retention_mode == "custom"
                and (row["retention_seconds"] is None or int(row["retention_seconds"]) <= 0)
            )
            or retention_mode != "custom"
            and row["retention_seconds"] is not None
        ):
            conflicts.append(f"adapter_id={adapter_id}:invalid_retention_fields")
        revision_value = row["revision"]
        if revision_value is None or int(revision_value) <= 0:
            conflicts.append(f"adapter_id={adapter_id}:invalid_revision={revision_value!r}")
        revision_is_initial = revision_value is not None and int(revision_value) == 1
        if source_type == "json" and row["json_value_sql_null"]:
            conflicts.append(f"adapter_id={adapter_id}:missing_json_value")
        if (
            source_type in {"none", "managed_files", "remote_files"}
            and not row["json_value_sql_null"]
        ):
            conflicts.append(f"adapter_id={adapter_id}:incompatible_json_value")

        if adapter_id in expected and source_type in SOURCE_TYPES:
            expected_source, expected_value = expected[adapter_id]
            if (
                source_type != expected_source
                or not _json_equal(row["json_value"], expected_value)
                or row["retention_mode"] != "system_default"
                or row["retention_seconds"] is not None
                or not revision_is_initial
            ):
                conflicts.append(
                    f"adapter_id={adapter_id}:input_config_conflict"
                    f" expected_source={expected_source} actual_source={source_type}"
                )

    if conflicts:
        _raise_conflicts(
            adapter_count=len(task_adapter_ids),
            expected_sources=expected_sources,
            conflicts=conflicts,
        )

    inserted_count = 0
    for adapter_id in task_adapter_ids:
        if adapter_id in config_groups:
            continue
        source_type, json_value = expected[adapter_id]
        connection.execute(
            sa.text(
                "INSERT INTO adapter_input_configs "
                "(adapter_id, source_type, json_value, retention_mode, "
                "retention_seconds, revision) "
                "VALUES (:adapter_id, :source_type, CAST(:json_value AS jsonb), "
                "'system_default', NULL, 1)"
            ),
            {
                "adapter_id": adapter_id,
                "source_type": source_type,
                "json_value": json.dumps(json_value, ensure_ascii=False),
            },
        )
        inserted_count += 1

    source_counts = dict(
        sorted(
            (
                str(row["source_type"]),
                int(row["count"]),
            )
            for row in connection.execute(
                sa.text(
                    "SELECT source_type, count(*) AS count "
                    "FROM adapter_input_configs GROUP BY source_type ORDER BY source_type"
                )
            ).mappings()
        )
    )
    stats = BackfillStats(
        adapter_count=len(task_adapter_ids),
        source_type_counts=source_counts,
        conflict_count=0,
        inserted_count=inserted_count,
    )
    print(_summary(stats))
    return stats


def _create_input_config_table_if_needed(connection: Connection) -> None:
    inspector = sa.inspect(connection)
    if "adapter_input_configs" not in inspector.get_table_names():
        op.create_table(
            "adapter_input_configs",
            sa.Column("adapter_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "source_type",
                sa.String(length=16),
                server_default=sa.text("'none'"),
                nullable=False,
            ),
            sa.Column("json_value", sa.dialects.postgresql.JSONB(), nullable=True),
            sa.Column(
                "retention_mode",
                sa.String(length=16),
                server_default=sa.text("'system_default'"),
                nullable=False,
            ),
            sa.Column("retention_seconds", sa.BigInteger(), nullable=True),
            sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "source_type IN ('none', 'json', 'managed_files', 'remote_files')",
                name="ck_adapter_input_configs_source_type",
            ),
            sa.CheckConstraint(
                "retention_mode IN ('system_default', 'custom', 'manual_delete')",
                name="ck_adapter_input_configs_retention_mode",
            ),
            sa.CheckConstraint("revision > 0", name="ck_adapter_input_configs_revision_positive"),
            sa.CheckConstraint(
                "((source_type IN ('none', 'managed_files', 'remote_files')) "
                "AND json_value IS NULL) OR (source_type = 'json' "
                "AND json_value IS NOT NULL)",
                name="ck_adapter_input_configs_source_fields",
            ),
            sa.CheckConstraint(
                "(retention_mode = 'custom' AND retention_seconds > 0) "
                "OR (retention_mode IN ('system_default', 'manual_delete') "
                "AND retention_seconds IS NULL)",
                name="ck_adapter_input_configs_retention_fields",
            ),
            sa.ForeignKeyConstraint(
                ["adapter_id"],
                ["adapters.id"],
                ondelete="CASCADE",
                name="fk_adapter_input_configs_adapter",
            ),
            sa.PrimaryKeyConstraint("adapter_id"),
        )
    inspector = sa.inspect(connection)
    existing_indexes = {index["name"] for index in inspector.get_indexes("adapter_input_configs")}
    if "ix_adapter_input_configs_source_type" not in existing_indexes:
        op.create_index(
            "ix_adapter_input_configs_source_type",
            "adapter_input_configs",
            ["source_type"],
        )
    if "ix_adapter_input_configs_revision" not in existing_indexes:
        op.create_index(
            "ix_adapter_input_configs_revision",
            "adapter_input_configs",
            ["revision"],
        )


def _add_schedule_blocked_columns_if_needed(connection: Connection) -> None:
    inspector = sa.inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("adapter_schedules")}
    additions = (
        ("last_blocked_reason", sa.String(length=64)),
        ("last_blocked_detail", sa.dialects.postgresql.JSONB()),
        ("last_blocked_at", sa.DateTime(timezone=True)),
        ("last_processed_due_at", sa.DateTime(timezone=True)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column("adapter_schedules", sa.Column(name, column_type, nullable=True))


def upgrade() -> None:
    connection = op.get_bind()
    _create_input_config_table_if_needed(connection)
    _add_schedule_blocked_columns_if_needed(connection)
    backfill_input_configs(connection)


def downgrade() -> None:
    """Test-only cleanup path; production downgrade would discard authority."""
    op.drop_column("adapter_schedules", "last_processed_due_at")
    op.drop_column("adapter_schedules", "last_blocked_at")
    op.drop_column("adapter_schedules", "last_blocked_detail")
    op.drop_column("adapter_schedules", "last_blocked_reason")
    op.drop_index("ix_adapter_input_configs_revision", table_name="adapter_input_configs")
    op.drop_index("ix_adapter_input_configs_source_type", table_name="adapter_input_configs")
    op.drop_table("adapter_input_configs")
