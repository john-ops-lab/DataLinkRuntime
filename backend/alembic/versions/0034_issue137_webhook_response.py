"""Add Webhook response policies and immutable receipt snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_webhook_response"
down_revision: str | None = "0033_unified_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapter_webhooks",
        sa.Column("response_mode", sa.String(16), nullable=False, server_default="accepted"),
    )
    op.add_column(
        "adapter_webhooks",
        sa.Column("response_timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
    )
    op.create_check_constraint(
        "ck_webhook_response_mode", "adapter_webhooks", "response_mode IN ('accepted', 'completed')"
    )
    op.create_check_constraint(
        "ck_webhook_response_timeout",
        "adapter_webhooks",
        "response_timeout_seconds BETWEEN 1 AND 300",
    )
    op.add_column(
        "executions", sa.Column("webhook_response_snapshot", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("executions", "webhook_response_snapshot")
    op.drop_constraint("ck_webhook_response_timeout", "adapter_webhooks", type_="check")
    op.drop_constraint("ck_webhook_response_mode", "adapter_webhooks", type_="check")
    op.drop_column("adapter_webhooks", "response_timeout_seconds")
    op.drop_column("adapter_webhooks", "response_mode")
