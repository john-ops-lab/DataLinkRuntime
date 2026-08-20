"""Persist the productized M5.8 KnowledgeSource configuration.

No row is seeded: an absent singleton preserves the pre-M5.8 environment
fallback for existing ima deployments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_m5_8_006_ks_settings"
down_revision: str | None = "0019_m5_6_3_preset_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_source_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "source_id",
            sa.String(length=32),
            server_default=sa.text("'ima'"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            sa.BigInteger(),
            nullable=True,
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
        sa.CheckConstraint("id = 1", name="ck_knowledge_source_settings_singleton"),
        sa.CheckConstraint(
            "source_id IN ('ima')",
            name="ck_knowledge_source_settings_source_id",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["credentials.id"],
            name="fk_knowledge_source_settings_credential_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_source_settings")
