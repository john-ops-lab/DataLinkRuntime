"""Issue #127 C0 Execution snapshots, Worker protocol and input Leases.

The migration is additive for existing deployments.  Historical Execution
rows keep their original input and remain nullable for fields that only exist
after the C0 claim protocol; new rows are populated by the Control service.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_issue127_c0_exec_lease"
down_revision: str | None = "0028_issue127_b0_managed_input"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    execution_columns = (
        sa.Column("timeout_seconds_snapshot", sa.Integer(), nullable=True),
        sa.Column("recovery_grace_seconds_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "workspace_cleanup_attempt_timeout_seconds_snapshot",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "workspace_cleanup_total_timeout_seconds_snapshot",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("claim_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token_hash", sa.String(length=64), nullable=True),
        sa.Column("cleanup_receipt_token_hash", sa.String(length=64), nullable=True),
        sa.Column("workspace_cleanup_status", sa.String(length=16), nullable=True),
        sa.Column("workspace_cleanup_error_code", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    for column in execution_columns:
        op.add_column("executions", column)

    op.create_index(
        "ix_executions_claim_deadline_at",
        "executions",
        ["status", "claim_deadline_at"],
    )
    op.create_index(
        "ix_executions_execution_deadline_at",
        "executions",
        ["status", "execution_deadline_at"],
    )
    op.create_check_constraint(
        "ck_executions_timeout_seconds_snapshot",
        "executions",
        "timeout_seconds_snapshot IS NULL OR timeout_seconds_snapshot BETWEEN 1 AND 86400",
    )
    op.create_check_constraint(
        "ck_executions_recovery_grace_seconds_snapshot",
        "executions",
        "recovery_grace_seconds_snapshot IS NULL OR "
        "recovery_grace_seconds_snapshot BETWEEN 10 AND 3600",
    )
    op.create_check_constraint(
        "ck_executions_cleanup_attempt_timeout_snapshot",
        "executions",
        "workspace_cleanup_attempt_timeout_seconds_snapshot IS NULL OR "
        "workspace_cleanup_attempt_timeout_seconds_snapshot BETWEEN 1 AND 60",
    )
    op.create_check_constraint(
        "ck_executions_cleanup_total_timeout_snapshot",
        "executions",
        "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
        "workspace_cleanup_total_timeout_seconds_snapshot BETWEEN 5 AND 300",
    )
    op.create_check_constraint(
        "ck_executions_cleanup_attempt_le_total",
        "executions",
        "workspace_cleanup_attempt_timeout_seconds_snapshot IS NULL OR "
        "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
        "workspace_cleanup_attempt_timeout_seconds_snapshot <= "
        "workspace_cleanup_total_timeout_seconds_snapshot",
    )
    op.create_check_constraint(
        "ck_executions_cleanup_total_lt_recovery_grace",
        "executions",
        "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
        "recovery_grace_seconds_snapshot IS NULL OR "
        "workspace_cleanup_total_timeout_seconds_snapshot < "
        "recovery_grace_seconds_snapshot",
    )
    op.create_check_constraint(
        "ck_executions_workspace_cleanup_status",
        "executions",
        "workspace_cleanup_status IS NULL OR "
        "workspace_cleanup_status IN ('pending', 'completed', 'deferred')",
    )
    op.create_check_constraint(
        "ck_executions_claim_token_hash_sha256",
        "executions",
        "claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_executions_cleanup_receipt_token_hash_sha256",
        "executions",
        "cleanup_receipt_token_hash IS NULL OR cleanup_receipt_token_hash ~ '^[0-9a-f]{64}$'",
    )

    op.add_column(
        "workers",
        sa.Column(
            "protocol_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=True,
        ),
    )
    op.execute(sa.text("UPDATE workers SET protocol_version = 1 WHERE protocol_version IS NULL"))
    op.alter_column("workers", "protocol_version", nullable=False, server_default=sa.text("1"))
    op.create_check_constraint(
        "ck_workers_protocol_version",
        "workers",
        "protocol_version BETWEEN 1 AND 2",
    )
    op.create_index("ix_workers_protocol_version", "workers", ["protocol_version"])

    op.create_table(
        "execution_input_artifact_leases",
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 7",
            name="ck_execution_input_artifact_leases_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name="fk_execution_input_artifact_leases_execution",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["managed_input_artifacts.id"],
            ondelete="RESTRICT",
            name="fk_execution_input_artifact_leases_artifact",
        ),
        sa.PrimaryKeyConstraint("execution_id", "artifact_id"),
        sa.UniqueConstraint(
            "execution_id",
            "ordinal",
            name="uq_execution_input_artifact_leases_execution_ordinal",
        ),
    )
    op.create_index(
        "ix_execution_input_artifact_leases_artifact_id",
        "execution_input_artifact_leases",
        ["artifact_id"],
    )


def downgrade() -> None:
    """Test-only cleanup path; production rollback must not downgrade schema."""
    op.drop_index(
        "ix_execution_input_artifact_leases_artifact_id",
        table_name="execution_input_artifact_leases",
    )
    op.drop_table("execution_input_artifact_leases")
    op.drop_index("ix_workers_protocol_version", table_name="workers")
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.drop_column("workers", "protocol_version")

    op.drop_constraint(
        "ck_executions_cleanup_receipt_token_hash_sha256", "executions", type_="check"
    )
    op.drop_constraint("ck_executions_claim_token_hash_sha256", "executions", type_="check")
    op.drop_constraint("ck_executions_workspace_cleanup_status", "executions", type_="check")
    op.drop_constraint("ck_executions_cleanup_total_lt_recovery_grace", "executions", type_="check")
    op.drop_constraint("ck_executions_cleanup_attempt_le_total", "executions", type_="check")
    op.drop_constraint("ck_executions_cleanup_total_timeout_snapshot", "executions", type_="check")
    op.drop_constraint(
        "ck_executions_cleanup_attempt_timeout_snapshot", "executions", type_="check"
    )
    op.drop_constraint("ck_executions_recovery_grace_seconds_snapshot", "executions", type_="check")
    op.drop_constraint("ck_executions_timeout_seconds_snapshot", "executions", type_="check")
    op.drop_index("ix_executions_execution_deadline_at", table_name="executions")
    op.drop_index("ix_executions_claim_deadline_at", table_name="executions")
    for column in (
        "error_code",
        "workspace_cleanup_error_code",
        "workspace_cleanup_status",
        "cleanup_receipt_token_hash",
        "claim_token_hash",
        "execution_deadline_at",
        "claim_deadline_at",
        "workspace_cleanup_total_timeout_seconds_snapshot",
        "workspace_cleanup_attempt_timeout_seconds_snapshot",
        "recovery_grace_seconds_snapshot",
        "timeout_seconds_snapshot",
    ):
        op.drop_column("executions", column)
