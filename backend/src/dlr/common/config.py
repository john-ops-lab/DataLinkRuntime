"""Platform settings loaded from environment variables."""

from os.path import isabs
from urllib.parse import unquote, urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """DLR platform settings.

    Environment variables are matched case-insensitively, e.g.
    ``database_url`` is set via ``DATABASE_URL``. Fields with an explicit
    ``validation_alias`` are set via their ``DLR_*`` variable name.
    """

    # RabbitMQ URLs may contain credentials.  Keep Pydantic's validation
    # errors from echoing the complete settings input when a startup gate
    # rejects an invalid deployment.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    database_url: str = "postgresql+psycopg://dlr:dlr@localhost:5432/dlr"

    # Static shared tokens (M2). Never persisted, never logged. When a token
    # is unset, the protected APIs answer 503 instead of running open.
    admin_token: str | None = Field(default=None, validation_alias="DLR_ADMIN_TOKEN")
    worker_token: str | None = Field(default=None, validation_alias="DLR_WORKER_TOKEN")

    # M4.1 effective-online lease window. Worker rows keep their last
    # self-reported stored status; Control derives current availability from
    # that status plus heartbeat freshness instead of rewriting the row.
    worker_heartbeat_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias="DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS",
    )

    # M3.2 Secret Store: deployment-level Master Key used to derive the
    # Fernet encryption key for credentials. Never persisted, never logged.
    # When unset, the credential APIs answer 503 instead of storing
    # unencrypted secrets.
    master_key: str | None = Field(default=None, validation_alias="DLR_MASTER_KEY")

    # Big-field limits in UTF-8 bytes; see docs/specs/m2-execution-loop.md §5.
    execution_input_max_bytes: int = Field(
        default=512 * 1024, validation_alias="DLR_EXECUTION_INPUT_MAX_BYTES"
    )
    # Issue #127 A1: keep old per-run input and Schedule input accepted only
    # during the bounded compatibility window.  The default preserves the
    # rolling-deploy contract; callers still distinguish an omitted field from
    # an explicit JSON null before consulting this setting.
    legacy_input_compat_enabled: bool = Field(
        default=True, validation_alias="DLR_LEGACY_INPUT_COMPAT_ENABLED"
    )
    execution_output_max_bytes: int = Field(
        default=512 * 1024, validation_alias="DLR_EXECUTION_OUTPUT_MAX_BYTES"
    )
    execution_output_preview_max_bytes: int = Field(
        default=16 * 1024, validation_alias="DLR_EXECUTION_OUTPUT_PREVIEW_MAX_BYTES"
    )
    execution_stream_max_bytes: int = Field(
        default=1024 * 1024, validation_alias="DLR_EXECUTION_STREAM_MAX_BYTES"
    )

    # M5.11 Wave B: terminal Execution retention.  Cleanup is shared by
    # manual/Webhook/Task/Schedule history, runs in retryable batches, and
    # never selects pending or running rows.  The per-trigger defaults keep
    # Webhook history bounded without forcing long-running Task/Schedule
    # adapters into the old fixed-100 behavior.
    execution_retention_webhook_days: int = Field(
        default=30, ge=1, validation_alias="DLR_EXECUTION_RETENTION_WEBHOOK_DAYS"
    )
    execution_retention_webhook_max_per_adapter: int = Field(
        default=100, ge=1, validation_alias="DLR_EXECUTION_RETENTION_WEBHOOK_MAX_PER_ADAPTER"
    )
    execution_retention_task_days: int = Field(
        default=30, ge=1, validation_alias="DLR_EXECUTION_RETENTION_TASK_DAYS"
    )
    execution_retention_task_max_per_adapter: int = Field(
        default=1000, ge=1, validation_alias="DLR_EXECUTION_RETENTION_TASK_MAX_PER_ADAPTER"
    )
    execution_retention_schedule_days: int = Field(
        default=90, ge=1, validation_alias="DLR_EXECUTION_RETENTION_SCHEDULE_DAYS"
    )
    execution_retention_schedule_max_per_adapter: int = Field(
        default=1000, ge=1, validation_alias="DLR_EXECUTION_RETENTION_SCHEDULE_MAX_PER_ADAPTER"
    )
    execution_retention_batch_size: int = Field(
        default=100, ge=1, le=10_000, validation_alias="DLR_EXECUTION_RETENTION_BATCH_SIZE"
    )
    execution_retention_interval_seconds: float = Field(
        default=3600.0,
        ge=60.0,
        le=86_400.0,
        allow_inf_nan=False,
        validation_alias="DLR_EXECUTION_RETENTION_INTERVAL_SECONDS",
    )

    # M5.11 Wave B: platform service logs are written to a watched file under
    # this root.  Compose maps the host value to the same fixed in-container
    # path so external logrotate can rename files without restarting DLR.
    platform_log_root: str = Field(
        default="/var/lib/dlr/platform-logs", validation_alias="DLR_PLATFORM_LOG_ROOT"
    )
    # AI tool audit records use an application-owned JSONL rotation policy so
    # they stay bounded even when the host's optional *.log policy is absent.
    ai_tool_audit_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
        validation_alias="DLR_AI_TOOL_AUDIT_MAX_BYTES",
    )
    ai_tool_audit_backup_count: int = Field(
        default=10,
        ge=1,
        le=100,
        validation_alias="DLR_AI_TOOL_AUDIT_BACKUP_COUNT",
    )
    # Timeout sent to the Worker in every task payload.
    execution_timeout_seconds: int = Field(
        default=300, validation_alias="DLR_EXECUTION_TIMEOUT_SECONDS"
    )

    # Issue #130 B1: these bounded values are copied into each RabbitMQ
    # Execution's closed Retry Policy snapshot.  Attempt transition/retry
    # dispatch remains a later Batch; this config only defines the immutable
    # policy fact created with an accepted Execution.
    execution_retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=100,
        validation_alias="DLR_EXECUTION_RETRY_MAX_ATTEMPTS",
    )
    execution_retry_initial_backoff_seconds: float = Field(
        default=5.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
        validation_alias="DLR_EXECUTION_RETRY_INITIAL_BACKOFF_SECONDS",
    )
    execution_retry_multiplier: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
        allow_inf_nan=False,
        validation_alias="DLR_EXECUTION_RETRY_MULTIPLIER",
    )
    execution_retry_max_backoff_seconds: float = Field(
        default=300.0,
        gt=0,
        le=3_600,
        allow_inf_nan=False,
        validation_alias="DLR_EXECUTION_RETRY_MAX_BACKOFF_SECONDS",
    )
    execution_retry_jitter_ratio: float = Field(
        default=0.2,
        ge=0,
        le=0.2,
        allow_inf_nan=False,
        validation_alias="DLR_EXECUTION_RETRY_JITTER_RATIO",
    )

    # Issue #127 C0: these values are copied into every new Execution.  The
    # cleanup budgets must remain shorter than the recovery grace period so a
    # Worker has a bounded chance to report before Control reconciles it.
    execution_claim_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=86_400,
        validation_alias="DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS",
    )
    execution_recovery_grace_seconds: int = Field(
        default=60,
        ge=10,
        le=3_600,
        validation_alias="DLR_EXECUTION_RECOVERY_GRACE_SECONDS",
    )
    workspace_cleanup_attempt_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        validation_alias="DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS",
    )
    workspace_cleanup_total_timeout_seconds: int = Field(
        default=20,
        ge=5,
        le=300,
        validation_alias="DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS",
    )
    min_worker_protocol_version: int = Field(
        default=1,
        ge=1,
        le=3,
        validation_alias="DLR_MIN_WORKER_PROTOCOL_VERSION",
    )
    # Final Cutover keeps this compatibility switch explicit.  Disabling the
    # legacy Execution claim path does not disable Worker cleanup receipts or
    # historical reads, and it is never inferred merely from protocol v3.
    legacy_execution_claim_enabled: bool = Field(
        default=True,
        validation_alias="DLR_LEGACY_EXECUTION_CLAIM_ENABLED",
    )
    # These operator attestations bind irreversible Cutover actions to an
    # actual backup/restore rehearsal, the target-Linux sandbox Gate, and the
    # post-ingress Slot Gate.  All default closed and are reported by the
    # migration inventory.
    cutover_backup_restore_gate_passed: bool = Field(
        default=False,
        validation_alias="DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED",
    )
    cutover_sandbox_gate_passed: bool = Field(
        default=False,
        validation_alias="DLR_CUTOVER_SANDBOX_GATE_PASSED",
    )
    cutover_slot_gate_passed: bool = Field(
        default=False,
        validation_alias="DLR_CUTOVER_SLOT_GATE_PASSED",
    )

    # Issue #130 B1: RabbitMQ ingress stays disabled until the later dark
    # launch/cutover gates.  Control may still read and repair additive
    # RabbitMQ rows while this flag is false.
    rabbitmq_execution_enabled: bool = Field(
        default=False, validation_alias="DLR_RABBITMQ_EXECUTION_ENABLED"
    )
    # Explicit canary/test entry is independent from ordinary Manual,
    # Schedule and Webhook traffic.  It stays disabled in production by
    # default and is never used to raise the minimum Worker protocol.
    rabbitmq_execution_canary_enabled: bool = Field(
        default=False, validation_alias="DLR_RABBITMQ_EXECUTION_CANARY_ENABLED"
    )
    rabbitmq_url: str | None = Field(default=None, validation_alias="DLR_RABBITMQ_URL")
    # Compose keeps the broker vhost as one raw value and the AMQP client
    # encodes it at connection construction time.  This avoids maintaining a
    # second, independently editable URL-path setting.
    rabbitmq_vhost: str | None = Field(default=None, validation_alias="DLR_RABBITMQ_VHOST")
    rabbitmq_management_url: str | None = Field(
        default=None, validation_alias="DLR_RABBITMQ_MANAGEMENT_URL"
    )
    rabbitmq_management_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_MANAGEMENT_TIMEOUT_SECONDS",
    )
    rabbitmq_capability_cache_seconds: int = Field(
        default=3_600,
        ge=60,
        le=604_800,
        validation_alias="DLR_RABBITMQ_CAPABILITY_CACHE_SECONDS",
    )
    rabbitmq_consumer_timeout_ms: int = Field(
        default=300_000,
        ge=1_000,
        le=900_000,
        validation_alias="DLR_RABBITMQ_CONSUMER_TIMEOUT_MS",
    )
    rabbitmq_queue_max_length: int = Field(
        default=2_000,
        ge=1,
        le=1_000_000,
        validation_alias="DLR_RABBITMQ_QUEUE_MAX_LENGTH",
    )
    rabbitmq_queue_max_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1_024,
        le=10 * 1024 * 1024 * 1024,
        validation_alias="DLR_RABBITMQ_QUEUE_MAX_BYTES",
    )
    rabbitmq_delivery_limit: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="DLR_RABBITMQ_DELIVERY_LIMIT",
    )
    rabbitmq_dispatch_message_max_bytes: int = Field(
        default=16 * 1024,
        ge=256,
        le=1 * 1024 * 1024,
        validation_alias="DLR_RABBITMQ_DISPATCH_MESSAGE_MAX_BYTES",
    )
    rabbitmq_publish_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_PUBLISH_TIMEOUT_SECONDS",
    )
    rabbitmq_stack_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_STACK_TIMEOUT_SECONDS",
    )
    rabbitmq_claim_handshake_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_CLAIM_HANDSHAKE_TIMEOUT_SECONDS",
    )
    rabbitmq_outbox_lease_seconds: float = Field(
        default=60.0,
        gt=0,
        le=3_600,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_OUTBOX_LEASE_SECONDS",
    )
    rabbitmq_retry_base_seconds: float = Field(
        default=1.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_RETRY_BASE_SECONDS",
    )
    rabbitmq_retry_max_seconds: float = Field(
        default=60.0,
        gt=0,
        le=3_600,
        allow_inf_nan=False,
        validation_alias="DLR_RABBITMQ_RETRY_MAX_SECONDS",
    )
    # RabbitMQ 4.3 Quorum Queues own the delay for a broker-level DEFER.  The
    # v3 consumer uses basic.nack(requeue=True), which is a native returned
    # disposition.  ``all`` also delays connection/channel requeues; the
    # narrower ``returned`` mode is available when only explicit DEFERs should
    # be delayed.
    rabbitmq_delayed_retry_type: str = Field(
        default="all", validation_alias="DLR_RABBITMQ_DELAYED_RETRY_TYPE"
    )
    # Relay publication is deliberately bounded even when several Relay
    # invocations overlap.  The defaults leave a small, explicit window for
    # the single-node Quorum Queue while keeping every publisher resource
    # finite and observable.
    rabbitmq_publisher_channel_count: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="DLR_RABBITMQ_PUBLISHER_CHANNEL_COUNT",
    )
    rabbitmq_publisher_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="DLR_RABBITMQ_PUBLISHER_MAX_CONCURRENCY",
    )
    rabbitmq_publisher_max_confirm_inflight: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="DLR_RABBITMQ_PUBLISHER_MAX_CONFIRM_INFLIGHT",
    )
    rabbitmq_broker_headroom_messages: int = Field(
        default=16,
        ge=1,
        le=1_000_000,
        validation_alias="DLR_RABBITMQ_BROKER_HEADROOM_MESSAGES",
    )
    rabbitmq_broker_headroom_bytes: int = Field(
        default=256 * 1024,
        ge=1_024,
        le=10 * 1024 * 1024 * 1024,
        validation_alias="DLR_RABBITMQ_BROKER_HEADROOM_BYTES",
    )

    # Business outstanding counters are charged once per non-terminal
    # RabbitMQ Execution.  The global limits intentionally exceed the
    # per-Adapter defaults so one busy Adapter cannot consume all capacity.
    admission_adapter_max_count: int = Field(
        default=100, ge=1, le=1_000_000, validation_alias="DLR_ADMISSION_ADAPTER_MAX_COUNT"
    )
    admission_adapter_max_bytes: int = Field(
        default=1 * 1024 * 1024 * 1024,
        ge=1,
        le=1 * 1024 * 1024 * 1024 * 1024,
        validation_alias="DLR_ADMISSION_ADAPTER_MAX_BYTES",
    )
    admission_global_max_count: int = Field(
        default=1_000, ge=1, le=10_000_000, validation_alias="DLR_ADMISSION_GLOBAL_MAX_COUNT"
    )
    admission_global_max_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=1,
        le=10 * 1024 * 1024 * 1024 * 1024,
        validation_alias="DLR_ADMISSION_GLOBAL_MAX_BYTES",
    )
    admission_reconcile_batch_size: int = Field(
        default=100,
        ge=1,
        le=1_000,
        validation_alias="DLR_ADMISSION_RECONCILE_BATCH_SIZE",
    )
    outbox_max_pending_count: int = Field(
        default=2_000, ge=1, le=10_000_000, validation_alias="DLR_OUTBOX_MAX_PENDING_COUNT"
    )
    outbox_max_pending_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        le=10 * 1024 * 1024 * 1024,
        validation_alias="DLR_OUTBOX_MAX_PENDING_BYTES",
    )
    outbox_max_oldest_seconds: int = Field(
        default=900, ge=1, le=604_800, validation_alias="DLR_OUTBOX_MAX_OLDEST_SECONDS"
    )

    attempt_lease_seconds: int = Field(
        default=60, ge=15, le=86_400, validation_alias="DLR_ATTEMPT_LEASE_SECONDS"
    )
    attempt_renew_seconds: int = Field(
        default=15, ge=1, le=28_800, validation_alias="DLR_ATTEMPT_RENEW_SECONDS"
    )
    attempt_reconcile_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
        validation_alias="DLR_ATTEMPT_RECONCILE_INTERVAL_SECONDS",
    )
    idempotency_retention_seconds: int = Field(
        default=86_400,
        ge=86_400,
        le=31_536_000,
        validation_alias="DLR_IDEMPOTENCY_RETENTION_SECONDS",
    )
    dead_letter_hold_seconds: int = Field(
        default=604_800,
        ge=60,
        le=31_536_000,
        validation_alias="DLR_DEAD_LETTER_HOLD_SECONDS",
    )
    dead_letter_hold_max_count: int = Field(
        default=10_000,
        ge=1,
        le=10_000_000,
        validation_alias="DLR_DEAD_LETTER_HOLD_MAX_COUNT",
    )
    dead_letter_hold_max_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=1,
        le=10 * 1024 * 1024 * 1024 * 1024,
        validation_alias="DLR_DEAD_LETTER_HOLD_MAX_BYTES",
    )

    # The resource profile is only snapshotted in B1.  Real cgroup/namespace
    # enforcement remains a later Batch 3 capability gate.
    sandbox_backend: str = Field(default="cgroup_v2", validation_alias="DLR_SANDBOX_BACKEND")
    sandbox_cpu_cores: float = Field(
        default=1.0,
        gt=0,
        le=128,
        allow_inf_nan=False,
        validation_alias="DLR_SANDBOX_CPU_CORES",
    )
    sandbox_memory_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=1 * 1024 * 1024 * 1024 * 1024,
        validation_alias="DLR_SANDBOX_MEMORY_BYTES",
    )
    sandbox_pids: int = Field(default=128, ge=16, le=1_000_000, validation_alias="DLR_SANDBOX_PIDS")
    sandbox_tmp_bytes: int = Field(
        default=1 * 1024 * 1024 * 1024,
        ge=1 * 1024 * 1024,
        le=1 * 1024 * 1024 * 1024 * 1024,
        validation_alias="DLR_SANDBOX_TMP_BYTES",
    )
    sandbox_nofile: int = Field(
        default=1_024, ge=64, le=1_048_576, validation_alias="DLR_SANDBOX_NOFILE"
    )

    # Issue #127 B0: physical ArtifactStore placement and lifecycle loops are
    # deployment concerns.  The managed-files flag remains disabled until the
    # later storage/Worker waves pass their release gates.
    artifact_store_root: str = Field(
        default="/var/lib/dlr/artifacts", validation_alias="DLR_ARTIFACT_STORE_ROOT"
    )
    managed_files_enabled: bool = Field(default=False, validation_alias="DLR_MANAGED_FILES_ENABLED")
    artifact_gc_interval_seconds: float = Field(
        default=300.0,
        gt=0,
        le=86_400.0,
        allow_inf_nan=False,
        validation_alias="DLR_ARTIFACT_GC_INTERVAL_SECONDS",
    )
    artifact_audit_interval_seconds: float = Field(
        default=3_600.0,
        gt=0,
        le=604_800.0,
        allow_inf_nan=False,
        validation_alias="DLR_ARTIFACT_AUDIT_INTERVAL_SECONDS",
    )
    artifact_delete_alert_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="DLR_ARTIFACT_DELETE_ALERT_THRESHOLD",
    )

    # M3 SSE: the simplest possible PostgreSQL polling implementation; see
    # docs/specs/m3-observability-ux.md §7.
    sse_poll_interval_seconds: float = Field(
        default=0.75, validation_alias="DLR_SSE_POLL_INTERVAL_SECONDS"
    )
    sse_keepalive_seconds: float = Field(default=15.0, validation_alias="DLR_SSE_KEEPALIVE_SECONDS")

    # M5.2 Schedule Trigger: the Control scheduler is a lightweight
    # PostgreSQL polling loop; this is its scan interval.
    schedule_poll_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=300.0,
        allow_inf_nan=False,
        validation_alias="DLR_SCHEDULE_POLL_SECONDS",
    )

    # M4 non-streaming Provider requests may generate a complete code Candidate.
    # Keep the deployment override bounded so a typo cannot create an
    # effectively immediate or unbounded network deadline.
    ai_provider_timeout_seconds: float = Field(
        default=180.0,
        ge=10.0,
        le=600.0,
        validation_alias="DLR_AI_PROVIDER_TIMEOUT_SECONDS",
    )
    # One complete Assist may contain several bounded Provider/tool rounds.
    # Keep the aggregate wall-clock budget narrower than the per-request
    # Provider override and reserve its tail for a tools-disabled final answer.
    ai_assist_total_timeout_seconds: float = Field(
        default=150.0,
        ge=120.0,
        le=180.0,
        allow_inf_nan=False,
        validation_alias="DLR_AI_ASSIST_TOTAL_TIMEOUT_SECONDS",
    )

    # M5.7 Wave C2: read-only KnowledgeSource (first target: Tencent ima).
    # The endpoint must be HTTPS and its host must appear in the official host
    # allowlist; redirects are never followed and only the three registered
    # read-only knowledge operations exist. The default is the official ima
    # OpenAPI base URL confirmed from the official Tencent ima skill package
    # (@tencent-adm/ima-skills v1.1.9, publisher tencent-ima); an explicitly
    # empty value disables the source (ks_not_configured). Secret truth
    # (Client ID / API Key) is stored in a DLR access_key Credential (Secret
    # Store) referenced by DLR_IMA_CREDENTIAL_NAME and resolved only at the
    # server-side execution point, never in the browser, prompts, tool
    # summaries/results, logs or errors.
    #
    # DLR_IMA_ALLOW_HTTP is a strict test/smoke escape hatch: when true, an
    # http endpoint is accepted for hosts explicitly added to the allowlist
    # (a fake official service on a private network). Production deployments
    # must keep it false (default).
    dlr_ima_endpoint: str = Field(
        default="https://ima.qq.com",
        validation_alias="DLR_IMA_ENDPOINT",
    )
    dlr_ima_credential_name: str | None = Field(
        default=None, validation_alias="DLR_IMA_CREDENTIAL_NAME"
    )
    dlr_ima_allowed_hosts: str = Field(
        default="ima.qq.com",
        validation_alias="DLR_IMA_ALLOWED_HOSTS",
    )
    dlr_ima_allow_http: bool = Field(
        default=False,
        validation_alias="DLR_IMA_ALLOW_HTTP",
    )
    # Bounded knowledge request deadline. The C1 per-tool timeout
    # (TOOL_TIMEOUT_SECONDS = 10) is the wall-clock bound the dispatcher
    # enforces after the handler returns, so the upstream deadline must stay
    # within it: the validation bound below (10s) enforces the documented
    # invariant "knowledge requests never outlive the tool budget".
    dlr_ima_timeout_seconds: float = Field(
        default=8.0,
        ge=1.0,
        le=10.0,
        allow_inf_nan=False,
        validation_alias="DLR_IMA_TIMEOUT_SECONDS",
    )

    @model_validator(mode="after")
    def validate_deployment_configuration(self) -> "Settings":
        """Reject invalid B0 deployment values before the app can start.

        Pydantic validates values loaded from the environment.  The explicit
        method is also called by ``create_app`` so tests and embedders that
        mutate the settings singleton cannot bypass the startup gate.
        """
        return validate_deployment_configuration(self)


def validate_deployment_configuration(value: Settings) -> Settings:
    """Run the deployment gate for both Pydantic and app-factory callers."""
    root = value.artifact_store_root
    if not isinstance(root, str) or not root.strip() or "\x00" in root or not isabs(root):
        raise ValueError("DLR_ARTIFACT_STORE_ROOT must be a non-empty absolute path")
    if not 0 < value.artifact_gc_interval_seconds <= 86_400:
        raise ValueError("DLR_ARTIFACT_GC_INTERVAL_SECONDS must be between 0 and 86400")
    if not 0 < value.artifact_audit_interval_seconds <= 604_800:
        raise ValueError("DLR_ARTIFACT_AUDIT_INTERVAL_SECONDS must be between 0 and 604800")
    if (
        value.workspace_cleanup_attempt_timeout_seconds
        > value.workspace_cleanup_total_timeout_seconds
    ):
        raise ValueError(
            "DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS must not exceed "
            "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS"
        )
    if value.workspace_cleanup_total_timeout_seconds >= value.execution_recovery_grace_seconds:
        raise ValueError(
            "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS must be less than "
            "DLR_EXECUTION_RECOVERY_GRACE_SECONDS"
        )
    if value.min_worker_protocol_version not in {1, 2, 3}:
        raise ValueError("DLR_MIN_WORKER_PROTOCOL_VERSION must be 1, 2, or 3")
    if not value.legacy_execution_claim_enabled and (
        not value.rabbitmq_execution_enabled
        or value.min_worker_protocol_version != 3
        or not value.cutover_backup_restore_gate_passed
        or not value.cutover_sandbox_gate_passed
        or not value.cutover_slot_gate_passed
    ):
        raise ValueError(
            "DLR_LEGACY_EXECUTION_CLAIM_ENABLED may be false only after RabbitMQ ingress, "
            "minimum protocol v3, backup/restore, Sandbox, and Slot Cutover gates pass"
        )
    if value.rabbitmq_retry_base_seconds > value.rabbitmq_retry_max_seconds:
        raise ValueError(
            "DLR_RABBITMQ_RETRY_BASE_SECONDS must not exceed DLR_RABBITMQ_RETRY_MAX_SECONDS"
        )
    if value.rabbitmq_delayed_retry_type not in {"all", "returned"}:
        raise ValueError("DLR_RABBITMQ_DELAYED_RETRY_TYPE must be all or returned")
    if not 1 <= value.execution_retry_max_attempts <= 100:
        raise ValueError("DLR_EXECUTION_RETRY_MAX_ATTEMPTS must be between 1 and 100")
    if not 0 < value.execution_retry_initial_backoff_seconds <= 300:
        raise ValueError("DLR_EXECUTION_RETRY_INITIAL_BACKOFF_SECONDS must be between 0 and 300")
    if not 1 <= value.execution_retry_multiplier <= 10:
        raise ValueError("DLR_EXECUTION_RETRY_MULTIPLIER must be between 1 and 10")
    if not 0 < value.execution_retry_max_backoff_seconds <= 3_600:
        raise ValueError("DLR_EXECUTION_RETRY_MAX_BACKOFF_SECONDS must be between 0 and 3600")
    if not 0 <= value.execution_retry_jitter_ratio <= 0.2:
        raise ValueError("DLR_EXECUTION_RETRY_JITTER_RATIO must be between 0 and 0.2")
    if value.execution_retry_initial_backoff_seconds > value.execution_retry_max_backoff_seconds:
        raise ValueError(
            "DLR_EXECUTION_RETRY_INITIAL_BACKOFF_SECONDS must not exceed "
            "DLR_EXECUTION_RETRY_MAX_BACKOFF_SECONDS"
        )
    if value.rabbitmq_claim_handshake_timeout_seconds * 1000 >= value.rabbitmq_consumer_timeout_ms:
        raise ValueError("DLR_RABBITMQ_CONSUMER_TIMEOUT_MS must exceed the Claim handshake budget")
    if value.rabbitmq_dispatch_message_max_bytes > value.rabbitmq_queue_max_bytes:
        raise ValueError(
            "DLR_RABBITMQ_DISPATCH_MESSAGE_MAX_BYTES must not exceed DLR_RABBITMQ_QUEUE_MAX_BYTES"
        )
    if value.rabbitmq_publisher_max_concurrency > value.rabbitmq_publisher_channel_count:
        raise ValueError(
            "DLR_RABBITMQ_PUBLISHER_MAX_CONCURRENCY must not exceed "
            "DLR_RABBITMQ_PUBLISHER_CHANNEL_COUNT"
        )
    if value.rabbitmq_publisher_max_confirm_inflight > value.rabbitmq_publisher_max_concurrency:
        raise ValueError(
            "DLR_RABBITMQ_PUBLISHER_MAX_CONFIRM_INFLIGHT must not exceed "
            "DLR_RABBITMQ_PUBLISHER_MAX_CONCURRENCY"
        )
    if (
        value.rabbitmq_queue_max_length
        < value.rabbitmq_publisher_max_confirm_inflight + value.rabbitmq_broker_headroom_messages
    ):
        raise ValueError(
            "DLR_RABBITMQ_QUEUE_MAX_LENGTH must leave room for publisher confirms and "
            "DLR_RABBITMQ_BROKER_HEADROOM_MESSAGES"
        )
    if (
        value.rabbitmq_queue_max_bytes
        < value.rabbitmq_publisher_max_confirm_inflight * value.rabbitmq_dispatch_message_max_bytes
        + value.rabbitmq_broker_headroom_bytes
    ):
        raise ValueError(
            "DLR_RABBITMQ_QUEUE_MAX_BYTES must leave room for publisher confirms and "
            "DLR_RABBITMQ_BROKER_HEADROOM_BYTES"
        )
    if value.outbox_max_pending_bytes < value.rabbitmq_dispatch_message_max_bytes:
        raise ValueError(
            "DLR_OUTBOX_MAX_PENDING_BYTES must be at least DLR_RABBITMQ_DISPATCH_MESSAGE_MAX_BYTES"
        )
    if value.admission_adapter_max_count > value.admission_global_max_count:
        raise ValueError(
            "DLR_ADMISSION_ADAPTER_MAX_COUNT must not exceed DLR_ADMISSION_GLOBAL_MAX_COUNT"
        )
    if value.admission_adapter_max_bytes > value.admission_global_max_bytes:
        raise ValueError(
            "DLR_ADMISSION_ADAPTER_MAX_BYTES must not exceed DLR_ADMISSION_GLOBAL_MAX_BYTES"
        )
    if value.attempt_renew_seconds * 3 >= value.attempt_lease_seconds:
        raise ValueError(
            "DLR_ATTEMPT_RENEW_SECONDS must be less than one third of DLR_ATTEMPT_LEASE_SECONDS"
        )
    if value.sandbox_backend != "cgroup_v2":
        raise ValueError("DLR_SANDBOX_BACKEND must be cgroup_v2")
    if value.rabbitmq_url:
        parsed = urlsplit(value.rabbitmq_url)
        if parsed.scheme not in {"amqp", "amqps"} or not parsed.hostname:
            raise ValueError("DLR_RABBITMQ_URL must be an amqp or amqps URL")
        if value.rabbitmq_vhost is not None and parsed.path not in {"", "/"}:
            raise ValueError(
                "DLR_RABBITMQ_URL must omit its vhost path when DLR_RABBITMQ_VHOST is configured"
            )
        username = unquote(parsed.username or "")
        if not username or username.casefold() == "guest":
            raise ValueError("DLR_RABBITMQ_URL must use a non-guest user")
        password = unquote(parsed.password or "")
        if not password:
            raise ValueError("DLR_RABBITMQ_URL must include a non-empty password")
    if value.rabbitmq_vhost is not None:
        try:
            vhost_bytes = value.rabbitmq_vhost.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            raise ValueError("DLR_RABBITMQ_VHOST must be valid UTF-8 text") from None
        if (
            not value.rabbitmq_vhost
            or len(vhost_bytes) > 255
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in value.rabbitmq_vhost
            )
        ):
            raise ValueError(
                "DLR_RABBITMQ_VHOST must be 1-255 UTF-8 bytes without control characters"
            )
    if value.rabbitmq_execution_enabled and not value.rabbitmq_url:
        raise ValueError("DLR_RABBITMQ_URL is required when RabbitMQ execution is enabled")
    if value.rabbitmq_url and not value.rabbitmq_management_url:
        raise ValueError(
            "DLR_RABBITMQ_MANAGEMENT_URL is required when DLR_RABBITMQ_URL is configured"
        )
    if value.rabbitmq_management_url:
        management = urlsplit(value.rabbitmq_management_url)
        if management.scheme not in {"http", "https"} or not management.hostname:
            raise ValueError("DLR_RABBITMQ_MANAGEMENT_URL must be an http or https URL")
        if management.username is not None or management.password is not None:
            raise ValueError("DLR_RABBITMQ_MANAGEMENT_URL must not contain credentials")
    return value


settings = Settings()
