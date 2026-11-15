"""Add bounded Attempt lease candidates without backfilling authority."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0081_issue129_attempt_lease"
down_revision = "0080_issue129_lease_candidate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An existing Attempt without a candidate remains unknown. It cannot gain
    # execution authority simply by upgrading the database.
    op.create_table(
        "attempt_lease_candidates_v4",
        sa.Column("attempt_id", sa.BigInteger(), nullable=False, primary_key=True),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("nominal_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guard_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proof_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("writer_xid", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                "instance_id",
                "instance_generation",
                "fencing_token",
            ],
            [
                "execution_attempts.id",
                "execution_attempts.worker_id",
                "execution_attempts.worker_identity_id",
                "execution_attempts.worker_instance_id",
                "execution_attempts.worker_instance_generation",
                "execution_attempts.fencing_token",
            ],
            name="fk_attempt_lease_candidate_attempt_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_attempt_lease_candidate_instance",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_attempt_lease_candidate_v4_id"),
        sa.CheckConstraint(
            "instance_generation > 0 AND fencing_token > 0 AND revision > 0 AND writer_xid > 0",
            name="ck_attempt_lease_candidate_v4_positive",
        ),
        sa.CheckConstraint(
            "proof_before <= guard_until", name="ck_attempt_lease_candidate_v4_guard"
        ),
    )
    op.execute(
        """
        CREATE TRIGGER attempt_candidate_xid_guard_v4
        BEFORE INSERT OR UPDATE ON attempt_lease_candidates_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE attempt_lease_candidates_v4 "
        "ENABLE ALWAYS TRIGGER attempt_candidate_xid_guard_v4"
    )
    op.execute(
        """
        CREATE TRIGGER attempt_candidate_xid_postguard_v4
        AFTER INSERT OR UPDATE ON attempt_lease_candidates_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE attempt_lease_candidates_v4 "
        "ENABLE ALWAYS TRIGGER attempt_candidate_xid_postguard_v4"
    )


def downgrade() -> None:
    # Older binaries do not enforce candidate proofs or unknown outcomes.
    raise RuntimeError("Attempt lease proof authority cannot be safely downgraded")
