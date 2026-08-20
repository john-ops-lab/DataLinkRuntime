"""M5.9 Wave C Adapter ownership and read/edit ACL.

Historical and deployment-admin-created Adapters stay system-owned with a
NULL ``owner_user_id``. Account users receive ownership only when they create
an Adapter through the account entry.

Revision ID: 0022_m5_9_wave_c_adapter_acl
Revises: 0021_m5_9_wave_a_accounts
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_m5_9_wave_c_adapter_acl"
down_revision: str | None = "0021_m5_9_wave_a_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapters",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_adapters_owner_user_id", "adapters", ["owner_user_id"])
    op.create_foreign_key(
        "fk_adapters_owner_user_id",
        "adapters",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "adapter_permissions",
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("permission", sa.String(length=8), nullable=False),
        sa.CheckConstraint(
            "permission IN ('read', 'edit')",
            name="ck_adapter_permissions_permission",
        ),
        sa.ForeignKeyConstraint(["adapter_id"], ["adapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("adapter_id", "user_id"),
        sa.UniqueConstraint("adapter_id", "user_id", name="uq_adapter_permissions_adapter_user"),
    )
    op.create_index("ix_adapter_permissions_user_id", "adapter_permissions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_adapter_permissions_user_id", table_name="adapter_permissions")
    op.drop_table("adapter_permissions")
    op.drop_constraint("fk_adapters_owner_user_id", "adapters", type_="foreignkey")
    op.drop_index("ix_adapters_owner_user_id", table_name="adapters")
    op.drop_column("adapters", "owner_user_id")
