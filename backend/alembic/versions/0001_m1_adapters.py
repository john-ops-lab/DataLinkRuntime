"""M1 adapter management tables.

Creates ``adapters`` and ``adapter_versions``. The two version pointer
foreign keys on ``adapters`` are added after ``adapter_versions`` exists,
because Adapter and AdapterVersion reference each other.

Revision ID: 0001_m1_adapters
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_m1_adapters"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adapters",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("latest_version_id", sa.BigInteger(), nullable=True),
        sa.Column("published_version_id", sa.BigInteger(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_adapters_name"),
    )
    op.create_table(
        "adapter_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "runtime_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("seq > 0", name="ck_adapter_versions_seq_positive"),
        sa.ForeignKeyConstraint(["adapter_id"], ["adapters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_id", "seq", name="uq_adapter_versions_adapter_id_seq"),
    )
    op.create_index("ix_adapter_versions_adapter_id", "adapter_versions", ["adapter_id"])
    op.create_foreign_key(
        "fk_adapters_latest_version_id",
        "adapters",
        "adapter_versions",
        ["latest_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_adapters_published_version_id",
        "adapters",
        "adapter_versions",
        ["published_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_adapters_published_version_id", "adapters", type_="foreignkey")
    op.drop_constraint("fk_adapters_latest_version_id", "adapters", type_="foreignkey")
    op.drop_index("ix_adapter_versions_adapter_id", table_name="adapter_versions")
    op.drop_table("adapter_versions")
    op.drop_table("adapters")
