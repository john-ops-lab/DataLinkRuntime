"""Bind legacy cleanup and prepare replacement provenance to cache operations.

Revision ID: 0043_issue161_legacy_replacement
Revises: 0042_issue161_cache_operations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_issue161_legacy_replacement"
down_revision = "0042_issue161_cache_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_cache_operations",
        sa.Column("operation_kind", sa.String(length=16), nullable=False, server_default="gc"),
    )
    op.add_column("worker_cache_operations", sa.Column("cleanup_id", sa.BigInteger()))
    op.add_column("worker_cache_operations", sa.Column("cleanup_claim_attempt", sa.Integer()))
    op.add_column("worker_cache_operations", sa.Column("observed_identity", postgresql.JSONB()))
    op.add_column("worker_cache_operations", sa.Column("replacement_context", postgresql.JSONB()))
    op.create_check_constraint(
        "ck_worker_cache_operations_kind",
        "worker_cache_operations",
        "operation_kind IN ('gc', 'cleanup', 'replacement')",
    )
    op.create_check_constraint(
        "ck_worker_cache_operations_cleanup_pair",
        "worker_cache_operations",
        "((cleanup_id IS NULL) = (cleanup_claim_attempt IS NULL)) AND "
        "((operation_kind = 'cleanup') = (cleanup_id IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_worker_cache_operations_replacement_context",
        "worker_cache_operations",
        "((operation_kind = 'replacement') = (replacement_context IS NOT NULL))",
    )


def downgrade() -> None:
    connection = op.get_bind()
    unfinished_specialized = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM worker_cache_operations "
            "WHERE phase = 'acquired' AND operation_kind IN ('cleanup', 'replacement')"
            ")"
        )
    )
    if unfinished_specialized:
        raise RuntimeError(
            "Cannot downgrade while a cleanup or replacement cache operation is unfinished"
        )
    op.drop_constraint(
        "ck_worker_cache_operations_replacement_context",
        "worker_cache_operations",
        type_="check",
    )
    op.drop_constraint(
        "ck_worker_cache_operations_cleanup_pair",
        "worker_cache_operations",
        type_="check",
    )
    op.drop_constraint("ck_worker_cache_operations_kind", "worker_cache_operations", type_="check")
    op.drop_column("worker_cache_operations", "replacement_context")
    op.drop_column("worker_cache_operations", "observed_identity")
    op.drop_column("worker_cache_operations", "cleanup_claim_attempt")
    op.drop_column("worker_cache_operations", "cleanup_id")
    op.drop_column("worker_cache_operations", "operation_kind")
