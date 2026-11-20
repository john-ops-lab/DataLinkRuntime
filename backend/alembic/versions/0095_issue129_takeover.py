"""Persist replication scan progress and provenance for Control takeover."""

import sqlalchemy as sa
from alembic import op

revision = "0095_issue129_takeover"
down_revision = "0094_issue129_control_ha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO runtime_reconciliation_cursors (name)
        VALUES ('schedule_due'), ('fixed_retry_due'), ('reserved_retry_due'),
               ('subscription_dispatch'), ('subscription_completion'), ('rabbitmq_topology'),
               ('admission_adapter'), ('admission_execution'), ('execution_retention'),
               ('subscription_quota'), ('subscription_global_quota')
    """)
    op.add_column("subscription_leases", sa.Column("control_instance_id", sa.Uuid()))
    op.add_column("event_receipts", sa.Column("receiver_control_instance_id", sa.Uuid()))
    op.add_column("event_receipts", sa.Column("source_owner_id", sa.String(128)))
    op.add_column("event_receipts", sa.Column("source_owner_generation", sa.BigInteger()))
    op.add_column(
        "execution_infrastructure_incidents", sa.Column("transport_event_key", sa.String(64))
    )
    op.create_unique_constraint(
        "uq_infrastructure_incident_transport_event",
        "execution_infrastructure_incidents",
        ["transport_event_key"],
    )
    op.create_check_constraint(
        "ck_event_receipt_control_provenance",
        "event_receipts",
        "(receiver_control_instance_id IS NULL AND source_owner_id IS NULL "
        "AND source_owner_generation IS NULL) OR (receiver_control_instance_id IS NOT NULL "
        "AND source_owner_id IS NOT NULL AND source_owner_generation IS NOT NULL "
        "AND source_owner_generation > 0)",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE subscription_leases, event_receipts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM subscription_leases WHERE control_instance_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM event_receipts WHERE receiver_control_instance_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM execution_infrastructure_incidents
                        WHERE transport_event_key IS NOT NULL)
             OR EXISTS (SELECT 1 FROM control_cluster_authority
                        WHERE schema_revision = '0095_issue129_takeover') THEN
            RAISE EXCEPTION 'Control takeover authority prevents downgrade';
          END IF;
        END $$;
    """)
    op.drop_constraint("ck_event_receipt_control_provenance", "event_receipts", type_="check")
    op.drop_column("event_receipts", "source_owner_generation")
    op.drop_column("event_receipts", "source_owner_id")
    op.drop_column("event_receipts", "receiver_control_instance_id")
    op.drop_column("subscription_leases", "control_instance_id")
    op.drop_constraint(
        "uq_infrastructure_incident_transport_event", "execution_infrastructure_incidents"
    )
    op.drop_column("execution_infrastructure_incidents", "transport_event_key")
    op.execute("""
        DELETE FROM runtime_reconciliation_cursors
        WHERE name IN ('schedule_due', 'fixed_retry_due', 'reserved_retry_due',
                       'subscription_dispatch', 'subscription_completion', 'rabbitmq_topology',
                       'admission_adapter', 'admission_execution', 'execution_retention',
                       'subscription_quota', 'subscription_global_quota')
    """)
