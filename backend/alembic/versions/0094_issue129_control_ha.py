"""Explicit dual-Control admission and shared PostgreSQL capacity state."""

import sqlalchemy as sa
from alembic import op

revision = "0094_issue129_control_ha"
down_revision = "0093_issue129_worker_placement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("control_process_instances", sa.Column("ha_slot", sa.String(1)))
    op.create_check_constraint(
        "ck_control_instance_ha_slot", "control_process_instances", "ha_slot IN ('a', 'b')"
    )
    op.create_table(
        "control_ha_capacity",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("storage_domain_id", sa.String(128), nullable=False),
        sa.Column("topology_epoch", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True)),
        sa.Column("probe_started_at", sa.DateTime(timezone=True)),
        sa.Column("writable_free_bytes", sa.BigInteger()),
        sa.Column("state", sa.String(16), nullable=False, server_default="unavailable"),
        sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("id = 1 AND topology_epoch > 0", name="ck_control_ha_singleton"),
        sa.CheckConstraint(
            "state IN ('unavailable', 'low', 'available', 'conflict')",
            name="ck_control_ha_capacity_state",
        ),
        sa.CheckConstraint(
            "(observed_at IS NULL AND writable_free_bytes IS NULL AND recorded_at IS NULL "
            "AND state = 'unavailable') OR (observed_at IS NOT NULL "
            "AND writable_free_bytes IS NOT NULL AND writable_free_bytes >= 0 "
            "AND recorded_at IS NOT NULL)",
            name="ck_control_ha_capacity_sample",
        ),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE control_ha_capacity, control_process_instances IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM control_ha_capacity)
             OR EXISTS (SELECT 1 FROM control_process_instances WHERE ha_slot IS NOT NULL)
             OR EXISTS (
                SELECT 1 FROM control_cluster_authority
                WHERE schema_revision = '0094_issue129_control_ha'
             ) THEN
            RAISE EXCEPTION 'Control HA authority prevents downgrade';
          END IF;
        END $$;
    """)
    op.drop_table("control_ha_capacity")
    op.drop_constraint("ck_control_instance_ha_slot", "control_process_instances", type_="check")
    op.drop_column("control_process_instances", "ha_slot")
