"""Add cleanup intents and experimental completion markers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0085_issue129_cleanup_intent"
down_revision = "0082_issue129_result_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Separate the Worker's reported removal from Control's subsequent
    # observation of the committed receipt. Existing Attempts start unsettled.
    op.add_column(
        "execution_attempts",
        sa.Column("cleanup_settled_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "worker_cleanup_intents_v4",
        sa.Column("attempt_id", sa.BigInteger(), nullable=False, primary_key=True),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("original_instance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("original_generation", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("instance_revision", sa.BigInteger(), nullable=False),
        sa.Column("instance_candidate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("intent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("proof_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("writer_xid", sa.BigInteger(), nullable=False),
        sa.Column("completion_id", UUID(as_uuid=True)),
        sa.Column("completion_xid", sa.BigInteger()),
        sa.ForeignKeyConstraint(
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                "original_instance_id",
                "original_generation",
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
            name="fk_worker_cleanup_intent_attempt_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_cleanup_intent_instance",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("intent_id", name="uq_worker_cleanup_intent_v4_id"),
        sa.UniqueConstraint("completion_id", name="uq_worker_cleanup_completion_v4_id"),
        sa.CheckConstraint(
            "original_generation > 0 AND instance_generation > 0 AND fencing_token > 0 "
            "AND instance_revision > 0 AND writer_xid > 0",
            name="ck_worker_cleanup_intent_v4_positive",
        ),
        sa.CheckConstraint(
            "(completion_id IS NULL AND completion_xid IS NULL) OR "
            "(completion_id IS NOT NULL AND completion_xid IS NOT NULL "
            "AND completion_xid > 0 AND completion_xid = writer_xid)",
            name="ck_worker_cleanup_completion_v4_complete",
        ),
    )
    for name, timing in (
        ("worker_cleanup_intent_xid_guard_v4", "BEFORE"),
        ("worker_cleanup_intent_xid_postguard_v4", "AFTER"),
    ):
        op.execute(
            f"CREATE TRIGGER {name} {timing} INSERT OR UPDATE ON worker_cleanup_intents_v4 "
            "FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()"
        )
        op.execute(f"ALTER TABLE worker_cleanup_intents_v4 ENABLE ALWAYS TRIGGER {name}")


def downgrade() -> None:
    raise RuntimeError("Worker cleanup intent responsibilities cannot be safely downgraded")
