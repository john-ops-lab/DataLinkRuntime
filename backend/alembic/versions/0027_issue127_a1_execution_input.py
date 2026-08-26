"""Issue #127 A1 immutable Execution input facts.

The A0 migration creates the Adapter-level input source and revision.  A1
stores the resolved source/revision beside the raw JSON input so manual,
Schedule and run-now executions remain auditable after later configuration
changes.  Historical executions retain their input and receive the
compatibility ``json``/revision-1 snapshot.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_issue127_a1_execution_input"
down_revision: str | None = "0026_issue127_a0_input_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("input_source_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("input_config_revision", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("input_snapshot", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE executions SET input_source_type = 'json', "
            "input_config_revision = 1, "
            'input_snapshot = \'{"source_type": "json", "revision": 1}\'::jsonb'
        )
    )
    op.alter_column("executions", "input_source_type", nullable=False)
    op.alter_column("executions", "input_config_revision", nullable=False)
    op.alter_column("executions", "input_snapshot", nullable=False)
    op.create_check_constraint(
        "ck_executions_input_source_type",
        "executions",
        "input_source_type IN ('none', 'json', 'managed_files', 'remote_files')",
    )
    op.create_check_constraint(
        "ck_executions_input_config_revision_positive",
        "executions",
        "input_config_revision > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_executions_input_config_revision_positive", "executions")
    op.drop_constraint("ck_executions_input_source_type", "executions")
    op.drop_column("executions", "input_snapshot")
    op.drop_column("executions", "input_config_revision")
    op.drop_column("executions", "input_source_type")
