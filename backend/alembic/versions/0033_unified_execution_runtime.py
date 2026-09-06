"""Make reliable execution the only runtime for new installations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_unified_execution"
down_revision: str | None = "0032_issue132_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM executions WHERE dispatch_backend <> 'rabbitmq')")
    ):
        raise RuntimeError("Unified runtime requires a fresh installation; legacy executions exist")
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM workers WHERE protocol_version <> 3)")
    ):
        raise RuntimeError(
            "Unified runtime requires current-protocol Workers; legacy Workers exist"
        )
    op.execute("DROP INDEX IF EXISTS uq_executions_active_adapter")
    for name in ("status", "backend_status", "dispatch_backend", "dispatch_generation"):
        op.drop_constraint(f"ck_executions_{name}", "executions", type_="check")
    statuses = (
        "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
        "'dead_letter', 'cancelled', 'expired')"
    )
    op.create_check_constraint("ck_executions_status", "executions", statuses)
    op.create_check_constraint(
        "ck_executions_backend_status",
        "executions",
        "dispatch_backend = 'rabbitmq' AND " + statuses,
    )
    op.create_check_constraint(
        "ck_executions_dispatch_backend", "executions", "dispatch_backend = 'rabbitmq'"
    )
    op.create_check_constraint(
        "ck_executions_dispatch_generation", "executions", "dispatch_generation >= 1"
    )
    op.alter_column("executions", "status", server_default="queued")
    op.alter_column("executions", "dispatch_backend", server_default="rabbitmq")
    op.alter_column("executions", "dispatch_generation", server_default=sa.text("1"))
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.create_check_constraint("ck_workers_protocol_version", "workers", "protocol_version = 3")
    op.alter_column("workers", "protocol_version", server_default=sa.text("3"))


def downgrade() -> None:
    raise RuntimeError(
        "Unified runtime does not support downgrade; redeploy a clean matching version"
    )
