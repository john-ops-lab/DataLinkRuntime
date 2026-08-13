"""M5.1 production entry semantics and production version locking.

Start no longer creates an Execution; it opens the production entry and
locks the production version. Adds ``production_version_id`` to adapters,
widens the trigger value space to include ``schedule`` / ``webhook`` for
future milestones, and updates the active-production unique index to cover
all production-class triggers.

Revision ID: 0006_m5_1_production_entry
Revises: 0005_m4_ai_editor
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_m5_1_production_entry"
down_revision: str | None = "0005_m4_ai_editor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- adapters: production version pointer ----------------------------------
    op.add_column(
        "adapters",
        sa.Column("production_version_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_adapters_production_version_id",
        "adapters",
        "adapter_versions",
        ["production_version_id"],
        ["id"],
        use_alter=True,
    )

    # Data migration: recover production_version_id for any Adapter that was
    # running at migration time. Priority: active Production Execution's
    # version_id > latest Production Execution's version_id > published_version_id.
    # Non-running adapters get NULL (the default).
    op.execute(
        """
        UPDATE adapters
        SET production_version_id = COALESCE(
            (
                SELECT e.version_id
                FROM executions e
                WHERE e.adapter_id = adapters.id
                  AND e.trigger = 'production'
                  AND e.status IN ('pending', 'running')
                ORDER BY e.id DESC
                LIMIT 1
            ),
            (
                SELECT e.version_id
                FROM executions e
                WHERE e.adapter_id = adapters.id
                  AND e.trigger = 'production'
                ORDER BY e.id DESC
                LIMIT 1
            ),
            adapters.published_version_id
        )
        WHERE adapters.production_state = 'running'
          AND adapters.production_version_id IS NULL
        """
    )

    # --- executions: widen trigger value space ---------------------------------
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_trigger",
        "executions",
        "trigger IN ('manual', 'production', 'schedule', 'webhook')",
    )

    # --- executions: update the active-production unique index -----------------
    op.execute("DROP INDEX uq_executions_active_production")
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_production "
        "ON executions (adapter_id) "
        "WHERE trigger IN ('production', 'schedule', 'webhook') "
        "AND status IN ('pending', 'running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_executions_active_production")
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_production "
        "ON executions (adapter_id) "
        "WHERE trigger = 'production' AND status IN ('pending', 'running')"
    )
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_trigger",
        "executions",
        "trigger IN ('manual', 'production')",
    )
    op.drop_constraint("fk_adapters_production_version_id", "adapters", type_="foreignkey")
    op.drop_column("adapters", "production_version_id")
