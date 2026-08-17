"""Ensure domestic and official dependency defaults without data loss.

Revision ID: 0016_m5_5_15_dep_sources
Revises: 0015_m5_5_11_execution_timeout
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dlr.control.package_source_defaults import DEFAULT_PACKAGE_SOURCES, PackageSourceDefault

revision: str = "0016_m5_5_15_dep_sources"
down_revision: str | None = "0015_m5_5_11_execution_timeout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _source_exists(connection: sa.Connection, default: PackageSourceDefault) -> bool:
    return (
        connection.execute(
            sa.text(
                "SELECT 1 FROM package_sources "
                "WHERE kind = :kind AND index_url = :index_url LIMIT 1"
            ),
            {"kind": default.kind, "index_url": default.index_url},
        ).first()
        is not None
    )


def _name_exists(connection: sa.Connection, name: str) -> bool:
    return (
        connection.execute(
            sa.text("SELECT 1 FROM package_sources WHERE name = :name LIMIT 1"),
            {"name": name},
        ).first()
        is not None
    )


def _available_name(connection: sa.Connection, preferred: str) -> str:
    name = preferred
    suffix = 1
    while _name_exists(connection, name):
        suffix += 1
        name = f"{preferred}（平台默认 {suffix}）"
    return name


def _has_default(connection: sa.Connection, kind: str) -> bool:
    return (
        connection.execute(
            sa.text("SELECT 1 FROM package_sources WHERE kind = :kind AND is_default LIMIT 1"),
            {"kind": kind},
        ).first()
        is not None
    )


def upgrade() -> None:
    connection = op.get_bind()
    kinds = {source.kind for source in DEFAULT_PACKAGE_SOURCES}
    existing_defaults = {kind: _has_default(connection, kind) for kind in kinds}
    for default in DEFAULT_PACKAGE_SOURCES:
        if _source_exists(connection, default):
            continue
        # Preserve an existing user-selected default. For a kind without one,
        # the domestic system source becomes the selected default.
        is_default = default.is_domestic and not existing_defaults[default.kind]
        connection.execute(
            sa.text(
                "INSERT INTO package_sources (name, kind, index_url, is_default) "
                "VALUES (:name, :kind, :index_url, :is_default)"
            ),
            {
                "name": _available_name(connection, default.name),
                "kind": default.kind,
                "index_url": default.index_url,
                "is_default": is_default,
            },
        )
        if is_default:
            existing_defaults[default.kind] = True


def downgrade() -> None:
    # This revision deliberately has no destructive downgrade. Canonical rows
    # may already be referenced or customized by an administrator by the time
    # a downgrade is requested, so leaving them is the only non-destructive
    # behavior. A later restore is idempotent and repairs missing system rows.
    pass
