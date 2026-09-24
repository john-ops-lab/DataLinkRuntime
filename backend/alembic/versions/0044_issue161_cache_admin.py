"""Add bounded cache administration operations and latest Worker snapshots.

Revision ID: 0044_issue161_cache_admin
Revises: 0043_issue161_legacy_replacement
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_issue161_cache_admin"
down_revision = "0043_issue161_legacy_replacement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_cache_management_operations",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("actor", postgresql.JSONB(), nullable=False),
        sa.Column("claim_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_kind", sa.String(32)),
        sa.Column("target_operation_id", sa.Uuid()),
        sa.Column("target_cleanup_id", sa.BigInteger()),
        sa.Column(
            "child_operations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('preview', 'clean', 'protect', 'retry')",
            name="ck_worker_cache_management_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_worker_cache_management_status",
        ),
        sa.CheckConstraint("claim_epoch >= 0", name="ck_worker_cache_management_claim_epoch"),
    )
    op.create_index(
        "uq_worker_cache_management_idempotency",
        "worker_cache_management_operations",
        ["worker_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_worker_cache_management_active",
        "worker_cache_management_operations",
        ["worker_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_worker_cache_management_audit",
        "worker_cache_management_operations",
        ["worker_id", "created_at", "operation_id"],
    )
    op.create_table(
        "worker_cache_snapshots",
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sample_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("cursor", sa.String(256)),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_worker_cache_snapshots_sequence"),
        sa.CheckConstraint(
            "state IN ('complete', 'incomplete', 'owner_unconfirmed')",
            name="ck_worker_cache_snapshots_state",
        ),
    )
    op.create_table(
        "worker_cache_snapshot_items",
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("sample_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("identity", postgresql.JSONB()),
        sa.Column("digest", sa.String(64)),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("rebuildability", sa.String(32), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "adapter_id > 0 AND version_id > 0", name="ck_worker_cache_snapshot_items_ids"
        ),
    )
    op.create_index(
        "ix_worker_cache_snapshot_items_sample",
        "worker_cache_snapshot_items",
        ["worker_id", "sample_id"],
    )
    op.create_table(
        "worker_cache_management_children",
        sa.Column(
            "management_operation_id",
            sa.Uuid(),
            sa.ForeignKey("worker_cache_management_operations.operation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "guard_operation_id",
            sa.Uuid(),
            sa.ForeignKey("worker_cache_operations.operation_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_worker_cache_management_children_generation"),
    )
    op.create_index(
        "ix_worker_cache_management_children_guard",
        "worker_cache_management_children",
        ["guard_operation_id"],
    )
    op.add_column("worker_cleanup_requests", sa.Column("retry_operation_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_worker_cleanup_retry_operation",
        "worker_cleanup_requests",
        "worker_cache_management_operations",
        ["retry_operation_id"],
        ["operation_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_worker_cleanup_retry_operation", "worker_cleanup_requests", ["retry_operation_id"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    unfinished = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM worker_cache_management_operations "
            "WHERE status IN ('pending','running') OR operation_id IN "
            "(SELECT retry_operation_id FROM worker_cleanup_requests "
            "WHERE retry_operation_id IS NOT NULL AND status <> 'completed') OR operation_id IN "
            "(SELECT c.management_operation_id FROM worker_cache_management_children c "
            "JOIN worker_cache_operations g ON g.operation_id = c.guard_operation_id "
            "WHERE g.phase = 'acquired'))"
        )
    )
    if unfinished:
        raise RuntimeError(
            "Cannot downgrade while cache administration responsibility is unfinished"
        )
    op.drop_constraint(
        "uq_worker_cleanup_retry_operation", "worker_cleanup_requests", type_="unique"
    )
    op.drop_constraint(
        "fk_worker_cleanup_retry_operation", "worker_cleanup_requests", type_="foreignkey"
    )
    op.drop_column("worker_cleanup_requests", "retry_operation_id")
    op.drop_index(
        "ix_worker_cache_management_children_guard", table_name="worker_cache_management_children"
    )
    op.drop_table("worker_cache_management_children")
    op.drop_index("ix_worker_cache_snapshot_items_sample", table_name="worker_cache_snapshot_items")
    op.drop_table("worker_cache_snapshot_items")
    op.drop_table("worker_cache_snapshots")
    op.drop_index(
        "ix_worker_cache_management_audit", table_name="worker_cache_management_operations"
    )
    op.drop_index(
        "uq_worker_cache_management_active", table_name="worker_cache_management_operations"
    )
    op.drop_index(
        "uq_worker_cache_management_idempotency", table_name="worker_cache_management_operations"
    )
    op.drop_table("worker_cache_management_operations")
