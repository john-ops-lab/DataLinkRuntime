"""Bind supervised recovery to an immutable administrator maintenance action."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0086_issue129_recovery"
down_revision = "0085_issue129_cleanup_intent"
branch_labels = None
depends_on = None

_OLD_ACTION = (
    "(action IN ('drain', 'activate') AND reason IS NULL) OR "
    "(action = 'force_stop' AND reason IS NOT NULL AND "
    "length(reason) BETWEEN 1 AND 256 AND length(btrim(reason)) > 0)"
)


def upgrade() -> None:
    op.add_column(
        "worker_maintenance_operations", sa.Column("recovery_binding", postgresql.JSONB())
    )
    op.drop_constraint(
        "ck_worker_maintenance_operation_action", "worker_maintenance_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_maintenance_operation_action",
        "worker_maintenance_operations",
        "(action IN ('drain', 'activate') AND reason IS NULL AND recovery_binding IS NULL) OR "
        "(action = 'force_stop' AND reason IS NOT NULL AND "
        "length(reason) BETWEEN 1 AND 256 AND length(btrim(reason)) > 0 "
        "AND recovery_binding IS NULL) OR "
        "(action = 'reconcile' AND reason IS NULL AND recovery_binding IS NOT NULL "
        "AND jsonb_typeof(recovery_binding) = 'object')",
    )


def downgrade() -> None:
    # Old binaries cannot interpret either the new audit or local checkpoints.
    # As with the preceding authority migrations, this is a stopped-writer
    # maintenance guard, not an online rollback or a journal cleanup operation.
    op.execute("LOCK TABLE workers, worker_maintenance_operations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM workers WHERE protocol_version = 4)
               OR EXISTS (SELECT 1 FROM worker_maintenance_operations
                          WHERE recovery_binding IS NOT NULL) THEN
                RAISE EXCEPTION 'v4 recovery provenance prevents downgrade';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "ck_worker_maintenance_operation_action", "worker_maintenance_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_worker_maintenance_operation_action", "worker_maintenance_operations", _OLD_ACTION
    )
    op.drop_column("worker_maintenance_operations", "recovery_binding")
