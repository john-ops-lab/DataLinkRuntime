"""Freeze the system locale on every Execution.

Revision ID: 0018_m5_6_2_execution_locale
Revises: 0017_m5_6_1_system_locale
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_m5_6_2_execution_locale"
down_revision: str | None = "0017_m5_6_1_system_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "locale",
            sa.String(length=10),
            server_default=sa.text("'zh-CN'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_executions_locale",
        "executions",
        "locale IN ('zh-CN', 'en')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_executions_locale", "executions", type_="check")
    op.drop_column("executions", "locale")
