"""Persist the Wave 2 cutover and bounded placement cycles."""

import sqlalchemy as sa
from alembic import op

revision = "0093_issue129_worker_placement"
down_revision = "0092_issue129_worker_capacity"
branch_labels = None
depends_on = None


_LEGACY_DISPOSITION = """
    (recovery_disposition_id IS NULL AND recovery_disposition_xid IS NULL
     AND recovery_disposition IS NULL) OR
    (recovery_disposition_id IS NOT NULL
     AND recovery_disposition_id <> '00000000-0000-0000-0000-000000000000'::uuid
     AND recovery_disposition_xid > 0 AND recovery_disposition_xid IS NOT NULL
     AND recovery_disposition IS NOT NULL AND action = 'reconcile'
     AND (jsonb_typeof(recovery_disposition) = 'object'
      AND recovery_disposition ?& ARRAY['kind','reason','stopped_sha256','result_intent_id',
          'body_mac','attempt_status','execution_status','acknowledgement',
          'dispatch_generation','attempt_no']
      AND recovery_disposition - ARRAY['kind','reason','stopped_sha256','result_intent_id',
          'body_mac','attempt_status','execution_status','acknowledgement',
          'dispatch_generation','attempt_no'] = '{}'::jsonb
      AND recovery_disposition->>'kind' = 'platform_terminal'
      AND recovery_disposition->>'reason' = 'result_uncommitted_outcome_unknown'
      AND recovery_disposition->>'acknowledgement' = 'result_uncommitted_outcome_unknown'
      AND recovery_disposition->>'stopped_sha256' ~ '^[0-9a-f]{64}$'
      AND recovery_disposition->>'body_mac' ~ '^[0-9a-f]{64}$'
      AND recovery_disposition->>'result_intent_id' ~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND recovery_disposition->>'result_intent_id' <>
          '00000000-0000-0000-0000-000000000000'
      AND ((recovery_disposition->>'attempt_status' = 'worker_lost'
            AND recovery_disposition->>'execution_status' IN ('dead_letter','cancelled'))
           OR (recovery_disposition->>'attempt_status' = 'cancelled'
               AND recovery_disposition->>'execution_status' = 'cancelled'))
      AND jsonb_typeof(recovery_disposition->'dispatch_generation') = 'number'
      AND jsonb_typeof(recovery_disposition->'attempt_no') = 'number'
      AND recovery_disposition->>'dispatch_generation' ~ '^[1-9][0-9]{0,18}$'
      AND recovery_disposition->>'attempt_no' ~ '^[1-9][0-9]{0,18}$'
     ) IS TRUE)
"""
_FORCE_DISPOSITION = """
    recovery_disposition_id IS NOT NULL
    AND recovery_disposition_id <> '00000000-0000-0000-0000-000000000000'::uuid
    AND recovery_disposition_xid IS NOT NULL AND recovery_disposition_xid > 0
    AND action = 'reconcile'
    AND jsonb_typeof(recovery_disposition) = 'object'
    AND recovery_disposition ?& ARRAY['kind','reason','stopped_sha256','force_stop_operation_id',
        'attempt_status','execution_status','acknowledgement','dispatch_generation','attempt_no']
    AND recovery_disposition - ARRAY['kind','reason','stopped_sha256','force_stop_operation_id',
        'attempt_status','execution_status','acknowledgement','dispatch_generation','attempt_no']
        = '{}'::jsonb
    AND recovery_disposition->>'kind' = 'force_stopped_no_result'
    AND recovery_disposition->>'reason' = 'force_stop_no_result_outcome_unknown'
    AND recovery_disposition->>'acknowledgement' = 'force_stop_no_result_outcome_unknown'
    AND recovery_disposition->>'stopped_sha256' ~ '^[0-9a-f]{64}$'
    AND recovery_disposition->>'force_stop_operation_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_disposition->>'force_stop_operation_id' <>
        '00000000-0000-0000-0000-000000000000'
    AND ((recovery_disposition->>'attempt_status' = 'worker_lost'
          AND recovery_disposition->>'execution_status' IN ('dead_letter','cancelled'))
         OR (recovery_disposition->>'attempt_status' = 'cancelled'
             AND recovery_disposition->>'execution_status' = 'cancelled'))
    AND jsonb_typeof(recovery_disposition->'dispatch_generation') = 'number'
    AND jsonb_typeof(recovery_disposition->'attempt_no') = 'number'
    AND recovery_disposition->>'dispatch_generation' ~ '^[1-9][0-9]{0,18}$'
    AND recovery_disposition->>'attempt_no' ~ '^[1-9][0-9]{0,18}$'
"""


def _set_recovery_check(*, allow_pool_retry: bool) -> None:
    table = "worker_maintenance_operations"
    name = "ck_worker_recovery_disposition_complete"
    condition = f"({_LEGACY_DISPOSITION}) OR (({_FORCE_DISPOSITION}) IS TRUE)"
    if allow_pool_retry:
        condition = condition.replace(
            "('dead_letter','cancelled')", "('dead_letter','cancelled','retry_wait')"
        )
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    _set_recovery_check(allow_pool_retry=True)
    op.create_table(
        "worker_placement_cutover",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "id = 1 AND ((mode = 'fixed' AND paused_at IS NULL AND enabled_at IS NULL) "
            "OR (mode = 'paused' AND paused_at IS NOT NULL AND enabled_at IS NULL) "
            "OR (mode = 'reserved' AND enabled_at >= paused_at AND paused_at IS NOT NULL "
            "AND enabled_at IS NOT NULL))",
            name="ck_worker_placement_cutover_state",
        ),
    )
    op.execute("INSERT INTO worker_placement_cutover (id, mode) VALUES (1, 'fixed')")
    op.create_table(
        "worker_capacity_verifiers",
        sa.Column("worker_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("worker_identity_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column(
            "environment_name",
            sa.String(64),
            sa.ForeignKey("worker_environment_classes.name", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_capacity_verifiers_identity",
        ),
        sa.CheckConstraint("octet_length(public_key)=32", name="ck_worker_capacity_verifier_key"),
    )
    op.execute("INSERT INTO runtime_reconciliation_cursors (name) VALUES ('worker_placement')")
    op.create_table(
        "execution_placement_actions",
        sa.Column(
            "execution_id",
            sa.BigInteger(),
            sa.ForeignKey("executions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("dispatch_generation", sa.BigInteger(), primary_key=True),
        sa.Column("action", sa.String(16), primary_key=True),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dispatch_generation > 0 AND action IN ('retry', 'cancel') AND "
            "((actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0))",
            name="ck_execution_placement_actions_binding",
        ),
    )
    op.add_column(
        "worker_capacities", sa.Column("placement_changed_at", sa.DateTime(timezone=True))
    )
    op.add_column("worker_capacities", sa.Column("quarantine_until", sa.DateTime(timezone=True)))
    op.execute("""
        CREATE OR REPLACE FUNCTION dlr_keep_worker_reservation_requirement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.requires_worker_reservation IS DISTINCT FROM OLD.requires_worker_reservation
               AND NOT (
                   NOT OLD.requires_worker_reservation AND NEW.requires_worker_reservation
                   AND current_user = session_user
                   AND current_user = pg_catalog.pg_get_userbyid((
                       SELECT relowner FROM pg_catalog.pg_class
                       WHERE oid = 'worker_placement_cutover'::regclass
                   ))
                   AND EXISTS (
                       SELECT 1 FROM worker_placement_cutover WHERE id=1 AND mode='paused'
                   )
               ) THEN
                RAISE EXCEPTION 'Immutable Worker reservation requirement' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for column in (
        sa.Column("placement_origin", sa.String(16)),
        sa.Column("placement_wait_timeout_seconds", sa.Integer()),
        sa.Column("placement_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("placement_checked_at", sa.DateTime(timezone=True)),
        sa.Column("next_placement_at", sa.DateTime(timezone=True)),
        sa.Column("placement_hold_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("placement_misses", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("placement_reason", sa.String(64)),
    ):
        op.add_column("executions", column)
    op.execute("""
        UPDATE executions e SET placement_origin = 'legacy_v3'
        FROM workers w
        WHERE e.target_worker_id_snapshot = w.id AND w.protocol_version = 3
          AND w.identity_lifecycle = 'legacy'
    """)
    op.create_check_constraint(
        "ck_executions_placement_cycle",
        "executions",
        "(placement_origin IS NULL OR placement_origin IN ('v4', 'legacy_v3', 'legacy_v4')) "
        "AND (placement_wait_timeout_seconds IS NULL "
        "OR placement_wait_timeout_seconds BETWEEN 30 AND 86400) "
        "AND (placement_deadline_at IS NULL OR placement_wait_timeout_seconds IS NOT NULL) "
        "AND (placement_origin IS DISTINCT FROM 'legacy_v3' "
        "OR (placement_deadline_at IS NULL AND placement_wait_timeout_seconds IS NULL)) "
        "AND placement_hold_count BETWEEN 0 AND 10 AND placement_misses BETWEEN 0 AND 6",
    )
    op.create_index(
        "ix_executions_placement_due",
        "executions",
        ["next_placement_at", "adapter_id", "id"],
        postgresql_where=sa.text("requires_worker_reservation AND status = 'queued'"),
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_placement_cutover WHERE mode <> 'fixed')
               OR EXISTS (SELECT 1 FROM worker_capacity_verifiers)
               OR EXISTS (SELECT 1 FROM execution_placement_actions)
               OR EXISTS (SELECT 1 FROM worker_maintenance_operations
                          WHERE recovery_disposition->>'execution_status' = 'retry_wait')
               OR EXISTS (SELECT 1 FROM executions WHERE placement_deadline_at IS NOT NULL
                                                   OR placement_hold_count <> 0)
               OR EXISTS (SELECT 1 FROM control_cluster_authority
                          WHERE schema_revision='0093_issue129_worker_placement') THEN
                RAISE EXCEPTION 'Worker placement downgrade would lose durable state';
            END IF;
        END $$;
    """)
    _set_recovery_check(allow_pool_retry=False)
    op.drop_index("ix_executions_placement_due", table_name="executions")
    op.execute("DELETE FROM runtime_reconciliation_cursors WHERE name='worker_placement'")
    op.drop_constraint("ck_executions_placement_cycle", "executions", type_="check")
    op.execute("""
        CREATE OR REPLACE FUNCTION dlr_keep_worker_reservation_requirement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.requires_worker_reservation IS DISTINCT FROM OLD.requires_worker_reservation THEN
                RAISE EXCEPTION 'Immutable Worker reservation requirement' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for column in (
        "placement_reason",
        "placement_misses",
        "placement_hold_count",
        "next_placement_at",
        "placement_checked_at",
        "placement_deadline_at",
        "placement_wait_timeout_seconds",
        "placement_origin",
    ):
        op.drop_column("executions", column)
    op.drop_table("worker_placement_cutover")
    op.drop_table("execution_placement_actions")
    op.drop_table("worker_capacity_verifiers")
    op.drop_column("worker_capacities", "placement_changed_at")
    op.drop_column("worker_capacities", "quarantine_until")
