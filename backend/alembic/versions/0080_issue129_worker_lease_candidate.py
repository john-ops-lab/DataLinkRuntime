"""Add bounded v4 lease candidates without trusting existing instances."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0080_issue129_lease_candidate"
down_revision = "0072_issue129_control_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing v4 rows intentionally receive no candidate. Their lease state is
    # unknown, not safe to renew or take over without a separate cutover.
    op.create_table(
        "worker_lease_candidates_v4",
        sa.Column("worker_id", sa.BigInteger(), nullable=False, primary_key=True),
        sa.Column("worker_identity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("nominal_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guard_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proof_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("writer_xid", sa.BigInteger(), nullable=False),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_lease_candidate_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_lease_candidate_instance",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_worker_lease_candidate_v4_id"),
        sa.CheckConstraint(
            "instance_generation > 0 AND revision > 0 AND writer_xid > 0",
            name="ck_worker_lease_candidate_v4_positive",
        ),
        sa.CheckConstraint(
            "proof_before <= guard_until", name="ck_worker_lease_candidate_v4_guard"
        ),
    )
    # A 32-bit xmin alone cannot authenticate a caller-supplied full xid. Keep
    # the stored xid tied to the actual write even across xid epoch wraparound.
    # Runtime roles must not be able to disable triggers or bypass this rule.
    op.execute(
        """
        CREATE FUNCTION dlr_v4_require_writer_xid() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.writer_xid IS DISTINCT FROM pg_catalog.txid_current() THEN
                RAISE EXCEPTION 'v4 writer transaction mismatch' USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'worker_lease_candidates_v4' AND TG_OP = 'UPDATE' THEN
                IF OLD.retired AND NOT NEW.retired AND
                   (NEW.instance_id IS NOT DISTINCT FROM OLD.instance_id OR
                    NEW.instance_generation <= OLD.instance_generation) THEN
                    RAISE EXCEPTION 'retired Worker generation cannot revive'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER worker_candidate_xid_guard_v4
        BEFORE INSERT OR UPDATE ON worker_lease_candidates_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE worker_lease_candidates_v4 ENABLE ALWAYS TRIGGER worker_candidate_xid_guard_v4"
    )
    # An additional BEFORE trigger could alter NEW after the first check.
    op.execute(
        """
        CREATE TRIGGER worker_candidate_xid_postguard_v4
        AFTER INSERT OR UPDATE ON worker_lease_candidates_v4
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_require_writer_xid()
        """
    )
    op.execute(
        "ALTER TABLE worker_lease_candidates_v4 "
        "ENABLE ALWAYS TRIGGER worker_candidate_xid_postguard_v4"
    )


def downgrade() -> None:
    # Old binaries cannot honor candidate proofs or their unknown state.
    # No completed protocol currently proves every old process has stopped.
    raise RuntimeError("worker lease proof authority cannot be safely downgraded")
