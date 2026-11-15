"""Add proof-bound result intents without storing result bodies or tokens."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0082_issue129_result_intent"
down_revision = "0081_issue129_attempt_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No historical Attempt is granted or inferred to have a result intent.
    op.create_table(
        "attempt_result_intents_v4",
        sa.Column("attempt_id", sa.BigInteger(), nullable=False, primary_key=True),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("attempt_revision", sa.BigInteger(), nullable=False),
        sa.Column("attempt_candidate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_revision", sa.BigInteger(), nullable=False),
        sa.Column("instance_candidate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("intent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("body_mac", sa.LargeBinary(), nullable=False),
        sa.Column("proof_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("writer_xid", sa.BigInteger(), nullable=False),
        sa.Column("finalization_id", UUID(as_uuid=True), nullable=True),
        sa.Column("finalization_xid", sa.BigInteger(), nullable=True),
        sa.Column("final_attempt_status", sa.String(32), nullable=True),
        sa.Column("final_execution_status", sa.String(32), nullable=True),
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
            name="fk_attempt_result_intent_attempt_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_attempt_result_intent_instance",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("intent_id", name="uq_attempt_result_intent_v4_id"),
        sa.UniqueConstraint("finalization_id", name="uq_attempt_result_finalization_v4_id"),
        sa.CheckConstraint(
            "instance_generation > 0 AND fencing_token > 0 AND attempt_revision > 0 "
            "AND instance_revision > 0 AND writer_xid > 0",
            name="ck_attempt_result_intent_v4_positive",
        ),
        sa.CheckConstraint("length(body_mac) = 32", name="ck_attempt_result_intent_v4_mac"),
        sa.CheckConstraint(
            "(finalization_id IS NULL AND finalization_xid IS NULL "
            "AND final_attempt_status IS NULL AND final_execution_status IS NULL) OR "
            "(finalization_id IS NOT NULL AND finalization_xid IS NOT NULL "
            "AND finalization_xid > 0 AND finalization_xid = writer_xid "
            "AND final_attempt_status IS NOT NULL AND final_attempt_status IN "
            "('succeeded', 'failed', 'timed_out', 'cancelled', 'worker_lost', "
            "'resource_exceeded') AND final_execution_status IS NOT NULL "
            "AND final_execution_status IN "
            "('succeeded', 'dead_letter', 'cancelled', 'retry_wait'))",
            name="ck_attempt_result_finalization_v4_complete",
        ),
    )
    op.execute(
        """
        CREATE TRIGGER result_intent_xid_guard_v4
        BEFORE INSERT OR UPDATE ON attempt_result_intents_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE attempt_result_intents_v4 ENABLE ALWAYS TRIGGER result_intent_xid_guard_v4"
    )
    op.execute(
        """
        CREATE TRIGGER result_intent_xid_postguard_v4
        AFTER INSERT OR UPDATE ON attempt_result_intents_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE attempt_result_intents_v4 ENABLE ALWAYS TRIGGER result_intent_xid_postguard_v4"
    )


def downgrade() -> None:
    raise RuntimeError("Attempt result intent responsibilities cannot be safely downgraded")
