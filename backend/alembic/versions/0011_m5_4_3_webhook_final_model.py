"""M5.4.3 Webhook final user model.

Webhook rows now exist from Adapter creation, may be configured without a
Credential while stopped, and only require their path to be unique while
enabled. The partial unique index is the final concurrent Start defense.

Revision ID: 0011_m5_4_3_webhook_final_model
Revises: 0010_m5_4_2_task_run_mode
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_m5_4_3_webhook_final_model"
down_revision: str | None = "0010_m5_4_2_task_run_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("adapter_webhooks", "credential_id", nullable=True)
    op.drop_constraint("adapter_webhooks_public_id_key", "adapter_webhooks", type_="unique")
    op.create_index(
        "uq_adapter_webhooks_enabled_public_id",
        "adapter_webhooks",
        ["public_id"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )
    # Existing webhook rows are retained. Only newly typed Webhook Adapters
    # that predate the singleton-row contract need a generated stopped row.
    op.execute(
        "INSERT INTO adapter_webhooks (adapter_id, public_id, enabled, credential_id) "
        "SELECT a.id, substr(md5(random()::text || clock_timestamp()::text || a.id::text), 1, 16), "
        "false, NULL FROM adapters a WHERE a.adapter_type = 'webhook' AND NOT EXISTS ("
        "SELECT 1 FROM adapter_webhooks w WHERE w.adapter_id = a.id)"
    )


def downgrade() -> None:
    op.drop_index("uq_adapter_webhooks_enabled_public_id", table_name="adapter_webhooks")
    # M5.3 cannot represent an unconfigured row or duplicate stopped paths.
    op.execute("DELETE FROM adapter_webhooks WHERE credential_id IS NULL")
    op.execute(
        "WITH duplicates AS ("
        "SELECT id, row_number() OVER (PARTITION BY public_id ORDER BY id) AS n "
        "FROM adapter_webhooks) "
        "UPDATE adapter_webhooks w SET public_id = substr(md5(w.id::text), 1, 32) "
        "FROM duplicates d WHERE w.id = d.id AND d.n > 1"
    )
    op.create_unique_constraint("adapter_webhooks_public_id_key", "adapter_webhooks", ["public_id"])
    op.alter_column("adapter_webhooks", "credential_id", nullable=False)
