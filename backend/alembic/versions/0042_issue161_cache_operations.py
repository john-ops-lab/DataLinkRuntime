"""Add durable cache guard operation receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_issue161_cache_operations"
down_revision: str | None = "0041_issue161_cache_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_cache_operations",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("worker_id > 0", name="ck_worker_cache_operations_worker_positive"),
        sa.CheckConstraint("version_id > 0", name="ck_worker_cache_operations_version_positive"),
        sa.CheckConstraint("adapter_id > 0", name="ck_worker_cache_operations_adapter_positive"),
        sa.CheckConstraint("generation > 0", name="ck_worker_cache_operations_generation_positive"),
        sa.CheckConstraint(
            "phase IN ('acquired', 'completed', 'aborted')",
            name="ck_worker_cache_operations_phase",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "uq_worker_cache_operations_generation",
        "worker_cache_operations",
        ["worker_id", "version_id", "generation"],
        unique=True,
    )
    op.create_index(
        "ix_worker_cache_operations_active",
        "worker_cache_operations",
        ["worker_id", "phase"],
    )
    op.execute(
        sa.text(
            "INSERT INTO worker_cache_operations "
            "(operation_id, worker_id, version_id, adapter_id, generation, phase, created_at) "
            "SELECT operation_id, worker_id, version_id, adapter_id, generation, "
            "'acquired', created_at FROM worker_cache_guards "
            "WHERE phase = 'acquired'"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    active = connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM worker_cache_operations WHERE phase = 'acquired')")
    )
    if active:
        raise RuntimeError("Cannot downgrade while a cache guard operation is unfinished")
    op.drop_index("ix_worker_cache_operations_active", table_name="worker_cache_operations")
    op.drop_index("uq_worker_cache_operations_generation", table_name="worker_cache_operations")
    op.drop_table("worker_cache_operations")
