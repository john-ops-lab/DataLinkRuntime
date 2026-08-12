"""M3.3 multi-language runtime constraints and dependency-source kinds.

Revision ID: 0004_m3_3_multilang_runtime
Revises: 0003_m3_2_production_lifecycle
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_m3_3_multilang_runtime"
down_revision: str | None = "0003_m3_2_production_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_adapters_language",
        "adapters",
        "language IN ('python', 'javascript', 'java')",
    )
    op.add_column(
        "package_sources",
        sa.Column(
            "kind",
            sa.String(length=16),
            server_default=sa.text("'pypi'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_package_sources_kind",
        "package_sources",
        "kind IN ('pypi', 'npm', 'maven')",
    )
    op.execute("DROP INDEX uq_package_sources_default")
    op.execute(
        "CREATE UNIQUE INDEX uq_package_sources_default ON package_sources (kind) WHERE is_default"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_package_sources_default")
    # Multiple defaults of different kinds cannot fit the M3.2 invariant.
    op.execute(
        "UPDATE package_sources SET is_default = false "
        "WHERE is_default AND id <> (SELECT min(id) FROM package_sources WHERE is_default)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_package_sources_default "
        "ON package_sources (is_default) WHERE is_default"
    )
    op.drop_constraint("ck_package_sources_kind", "package_sources", type_="check")
    op.drop_column("package_sources", "kind")
    op.drop_constraint("ck_adapters_language", "adapters", type_="check")
