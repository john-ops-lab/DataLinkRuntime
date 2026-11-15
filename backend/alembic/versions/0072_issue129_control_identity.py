"""Isolated Control cluster authority and process lease primitives."""

import sqlalchemy as sa
from alembic import op

revision = "0072_issue129_control_id"
down_revision = "0069_issue129_fresh_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_cluster_authority",
        sa.Column("singleton", sa.SmallInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("authority_epoch", sa.Uuid(), nullable=False),
        sa.Column("schema_revision", sa.String(32), nullable=False),
        sa.Column("image_digest", sa.String(71), nullable=False),
        sa.Column("master_check", sa.LargeBinary(), nullable=False),
        sa.Column("admin_check", sa.LargeBinary(), nullable=False),
        sa.Column("worker_check", sa.LargeBinary(), nullable=False),
        # No key is invented for an existing deployment; explicit authority
        # initialization supplies it only from trusted deployment material.
        sa.Column("worker_proof_public_key", sa.LargeBinary()),
        sa.Column("managed_fingerprint", sa.String(64), nullable=False),
        sa.Column("builtin_fingerprint", sa.String(64), nullable=False),
        sa.Column("attachment_fingerprint", sa.String(64), nullable=False),
        sa.Column("capacity_fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_control_cluster_singleton"),
        sa.CheckConstraint(
            "octet_length(master_check) = 32 AND octet_length(admin_check) = 32 "
            "AND octet_length(worker_check) = 32",
            name="ck_control_cluster_auth_checks",
        ),
        sa.CheckConstraint(
            "worker_proof_public_key IS NULL OR octet_length(worker_proof_public_key) = 32",
            name="ck_control_cluster_worker_proof_key",
        ),
    )
    op.create_table(
        "control_process_instances",
        sa.Column("instance_id", sa.Uuid(), primary_key=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("authority_epoch", sa.Uuid(), nullable=False),
        sa.Column("instance_token_digest", sa.LargeBinary(), nullable=False),
        sa.Column("image_digest", sa.String(71), nullable=False),
        sa.Column("managed_fingerprint", sa.String(64), nullable=False),
        sa.Column("builtin_fingerprint", sa.String(64), nullable=False),
        sa.Column("attachment_fingerprint", sa.String(64), nullable=False),
        sa.Column("capacity_fingerprint", sa.String(64), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(instance_token_digest) = 32", name="ck_control_instance_token_digest"
        ),
    )
    # Neither a key nor its cluster/epoch may be silently changed in place.
    # Future authority rotation needs a separately audited maintenance migration.
    op.execute("""
        CREATE FUNCTION dlr_control_proof_authority_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.cluster_id IS DISTINCT FROM OLD.cluster_id OR
             NEW.authority_epoch IS DISTINCT FROM OLD.authority_epoch OR
             NEW.worker_proof_public_key IS DISTINCT FROM OLD.worker_proof_public_key THEN
            RAISE EXCEPTION 'Control proof authority is immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_control_proof_authority_immutable
        BEFORE UPDATE ON control_cluster_authority
        FOR EACH ROW EXECUTE FUNCTION dlr_control_proof_authority_immutable()
    """)
    op.execute("""
        CREATE TRIGGER dlr_control_proof_authority_immutable_after
        AFTER UPDATE ON control_cluster_authority
        FOR EACH ROW EXECUTE FUNCTION dlr_control_proof_authority_immutable()
    """)
    op.execute(
        "ALTER TABLE control_cluster_authority "
        "ENABLE ALWAYS TRIGGER dlr_control_proof_authority_immutable"
    )
    op.execute(
        "ALTER TABLE control_cluster_authority "
        "ENABLE ALWAYS TRIGGER dlr_control_proof_authority_immutable_after"
    )


def downgrade() -> None:
    # Never silently discard authority or historical live/fenced identities.
    op.execute(
        "LOCK TABLE control_cluster_authority, control_process_instances IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM control_cluster_authority) OR
             EXISTS (SELECT 1 FROM control_process_instances) THEN
            RAISE EXCEPTION 'Control cluster identity prevents downgrade';
          END IF;
        END $$
    """)
    op.drop_table("control_process_instances")
    op.drop_table("control_cluster_authority")
    op.execute("DROP FUNCTION dlr_control_proof_authority_immutable()")
