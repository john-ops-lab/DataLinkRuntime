"""Kubernetes batch qualification, prepaid controls and retained audit facts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_issue219_kubernetes"
down_revision = "0047_issue219_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscription_config_versions", sa.Column("source_identity", postgresql.JSONB()))
    for column in (
        sa.Column("activation_id", sa.BigInteger()),
        sa.Column("source_epoch", sa.String(64)),
        sa.Column("owner_id", sa.String(128)),
        sa.Column("owner_generation", sa.BigInteger()),
        sa.Column("completion_committed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("completion_abandoned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("drain_open", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reserved_control_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_control_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ready_kind", sa.String(32)),
        sa.Column("ready_since", sa.DateTime(timezone=True)),
        sa.Column("pending_gap", postgresql.JSONB()),
        sa.Column("started_receipt_id", sa.BigInteger()),
        sa.Column("completed_receipt_id", sa.BigInteger()),
        sa.Column("failed_receipt_id", sa.BigInteger()),
    ):
        op.add_column("subscription_sync_batches", column)
    op.create_foreign_key(
        "fk_sync_batch_activation",
        "subscription_sync_batches",
        "subscription_activations",
        ["activation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_sync_control_reserve",
        "subscription_sync_batches",
        "reserved_control_count >= 0 AND reserved_control_count <= 3 "
        "AND reserved_control_bytes >= 0 AND reserved_control_bytes <= 24576",
    )
    op.create_index(
        "uq_subscription_open_sync_batch",
        "subscription_sync_batches",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("activation_id IS NOT NULL AND finished_at IS NULL"),
    )
    op.create_index(
        "ix_sync_ready_control",
        "subscription_sync_batches",
        ["ready_since", "id"],
        postgresql_where=sa.text("ready_kind IS NOT NULL"),
    )
    for column in (
        sa.Column("source_phase", sa.String(32)),
        sa.Column("list_page_index", sa.BigInteger()),
        sa.Column("object_key_hash", sa.String(64)),
        sa.Column("coalesced_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("first_source_event_id", sa.String(128)),
    ):
        op.add_column("event_receipts", column)
    op.create_index(
        "ix_receipt_sync_members", "event_receipts", ["batch_id", "source_phase", "state", "id"]
    )
    op.create_index(
        "ix_receipt_pending_object",
        "event_receipts",
        ["subscription_id", "source_epoch", "object_key_hash", "id"],
        postgresql_where=sa.text("source_phase = 'watch' AND state = 'pending'"),
    )
    op.add_column("delivery_dispositions", sa.Column("batch_id", sa.BigInteger()))
    op.add_column(
        "delivery_dispositions",
        sa.Column("audit_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_disposition_sync_batch",
        "delivery_dispositions",
        "subscription_sync_batches",
        ["batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_sync_abandon_audit",
        "delivery_dispositions",
        ["batch_id"],
        unique=True,
        postgresql_where=sa.text("action = 'abandon_completion' AND batch_id IS NOT NULL"),
    )
    op.drop_constraint("ck_subscription_quota_nonnegative", "subscription_quotas")
    op.create_check_constraint(
        "ck_subscription_quota_nonnegative",
        "subscription_quotas",
        "pending_count >= 0 AND pending_bytes >= 0 AND history_count >= 0 "
        "AND history_bytes >= 0 AND dedupe_count >= 0 "
        "AND reserved_control_count >= 0 AND reserved_control_bytes >= 0",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM subscription_config_versions "
            "WHERE source_identity IS NOT NULL) OR EXISTS(SELECT 1 "
            "FROM subscription_sync_batches WHERE activation_id IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM event_receipts WHERE source_phase IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM delivery_dispositions WHERE audit_bytes > 0)"
        )
    ):
        raise RuntimeError("Kubernetes responsibility exists; preserve history and repair forward")
    op.drop_constraint("ck_subscription_quota_nonnegative", "subscription_quotas")
    op.create_check_constraint(
        "ck_subscription_quota_nonnegative",
        "subscription_quotas",
        "pending_count >= 0 AND pending_bytes >= 0 AND history_count >= 0 "
        "AND history_bytes >= 0 AND dedupe_count >= 0",
    )
    op.drop_index("uq_sync_abandon_audit", table_name="delivery_dispositions")
    op.drop_constraint("fk_disposition_sync_batch", "delivery_dispositions")
    op.drop_column("delivery_dispositions", "audit_bytes")
    op.drop_column("delivery_dispositions", "batch_id")
    op.drop_index("ix_receipt_pending_object", table_name="event_receipts")
    op.drop_index("ix_receipt_sync_members", table_name="event_receipts")
    for name in (
        "first_source_event_id",
        "coalesced_count",
        "object_key_hash",
        "list_page_index",
        "source_phase",
    ):
        op.drop_column("event_receipts", name)
    op.drop_index("ix_sync_ready_control", table_name="subscription_sync_batches")
    op.drop_index("uq_subscription_open_sync_batch", table_name="subscription_sync_batches")
    op.drop_constraint("ck_sync_control_reserve", "subscription_sync_batches")
    op.drop_constraint("fk_sync_batch_activation", "subscription_sync_batches")
    for name in (
        "failed_receipt_id",
        "completed_receipt_id",
        "started_receipt_id",
        "pending_gap",
        "ready_since",
        "ready_kind",
        "reserved_control_bytes",
        "reserved_control_count",
        "drain_open",
        "completion_abandoned",
        "completion_committed",
        "owner_generation",
        "owner_id",
        "source_epoch",
        "activation_id",
    ):
        op.drop_column("subscription_sync_batches", name)
    op.drop_column("subscription_config_versions", "source_identity")
