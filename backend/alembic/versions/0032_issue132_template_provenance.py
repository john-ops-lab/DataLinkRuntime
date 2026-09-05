"""Issue #132 immutable Template Gallery provenance on copied Adapters."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_issue132_templates"
down_revision: str | None = "0031_issue130_b2_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adapters",
        sa.Column("template_scenario_slug", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "adapters",
        sa.Column("template_version", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_adapters_template_provenance_pair",
        "adapters",
        "(template_scenario_slug IS NULL AND template_version IS NULL) "
        "OR (template_scenario_slug IS NOT NULL AND template_version IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_adapters_template_provenance_pair",
        "adapters",
        type_="check",
    )
    op.drop_column("adapters", "template_version")
    op.drop_column("adapters", "template_scenario_slug")
