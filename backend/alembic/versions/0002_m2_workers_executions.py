"""M2 workers and executions tables.

Creates ``workers`` and ``executions``. Execution history is protected by
restrict-delete foreign keys to ``adapters`` / ``adapter_versions`` so an
Adapter with executions can never be physically removed.

Revision ID: 0002_m2_workers_executions
Revises: 0001_m1_adapters
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_m2_workers_executions"
down_revision: str | None = "0001_m1_adapters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "last_heartbeat",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('online', 'offline')", name="ck_workers_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_workers_name"),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("output_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "output_truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("output_preview", sa.Text(), nullable=True),
        sa.Column("stdout", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "stdout_truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("stderr", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "stderr_truncated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
            name="ck_executions_status",
        ),
        sa.CheckConstraint("trigger IN ('manual')", name="ck_executions_trigger"),
        sa.ForeignKeyConstraint(["adapter_id"], ["adapters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id"], ["adapter_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executions_adapter_id", "executions", ["adapter_id"])
    op.create_index("ix_executions_version_id", "executions", ["version_id"])
    op.create_index("ix_executions_claim", "executions", ["status", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_executions_claim", table_name="executions")
    op.drop_index("ix_executions_version_id", table_name="executions")
    op.drop_index("ix_executions_adapter_id", table_name="executions")
    op.drop_table("executions")
    op.drop_table("workers")
