"""M4 singleton AI model setting.

The provider API key remains in the existing encrypted Credential store;
this table only keeps the optional credential reference and non-secret model
configuration.

Revision ID: 0005_m4_ai_editor
Revises: 0004_m3_3_multilang_runtime
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_m4_ai_editor"
down_revision: str | None = "0004_m3_3_multilang_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_model_settings",
        # M4 intentionally has one global active configuration, not profiles.
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("credential_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "reasoning_mode",
            sa.String(length=16),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
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
        sa.CheckConstraint("id = 1", name="ck_ai_model_settings_singleton"),
        sa.CheckConstraint(
            "provider IN ('openai', 'deepseek', 'kimi', 'minimax', 'custom_openai_compatible')",
            name="ck_ai_model_settings_provider",
        ),
        sa.CheckConstraint(
            "reasoning_mode IN ('default', 'enabled', 'disabled')",
            name="ck_ai_model_settings_reasoning_mode",
        ),
        sa.CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN ('low', 'medium', 'high', 'max')",
            name="ck_ai_model_settings_reasoning_effort",
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ai_model_settings")
