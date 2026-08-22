"""M5.11 Wave C durable Worker cleanup requests for Adapter deletion.

Revision ID: 0023_m5_11_wave_c_adapter_delete
Revises: 0022_m5_9_wave_c_adapter_acl
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_m5_11_wave_c_adapter_delete"
down_revision: str | None = "0022_m5_9_wave_c_adapter_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_cleanup_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_worker_cleanup_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="RESTRICT",
            name="fk_worker_cleanup_requests_worker",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_cleanup_requests_worker_id",
        "worker_cleanup_requests",
        ["worker_id"],
    )
    op.create_index(
        "ix_worker_cleanup_requests_adapter_id",
        "worker_cleanup_requests",
        ["adapter_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_cleanup_requests_adapter_id", table_name="worker_cleanup_requests")
    op.drop_index("ix_worker_cleanup_requests_worker_id", table_name="worker_cleanup_requests")
    op.drop_table("worker_cleanup_requests")
