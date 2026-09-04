"""Issue #130 B1 additive reliable-runtime schema.

The migration extends the existing legacy execution model without changing
the legacy Claim path.  Existing rows are explicitly backfilled as
``dispatch_backend=legacy``; RabbitMQ rows and the Attempt/Slot state machine
are only data contracts in this Batch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_issue130_reliable_runtime"
down_revision: str | None = "0029_issue127_c0_exec_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_TERMINAL_SQL = "('succeeded', 'failed', 'timeout', 'cancelled')"


def upgrade() -> None:
    # --- Execution: additive fields and deterministic legacy backfill -------
    execution_columns = (
        sa.Column(
            "dispatch_backend",
            sa.String(length=16),
            server_default=sa.text("'legacy'"),
            nullable=True,
        ),
        sa.Column(
            "dispatch_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "max_attempts_snapshot", sa.Integer(), server_default=sa.text("1"), nullable=True
        ),
        sa.Column(
            "retry_policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
        sa.Column(
            "resource_profile_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
        sa.Column(
            "credential_bindings_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=True,
        ),
        sa.Column(
            "schedule_policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("resource_class", sa.String(length=64), nullable=True),
        sa.Column("target_worker_id_snapshot", sa.BigInteger(), nullable=True),
        sa.Column(
            "logical_input_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column("idempotency_record_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("admission_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_of_execution_id", sa.BigInteger(), nullable=True),
    )
    for column in execution_columns:
        op.add_column("executions", column)

    op.execute(
        sa.text(
            "UPDATE executions SET "
            "dispatch_backend = COALESCE(dispatch_backend, 'legacy'), "
            "dispatch_generation = COALESCE(dispatch_generation, 0), "
            "attempt_count = COALESCE(attempt_count, 0), "
            "max_attempts_snapshot = COALESCE(max_attempts_snapshot, 1), "
            "retry_policy_snapshot = COALESCE(retry_policy_snapshot, '{}'::jsonb), "
            "resource_profile_snapshot = COALESCE(resource_profile_snapshot, '{}'::jsonb), "
            "credential_bindings_snapshot = COALESCE(credential_bindings_snapshot, '[]'::jsonb), "
            "resource_class = COALESCE(resource_class, 'legacy'), "
            "target_worker_id_snapshot = COALESCE(target_worker_id_snapshot, target_worker_id), "
            "logical_input_bytes = COALESCE(logical_input_bytes, "
            "CASE WHEN input_source_type = 'none' THEN 0 "
            "ELSE octet_length(convert_to(input::text, 'UTF8')) END), "
            "admission_released_at = CASE "
            "WHEN status IN "
            + LEGACY_TERMINAL_SQL
            + " THEN COALESCE(admission_released_at, ended_at, created_at) "
            "ELSE admission_released_at END "
            "WHERE dispatch_backend IS NULL OR dispatch_generation IS NULL "
            "OR attempt_count IS NULL OR max_attempts_snapshot IS NULL "
            "OR retry_policy_snapshot IS NULL OR resource_profile_snapshot IS NULL "
            "OR credential_bindings_snapshot IS NULL "
            "OR resource_class IS NULL OR target_worker_id_snapshot IS NULL "
            "OR logical_input_bytes IS NULL"
        )
    )
    for column in (
        "dispatch_backend",
        "dispatch_generation",
        "attempt_count",
        "max_attempts_snapshot",
        "retry_policy_snapshot",
        "resource_profile_snapshot",
        "credential_bindings_snapshot",
        "logical_input_bytes",
    ):
        op.alter_column("executions", column, nullable=False)

    op.drop_constraint("ck_executions_status", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_status",
        "executions",
        "status IN ('pending', 'queued', 'running', 'retry_wait', 'succeeded', "
        "'failed', 'timeout', 'dead_letter', 'cancelled', 'expired')",
    )
    op.create_check_constraint(
        "ck_executions_dispatch_backend",
        "executions",
        "dispatch_backend IN ('legacy', 'rabbitmq')",
    )
    op.create_check_constraint(
        "ck_executions_backend_status",
        "executions",
        "(dispatch_backend = 'legacy' AND status IN "
        "('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')) OR "
        "(dispatch_backend = 'rabbitmq' AND status IN "
        "('queued', 'running', 'retry_wait', 'succeeded', 'dead_letter', 'cancelled', 'expired'))",
    )
    op.create_check_constraint(
        "ck_executions_dispatch_generation",
        "executions",
        "dispatch_generation >= 0 AND (dispatch_backend = 'legacy' OR dispatch_generation >= 1)",
    )
    op.create_check_constraint(
        "ck_executions_attempt_count_nonnegative",
        "executions",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_executions_max_attempts_snapshot",
        "executions",
        "max_attempts_snapshot BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        "ck_executions_logical_input_bytes",
        "executions",
        "logical_input_bytes >= 0",
    )
    op.create_check_constraint(
        "ck_executions_resource_class",
        "executions",
        "resource_class IS NULL OR length(resource_class) BETWEEN 1 AND 64",
    )
    # Keep the legacy index and its name for the rolling deployment window,
    # but scope it to legacy rows. RabbitMQ queued/running rows are allowed to
    # coexist; Adapter Slot/Attempt is the later runtime concurrency authority.
    op.execute("DROP INDEX uq_executions_active_adapter")
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_adapter "
        "ON executions (adapter_id) "
        "WHERE dispatch_backend = 'legacy' AND status IN ('pending', 'running')"
    )
    op.create_index(
        "ix_executions_backend_status_created",
        "executions",
        ["dispatch_backend", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_executions_next_attempt_at",
        "executions",
        ["dispatch_backend", "status", "next_attempt_at"],
    )

    # --- Worker protocol: v3 can be stored, but minimum remains 1/2 ---------
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.create_check_constraint(
        "ck_workers_protocol_version",
        "workers",
        "protocol_version BETWEEN 1 AND 3",
    )

    # --- Idempotency ---------------------------------------------------------
    op.create_table(
        "execution_idempotency_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(key_hash) = 32",
            name="ck_execution_idempotency_records_key_hash_length",
        ),
        sa.CheckConstraint(
            "octet_length(payload_hash) = 32",
            name="ck_execution_idempotency_records_payload_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"], ["adapters.id"], ondelete="CASCADE", name="fk_idempotency_adapter"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_execution",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "adapter_id", "key_hash", name="uq_execution_idempotency_records_adapter_key"
        ),
        sa.UniqueConstraint("execution_id", name="uq_execution_idempotency_records_execution"),
    )
    op.create_index(
        "ix_execution_idempotency_records_expires_at",
        "execution_idempotency_records",
        ["expires_at"],
    )
    op.create_foreign_key(
        "fk_executions_idempotency_record_id",
        "executions",
        "execution_idempotency_records",
        ["idempotency_record_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_executions_replay_of_execution_id",
        "executions",
        "executions",
        ["replay_of_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- Admission counters --------------------------------------------------
    op.create_table(
        "adapter_execution_admission",
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "outstanding_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "outstanding_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outstanding_count >= 0", name="ck_adapter_execution_admission_count_nonnegative"
        ),
        sa.CheckConstraint(
            "outstanding_bytes >= 0", name="ck_adapter_execution_admission_bytes_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"], ["adapters.id"], ondelete="CASCADE", name="fk_admission_adapter"
        ),
        sa.PrimaryKeyConstraint("adapter_id"),
    )
    op.create_table(
        "global_execution_admission",
        sa.Column("singleton_key", sa.String(length=16), nullable=False),
        sa.Column(
            "outstanding_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "outstanding_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outstanding_count >= 0", name="ck_global_execution_admission_count_nonnegative"
        ),
        sa.CheckConstraint(
            "outstanding_bytes >= 0", name="ck_global_execution_admission_bytes_nonnegative"
        ),
        sa.CheckConstraint(
            "singleton_key = 'global'",
            name="ck_global_execution_admission_singleton_key",
        ),
        sa.PrimaryKeyConstraint("singleton_key"),
    )
    op.execute(
        sa.text(
            "INSERT INTO global_execution_admission (singleton_key) VALUES ('global') "
            "ON CONFLICT (singleton_key) DO NOTHING"
        )
    )
    op.create_table(
        "rabbitmq_runtime_capabilities",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("broker_version", sa.String(length=32), nullable=False),
        sa.Column("feature_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("worker_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_rabbitmq_runtime_capabilities_singleton"),
        sa.CheckConstraint(
            "configuration_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_rabbitmq_runtime_capabilities_fingerprint",
        ),
        sa.CheckConstraint(
            "broker_version = '4.3.5'",
            name="ck_rabbitmq_runtime_capabilities_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(feature_flags) = 'array'",
            name="ck_rabbitmq_runtime_capabilities_feature_flags_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(worker_ids) = 'array'",
            name="ck_rabbitmq_runtime_capabilities_worker_ids_array",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO adapter_execution_admission (adapter_id) "
            "SELECT id FROM adapters ON CONFLICT (adapter_id) DO NOTHING"
        )
    )

    # --- Immutable Credential references for queued RabbitMQ rows ----------
    # The binding identity is copied rather than referenced: replacing an
    # Adapter's live binding must not invalidate an already accepted
    # Execution.  The Credential FK is RESTRICT so deleting a Credential
    # cannot strand that Execution before its terminal retention cleanup.
    op.create_table(
        "execution_credential_binding_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("binding_id", sa.BigInteger(), nullable=False),
        sa.Column("credential_id", sa.BigInteger(), nullable=False),
        sa.Column("env_key", sa.String(length=128), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.CheckConstraint("binding_id > 0", name="ck_execution_credential_snapshots_binding_id"),
        sa.CheckConstraint(
            "credential_id > 0", name="ck_execution_credential_snapshots_credential_id"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name="fk_execution_credential_snapshots_execution",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["credentials.id"],
            ondelete="RESTRICT",
            name="fk_execution_credential_snapshots_credential",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "binding_id",
            name="uq_execution_credential_snapshots_execution_binding",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "env_key",
            name="uq_execution_credential_snapshots_execution_env_key",
        ),
    )
    op.create_index(
        "ix_execution_credential_binding_snapshots_execution_id",
        "execution_credential_binding_snapshots",
        ["execution_id"],
    )
    op.create_index(
        "ix_execution_credential_binding_snapshots_credential_id",
        "execution_credential_binding_snapshots",
        ["credential_id"],
    )

    # --- Transactional Outbox -----------------------------------------------
    op.create_table(
        "execution_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_generation", sa.BigInteger(), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("routing_key", sa.String(length=256), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('pending', 'published')", name="ck_execution_outbox_status"),
        sa.CheckConstraint("dispatch_generation >= 1", name="ck_execution_outbox_generation"),
        sa.CheckConstraint("payload_bytes >= 0", name="ck_execution_outbox_payload_bytes"),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_execution_outbox_publish_attempts"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE", name="fk_outbox_execution"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_execution_outbox_message_id"),
        sa.UniqueConstraint(
            "execution_id",
            "dispatch_generation",
            name="uq_execution_outbox_execution_generation",
        ),
    )
    op.create_index(
        "ix_execution_outbox_due",
        "execution_outbox",
        ["status", "available_at", "lease_expires_at", "created_at"],
    )

    # --- Attempt and explicit Slot authority --------------------------------
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("resource_usage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cleanup_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_execution_attempts_attempt_no_positive"),
        sa.CheckConstraint("fencing_token > 0", name="ck_execution_attempts_fencing_positive"),
        sa.CheckConstraint(
            "status IN ('claimed', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'worker_lost', 'resource_exceeded')",
            name="ck_execution_attempts_status",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE", name="fk_attempt_execution"
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"], ["adapters.id"], ondelete="RESTRICT", name="fk_attempt_adapter"
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"], ["workers.id"], ondelete="RESTRICT", name="fk_attempt_worker"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "attempt_no", name="uq_execution_attempts_execution_attempt_no"
        ),
    )
    op.create_index(
        "uq_execution_attempts_active_execution",
        "execution_attempts",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('claimed', 'running')"),
    )
    op.create_index(
        "ix_execution_attempts_lease",
        "execution_attempts",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "adapter_execution_slots",
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_no", sa.SmallInteger(), nullable=False),
        sa.Column("active_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("slot_no >= 0", name="ck_adapter_execution_slots_slot_no"),
        sa.CheckConstraint(
            "fencing_token >= 0", name="ck_adapter_execution_slots_fencing_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"], ["adapters.id"], ondelete="CASCADE", name="fk_slot_adapter"
        ),
        sa.ForeignKeyConstraint(
            ["active_attempt_id"],
            ["execution_attempts.id"],
            ondelete="SET NULL",
            name="fk_slot_active_attempt",
        ),
        sa.PrimaryKeyConstraint("adapter_id", "slot_no"),
        sa.UniqueConstraint("active_attempt_id", name="uq_adapter_execution_slots_active_attempt"),
    )
    op.create_index(
        "ix_adapter_execution_slots_lease",
        "adapter_execution_slots",
        ["lease_expires_at"],
    )
    op.execute(
        sa.text(
            "INSERT INTO adapter_execution_slots (adapter_id, slot_no) "
            "SELECT id, 0 FROM adapters ON CONFLICT (adapter_id, slot_no) DO NOTHING"
        )
    )

    # --- Schedule policy and bounded audit tables ----------------------------
    op.add_column(
        "adapter_schedules",
        sa.Column(
            "misfire_policy",
            sa.String(length=32),
            server_default=sa.text("'coalesce_latest'"),
            nullable=True,
        ),
    )
    op.add_column(
        "adapter_schedules",
        sa.Column(
            "max_catchup_count", sa.BigInteger(), server_default=sa.text("100"), nullable=True
        ),
    )
    op.add_column(
        "adapter_schedules",
        sa.Column(
            "max_catchup_age_seconds",
            sa.BigInteger(),
            server_default=sa.text("86400"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE adapter_schedules SET misfire_policy = "
            "COALESCE(misfire_policy, 'coalesce_latest'), "
            "max_catchup_count = COALESCE(max_catchup_count, 100), "
            "max_catchup_age_seconds = COALESCE(max_catchup_age_seconds, 86400) "
            "WHERE misfire_policy IS NULL OR max_catchup_count IS NULL "
            "OR max_catchup_age_seconds IS NULL"
        )
    )
    for column, default in (
        ("misfire_policy", "'coalesce_latest'"),
        ("max_catchup_count", "100"),
        ("max_catchup_age_seconds", "86400"),
    ):
        op.alter_column(
            "adapter_schedules",
            column,
            nullable=False,
            server_default=sa.text(default),
        )
    op.create_check_constraint(
        "ck_adapter_schedules_misfire_policy",
        "adapter_schedules",
        "misfire_policy IN ('coalesce_latest', 'queue_every_occurrence', 'skip_while_busy')",
    )
    op.create_check_constraint(
        "ck_adapter_schedules_max_catchup_count",
        "adapter_schedules",
        "max_catchup_count BETWEEN 1 AND 1000",
    )
    op.create_check_constraint(
        "ck_adapter_schedules_max_catchup_age_seconds",
        "adapter_schedules",
        "max_catchup_age_seconds BETWEEN 60 AND 604800",
    )

    op.create_table(
        "schedule_dispatch_outcomes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("first_scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("cron_snapshot", sa.Text(), nullable=False),
        sa.Column("timezone_snapshot", sa.String(length=64), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('enqueued', 'coalesced', 'skipped', 'expired')",
            name="ck_schedule_dispatch_outcomes_outcome",
        ),
        sa.CheckConstraint(
            "occurrence_count > 0", name="ck_schedule_dispatch_outcomes_count_positive"
        ),
        # Five-field Cron is bounded to one point per minute and the service's
        # seven-day audit page is 604800 // 60 + 2 = 10082 points.
        sa.CheckConstraint(
            "occurrence_count <= 10082",
            name="ck_schedule_dispatch_outcomes_count_bounded",
        ),
        sa.CheckConstraint(
            "last_scheduled_for >= first_scheduled_for",
            name="ck_schedule_dispatch_outcomes_range_order",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["adapter_schedules.id"],
            ondelete="CASCADE",
            name="fk_schedule_outcome_schedule",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="SET NULL",
            name="fk_schedule_outcome_execution",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_schedule_dispatch_outcomes_schedule_time",
        "schedule_dispatch_outcomes",
        ["schedule_id", "first_scheduled_for"],
    )

    op.create_table(
        "execution_infrastructure_incidents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=True),
        sa.Column("dispatch_generation", sa.BigInteger(), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')",
            name="ck_execution_infrastructure_incidents_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_execution_infrastructure_incidents_attempts"),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name="fk_incident_execution",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_infrastructure_incidents_execution_generation",
        "execution_infrastructure_incidents",
        ["execution_id", "dispatch_generation"],
    )

    op.create_table(
        "execution_artifact_holds",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "reason",
            sa.String(length=32),
            server_default=sa.text("'dead_letter_replay'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "reason IN ('dead_letter_replay')", name="ck_execution_artifact_holds_reason"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name="fk_artifact_hold_execution",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["managed_input_artifacts.id"],
            ondelete="RESTRICT",
            name="fk_artifact_hold_artifact",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "artifact_id",
            "reason",
            name="uq_execution_artifact_holds_execution_artifact_reason",
        ),
    )
    op.create_index(
        "ix_execution_artifact_holds_expiry",
        "execution_artifact_holds",
        ["expires_at", "purged_at"],
    )


def downgrade() -> None:
    """Test-only reverse path; production rollback remains additive/fail-closed."""
    op.drop_index("ix_execution_artifact_holds_expiry", table_name="execution_artifact_holds")
    op.drop_table("execution_artifact_holds")
    op.drop_index(
        "ix_execution_infrastructure_incidents_execution_generation",
        table_name="execution_infrastructure_incidents",
    )
    op.drop_table("execution_infrastructure_incidents")
    op.drop_index(
        "ix_schedule_dispatch_outcomes_schedule_time",
        table_name="schedule_dispatch_outcomes",
    )
    op.drop_table("schedule_dispatch_outcomes")

    op.drop_constraint(
        "ck_adapter_schedules_max_catchup_age_seconds", "adapter_schedules", type_="check"
    )
    op.drop_constraint("ck_adapter_schedules_max_catchup_count", "adapter_schedules", type_="check")
    op.drop_constraint("ck_adapter_schedules_misfire_policy", "adapter_schedules", type_="check")
    op.drop_column("adapter_schedules", "max_catchup_age_seconds")
    op.drop_column("adapter_schedules", "max_catchup_count")
    op.drop_column("adapter_schedules", "misfire_policy")

    op.drop_index("ix_adapter_execution_slots_lease", table_name="adapter_execution_slots")
    op.drop_table("adapter_execution_slots")
    op.drop_index("ix_execution_attempts_lease", table_name="execution_attempts")
    op.drop_index("uq_execution_attempts_active_execution", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index(
        "ix_execution_credential_binding_snapshots_credential_id",
        table_name="execution_credential_binding_snapshots",
    )
    op.drop_index(
        "ix_execution_credential_binding_snapshots_execution_id",
        table_name="execution_credential_binding_snapshots",
    )
    op.drop_table("execution_credential_binding_snapshots")
    op.drop_index("ix_execution_outbox_due", table_name="execution_outbox")
    op.drop_table("execution_outbox")

    op.drop_table("rabbitmq_runtime_capabilities")
    op.drop_table("global_execution_admission")
    op.drop_table("adapter_execution_admission")
    op.drop_constraint("fk_executions_replay_of_execution_id", "executions", type_="foreignkey")
    op.drop_constraint("fk_executions_idempotency_record_id", "executions", type_="foreignkey")
    op.drop_index(
        "ix_execution_idempotency_records_expires_at",
        table_name="execution_idempotency_records",
    )
    op.drop_table("execution_idempotency_records")

    op.drop_index("ix_executions_next_attempt_at", table_name="executions")
    op.drop_index("ix_executions_backend_status_created", table_name="executions")
    for constraint in (
        "ck_executions_resource_class",
        "ck_executions_logical_input_bytes",
        "ck_executions_max_attempts_snapshot",
        "ck_executions_attempt_count_nonnegative",
        "ck_executions_dispatch_generation",
        "ck_executions_backend_status",
        "ck_executions_dispatch_backend",
    ):
        op.drop_constraint(constraint, "executions", type_="check")
    op.drop_constraint("ck_executions_status", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_status",
        "executions",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
    )
    op.execute("DROP INDEX uq_executions_active_adapter")
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_adapter "
        "ON executions (adapter_id) WHERE status IN ('pending', 'running')"
    )
    for column in (
        "replay_of_execution_id",
        "admission_released_at",
        "last_error_code",
        "idempotency_record_id",
        "logical_input_bytes",
        "target_worker_id_snapshot",
        "resource_class",
        "schedule_policy_snapshot",
        "credential_bindings_snapshot",
        "resource_profile_snapshot",
        "retry_policy_snapshot",
        "max_attempts_snapshot",
        "attempt_count",
        "next_attempt_at",
        "queued_at",
        "dispatch_generation",
        "dispatch_backend",
    ):
        op.drop_column("executions", column)
    op.drop_constraint("ck_workers_protocol_version", "workers", type_="check")
    op.create_check_constraint(
        "ck_workers_protocol_version", "workers", "protocol_version BETWEEN 1 AND 2"
    )
