"""Immutable business-batch membership and database-enforced execution ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_issue222_business_batches"
down_revision = "0051_issue222_durable_http"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("subscription_delivery_kind", sa.String(16)))
    op.create_check_constraint(
        "ck_execution_subscription_kind",
        "executions",
        "subscription_delivery_kind IS NULL OR subscription_delivery_kind IN ('single','batch')",
    )
    op.execute(
        "UPDATE executions e SET subscription_delivery_kind='single' WHERE EXISTS "
        "(SELECT 1 FROM event_deliveries d WHERE d.execution_id=e.id)"
    )
    for column in (
        sa.Column("batch_execution_id", sa.BigInteger()),
        sa.Column("batch_member_outcome", sa.String(16)),
        sa.Column("batch_member_code", sa.String(64)),
        sa.Column("retained_batch_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    ):
        op.add_column("event_deliveries", column)
    op.create_unique_constraint(
        "uq_delivery_batch_identity", "event_deliveries", ["id", "receipt_id", "batch_execution_id"]
    )
    op.create_check_constraint(
        "ck_delivery_single_or_batch",
        "event_deliveries",
        "execution_id IS NULL OR batch_execution_id IS NULL",
    )
    op.create_check_constraint(
        "ck_delivery_batch_bytes", "event_deliveries", "retained_batch_bytes BETWEEN 0 AND 8192"
    )
    op.create_check_constraint(
        "ck_delivery_batch_outcome",
        "event_deliveries",
        "batch_member_outcome IS NULL OR batch_member_outcome IN "
        "('succeeded','failed','unknown','not_run')",
    )
    op.create_table(
        "subscription_execution_batches",
        sa.Column(
            "execution_id",
            sa.BigInteger(),
            sa.ForeignKey("executions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("batch_id", sa.String(32), nullable=False, unique=True),
        sa.Column("mode", sa.String(32), nullable=False, server_default="business_batch"),
        sa.Column(
            "subscription_id",
            sa.BigInteger(),
            sa.ForeignKey("event_subscriptions.id"),
            nullable=False,
        ),
        sa.Column("source_epoch", sa.String(64), nullable=False),
        sa.Column("shard", sa.String(512), nullable=False),
        sa.Column(
            "activation_id",
            sa.BigInteger(),
            sa.ForeignKey("subscription_activations.id"),
            nullable=False,
        ),
        sa.Column(
            "revision_id", sa.BigInteger(), sa.ForeignKey("adapter_versions.id"), nullable=False
        ),
        sa.Column("revision_override_id", sa.BigInteger(), sa.ForeignKey("adapter_versions.id")),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("member_count", sa.SmallInteger(), nullable=False),
        sa.Column("input_bytes", sa.BigInteger(), nullable=False),
        sa.Column("retained_input_bytes", sa.BigInteger(), nullable=False),
        sa.Column("retained_batch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("result_counts", postgresql.JSONB(none_as_null=True)),
        sa.Column("result_provenance", sa.String(32)),
        sa.Column(
            "result_attempt_id",
            sa.BigInteger(),
            sa.ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("member_count BETWEEN 1 AND 100", name="ck_execution_batch_count"),
        sa.CheckConstraint(
            "input_bytes BETWEEN 1 AND 262144 AND retained_input_bytes >= input_bytes "
            "AND retained_batch_bytes BETWEEN 0 AND 819200",
            name="ck_execution_batch_bytes",
        ),
        sa.CheckConstraint("mode='business_batch'", name="ck_execution_batch_mode"),
        sa.CheckConstraint(
            "result_provenance IS NULL OR result_provenance IN "
            "('worker_validated','control_unknown','control_not_started')",
            name="ck_execution_batch_provenance",
        ),
    )
    op.create_index(
        "ix_subscription_execution_batches_subscription_id",
        "subscription_execution_batches",
        ["subscription_id"],
    )
    op.create_index(
        "ix_execution_batch_cleanup",
        "subscription_execution_batches",
        ["subscription_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_delivery_business_batch",
        "event_deliveries",
        "subscription_execution_batches",
        ["batch_execution_id"],
        ["execution_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "subscription_execution_batch_members",
        sa.Column("delivery_id", sa.BigInteger(), primary_key=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "execution_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "subscription_execution_batches.execution_id", deferrable=True, initially="DEFERRED"
            ),
            nullable=False,
        ),
        sa.Column("member_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["delivery_id", "receipt_id", "execution_id"],
            [
                "event_deliveries.id",
                "event_deliveries.receipt_id",
                "event_deliveries.batch_execution_id",
            ],
            name="fk_execution_batch_member_delivery",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("execution_id", "receipt_id", name="uq_execution_batch_receipt"),
        sa.UniqueConstraint("execution_id", "member_index", name="uq_execution_batch_member_index"),
        sa.CheckConstraint("member_index BETWEEN 0 AND 99", name="ck_execution_batch_member_index"),
    )
    op.create_index(
        "ix_subscription_execution_batch_members_execution_id",
        "subscription_execution_batch_members",
        ["execution_id"],
    )
    op.create_index(
        "ix_subscription_execution_batch_members_receipt_id",
        "subscription_execution_batch_members",
        ["receipt_id"],
    )
    op.create_index(
        "ix_delivery_batch_execution",
        "event_deliveries",
        ["batch_execution_id"],
        postgresql_where=sa.text("batch_execution_id IS NOT NULL"),
    )
    op.execute(_KIND_GUARD)
    op.execute(_IMMUTABLE_GUARD)
    op.execute(_VALIDATE)
    op.execute(_DEFERRED)
    op.execute(_DELETE_GUARD)
    for statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM executions WHERE subscription_delivery_kind='batch') "
            "OR EXISTS (SELECT 1 FROM subscription_config_versions "
            "WHERE config->>'mode'='business_batch')"
        )
    ):
        raise RuntimeError(
            "Preserve business-batch responsibility; use a compatible application rollback"
        )
    for name, table in (
        ("tr_delivery_owner", "event_deliveries"),
        ("tr_batch_owner", "subscription_execution_batches"),
        ("tr_execution_kind_immutable", "executions"),
        ("tr_batch_identity_immutable", "subscription_execution_batches"),
        ("tr_member_identity_immutable", "subscription_execution_batch_members"),
        ("tr_delivery_batch_immutable", "event_deliveries"),
        ("tr_batch_set_dirty", "subscription_execution_batches"),
        ("tr_member_set_dirty", "subscription_execution_batch_members"),
        ("tr_delivery_set_dirty", "event_deliveries"),
        ("tr_execution_set_dirty", "executions"),
        ("tr_execution_batch_deferred", "executions"),
        ("tr_batch_set_deferred", "subscription_execution_batches"),
        ("tr_member_set_deferred", "subscription_execution_batch_members"),
        ("tr_delivery_set_deferred", "event_deliveries"),
        ("tr_batch_delete_guard", "subscription_execution_batches"),
        ("tr_receipt_batch_immutable", "event_receipts"),
    ):
        op.execute(f"DROP TRIGGER {name} ON {table}")
    for name, signature in (
        ("dlr_batch_delete_guard", ""),
        ("dlr_batch_deferred", ""),
        ("dlr_batch_dirty", ""),
        ("dlr_batch_validate", "bigint"),
        ("dlr_batch_immutable", ""),
        ("dlr_batch_owner", ""),
    ):
        op.execute(f"DROP FUNCTION {name}({signature})")
    op.drop_table("subscription_execution_batch_members")
    op.drop_constraint("fk_delivery_business_batch", "event_deliveries", type_="foreignkey")
    op.drop_table("subscription_execution_batches")
    op.drop_index("ix_delivery_batch_execution", table_name="event_deliveries")
    for name in ("uq_delivery_batch_identity",):
        op.drop_constraint(name, "event_deliveries", type_="unique")
    for name in (
        "ck_delivery_single_or_batch",
        "ck_delivery_batch_bytes",
        "ck_delivery_batch_outcome",
    ):
        op.drop_constraint(name, "event_deliveries", type_="check")
    for column in (
        "batch_execution_id",
        "batch_member_outcome",
        "batch_member_code",
        "retained_batch_bytes",
    ):
        op.drop_column("event_deliveries", column)
    op.drop_constraint("ck_execution_subscription_kind", "executions", type_="check")
    op.drop_column("executions", "subscription_delivery_kind")


_KIND_GUARD = """
CREATE FUNCTION dlr_batch_owner() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint; wanted text;
BEGIN
 IF TG_TABLE_NAME='event_deliveries' THEN
  IF EXISTS (SELECT 1 FROM event_receipts r
    JOIN subscription_activations a ON a.id=coalesce(NEW.activation_override_id,r.activation_id)
    JOIN subscription_config_versions c ON c.id=a.config_version_id
    WHERE r.id=NEW.receipt_id AND c.config->>'mode'='business_batch') THEN
    IF NEW.execution_id IS NOT NULL THEN
     RAISE EXCEPTION 'business batch requires immutable membership' USING ERRCODE='23514';
    END IF;
    IF NEW.retained_batch_bytes<>8192 OR NEW.retained_execution_bytes<=0 THEN
     RAISE EXCEPTION 'business batch requires prepaid bounded shares' USING ERRCODE='23514';
    END IF;
  END IF;
  IF NEW.execution_id IS NULL THEN RETURN NEW; END IF;
  target:=NEW.execution_id; wanted:='single';
 ELSE target:=NEW.execution_id; wanted:='batch'; END IF;
 UPDATE executions SET subscription_delivery_kind=wanted
 WHERE id=target AND trigger='subscription'
   AND (subscription_delivery_kind IS NULL OR subscription_delivery_kind=wanted);
 IF NOT FOUND THEN RAISE EXCEPTION 'incompatible subscription execution owner' USING
  ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
"""

_IMMUTABLE_GUARD = """
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

_VALIDATE = """
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

_DEFERRED = """
CREATE FUNCTION dlr_batch_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint;
BEGIN
 IF TG_TABLE_NAME='executions' THEN target:=NEW.id;
 ELSIF TG_TABLE_NAME='event_deliveries' THEN target:=CASE WHEN TG_OP='DELETE' THEN
  OLD.batch_execution_id ELSE NEW.batch_execution_id END;
 ELSE target:=CASE WHEN TG_OP='DELETE' THEN OLD.execution_id ELSE NEW.execution_id END; END IF;
 IF target IS NOT NULL AND to_regclass('pg_temp.dlr_batch_validation_cache') IS NOT NULL THEN
  DELETE FROM pg_temp.dlr_batch_validation_cache WHERE execution_id=target;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE FUNCTION dlr_batch_deferred() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint; e executions%ROWTYPE; b subscription_execution_batches%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='executions' THEN
  IF NEW.subscription_delivery_kind IS NULL THEN RETURN NULL; END IF;
  SELECT * INTO e FROM executions WHERE id=NEW.id;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF e.subscription_delivery_kind='batch' THEN
   SELECT * INTO b FROM subscription_execution_batches WHERE execution_id=e.id;
   IF NOT FOUND THEN
    IF e.input IS DISTINCT FROM 'null'::jsonb THEN RAISE EXCEPTION 'batch authority missing'
  USING ERRCODE='23514'; END IF;
   ELSIF e.status IN ('succeeded','dead_letter','cancelled','expired') THEN
    IF b.result IS NULL OR b.finalized_at IS NULL OR b.result_counts IS NULL THEN
     RAISE EXCEPTION 'batch terminal lacks validated member result' USING ERRCODE='23514';
    END IF;
    IF e.status='succeeded' AND (b.result_counts->>'succeeded')::integer
       IS DISTINCT FROM b.member_count THEN
     RAISE EXCEPTION 'batch logical status differs from members' USING ERRCODE='23514';
    END IF;
   END IF;
   PERFORM dlr_batch_validate(e.id);
  ELSIF EXISTS(SELECT 1 FROM subscription_execution_batches WHERE execution_id=e.id) THEN
   RAISE EXCEPTION 'mixed execution kind' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
 END IF;
 IF TG_TABLE_NAME='event_deliveries' THEN target:=CASE WHEN TG_OP='DELETE' THEN
  OLD.batch_execution_id ELSE NEW.batch_execution_id END;
 ELSE target:=CASE WHEN TG_OP='DELETE' THEN OLD.execution_id ELSE NEW.execution_id END; END IF;
 PERFORM dlr_batch_validate(target);
 RETURN NULL;
END $$;
"""

_DELETE_GUARD = """
CREATE FUNCTION dlr_batch_delete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE permanent boolean;
BEGIN
 SELECT (s.deleted_at IS NOT NULL AND s.desired_state='disabled' AND s.connection_state='stopped'
  AND a.archived_at IS NOT NULL)
 INTO permanent FROM event_subscriptions s JOIN adapters a ON a.id=s.adapter_id WHERE
  s.id=OLD.subscription_id;
 IF EXISTS(
  SELECT 1 FROM jsonb_array_elements(OLD.manifest) item
  LEFT JOIN event_receipts r ON r.id=(item->>'receipt_id')::bigint
  WHERE r.id IS NULL OR r.state NOT IN ('succeeded','skipped','superseded','abandoned')
   OR r.terminal_at IS NULL OR (NOT coalesce(permanent,false) AND r.terminal_at>now()-interval
  '30 days')
 ) OR EXISTS(SELECT 1 FROM executions WHERE id=OLD.execution_id AND status NOT IN
  ('succeeded','dead_letter','cancelled','expired')) THEN
  RAISE EXCEPTION 'batch is still protected' USING ERRCODE='23514';
 END IF;
 RETURN OLD;
END $$;
"""

_TRIGGERS = (
    "CREATE TRIGGER tr_receipt_batch_immutable BEFORE UPDATE ON event_receipts "
    "FOR EACH ROW EXECUTE FUNCTION dlr_batch_immutable()",
    (
        "CREATE TRIGGER tr_delivery_owner BEFORE INSERT OR UPDATE OF "
        "execution_id,activation_override_id ON event_deliveries FOR EACH ROW EXECUTE "
        "FUNCTION dlr_batch_owner()"
    ),
    (
        "CREATE TRIGGER tr_batch_owner BEFORE INSERT ON subscription_execution_batches "
        "FOR EACH ROW EXECUTE FUNCTION dlr_batch_owner()"
    ),
    (
        "CREATE TRIGGER tr_execution_kind_immutable BEFORE UPDATE ON executions FOR "
        "EACH ROW EXECUTE FUNCTION dlr_batch_immutable()"
    ),
    (
        "CREATE TRIGGER tr_batch_identity_immutable BEFORE UPDATE ON "
        "subscription_execution_batches FOR EACH ROW EXECUTE FUNCTION "
        "dlr_batch_immutable()"
    ),
    (
        "CREATE TRIGGER tr_member_identity_immutable BEFORE UPDATE ON "
        "subscription_execution_batch_members FOR EACH ROW EXECUTE FUNCTION "
        "dlr_batch_immutable()"
    ),
    (
        "CREATE TRIGGER tr_delivery_batch_immutable BEFORE UPDATE ON event_deliveries "
        "FOR EACH ROW EXECUTE FUNCTION dlr_batch_immutable()"
    ),
    (
        "CREATE TRIGGER tr_batch_set_dirty AFTER INSERT OR UPDATE OR DELETE ON "
        "subscription_execution_batches FOR EACH ROW EXECUTE FUNCTION dlr_batch_dirty()"
    ),
    (
        "CREATE TRIGGER tr_member_set_dirty AFTER INSERT OR UPDATE OR DELETE ON "
        "subscription_execution_batch_members FOR EACH ROW EXECUTE FUNCTION "
        "dlr_batch_dirty()"
    ),
    (
        "CREATE TRIGGER tr_delivery_set_dirty AFTER INSERT OR UPDATE OR DELETE ON "
        "event_deliveries FOR EACH ROW EXECUTE FUNCTION dlr_batch_dirty()"
    ),
    (
        "CREATE TRIGGER tr_execution_set_dirty AFTER INSERT OR UPDATE ON executions FOR "
        "EACH ROW EXECUTE FUNCTION dlr_batch_dirty()"
    ),
    (
        "CREATE CONSTRAINT TRIGGER tr_execution_batch_deferred AFTER INSERT OR UPDATE "
        "ON executions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        "dlr_batch_deferred()"
    ),
    (
        "CREATE CONSTRAINT TRIGGER tr_batch_set_deferred AFTER INSERT OR UPDATE OR "
        "DELETE ON subscription_execution_batches DEFERRABLE INITIALLY DEFERRED FOR "
        "EACH ROW EXECUTE FUNCTION dlr_batch_deferred()"
    ),
    (
        "CREATE CONSTRAINT TRIGGER tr_member_set_deferred AFTER INSERT OR UPDATE OR "
        "DELETE ON subscription_execution_batch_members DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION dlr_batch_deferred()"
    ),
    (
        "CREATE CONSTRAINT TRIGGER tr_delivery_set_deferred AFTER INSERT OR UPDATE OR "
        "DELETE ON event_deliveries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE "
        "FUNCTION dlr_batch_deferred()"
    ),
    (
        "CREATE TRIGGER tr_batch_delete_guard BEFORE DELETE ON "
        "subscription_execution_batches FOR EACH ROW EXECUTE FUNCTION "
        "dlr_batch_delete_guard()"
    ),
)
