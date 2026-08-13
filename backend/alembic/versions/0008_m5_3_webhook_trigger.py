"""M5.3 Webhook Trigger: adapter_webhooks table.

Adds the singleton Webhook table (one row per Adapter). ``public_id`` is a
random routing identifier (unique); authentication is the referenced
token-type Credential, protected by a RESTRICT foreign key so a Webhook's
Credential can never be deleted underneath it.

Revision ID: 0008_m5_3_webhook_trigger
Revises: 0007_m5_2_schedule_trigger
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_m5_3_webhook_trigger"
down_revision: str | None = "0007_m5_2_schedule_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- adapter_webhooks: one Webhook per Adapter ---------------------------
    op.create_table(
        "adapter_webhooks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "adapter_id",
            sa.BigInteger(),
            sa.ForeignKey("adapters.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("public_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "credential_id",
            sa.BigInteger(),
            sa.ForeignKey("credentials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
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


def downgrade() -> None:
    op.drop_table("adapter_webhooks")
