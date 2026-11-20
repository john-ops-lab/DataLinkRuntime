"""Persist shared file namespace authority and maintenance cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0096_issue129_shared_storage"
down_revision = "0095_issue129_takeover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("builtin_package_uploads", sa.Column("cancelled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "builtin_package_uploads", sa.Column("writer_started_at", sa.DateTime(timezone=True))
    )
    op.create_table(
        "control_storage_bindings",
        sa.Column("purpose", sa.String(20), primary_key=True),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("namespace_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("previous_namespace_id", sa.Uuid()),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('managed_files','builtin_packages','ai_attachments') "
            "AND generation > 0 AND state IN ('active','copying','disabled') "
            "AND (state <> 'active' OR manifest_sha256 IS NOT NULL)",
            name="ck_control_storage_binding",
        ),
    )
    op.create_table(
        "control_storage_maintenance",
        sa.Column("purpose", sa.String(20), primary_key=True),
        sa.Column("shard", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("after_key", sa.String(64), server_default="", nullable=False),
        sa.Column("owner", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "shard >= 0 AND (owner IS NULL) = (lease_until IS NULL)",
            name="ck_control_storage_maintenance",
        ),
    )
    op.create_table(
        "control_storage_observations",
        sa.Column("purpose", sa.String(20), primary_key=True),
        sa.Column("slot", sa.String(1), primary_key=True),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("mount_id", sa.BigInteger(), nullable=False),
        sa.Column("proof_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "slot IN ('a','b') AND mount_id > 0", name="ck_control_storage_observation"
        ),
    )
    op.execute("""
        INSERT INTO control_storage_maintenance (purpose)
        VALUES ('managed_files'), ('builtin_packages'), ('ai_attachments')
    """)
    op.create_table(
        "control_attachment_scopes",
        sa.Column("namespace_id", sa.Uuid(), primary_key=True),
        sa.Column("principal_key", sa.String(64), primary_key=True),
        sa.Column("adapter_id", sa.BigInteger(), primary_key=True),
        sa.Column("scope_id", sa.Uuid(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "control_attachment_objects",
        sa.Column("reference", sa.String(96), primary_key=True),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_key", sa.String(64), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("charge", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("parse_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("resume_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_id", sa.Uuid()),
        sa.Column("result_sha256", sa.String(64)),
        sa.CheckConstraint(
            "size_bytes > 0 AND charge >= size_bytes AND parse_generation >= 0 "
            "AND state IN ('allocating','pending','ready','closed') "
            "AND (state <> 'ready' OR (result_id IS NOT NULL AND result_sha256 IS NOT NULL))",
            name="ck_control_attachment_object",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id", "principal_key", "adapter_id", "scope_id"],
            [
                f"control_attachment_scopes.{name}"
                for name in ("namespace_id", "principal_key", "adapter_id", "scope_id")
            ],
        ),
    )
    op.create_index(
        "ix_control_attachment_expiry", "control_attachment_objects", ["namespace_id", "expires_at"]
    )
    op.create_table(
        "control_attachment_jobs",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("reference", sa.String(96), nullable=False),
        sa.Column("converter_id", sa.Uuid(), nullable=False),
        sa.Column("control_slot", sa.String(1), nullable=False),
        sa.Column("parse_generation", sa.BigInteger()),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("charge", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["reference"], ["control_attachment_objects.reference"]),
        sa.CheckConstraint(
            "charge >= 0 AND control_slot IN ('a','b')", name="ck_control_attachment_job"
        ),
    )
    op.create_index(
        "ix_control_attachment_jobs_reference", "control_attachment_jobs", ["reference"]
    )
    op.create_table(
        "control_attachment_retirements",
        sa.Column("namespace_id", sa.Uuid(), primary_key=True),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "retired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "worker_v4_input_streams",
        sa.Column(
            "attempt_id",
            sa.BigInteger(),
            sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("artifact_id", sa.BigInteger(), primary_key=True),
        sa.Column("builtin", sa.Boolean(), primary_key=True),
        sa.Column("stream_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_worker_v4_active_input_stream",
        "worker_v4_input_streams",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("NOT closed"),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE control_storage_bindings IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM control_storage_bindings)
             OR EXISTS (SELECT 1 FROM control_cluster_authority
                        WHERE schema_revision = '0096_issue129_shared_storage') THEN
            RAISE EXCEPTION 'Shared storage authority prevents downgrade';
          END IF;
        END $$;
    """)
    op.drop_table("control_storage_maintenance")
    op.drop_table("control_storage_observations")
    op.drop_table("control_storage_bindings")
    op.drop_table("control_attachment_jobs")
    op.drop_table("control_attachment_objects")
    op.drop_table("control_attachment_scopes")
    op.drop_table("control_attachment_retirements")
    op.drop_table("worker_v4_input_streams")
    op.drop_column("builtin_package_uploads", "writer_started_at")
    op.drop_column("builtin_package_uploads", "cancelled_at")
