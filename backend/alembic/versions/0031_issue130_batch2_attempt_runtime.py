"""Issue #130 Batch 2 Attempt credentials and Worker capability facts.

This migration is additive. It does not change the minimum Worker protocol,
drop the legacy active-Execution index, or enable RabbitMQ ingress. Existing
Attempt rows from the data-contract-only Batch 1 schema receive opaque
placeholder digests; no raw credential is reconstructed or persisted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_issue130_b2_runtime"
down_revision: str | None = "0030_issue130_reliable_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "isolation_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
    )
    op.add_column(
        "workers",
        sa.Column(
            "isolation_preflight_status",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=True,
        ),
    )
    op.add_column(
        "workers",
        sa.Column("isolation_preflight_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workers",
        sa.Column(
            "rabbitmq_execution_v3",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE workers SET isolation_capabilities = "
            "COALESCE(isolation_capabilities, '{}'::jsonb), "
            "isolation_preflight_status = COALESCE(isolation_preflight_status, 'unknown'), "
            "rabbitmq_execution_v3 = COALESCE(rabbitmq_execution_v3, false)"
        )
    )
    for column in (
        "isolation_capabilities",
        "isolation_preflight_status",
        "rabbitmq_execution_v3",
    ):
        op.alter_column("workers", column, nullable=False)
    op.create_check_constraint(
        "ck_workers_isolation_preflight_status",
        "workers",
        "isolation_preflight_status IN ('unknown', 'passed', 'failed')",
    )
    op.create_index(
        "ix_workers_rabbitmq_execution_v3",
        "workers",
        ["protocol_version", "rabbitmq_execution_v3"],
    )

    zero_digest = "0" * 64
    op.add_column(
        "execution_attempts",
        sa.Column(
            "claim_token_hash",
            sa.String(length=64),
            server_default=sa.text(f"'{zero_digest}'"),
            nullable=True,
        ),
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "cleanup_token_hash",
            sa.String(length=64),
            server_default=sa.text(f"'{zero_digest}'"),
            nullable=True,
        ),
    )
    # Batch 1 intentionally did not create active Attempts. If an operator
    # inserted a data-contract row, preserve it without ever inventing a raw
    # token; the placeholder can never match a generated Worker credential.
    op.execute(
        sa.text(
            "UPDATE execution_attempts SET "
            "claim_token_hash = COALESCE(claim_token_hash, lpad(to_hex(id), 64, '0')), "
            "cleanup_token_hash = COALESCE(cleanup_token_hash, lpad(to_hex(id + 1), 64, '0'))"
        )
    )
    op.alter_column("execution_attempts", "claim_token_hash", nullable=False)
    op.alter_column("execution_attempts", "cleanup_token_hash", nullable=False)
    op.create_check_constraint(
        "ck_execution_attempts_token_hashes_sha256",
        "execution_attempts",
        "claim_token_hash ~ '^[0-9a-f]{64}$' AND cleanup_token_hash ~ '^[0-9a-f]{64}$'",
    )

    op.add_column(
        "execution_artifact_holds", sa.Column("purged_by", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "execution_artifact_holds",
        sa.Column("purge_reason", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "execution_artifact_holds",
        sa.Column("held_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE execution_artifact_holds AS hold "
            "SET held_bytes = artifact.size_bytes "
            "FROM managed_input_artifacts AS artifact "
            "WHERE artifact.id = hold.artifact_id"
        )
    )
    op.alter_column("execution_artifact_holds", "held_bytes", nullable=False)


def downgrade() -> None:
    op.drop_column("execution_artifact_holds", "held_bytes")
    op.drop_column("execution_artifact_holds", "purge_reason")
    op.drop_column("execution_artifact_holds", "purged_by")
    op.drop_constraint(
        "ck_execution_attempts_token_hashes_sha256", "execution_attempts", type_="check"
    )
    op.drop_column("execution_attempts", "cleanup_token_hash")
    op.drop_column("execution_attempts", "claim_token_hash")
    op.drop_index("ix_workers_rabbitmq_execution_v3", table_name="workers")
    op.drop_constraint("ck_workers_isolation_preflight_status", "workers", type_="check")
    op.drop_column("workers", "rabbitmq_execution_v3")
    op.drop_column("workers", "isolation_preflight_at")
    op.drop_column("workers", "isolation_preflight_status")
    op.drop_column("workers", "isolation_capabilities")
