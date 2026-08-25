"""Default new custom AI providers to tool support enabled.

Revision ID: 0025_default_ai_tools_enabled
Revises: 0024_m5_11_wave_d_ai
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_default_ai_tools_enabled"
down_revision: str | None = "0024_m5_11_wave_d_ai"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "ai_custom_providers",
        "tools_supported",
        existing_type=sa.Boolean(),
        server_default=sa.text("true"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "ai_custom_providers",
        "tools_supported",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        existing_nullable=False,
    )
