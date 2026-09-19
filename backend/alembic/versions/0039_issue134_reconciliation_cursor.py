"""Add restart-safe bounded Attempt reconciliation progress."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_issue134_reconcile"
down_revision: str | None = "0038_issue138_languages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_reconciliation_cursors",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("after_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("upper_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "after_id >= 0 AND upper_id >= 0 AND after_id <= upper_id",
            name="ck_runtime_reconciliation_cursors_bounds",
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.execute(
        sa.text(
            "INSERT INTO runtime_reconciliation_cursors (name, after_id, upper_id) "
            "VALUES ('expired_attempts', 0, 0)"
        )
    )
    op.create_index(
        "ix_execution_attempts_active_id",
        "execution_attempts",
        ["id"],
        postgresql_where=sa.text("status IN ('claimed', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_execution_attempts_active_id", table_name="execution_attempts")
    op.drop_table("runtime_reconciliation_cursors")
