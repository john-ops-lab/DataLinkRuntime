"""Persist the M5.6 deployment-wide system locale.

Revision ID: 0017_m5_6_1_system_locale
Revises: 0016_m5_5_15_dep_sources
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_m5_6_1_system_locale"
down_revision: str | None = "0016_m5_5_15_dep_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "locale",
            sa.String(length=10),
            server_default=sa.text("'zh-CN'"),
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
        sa.CheckConstraint("id = 1", name="ck_system_settings_singleton"),
        sa.CheckConstraint(
            "locale IN ('zh-CN', 'en')",
            name="ck_system_settings_locale",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(sa.text("INSERT INTO system_settings (id, locale) VALUES (1, 'zh-CN')"))


def downgrade() -> None:
    op.drop_table("system_settings")
