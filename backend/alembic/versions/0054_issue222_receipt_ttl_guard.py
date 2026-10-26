"""Require a known terminal time before expiring a retained batch Envelope.

0052/0053 remain unchanged for deployed databases. Replacing only the existing
function preserves trigger identities, ownership, grants and all shared-input,
member and delivery guards. Downgrade restores the exact prior function body.
"""

from alembic import op

revision = "0054_issue222_receipt_ttl_guard"
down_revision = "0053_issue222_batch_guard_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_IMMUTABLE_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))


def downgrade() -> None:
    op.execute(_PREVIOUS_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))


# Freeze the deployed body rather than importing an earlier migration at runtime.
_PREVIOUS_GUARD = """
CREATE FUNCTION dlr_batch_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='executions' THEN
  IF OLD.subscription_delivery_kind IS NOT NULL AND NEW.subscription_delivery_kind IS DISTINCT
  FROM OLD.subscription_delivery_kind THEN
   RAISE EXCEPTION 'execution delivery kind is immutable' USING ERRCODE='23514';
  END IF;
  IF OLD.subscription_delivery_kind='batch' AND NEW.input IS DISTINCT FROM OLD.input
    AND EXISTS(SELECT 1 FROM subscription_execution_batches WHERE execution_id=OLD.id) THEN
   RAISE EXCEPTION 'shared batch input is immutable until whole group cleanup' USING
  ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='subscription_execution_batches' THEN
  IF (to_jsonb(NEW)-ARRAY['result','result_counts','result_provenance',
    'result_attempt_id','finalized_at','created_at'])
     IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['result','result_counts','result_provenance',
    'result_attempt_id','finalized_at','created_at']) THEN
   RAISE EXCEPTION 'batch identity is immutable' USING ERRCODE='23514';
  END IF;
  IF OLD.result IS NOT NULL AND
  (NEW.result,NEW.result_counts,NEW.result_provenance,NEW.result_attempt_id,NEW.finalized_at)
      IS DISTINCT FROM
  (OLD.result,OLD.result_counts,OLD.result_provenance,OLD.result_attempt_id,OLD.finalized_at) THEN
   RAISE EXCEPTION 'batch result is immutable' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='event_receipts' THEN
  IF EXISTS(SELECT 1 FROM subscription_execution_batch_members WHERE receipt_id=OLD.id) THEN
   IF (NEW.activation_id,NEW.subscription_id,NEW.source_epoch,NEW.source_event_id,
       NEW.content_hash,NEW.shard,NEW.sequence) IS DISTINCT FROM
      (OLD.activation_id,OLD.subscription_id,OLD.source_epoch,OLD.source_event_id,
       OLD.content_hash,OLD.shard,OLD.sequence) THEN
    RAISE EXCEPTION 'batch receipt identity is immutable' USING ERRCODE='23514';
   END IF;
   IF NEW.envelope IS DISTINCT FROM OLD.envelope AND NOT
      (NEW.envelope IS NULL AND NEW.state IN ('succeeded','skipped','superseded','abandoned')
       AND NEW.terminal_at<=now()-interval '7 days') THEN
    RAISE EXCEPTION 'batch receipt payload is immutable before TTL' USING ERRCODE='23514';
   END IF;
  END IF;
 ELSIF TG_TABLE_NAME='subscription_execution_batch_members' THEN
  IF NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'batch member is immutable' USING
  ERRCODE='23514'; END IF;
 ELSE
  IF OLD.batch_execution_id IS NOT NULL AND
     (NEW.batch_execution_id,NEW.receipt_id,NEW.ordinal,
        NEW.activation_override_id,NEW.revision_override_id)
      IS DISTINCT FROM
  (OLD.batch_execution_id,OLD.receipt_id,OLD.ordinal,
    OLD.activation_override_id,OLD.revision_override_id) THEN
   RAISE EXCEPTION 'delivery batch identity is immutable' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
"""

# SQL NOT(NULL) is NULL, so IF would otherwise skip the rejection branch.
_IMMUTABLE_GUARD = _PREVIOUS_GUARD.replace(
    "AND NEW.terminal_at<=", "AND NEW.terminal_at IS NOT NULL AND NEW.terminal_at<=", 1
)
