"""Expand immutable batch JSON once without weakening database guards.

Keep 0052 intact for deployed databases. Only replace its validation function;
all trigger frequency, textual identity/NULL comparisons, ownership conflicts,
prepaid shares and terminal/Attempt proof checks retain their original rules.
"""

from alembic import op

revision = "0053_issue222_batch_guard_json"
down_revision = "0052_issue222_business_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_VALIDATE)


def downgrade() -> None:
    op.execute(_PREVIOUS_VALIDATE.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))


# Frozen 0052 body makes rollback reproducible without importing mutable files.
_PREVIOUS_VALIDATE = """
CREATE FUNCTION dlr_batch_validate(target bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE b subscription_execution_batches%ROWTYPE; e executions%ROWTYPE;
 actual_count integer; low_index integer; high_index integer;
 paid_input bigint; paid_batch bigint;
 projected_counts jsonb;
BEGIN
 IF target IS NULL THEN RETURN; END IF;
 IF to_regclass('pg_temp.dlr_batch_validation_cache') IS NULL THEN
  CREATE TEMP TABLE dlr_batch_validation_cache(execution_id bigint PRIMARY KEY) ON COMMIT DELETE
  ROWS;
 END IF;
 IF EXISTS (SELECT 1 FROM pg_temp.dlr_batch_validation_cache WHERE execution_id=target) THEN
  RETURN; END IF;
 SELECT * INTO b FROM subscription_execution_batches WHERE execution_id=target;
 IF NOT FOUND THEN
  IF EXISTS(SELECT 1 FROM subscription_execution_batch_members WHERE execution_id=target)
     OR EXISTS(SELECT 1 FROM event_deliveries WHERE batch_execution_id=target) THEN
    RAISE EXCEPTION 'batch authority is missing' USING ERRCODE='23514';
  END IF;
  RETURN;
 END IF;
 SELECT * INTO e FROM executions WHERE id=target;
 IF e.subscription_delivery_kind IS DISTINCT FROM 'batch'
    OR EXISTS(SELECT 1 FROM event_deliveries WHERE execution_id=target) THEN
  RAISE EXCEPTION 'mixed single and batch execution' USING ERRCODE='23514';
 END IF;
 SELECT count(*),min(member_index),max(member_index) INTO actual_count,low_index,high_index
 FROM subscription_execution_batch_members WHERE execution_id=target;
 SELECT coalesce(sum(d.retained_execution_bytes),0),coalesce(sum(d.retained_batch_bytes),0)
 INTO paid_input,paid_batch FROM subscription_execution_batch_members m
 JOIN event_deliveries d ON d.id=m.delivery_id WHERE m.execution_id=target;
 IF actual_count<>b.member_count OR low_index<>0 OR high_index<>b.member_count-1
   OR paid_input<>b.retained_input_bytes OR paid_batch<>b.retained_batch_bytes
   OR EXISTS(SELECT 1 FROM event_deliveries d WHERE d.batch_execution_id=target
       AND NOT EXISTS(SELECT 1 FROM subscription_execution_batch_members m WHERE
  m.delivery_id=d.id))
   OR jsonb_typeof(b.manifest)<>'array' OR jsonb_array_length(b.manifest)<>b.member_count
   OR e.input->>'schema' IS DISTINCT FROM 'dlr.event.batch.v1'
   OR e.input->>'batch_id' IS DISTINCT FROM b.batch_id
   OR e.input->>'manifest_hash' IS DISTINCT FROM b.manifest_hash
   OR jsonb_typeof(e.input->'members') IS DISTINCT FROM 'array'
   OR jsonb_array_length(e.input->'members')<>b.member_count THEN
  RAISE EXCEPTION 'batch member set differs from frozen manifest' USING ERRCODE='23514';
 END IF;
 IF EXISTS (
  SELECT 1 FROM subscription_execution_batch_members m
  JOIN event_deliveries d ON d.id=m.delivery_id JOIN event_receipts r ON r.id=m.receipt_id
  JOIN subscription_activations a ON a.id=coalesce(d.activation_override_id,r.activation_id)
  JOIN subscription_config_versions c ON c.id=a.config_version_id
  JOIN adapter_versions v ON v.id=coalesce(d.revision_override_id,a.revision_id)
  WHERE m.execution_id=target AND (
   d.receipt_id<>m.receipt_id OR d.batch_execution_id IS DISTINCT FROM target OR d.execution_id
  IS NOT NULL
   OR r.subscription_id<>b.subscription_id OR r.source_epoch<>b.source_epoch OR r.shard<>b.shard
   OR a.id<>b.activation_id OR v.id<>b.revision_id
   OR d.revision_override_id IS DISTINCT FROM b.revision_override_id
   OR c.config->>'source_type' NOT IN ('http','rabbitmq')
   OR c.config->>'mode' IS DISTINCT FROM 'business_batch'
   OR v.runtime_config->>'event_batch_schema' IS DISTINCT FROM 'dlr.event.batch.v1'
   OR b.manifest->m.member_index->>'receipt_id' IS DISTINCT FROM r.id::text
   OR b.manifest->m.member_index->>'delivery_id' IS DISTINCT FROM d.id::text
   OR b.manifest->m.member_index->>'delivery_ordinal' IS DISTINCT FROM d.ordinal::text
   OR b.manifest->m.member_index->>'member_index' IS DISTINCT FROM m.member_index::text
   OR b.manifest->m.member_index->>'content_hash' IS DISTINCT FROM r.content_hash
   OR e.input->'members'->m.member_index->>'receipt_id' IS DISTINCT FROM r.id::text
   OR e.input->'members'->m.member_index->>'delivery_id' IS DISTINCT FROM d.id::text
   OR e.input->'members'->m.member_index->>'delivery_ordinal' IS DISTINCT FROM d.ordinal::text
   OR e.input->'members'->m.member_index->>'member_index' IS DISTINCT FROM m.member_index::text
   OR (r.envelope IS NOT NULL AND e.input->'members'->m.member_index->'input' IS DISTINCT FROM
  r.envelope)
  )
 ) THEN RAISE EXCEPTION 'batch member binding differs' USING ERRCODE='23514'; END IF;
 IF b.result IS NOT NULL THEN
  SELECT jsonb_build_object(
    'succeeded',count(*) FILTER(WHERE item->>'status'='succeeded'),
    'failed',count(*) FILTER(WHERE item->>'status'='failed'),
    'unknown',count(*) FILTER(WHERE item->>'status'='unknown'),
    'not_run',count(*) FILTER(WHERE item->>'status'='not_run')) INTO projected_counts
  FROM jsonb_array_elements(b.result->'members') item;
  IF b.result_provenance IS NULL OR b.finalized_at IS NULL OR b.result_counts IS NULL
    OR b.result_counts IS DISTINCT FROM projected_counts
    OR jsonb_array_length(b.result->'members')<>b.member_count
    OR b.result->>'manifest_hash' IS DISTINCT FROM b.manifest_hash
    OR b.result->>'batch_id' IS DISTINCT FROM b.batch_id
    OR octet_length(b.result::text)>least(65536,512+512*b.member_count) THEN
    RAISE EXCEPTION 'invalid retained batch result' USING ERRCODE='23514';
  END IF;
  IF (b.result_provenance='worker_validated' AND b.result_attempt_id IS NULL)
     OR (b.result_attempt_id IS NOT NULL AND NOT EXISTS(
       SELECT 1 FROM execution_attempts WHERE id=b.result_attempt_id AND execution_id=target))
     OR (b.result_provenance='control_not_started' AND EXISTS(
       SELECT 1 FROM execution_attempts WHERE execution_id=target)) THEN
    RAISE EXCEPTION 'batch result proof differs from authoritative attempt' USING ERRCODE='23514';
  END IF;
 END IF;
 INSERT INTO pg_temp.dlr_batch_validation_cache VALUES(target) ON CONFLICT DO NOTHING;
END $$;
"""

# Each array is materialized once. LEFT JOIN retains missing-member rejection.
_VALIDATE = """
CREATE OR REPLACE FUNCTION dlr_batch_validate(target bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE b subscription_execution_batches%ROWTYPE; e executions%ROWTYPE;
 actual_count integer; low_index integer; high_index integer;
 paid_input bigint; paid_batch bigint;
 projected_counts jsonb; input_members jsonb;
BEGIN
 IF target IS NULL THEN RETURN; END IF;
 IF to_regclass('pg_temp.dlr_batch_validation_cache') IS NULL THEN
  CREATE TEMP TABLE dlr_batch_validation_cache(execution_id bigint PRIMARY KEY) ON COMMIT DELETE
  ROWS;
 END IF;
 IF EXISTS (SELECT 1 FROM pg_temp.dlr_batch_validation_cache WHERE execution_id=target) THEN
  RETURN; END IF;
 SELECT * INTO b FROM subscription_execution_batches WHERE execution_id=target;
 IF NOT FOUND THEN
  IF EXISTS(SELECT 1 FROM subscription_execution_batch_members WHERE execution_id=target)
     OR EXISTS(SELECT 1 FROM event_deliveries WHERE batch_execution_id=target) THEN
    RAISE EXCEPTION 'batch authority is missing' USING ERRCODE='23514';
  END IF;
  RETURN;
 END IF;
 SELECT * INTO e FROM executions WHERE id=target;
 IF e.subscription_delivery_kind IS DISTINCT FROM 'batch'
    OR EXISTS(SELECT 1 FROM event_deliveries WHERE execution_id=target) THEN
  RAISE EXCEPTION 'mixed single and batch execution' USING ERRCODE='23514';
 END IF;
 input_members:=e.input->'members';
 SELECT count(*),min(member_index),max(member_index) INTO actual_count,low_index,high_index
 FROM subscription_execution_batch_members WHERE execution_id=target;
 SELECT coalesce(sum(d.retained_execution_bytes),0),coalesce(sum(d.retained_batch_bytes),0)
 INTO paid_input,paid_batch FROM subscription_execution_batch_members m
 JOIN event_deliveries d ON d.id=m.delivery_id WHERE m.execution_id=target;
 IF actual_count<>b.member_count OR low_index<>0 OR high_index<>b.member_count-1
   OR paid_input<>b.retained_input_bytes OR paid_batch<>b.retained_batch_bytes
   OR EXISTS(SELECT 1 FROM event_deliveries d WHERE d.batch_execution_id=target
       AND NOT EXISTS(SELECT 1 FROM subscription_execution_batch_members m WHERE
  m.delivery_id=d.id))
   OR jsonb_typeof(b.manifest)<>'array' OR jsonb_array_length(b.manifest)<>b.member_count
   OR e.input->>'schema' IS DISTINCT FROM 'dlr.event.batch.v1'
   OR e.input->>'batch_id' IS DISTINCT FROM b.batch_id
   OR e.input->>'manifest_hash' IS DISTINCT FROM b.manifest_hash
   OR jsonb_typeof(input_members) IS DISTINCT FROM 'array'
   OR jsonb_array_length(input_members)<>b.member_count THEN
  RAISE EXCEPTION 'batch member set differs from frozen manifest' USING ERRCODE='23514';
 END IF;
 IF EXISTS (
  WITH input_rows AS MATERIALIZED (
   SELECT ordinality-1 AS member_index, value AS member
   FROM jsonb_array_elements(input_members) WITH ORDINALITY AS item(value,ordinality)
  ), manifest_rows AS MATERIALIZED (
   SELECT ordinality-1 AS member_index, value AS member
   FROM jsonb_array_elements(b.manifest) WITH ORDINALITY AS item(value,ordinality)
  )
  SELECT 1 FROM subscription_execution_batch_members m
  JOIN event_deliveries d ON d.id=m.delivery_id JOIN event_receipts r ON r.id=m.receipt_id
  JOIN subscription_activations a ON a.id=coalesce(d.activation_override_id,r.activation_id)
  JOIN subscription_config_versions c ON c.id=a.config_version_id
  JOIN adapter_versions v ON v.id=coalesce(d.revision_override_id,a.revision_id)
  LEFT JOIN input_rows supplied ON supplied.member_index=m.member_index
  LEFT JOIN manifest_rows fixed ON fixed.member_index=m.member_index
  WHERE m.execution_id=target AND (
   d.receipt_id<>m.receipt_id OR d.batch_execution_id IS DISTINCT FROM target OR d.execution_id
  IS NOT NULL
   OR r.subscription_id<>b.subscription_id OR r.source_epoch<>b.source_epoch OR r.shard<>b.shard
   OR a.id<>b.activation_id OR v.id<>b.revision_id
   OR d.revision_override_id IS DISTINCT FROM b.revision_override_id
   OR c.config->>'source_type' NOT IN ('http','rabbitmq')
   OR c.config->>'mode' IS DISTINCT FROM 'business_batch'
   OR v.runtime_config->>'event_batch_schema' IS DISTINCT FROM 'dlr.event.batch.v1'
   OR fixed.member->>'receipt_id' IS DISTINCT FROM r.id::text
   OR fixed.member->>'delivery_id' IS DISTINCT FROM d.id::text
   OR fixed.member->>'delivery_ordinal' IS DISTINCT FROM d.ordinal::text
   OR fixed.member->>'member_index' IS DISTINCT FROM m.member_index::text
   OR fixed.member->>'content_hash' IS DISTINCT FROM r.content_hash
   OR supplied.member->>'receipt_id' IS DISTINCT FROM r.id::text
   OR supplied.member->>'delivery_id' IS DISTINCT FROM d.id::text
   OR supplied.member->>'delivery_ordinal' IS DISTINCT FROM d.ordinal::text
   OR supplied.member->>'member_index' IS DISTINCT FROM m.member_index::text
   OR (r.envelope IS NOT NULL AND supplied.member->'input' IS DISTINCT FROM
  r.envelope)
  )
 ) THEN RAISE EXCEPTION 'batch member binding differs' USING ERRCODE='23514'; END IF;
 IF b.result IS NOT NULL THEN
  SELECT jsonb_build_object(
    'succeeded',count(*) FILTER(WHERE item->>'status'='succeeded'),
    'failed',count(*) FILTER(WHERE item->>'status'='failed'),
    'unknown',count(*) FILTER(WHERE item->>'status'='unknown'),
    'not_run',count(*) FILTER(WHERE item->>'status'='not_run')) INTO projected_counts
  FROM jsonb_array_elements(b.result->'members') item;
  IF b.result_provenance IS NULL OR b.finalized_at IS NULL OR b.result_counts IS NULL
    OR b.result_counts IS DISTINCT FROM projected_counts
    OR jsonb_array_length(b.result->'members')<>b.member_count
    OR b.result->>'manifest_hash' IS DISTINCT FROM b.manifest_hash
    OR b.result->>'batch_id' IS DISTINCT FROM b.batch_id
    OR octet_length(b.result::text)>least(65536,512+512*b.member_count) THEN
    RAISE EXCEPTION 'invalid retained batch result' USING ERRCODE='23514';
  END IF;
  IF (b.result_provenance='worker_validated' AND b.result_attempt_id IS NULL)
     OR (b.result_attempt_id IS NOT NULL AND NOT EXISTS(
       SELECT 1 FROM execution_attempts WHERE id=b.result_attempt_id AND execution_id=target))
     OR (b.result_provenance='control_not_started' AND EXISTS(
       SELECT 1 FROM execution_attempts WHERE execution_id=target)) THEN
    RAISE EXCEPTION 'batch result proof differs from authoritative attempt' USING ERRCODE='23514';
  END IF;
 END IF;
 INSERT INTO pg_temp.dlr_batch_validation_cache VALUES(target) ON CONFLICT DO NOTHING;
END $$;
"""
