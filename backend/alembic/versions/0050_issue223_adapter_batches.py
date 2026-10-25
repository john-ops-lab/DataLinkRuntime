"""Persist bounded Adapter batch intent and atomic per-item receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_issue223_adapter_batches"
down_revision = "0049_issue219_queue_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adapter_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_key", sa.String(64), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("action IN ('start', 'stop')", name="ck_adapter_batch_action"),
    )
    op.create_index("ix_adapter_batches_owner_key", "adapter_batches", ["owner_key"])
    op.create_table(
        "adapter_batch_items",
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("adapter_batches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("adapter_id", sa.BigInteger(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("expected_token", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=True),
        sa.Column("action", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("execution_ids", postgresql.JSONB(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("activation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("adapter_id > 0", name="ck_adapter_batch_item_adapter"),
        sa.CheckConstraint("position BETWEEN 0 AND 99", name="ck_adapter_batch_item_position"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'stopping', 'succeeded', 'no_change', 'failed')",
            name="ck_adapter_batch_item_status",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM adapter_batches)")):
        raise RuntimeError(
            "Preserve batch receipts; roll back application without dropping history"
        )
    op.drop_table("adapter_batch_items")
    op.drop_table("adapter_batches")
