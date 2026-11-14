"""Store untrusted, diagnostic-only Worker capacity observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065_issue129_worker_observation"
down_revision = "0064_issue129_worker_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_capability_observations",
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("worker_identity_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("report_version", sa.BigInteger(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_state", sa.String(16), nullable=False, server_default="self_reported"),
        sa.ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_observation_identity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "instance_generation > 0 AND report_version > 0", name="ck_worker_observation_versions"
        ),
        sa.CheckConstraint(
            "evidence_state = 'self_reported'", name="ck_worker_observation_evidence"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(observation) = 'object' AND octet_length(observation::text) <= 8192",
            name="ck_worker_observation_payload",
        ),
    )


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute("LOCK TABLE worker_capability_observations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_capability_observations) THEN
                RAISE EXCEPTION 'worker capability observations prevent downgrade';
            END IF;
        END $$
    """)
    op.drop_table("worker_capability_observations")
