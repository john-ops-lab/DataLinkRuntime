"""Keep the one-time legacy upgrade authorization immutable."""

from alembic import op

revision = "0090_issue129_worker_cutover"
down_revision = "0089_issue129_credential_ops"
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
_LEGACY_ACTION = """
    (action IN ('drain', 'activate') AND reason IS NULL AND recovery_binding IS NULL) OR
    (action = 'force_stop' AND reason IS NOT NULL AND length(reason) BETWEEN 1 AND 256
      AND length(btrim(reason)) > 0 AND recovery_binding IS NULL) OR
    (action = 'reconcile' AND reason IS NULL AND recovery_binding IS NOT NULL
      AND jsonb_typeof(recovery_binding) = 'object')
"""
_FORCE_ACTION = """
    (action IN ('drain', 'activate') AND reason IS NULL AND recovery_binding IS NULL) OR
    (action = 'force_stop' AND reason IS NOT NULL AND length(reason) BETWEEN 1 AND 256
      AND length(btrim(reason)) > 0 AND ((
    jsonb_typeof(recovery_binding) = 'object'
    AND recovery_binding ?& ARRAY['attempt_id','fencing_token','worker_identity_id',
        'original_instance_id','original_instance_generation']
    AND recovery_binding - ARRAY['attempt_id','fencing_token','worker_identity_id',
        'original_instance_id','original_instance_generation'] = '{}'::jsonb
    AND recovery_binding->>'worker_identity_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_binding->>'worker_identity_id' <> '00000000-0000-0000-0000-000000000000'
    AND recovery_binding->>'original_instance_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_binding->>'original_instance_id' <> '00000000-0000-0000-0000-000000000000'
    AND recovery_binding->>'attempt_id' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'attempt_id') = 'number'
    AND recovery_binding->>'fencing_token' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'fencing_token') = 'number'
    AND recovery_binding->>'original_instance_generation' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'original_instance_generation') = 'number'
    ) IS TRUE)) OR (action = 'reconcile' AND reason IS NULL AND recovery_binding IS NOT NULL
        AND jsonb_typeof(recovery_binding) = 'object')
"""


def _maintenance_checks(action: str, disposition: str) -> None:
    table = "worker_maintenance_operations"
    for name, condition in (
        ("ck_worker_maintenance_operation_action", action),
        ("ck_worker_recovery_disposition_complete", disposition),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)


_CACHE_OWNERS = (
    ("worker_cache_operations", "ck_cache_operation_v4_owner"),
    ("worker_cache_management_operations", "ck_cache_command_v4_owner"),
    ("worker_cache_snapshots", "ck_cache_snapshot_v4_owner"),
)

_ATTEMPT_RECEIPTS = (
    (
        "attempt_lease_candidates_v4",
        "fk_attempt_lease_candidate_attempt_binding",
        "instance_id",
        "instance_generation",
    ),
    (
        "attempt_result_intents_v4",
        "fk_attempt_result_intent_attempt_binding",
        "instance_id",
        "instance_generation",
    ),
    (
        "worker_cleanup_intents_v4",
        "fk_worker_cleanup_intent_attempt_binding",
        "original_instance_id",
        "original_generation",
    ),
)


def _attempt_receipt_fks(ondelete: str) -> None:
    for table, name, instance, generation in _ATTEMPT_RECEIPTS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            "execution_attempts",
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                instance,
                generation,
                "fencing_token",
            ],
            [
                "id",
                "worker_id",
                "worker_identity_id",
                "worker_instance_id",
                "worker_instance_generation",
                "fencing_token",
            ],
            ondelete=ondelete,
        )


def upgrade() -> None:
    _maintenance_checks(
        _FORCE_ACTION, f"({_LEGACY_DISPOSITION}) OR (({_FORCE_DISPOSITION}) IS TRUE)"
    )
    op.execute("""
        ALTER TABLE workers ADD COLUMN v4_storage_profile VARCHAR(16),
          ADD CONSTRAINT ck_workers_v4_storage_profile CHECK (
            v4_storage_profile IS NULL OR (
              v4_storage_profile IN ('ephemeral', 'managed_cache') AND protocol_version=4
            )
          )
    """)
    op.execute("""
        ALTER TABLE worker_cleanup_requests
          ADD COLUMN v4_identity_id UUID,
          ADD COLUMN v4_instance_id UUID,
          ADD COLUMN v4_generation BIGINT,
          ADD CONSTRAINT ck_worker_cleanup_v4_owner CHECK (
            (v4_instance_id IS NULL AND v4_generation IS NULL) OR
            (v4_identity_id IS NOT NULL AND v4_instance_id IS NOT NULL
             AND v4_generation IS NOT NULL AND v4_generation > 0)
          )
    """)
    for table, constraint in _CACHE_OWNERS:
        op.execute(f"""
            ALTER TABLE {table}
              ADD COLUMN v4_identity_id UUID,
              ADD COLUMN v4_instance_id UUID,
              ADD COLUMN v4_generation BIGINT,
              ADD CONSTRAINT {constraint} CHECK (
                (v4_identity_id IS NULL AND v4_instance_id IS NULL AND v4_generation IS NULL) OR
                (v4_identity_id IS NOT NULL AND v4_instance_id IS NOT NULL
                 AND v4_generation IS NOT NULL AND v4_generation > 0)
              )
        """)
    _attempt_receipt_fks("CASCADE")
    op.execute("""
        ALTER TABLE execution_attempts ADD COLUMN journal_retired_at TIMESTAMPTZ
    """)
    op.execute("""
        CREATE FUNCTION dlr_v4_confirm_journal_retirement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.journal_retired_at IS NULL AND TG_OP='INSERT' THEN
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' THEN
                IF NEW.journal_retired_at IS NOT DISTINCT FROM OLD.journal_retired_at THEN
                    RETURN NEW;
                END IF;
                IF OLD.journal_retired_at IS NULL AND NEW.cleanup_settled_at IS NOT NULL
                    AND NEW.journal_retired_at >= NEW.cleanup_settled_at
                    AND EXISTS (
                        SELECT FROM worker_cleanup_intents_v4 c JOIN workers w ON w.id=c.worker_id
                        WHERE c.attempt_id=OLD.id AND c.worker_identity_id=w.identity_id
                          AND c.instance_id=w.instance_id
                          AND c.instance_generation=w.instance_generation
                          AND w.instance_lease_until > clock_timestamp()
                          AND c.completion_id IS NOT NULL AND c.completion_xid=c.writer_xid
                    ) THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'v4 journal retirement unavailable' USING ERRCODE='23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_v4_confirm_journal_retirement
        BEFORE INSERT OR UPDATE OF journal_retired_at ON execution_attempts
        FOR EACH ROW EXECUTE FUNCTION dlr_v4_confirm_journal_retirement()
    """)
    op.execute("""
        ALTER TABLE execution_attempts ENABLE ALWAYS TRIGGER dlr_v4_confirm_journal_retirement
    """)
    op.execute("""
        CREATE FUNCTION dlr_v4_require_retired_adapter_attempt() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.worker_identity_id IS NOT NULL AND (
                OLD.cleanup_settled_at IS NULL
                OR OLD.status NOT IN ('succeeded','failed','timed_out','worker_lost','cancelled')
                OR NOT EXISTS (
                    SELECT FROM worker_cleanup_intents_v4 c
                    WHERE c.attempt_id=OLD.id AND c.completion_id IS NOT NULL
                      AND c.completion_xid=c.writer_xid
                      AND (OLD.journal_retired_at >= OLD.cleanup_settled_at OR EXISTS (
                        SELECT FROM executions e
                        JOIN adapters a ON a.id=e.adapter_id AND a.archived_at IS NOT NULL
                        JOIN worker_cleanup_requests r ON r.adapter_id=a.id
                          AND r.worker_id=OLD.worker_id
                          AND r.v4_identity_id=OLD.worker_identity_id
                          AND r.status='completed' AND r.completed_at IS NOT NULL
                        WHERE e.id=OLD.execution_id
                      ))
                )
            ) THEN
                RAISE EXCEPTION 'v4 Attempt has retained local responsibility'
                    USING ERRCODE='23514';
            END IF;
            RETURN OLD;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_v4_require_retired_adapter_attempt
        BEFORE DELETE ON execution_attempts FOR EACH ROW
        EXECUTE FUNCTION dlr_v4_require_retired_adapter_attempt()
    """)
    op.execute("""
        ALTER TABLE execution_attempts ENABLE ALWAYS
        TRIGGER dlr_v4_require_retired_adapter_attempt
    """)
    op.execute("""
        ALTER TABLE worker_identity_reservations ADD COLUMN legacy_stop_evidence_sha256 VARCHAR(64)
        CONSTRAINT ck_worker_identity_reservation_legacy_evidence
            CHECK (legacy_stop_evidence_sha256 ~ '^[a-f0-9]{64}$')
    """)
    op.execute("""
        CREATE FUNCTION dlr_worker_legacy_window_once() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' AND current_user = session_user
              AND current_user = pg_catalog.pg_get_userbyid(
                (SELECT relowner FROM pg_catalog.pg_class WHERE oid=TG_RELID)
              ) AND NEW.id = OLD.id AND (
              (OLD.opened_at IS NULL AND NEW.opened_at IS NOT NULL
                AND NEW.closed_at IS NULL AND NEW.deadline_at > pg_catalog.clock_timestamp())
              OR
              (OLD.opened_at IS NOT NULL AND OLD.opened_at = NEW.opened_at
                AND OLD.deadline_at = NEW.deadline_at
                AND (OLD.closed_at IS NULL OR OLD.closed_at = NEW.closed_at))
          ) THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'Worker legacy window transition is immutable' USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_legacy_window_once
        BEFORE INSERT OR UPDATE OR DELETE ON worker_legacy_window
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_legacy_window_once()
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_legacy_window_no_truncate
        BEFORE TRUNCATE ON worker_legacy_window
        FOR EACH STATEMENT EXECUTE FUNCTION dlr_worker_legacy_window_once()
    """)
    op.execute("""
        ALTER TABLE worker_legacy_window ENABLE ALWAYS TRIGGER dlr_worker_legacy_window_once
    """)
    op.execute("""
        ALTER TABLE worker_legacy_window ENABLE ALWAYS TRIGGER dlr_worker_legacy_window_no_truncate
    """)
    op.execute("""
        CREATE TABLE control_proof_rotations (
            generation BIGINT PRIMARY KEY CHECK (generation > 0),
            operation_id UUID NOT NULL UNIQUE,
            source_epoch UUID NOT NULL UNIQUE,
            target_epoch UUID NOT NULL UNIQUE,
            source_key BYTEA NOT NULL CHECK (octet_length(source_key)=32),
            target_key BYTEA NOT NULL UNIQUE CHECK (octet_length(target_key)=32),
            image_digest VARCHAR(71) NOT NULL,
            image_transition_id UUID REFERENCES control_image_transitions(operation_id),
            evidence_sha256 BYTEA NOT NULL CHECK (octet_length(evidence_sha256)=32),
            writer_xid BIGINT NOT NULL CHECK (writer_xid > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CHECK (operation_id <> '00000000-0000-0000-0000-000000000000'::uuid
                   AND source_epoch <> target_epoch AND source_key <> target_key
                   AND target_epoch <> '00000000-0000-0000-0000-000000000000'::uuid)
        )
    """)
    op.execute("""
        CREATE FUNCTION dlr_control_proof_rotation_append() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            base control_cluster_authority%ROWTYPE;
            previous control_proof_rotations%ROWTYPE;
            head UUID;
        BEGIN
            IF TG_OP <> 'INSERT' OR current_user <> session_user
                OR current_user <> pg_catalog.pg_get_userbyid(
                    (SELECT relowner FROM pg_catalog.pg_class WHERE oid=TG_RELID)) THEN
                RAISE EXCEPTION 'Control proof rotation is owner append-only' USING ERRCODE='23514';
            END IF;
            SELECT * INTO STRICT base FROM control_cluster_authority WHERE singleton=1 FOR UPDATE;
            SELECT * INTO previous FROM control_proof_rotations ORDER BY generation DESC LIMIT 1;
            SELECT operation_id INTO head FROM control_image_transitions t
                WHERE NOT EXISTS (SELECT 1 FROM control_image_transitions n
                                  WHERE n.previous_id=t.operation_id);
            IF NEW.generation <> COALESCE(previous.generation,0)+1
                OR NEW.source_epoch IS DISTINCT FROM
                    COALESCE(previous.target_epoch,base.authority_epoch)
                OR NEW.source_key IS DISTINCT FROM
                    COALESCE(previous.target_key,base.worker_proof_public_key)
                OR NEW.target_epoch = base.authority_epoch
                OR NEW.target_key = base.worker_proof_public_key
                OR NEW.image_digest <> base.image_digest
                OR NEW.image_transition_id IS DISTINCT FROM head
                OR NEW.writer_xid <> pg_catalog.txid_current() THEN
                RAISE EXCEPTION 'Control proof rotation source unavailable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_control_proof_rotation_append
        BEFORE INSERT OR UPDATE OR DELETE ON control_proof_rotations
        FOR EACH ROW EXECUTE FUNCTION dlr_control_proof_rotation_append()
    """)
    op.execute("""
        ALTER TABLE control_proof_rotations ENABLE ALWAYS TRIGGER dlr_control_proof_rotation_append
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE worker_legacy_window, control_proof_rotations, worker_maintenance_operations, "
        "worker_cleanup_requests, execution_attempts IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_legacy_window WHERE opened_at IS NOT NULL) THEN
                RAISE EXCEPTION 'legacy upgrade history prevents downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM control_proof_rotations) THEN
                RAISE EXCEPTION 'Control proof rotation history prevents downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM worker_maintenance_operations WHERE action = 'force_stop'
                       OR recovery_disposition->>'kind' = 'force_stopped_no_result') THEN
                RAISE EXCEPTION 'force-stop responsibility prevents downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM worker_cleanup_requests WHERE v4_identity_id IS NOT NULL) THEN
                RAISE EXCEPTION 'v4 Adapter cleanup responsibility prevents downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM execution_attempts WHERE journal_retired_at IS NOT NULL) THEN
                RAISE EXCEPTION 'v4 journal retirement history prevents downgrade';
            END IF;
        END $$
    """)
    _maintenance_checks(_LEGACY_ACTION, _LEGACY_DISPOSITION)
    op.execute("""
        ALTER TABLE workers DROP CONSTRAINT ck_workers_v4_storage_profile,
          DROP COLUMN v4_storage_profile
    """)
    op.execute("DROP TRIGGER dlr_v4_require_retired_adapter_attempt ON execution_attempts")
    op.execute("DROP FUNCTION dlr_v4_require_retired_adapter_attempt()")
    op.execute("DROP TRIGGER dlr_v4_confirm_journal_retirement ON execution_attempts")
    op.execute("DROP FUNCTION dlr_v4_confirm_journal_retirement()")
    op.execute("ALTER TABLE execution_attempts DROP COLUMN journal_retired_at")
    _attempt_receipt_fks("RESTRICT")
    for table, constraint in _CACHE_OWNERS:
        op.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM {table} WHERE v4_identity_id IS NOT NULL) THEN
                    RAISE EXCEPTION 'v4 cache ownership prevents downgrade';
                END IF;
            END $$
        """)
        op.execute(f"""
            ALTER TABLE {table} DROP CONSTRAINT {constraint},
              DROP COLUMN v4_generation, DROP COLUMN v4_instance_id, DROP COLUMN v4_identity_id
        """)
    op.execute("""
        ALTER TABLE worker_cleanup_requests DROP CONSTRAINT ck_worker_cleanup_v4_owner,
          DROP COLUMN v4_generation, DROP COLUMN v4_instance_id, DROP COLUMN v4_identity_id
    """)
    op.execute("DROP TRIGGER dlr_worker_legacy_window_once ON worker_legacy_window")
    op.execute("DROP TRIGGER dlr_worker_legacy_window_no_truncate ON worker_legacy_window")
    op.execute("DROP FUNCTION dlr_worker_legacy_window_once()")
    op.drop_table("worker_legacy_window")
    op.execute("ALTER TABLE worker_identity_reservations DROP COLUMN legacy_stop_evidence_sha256")
    op.drop_table("control_proof_rotations")
    op.execute("DROP FUNCTION dlr_control_proof_rotation_append()")
