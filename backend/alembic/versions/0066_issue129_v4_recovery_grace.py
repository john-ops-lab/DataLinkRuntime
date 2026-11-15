"""Persist v4 lost-Attempt recovery boundaries without proving physical stop."""

import sqlalchemy as sa
from alembic import op

revision = "0066_issue129_v4_recovery_grace"
down_revision = "0065_issue129_worker_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_attempts",
        sa.Column("recovery_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM execution_attempts a
                JOIN executions e ON e.id = a.execution_id
                WHERE a.worker_identity_id IS NOT NULL AND a.status = 'worker_lost'
                  AND e.recovery_grace_seconds_snapshot IS NULL
            ) THEN
                RAISE EXCEPTION 'v4 lost Attempt lacks recovery grace snapshot';
            END IF;
        END $$
    """)
    op.execute("""
        UPDATE execution_attempts a
        SET recovery_not_before = a.lease_expires_at
            + make_interval(secs => e.recovery_grace_seconds_snapshot)
        FROM executions e
        WHERE e.id = a.execution_id
          AND a.worker_identity_id IS NOT NULL AND a.status = 'worker_lost'
    """)
    op.create_check_constraint(
        "ck_execution_attempts_v4_recovery_not_before",
        "execution_attempts",
        "recovery_not_before IS NULL OR "
        "(worker_identity_id IS NOT NULL AND recovery_not_before >= lease_expires_at)",
    )


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute("LOCK TABLE execution_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM execution_attempts WHERE recovery_not_before IS NOT NULL) THEN
                RAISE EXCEPTION 'v4 recovery responsibility prevents downgrade';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "ck_execution_attempts_v4_recovery_not_before", "execution_attempts", type_="check"
    )
    op.drop_column("execution_attempts", "recovery_not_before")
