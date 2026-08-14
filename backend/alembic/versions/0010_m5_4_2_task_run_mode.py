"""M5.4.2 Task run mode.

Task Adapters default to manual execution and can explicitly switch to the
existing singleton Schedule model. Webhook Adapters keep the default value as
an internal compatibility field until their final M5.4.3 user model lands.

Revision ID: 0010_m5_4_2_task_run_mode
Revises: 0009_m5_4_1_adapter_lifecycle
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_m5_4_2_task_run_mode"
down_revision: str | None = "0009_m5_4_1_adapter_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapters",
        sa.Column(
            "run_mode",
            sa.String(length=16),
            server_default=sa.text("'manual'"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE adapters SET run_mode = 'schedule' "
        "WHERE adapter_type = 'task' AND EXISTS ("
        "SELECT 1 FROM adapter_schedules s WHERE s.adapter_id = adapters.id)"
    )
    op.create_check_constraint(
        "ck_adapters_run_mode",
        "adapters",
        "run_mode IN ('manual', 'schedule')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_adapters_run_mode", "adapters", type_="check")
    op.drop_column("adapters", "run_mode")
