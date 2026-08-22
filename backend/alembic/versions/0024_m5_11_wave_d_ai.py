"""M5.11 Wave D provider catalog, custom profiles and setting reference.

Revision ID: 0024_m5_11_wave_d_ai
Revises: 0023_m5_11_wave_c_adapter_delete
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_m5_11_wave_d_ai"
down_revision: str | None = "0023_m5_11_wave_c_adapter_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_custom_providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("protocol", sa.String(length=24), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.BigInteger(), nullable=True),
        sa.Column("images_native", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("files_native", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tools_supported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "protocol IN ('openai_compatible', 'anthropic', 'gemini')",
            name="ck_ai_custom_providers_protocol",
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.add_column(
        "ai_model_settings",
        sa.Column("custom_provider_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_model_settings_custom_provider",
        "ai_model_settings",
        "ai_custom_providers",
        ["custom_provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("ck_ai_model_settings_provider", "ai_model_settings", type_="check")
    op.create_check_constraint(
        "ck_ai_model_settings_provider",
        "ai_model_settings",
        "provider IN ("
        "'openai', 'anthropic', 'gemini', 'deepseek', 'qwen', 'kimi', 'minimax', "
        "'glm', 'doubao', 'hunyuan', 'openrouter', 'siliconflow', 'ollama', "
        "'custom_openai_compatible')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_model_settings_provider", "ai_model_settings", type_="check")
    op.create_check_constraint(
        "ck_ai_model_settings_provider",
        "ai_model_settings",
        "provider IN ('openai', 'deepseek', 'kimi', 'minimax', 'custom_openai_compatible')",
    )
    op.drop_constraint(
        "fk_ai_model_settings_custom_provider", "ai_model_settings", type_="foreignkey"
    )
    op.drop_column("ai_model_settings", "custom_provider_id")
    op.drop_table("ai_custom_providers")
