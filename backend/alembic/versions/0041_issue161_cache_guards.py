"""Add stable Worker cache guard authority."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_issue161_cache_guards"
down_revision: str | None = "0040_issue152_dispositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_cache_guards",
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("phase", sa.String(length=16), server_default=sa.text("'idle'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("worker_id > 0", name="ck_worker_cache_guards_worker_positive"),
        sa.CheckConstraint("version_id > 0", name="ck_worker_cache_guards_version_positive"),
        sa.CheckConstraint("adapter_id > 0", name="ck_worker_cache_guards_adapter_positive"),
        sa.CheckConstraint("generation >= 0", name="ck_worker_cache_guards_generation_nonnegative"),
        sa.CheckConstraint("phase IN ('idle', 'acquired')", name="ck_worker_cache_guards_phase"),
        sa.CheckConstraint(
            "(phase = 'idle' AND operation_id IS NULL) OR "
            "(phase = 'acquired' AND operation_id IS NOT NULL)",
            name="ck_worker_cache_guards_operation_phase",
        ),
        sa.PrimaryKeyConstraint("worker_id", "version_id"),
    )
    op.create_index(
        "ix_worker_cache_guards_operation",
        "worker_cache_guards",
        ["operation_id"],
        unique=True,
    )


def downgrade() -> None:
    connection = op.get_bind()
    active = connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM worker_cache_guards WHERE phase <> 'idle')")
    )
    if active:
        raise RuntimeError("Cannot downgrade while a cache guard operation is unfinished")
    op.drop_index("ix_worker_cache_guards_operation", table_name="worker_cache_guards")
    op.drop_table("worker_cache_guards")
