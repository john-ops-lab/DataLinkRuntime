"""M5.5.9 Active Adapter name uniqueness.

Adapter names must be unique only among active (non-soft-deleted) Adapters so a
soft-deleted name can be reused. The full-table unique constraint is replaced
by a partial unique index on ``name WHERE archived_at IS NULL``; the index is
the final database defense for concurrent create/rename, while the service
layer performs the authoritative pre-checks.

Revision ID: 0012_m5_5_9_active_name_unique
Revises: 0011_m5_4_3_webhook_final_model
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_m5_5_9_active_name_unique"
down_revision: str | None = "0011_m5_4_3_webhook_final_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_adapters_name", "adapters", type_="unique")
    op.create_index(
        "uq_adapters_active_name",
        "adapters",
        ["name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_adapters_active_name", table_name="adapters")
    op.create_unique_constraint("uq_adapters_name", "adapters", ["name"])
