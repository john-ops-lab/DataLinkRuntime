"""M5.4.1 Adapter type and simplified runtime lifecycle.

The pre-M5.4 Publish / Production pointers are removed. Adapters keep an
immutable latest Revision, one configured runtime Worker, soft deletion and
one active Execution across every trigger type.

Revision ID: 0009_m5_4_1_adapter_lifecycle
Revises: 0008_m5_3_webhook_trigger
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_m5_4_1_adapter_lifecycle"
down_revision: str | None = "0008_m5_3_webhook_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # DLR has no production data before M5.4. Active test rows are deliberately
    # closed so the new cross-trigger unique constraint can be installed
    # without preserving the obsolete production lifecycle.
    op.execute(
        "UPDATE executions SET status = 'cancelled', ended_at = now() "
        "WHERE status IN ('pending', 'running')"
    )
    op.execute("DELETE FROM executions WHERE trigger = 'production'")

    op.execute("DROP INDEX uq_executions_active_production")
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_trigger",
        "executions",
        "trigger IN ('manual', 'schedule', 'webhook')",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_adapter "
        "ON executions (adapter_id) WHERE status IN ('pending', 'running')"
    )

    op.add_column("adapters", sa.Column("adapter_type", sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE adapters SET adapter_type = CASE "
        "WHEN EXISTS (SELECT 1 FROM adapter_webhooks w WHERE w.adapter_id = adapters.id) "
        "THEN 'webhook' ELSE 'task' END"
    )
    op.alter_column("adapters", "adapter_type", nullable=False)
    op.create_check_constraint(
        "ck_adapters_adapter_type",
        "adapters",
        "adapter_type IN ('task', 'webhook')",
    )

    op.drop_constraint("fk_adapters_production_worker_id", "adapters", type_="foreignkey")
    op.alter_column("adapters", "production_worker_id", new_column_name="runtime_worker_id")
    op.create_foreign_key(
        "fk_adapters_runtime_worker_id",
        "adapters",
        "workers",
        ["runtime_worker_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("fk_adapters_published_version_id", "adapters", type_="foreignkey")
    op.drop_constraint("fk_adapters_production_version_id", "adapters", type_="foreignkey")
    op.drop_constraint("ck_adapters_production_state", "adapters", type_="check")
    op.drop_column("adapters", "published_version_id")
    op.drop_column("adapters", "production_version_id")
    op.drop_column("adapters", "production_state")


def downgrade() -> None:
    op.add_column("adapters", sa.Column("published_version_id", sa.BigInteger(), nullable=True))
    op.add_column("adapters", sa.Column("production_version_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "adapters",
        sa.Column(
            "production_state",
            sa.String(length=16),
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_adapters_production_state",
        "adapters",
        "production_state IN ('idle', 'running', 'stopped')",
    )
    op.create_foreign_key(
        "fk_adapters_published_version_id",
        "adapters",
        "adapter_versions",
        ["published_version_id"],
        ["id"],
        use_alter=True,
    )
    op.create_foreign_key(
        "fk_adapters_production_version_id",
        "adapters",
        "adapter_versions",
        ["production_version_id"],
        ["id"],
        use_alter=True,
    )

    op.drop_constraint("fk_adapters_runtime_worker_id", "adapters", type_="foreignkey")
    op.alter_column("adapters", "runtime_worker_id", new_column_name="production_worker_id")
    op.create_foreign_key(
        "fk_adapters_production_worker_id",
        "adapters",
        "workers",
        ["production_worker_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("ck_adapters_adapter_type", "adapters", type_="check")
    op.drop_column("adapters", "adapter_type")

    op.execute("DROP INDEX uq_executions_active_adapter")
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_trigger",
        "executions",
        "trigger IN ('manual', 'production', 'schedule', 'webhook')",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_production "
        "ON executions (adapter_id) "
        "WHERE trigger IN ('production', 'schedule', 'webhook') "
        "AND status IN ('pending', 'running')"
    )
