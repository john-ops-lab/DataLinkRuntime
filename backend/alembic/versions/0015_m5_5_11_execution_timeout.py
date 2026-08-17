"""M5.5.11 Adapter-level single-run execution timeout.

Every Adapter now carries its own authoritative execution timeout in
seconds (default 5 minutes, 1..86400 = max 24 hours, never "unlimited").
Existing rows are backfilled to the 300s default. Task manual and Task
schedule runs share this value, and the Worker receives it in every task
payload; the Worker already terminates the subprocess process group and
reports status ``timeout`` when it expires.

Revision ID: 0015_m5_5_11_execution_timeout
Revises: 0014_m5_5_7_access_key_fields
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_m5_5_11_execution_timeout"
down_revision: str | None = "0014_m5_5_7_access_key_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapters",
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            server_default=sa.text("300"),
            nullable=False,
        ),
    )
    # Existing Adapters keep the 300s default; the column is NOT NULL so the
    # backfill is implicit via the server default.
    op.create_check_constraint(
        "ck_adapters_timeout_seconds",
        "adapters",
        "timeout_seconds BETWEEN 1 AND 86400",
    )


def downgrade() -> None:
    op.drop_constraint("ck_adapters_timeout_seconds", "adapters", type_="check")
    op.drop_column("adapters", "timeout_seconds")
