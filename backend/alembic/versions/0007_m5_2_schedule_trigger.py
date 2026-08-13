"""M5.2 Schedule Trigger: adapter_schedules table and scheduled_for column.

Adds the singleton Schedule table (one row per Adapter), the
``executions.scheduled_for`` troubleshooting column, and the partial unique
index guaranteeing one planned point yields at most one Schedule Execution.

Revision ID: 0007_m5_2_schedule_trigger
Revises: 0006_m5_1_production_entry
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_m5_2_schedule_trigger"
down_revision: str | None = "0006_m5_1_production_entry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- adapter_schedules: one Cron Schedule per Adapter --------------------
    op.create_table(
        "adapter_schedules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "adapter_id",
            sa.BigInteger(),
            sa.ForeignKey("adapters.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        # Any JSON value is valid input, including JSON null.
        sa.Column("input", sa.dialects.postgresql.JSONB(), nullable=False, server_default="null"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- executions: the planned point a Schedule Execution represents -------
    op.add_column(
        "executions",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    # One planned point per Adapter yields at most one Schedule Execution,
    # enforced by the database (final duplicate-creation defense).
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_schedule_point "
        "ON executions (adapter_id, scheduled_for) "
        "WHERE trigger = 'schedule'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_executions_schedule_point")
    op.drop_column("executions", "scheduled_for")
    op.drop_table("adapter_schedules")
