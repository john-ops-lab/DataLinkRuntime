"""Add immutable HTTP routes, raw input, and adopted capacity configuration."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_issue222_durable_http"
down_revision = "0050_issue223_adapter_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_receipts", sa.Column("source_raw_body", sa.LargeBinary(), nullable=True))
    op.add_column(
        "subscription_activations",
        sa.Column("execution_policy_snapshot", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_index(
        "ix_subscription_activation_policy_legacy",
        "subscription_activations",
        ["id"],
        postgresql_where=sa.text("execution_policy_snapshot IS NULL"),
    )
    for table, columns in (
        ("event_receipts", ("retained_payload_bytes", "retained_metadata_bytes")),
        ("event_deliveries", ("retained_execution_bytes", "retained_metadata_bytes")),
    ):
        for name in columns:
            op.add_column(
                table, sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
            )
    op.drop_constraint("ck_sync_control_reserve", "subscription_sync_batches", type_="check")
    op.create_check_constraint(
        "ck_sync_control_reserve",
        "subscription_sync_batches",
        "reserved_control_count >= 0 AND reserved_control_count <= 3 "
        "AND reserved_control_bytes >= 0 AND reserved_control_bytes <= 62976",
    )
    _backfill_input_budgets()
    op.create_check_constraint(
        "ck_receipt_retained_budget",
        "event_receipts",
        "retained_payload_bytes >= 0 AND retained_metadata_bytes BETWEEN 0 AND 4096",
    )
    op.create_check_constraint(
        "ck_delivery_retained_budget",
        "event_deliveries",
        "retained_execution_bytes >= 0 AND retained_metadata_bytes BETWEEN 0 AND 512",
    )
    op.create_index(
        "ix_receipt_unsettled_shard",
        "event_receipts",
        ["subscription_id", "source_epoch", "shard", "sequence", "id"],
        postgresql_where=sa.text("state NOT IN ('succeeded','skipped','superseded','abandoned')"),
    )
    op.create_table(
        "http_ingress_routes",
        sa.Column("public_id", sa.String(64), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.BigInteger(),
            sa.ForeignKey("event_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_epoch", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("subscription_id", "source_epoch", name="uq_http_ingress_epoch"),
    )
    op.create_index(
        "ix_receipt_active_state",
        "event_receipts",
        ["subscription_id", "state"],
        postgresql_where=sa.text("state IN ('pending','processing','failed')"),
    )
    op.create_index(
        "ix_receipt_pending_age",
        "event_receipts",
        ["subscription_id", "first_received_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_table(
        "ingress_capacity_configurations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column(
            "adopted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1", name="ck_ingress_capacity_singleton"),
    )
    op.create_table(
        "ingress_capacity_audits",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("previous_fingerprint", sa.String(64), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM subscription_activations "
                "WHERE execution_policy_snapshot IS NOT NULL) "
                "OR EXISTS (SELECT 1 FROM subscription_sync_batches WHERE reserved_control_count>0)"
            )
        )
        or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM event_receipts)"))
        or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM http_ingress_routes)"))
        or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ingress_capacity_audits)"))
    ):
        raise RuntimeError(
            "Preserve ingress routes and capacity audit; use a compatible application rollback"
        )
    op.drop_table("ingress_capacity_audits")
    op.drop_table("ingress_capacity_configurations")
    op.drop_table("http_ingress_routes")
    op.drop_index("ix_receipt_unsettled_shard", table_name="event_receipts")
    op.drop_index("ix_receipt_active_state", table_name="event_receipts")
    op.drop_index("ix_receipt_pending_age", table_name="event_receipts")
    op.drop_column("event_receipts", "source_raw_body")
    op.drop_constraint("ck_receipt_retained_budget", "event_receipts", type_="check")
    op.drop_constraint("ck_delivery_retained_budget", "event_deliveries", type_="check")
    for table, columns in (
        ("event_receipts", ("retained_payload_bytes", "retained_metadata_bytes")),
        ("event_deliveries", ("retained_execution_bytes", "retained_metadata_bytes")),
    ):
        for name in columns:
            op.drop_column(table, name)
    op.drop_index("ix_subscription_activation_policy_legacy", table_name="subscription_activations")
    op.drop_column("subscription_activations", "execution_policy_snapshot")
    op.drop_constraint("ck_sync_control_reserve", "subscription_sync_batches", type_="check")
    op.create_check_constraint(
        "ck_sync_control_reserve",
        "subscription_sync_batches",
        "reserved_control_count >= 0 AND reserved_control_count <= 3 "
        "AND reserved_control_bytes >= 0 AND reserved_control_bytes <= 24576",
    )


def _backfill_input_budgets() -> None:
    """Add only newly retained costs; never overwrite an existing quota counter."""
    connection = op.get_bind()
    connection.execute(
        sa.text("""
        CREATE TEMPORARY TABLE ingress_previous_budgets ON COMMIT DROP AS
        SELECT r.subscription_id,
            coalesce(sum(CASE WHEN d.state = 'pending'
              THEN r.payload_bytes ELSE 0 END),0)::bigint AS pending_bytes,
            count(*) FILTER (WHERE d.id IS NOT NULL
              AND r.envelope IS NOT NULL)::bigint AS history_count,
            coalesce(sum(CASE WHEN d.id IS NOT NULL AND r.envelope IS NOT NULL
              THEN r.payload_bytes ELSE 0 END),0)::bigint AS history_bytes
        FROM event_receipts r LEFT JOIN event_deliveries d ON d.receipt_id = r.id
        GROUP BY r.subscription_id
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE event_receipts SET
            retained_payload_bytes = CASE WHEN envelope IS NOT NULL THEN payload_bytes ELSE 0 END,
            retained_metadata_bytes = 4096
    """)
    )
    # A correlated scalar keeps NULL/missing execution references unambiguous.
    connection.execute(
        sa.text("""
        UPDATE event_deliveries d SET
            retained_execution_bytes = coalesce(
                (SELECT octet_length(e.input::text) FROM executions e WHERE e.id=d.execution_id),
                (SELECT CASE WHEN r.envelope IS NOT NULL THEN r.payload_bytes ELSE 0 END
                 FROM event_receipts r WHERE r.id=d.receipt_id)),
            retained_metadata_bytes = 512
    """)
    )
    connection.execute(sa.text("UPDATE event_receipts SET payload_bytes=2*payload_bytes+4608"))
    too_large = connection.scalar(
        sa.text("""
        SELECT EXISTS (
          SELECT 1 FROM event_receipts r
          WHERE octet_length((to_jsonb(r)-ARRAY['envelope','source_raw_body'])::text)>4096
        ) OR EXISTS (
          SELECT 1 FROM event_deliveries d WHERE octet_length(to_jsonb(d)::text)>512
        )
    """)
    )
    if too_large:
        raise RuntimeError(
            "Retained metadata exceeds the migration budget; preserve data and inspect"
        )
    connection.execute(
        sa.text("""
        CREATE TEMPORARY TABLE ingress_budget_deltas ON COMMIT DROP AS
        WITH receipt_cost AS (
            SELECT subscription_id,
              sum(retained_payload_bytes+retained_metadata_bytes)::bigint AS bytes
            FROM event_receipts GROUP BY subscription_id
        ), delivery_cost AS (
            SELECT r.subscription_id,
              coalesce(sum(CASE WHEN d.state='pending'
                THEN r.payload_bytes ELSE 0 END),0)::bigint AS pending_bytes,
              count(*) FILTER(WHERE d.id IS NOT NULL AND
                (r.retained_payload_bytes>0 OR d.retained_execution_bytes>0))
                  ::bigint AS history_count,
              coalesce(sum(d.retained_execution_bytes+d.retained_metadata_bytes),0)::bigint AS bytes
            FROM event_receipts r LEFT JOIN event_deliveries d ON d.receipt_id=r.id
            GROUP BY r.subscription_id
        )
        SELECT p.subscription_id,
          d.pending_bytes-p.pending_bytes AS pending_bytes,
          d.history_count-p.history_count AS history_count,
          r.bytes+d.bytes-p.history_bytes AS history_bytes
        FROM ingress_previous_budgets p JOIN receipt_cost r USING(subscription_id)
        JOIN delivery_cost d USING(subscription_id)
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO subscription_quotas(scope,pending_bytes,history_count,history_bytes)
        SELECT 'sub:'||subscription_id, pending_bytes, history_count, history_bytes
        FROM ingress_budget_deltas
        ON CONFLICT(scope) DO UPDATE SET
          pending_bytes=subscription_quotas.pending_bytes+excluded.pending_bytes,
          history_count=subscription_quotas.history_count+excluded.history_count,
          history_bytes=subscription_quotas.history_bytes+excluded.history_bytes
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO subscription_quotas(scope,pending_bytes,history_count,history_bytes)
        SELECT 'global',coalesce(sum(pending_bytes),0),coalesce(sum(history_count),0),
          coalesce(sum(history_bytes),0)
        FROM ingress_budget_deltas HAVING count(*)>0
        ON CONFLICT(scope) DO UPDATE SET
          pending_bytes=subscription_quotas.pending_bytes+excluded.pending_bytes,
          history_count=subscription_quotas.history_count+excluded.history_count,
          history_bytes=subscription_quotas.history_bytes+excluded.history_bytes
    """)
    )
    connection.execute(
        sa.text("""
        WITH deltas AS (
            SELECT subscription_id,sum(reserved_control_count*(20992-8192))::bigint AS bytes
            FROM subscription_sync_batches GROUP BY subscription_id
        )
        UPDATE subscription_quotas q SET reserved_control_bytes=q.reserved_control_bytes+d.bytes
        FROM deltas d WHERE q.scope='sub:'||d.subscription_id
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE subscription_quotas SET reserved_control_bytes=reserved_control_bytes+
          (SELECT coalesce(sum(reserved_control_count*(20992-8192)),0)
           FROM subscription_sync_batches)
        WHERE scope='global'
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE subscription_sync_batches SET reserved_control_bytes=reserved_control_bytes+
            reserved_control_count*(20992-8192)
    """)
    )
