"""Platform settings loaded from environment variables."""

from os.path import isabs

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """DLR platform settings.

    Environment variables are matched case-insensitively, e.g.
    ``database_url`` is set via ``DATABASE_URL``. Fields with an explicit
    ``validation_alias`` are set via their ``DLR_*`` variable name.
    """

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
        le=2,
        validation_alias="DLR_MIN_WORKER_PROTOCOL_VERSION",
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
    if value.min_worker_protocol_version not in {1, 2}:
        raise ValueError("DLR_MIN_WORKER_PROTOCOL_VERSION must be 1 or 2")
    return value


settings = Settings()
