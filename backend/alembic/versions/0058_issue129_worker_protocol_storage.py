"""Allow protocol 4 storage and preserve Attempt instance identity snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0058_issue129_protocol_storage"
down_revision = "0055_issue129_worker_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.create_check_constraint(
        "ck_workers_protocol_version", "workers", "protocol_version IN (3, 4)"
    )
    op.add_column("execution_attempts", sa.Column("worker_identity_id", sa.Uuid()))
    op.add_column("execution_attempts", sa.Column("worker_instance_id", sa.Uuid()))
    op.add_column("execution_attempts", sa.Column("worker_instance_generation", sa.BigInteger()))
    # MATCH SIMPLE leaves legacy NULL identity snapshots readable while
    # requiring every v4 snapshot to belong to the same Worker ID.
    op.create_foreign_key(
        "fk_execution_attempts_worker_identity",
        "execution_attempts",
        "workers",
        ["worker_id", "worker_identity_id"],
        ["id", "identity_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_execution_attempts_v4_binding",
        "execution_attempts",
        [
            "id",
            "worker_id",
            "worker_identity_id",
            "worker_instance_id",
            "worker_instance_generation",
            "fencing_token",
        ],
    )
    op.create_index(
        "ix_execution_attempts_v4_adapter",
        "execution_attempts",
        ["adapter_id"],
        postgresql_where=sa.text("worker_identity_id IS NOT NULL"),
    )
    op.create_index(
        "ix_execution_attempts_v4_worker",
        "execution_attempts",
        ["worker_id", "worker_identity_id"],
        postgresql_where=sa.text("worker_identity_id IS NOT NULL"),
    )
    op.create_index(
        "ix_execution_attempts_v4_identity",
        "execution_attempts",
        ["worker_identity_id"],
        postgresql_where=sa.text("worker_identity_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_execution_attempts_worker_instance_shape",
        "execution_attempts",
        "(worker_identity_id IS NULL AND worker_instance_id IS NULL "
        "AND worker_instance_generation IS NULL) OR "
        "(worker_identity_id IS NOT NULL AND worker_instance_id IS NOT NULL "
        "AND worker_instance_generation IS NOT NULL AND worker_instance_generation > 0)",
    )


def downgrade() -> None:
    # Claim follows the Attempt/Worker prefix. Freeze both before checking.
    op.execute("LOCK TABLE execution_attempts, workers IN ACCESS EXCLUSIVE MODE")
    # The old schema cannot represent v4 identities; do not erase either a
    # registered v4 Worker or an Attempt snapshot, even if the latter ended.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM workers WHERE protocol_version = 4)
              OR EXISTS (
                  SELECT 1 FROM execution_attempts
                  WHERE worker_identity_id IS NOT NULL
                     OR worker_instance_id IS NOT NULL
                     OR worker_instance_generation IS NOT NULL
              )
            THEN
                RAISE EXCEPTION 'worker protocol 4 state prevents downgrade';
            END IF;
        END $$
    """)
    op.drop_index("ix_execution_attempts_v4_identity", table_name="execution_attempts")
    op.drop_index("ix_execution_attempts_v4_worker", table_name="execution_attempts")
    op.drop_index("ix_execution_attempts_v4_adapter", table_name="execution_attempts")
    op.drop_constraint(
        "ck_execution_attempts_worker_instance_shape", "execution_attempts", type_="check"
    )
    op.drop_constraint(
        "fk_execution_attempts_worker_identity", "execution_attempts", type_="foreignkey"
    )
    op.drop_constraint("uq_execution_attempts_v4_binding", "execution_attempts", type_="unique")
    op.drop_column("execution_attempts", "worker_instance_generation")
    op.drop_column("execution_attempts", "worker_instance_id")
    op.drop_column("execution_attempts", "worker_identity_id")
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.create_check_constraint("ck_workers_protocol_version", "workers", "protocol_version = 3")
