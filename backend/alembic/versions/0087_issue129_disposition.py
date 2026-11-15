"""Persist immutable supervised disposition of stopped, uncommitted results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0087_issue129_disposition"
down_revision = "0086_issue129_recovery"
branch_labels = None
depends_on = None

_CHECK = """
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
_GUARD = """
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.recovery_disposition_id IS NOT NULL THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'v4 recovery disposition immutable' USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.recovery_disposition_id IS NOT NULL THEN
                IF NEW.recovery_disposition_xid IS DISTINCT FROM pg_catalog.txid_current() THEN
                    RAISE EXCEPTION 'v4 recovery transaction mismatch' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        """


def upgrade() -> None:
    op.add_column(
        "worker_maintenance_operations",
        sa.Column("recovery_disposition_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "worker_maintenance_operations", sa.Column("recovery_disposition_xid", sa.BigInteger())
    )
    op.add_column(
        "worker_maintenance_operations", sa.Column("recovery_disposition", postgresql.JSONB())
    )
    op.create_unique_constraint(
        "uq_worker_recovery_disposition",
        "worker_maintenance_operations",
        ["recovery_disposition_id"],
    )
    op.create_check_constraint(
        "ck_worker_recovery_disposition_complete", "worker_maintenance_operations", _CHECK
    )
    op.execute(
        "CREATE FUNCTION dlr_v4_require_disposition() RETURNS trigger "
        "LANGUAGE plpgsql AS $body$" + _GUARD + "$body$"
    )
    for name, timing in (
        ("recovery_disposition_guard_v4", "BEFORE"),
        ("recovery_disposition_postguard_v4", "AFTER"),
    ):
        op.execute(
            f"CREATE TRIGGER {name} {timing} INSERT OR UPDATE "
            "ON worker_maintenance_operations FOR EACH ROW "
            "EXECUTE FUNCTION dlr_v4_require_disposition()"
        )
        op.execute(f"ALTER TABLE worker_maintenance_operations ENABLE ALWAYS TRIGGER {name}")


def downgrade() -> None:
    op.execute("LOCK TABLE workers, worker_maintenance_operations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_maintenance_operations
                       WHERE recovery_disposition_id IS NOT NULL) THEN
                RAISE EXCEPTION 'v4 disposition responsibility prevents downgrade';
            END IF;
        END $$
    """)
    for name in ("recovery_disposition_guard_v4", "recovery_disposition_postguard_v4"):
        op.execute(f"DROP TRIGGER {name} ON worker_maintenance_operations")
    op.execute("DROP FUNCTION dlr_v4_require_disposition()")
    op.drop_constraint(
        "ck_worker_recovery_disposition_complete", "worker_maintenance_operations", type_="check"
    )
    op.drop_constraint(
        "uq_worker_recovery_disposition", "worker_maintenance_operations", type_="unique"
    )
    for name in ("recovery_disposition", "recovery_disposition_xid", "recovery_disposition_id"):
        op.drop_column("worker_maintenance_operations", name)
