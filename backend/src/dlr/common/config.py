"""Platform settings loaded from environment variables."""

from pydantic import Field
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
    execution_output_max_bytes: int = Field(
        default=512 * 1024, validation_alias="DLR_EXECUTION_OUTPUT_MAX_BYTES"
    )
    execution_output_preview_max_bytes: int = Field(
        default=16 * 1024, validation_alias="DLR_EXECUTION_OUTPUT_PREVIEW_MAX_BYTES"
    )
    execution_stream_max_bytes: int = Field(
        default=1024 * 1024, validation_alias="DLR_EXECUTION_STREAM_MAX_BYTES"
    )
    # Timeout sent to the Worker in every task payload.
    execution_timeout_seconds: int = Field(
        default=300, validation_alias="DLR_EXECUTION_TIMEOUT_SECONDS"
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

    # M5.7 Wave C2: read-only KnowledgeSource (first target: Tencent ima).
    # The endpoint must be HTTPS and its host must appear in the official host
    # allowlist; redirects are never followed and only the three registered
    # read-only knowledge operations exist. Secret truth (Client ID / API
    # Key / Token) is stored in DLR Credentials (Secret Store) and resolved
    # only at the server-side execution point, never in the browser, prompts,
    # tool summaries/results, logs or errors.
    #
    # DLR_IMA_ALLOW_HTTP is a strict test/smoke escape hatch: when true, an
    # http endpoint is accepted for hosts explicitly added to the allowlist
    # (a fake official service on a private network). Production deployments
    # must keep it false (default).
    dlr_ima_endpoint: str | None = Field(default=None, validation_alias="DLR_IMA_ENDPOINT")
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
    # Bounded knowledge request deadline. Kept below the C1 per-tool timeout
    # (TOOL_TIMEOUT_SECONDS = 10) so a stuck upstream can never outlive the
    # tool budget; both the socket phases and a total wall-clock deadline are
    # enforced by the adapter.
    dlr_ima_timeout_seconds: float = Field(
        default=8.0,
        ge=1.0,
        le=30.0,
        allow_inf_nan=False,
        validation_alias="DLR_IMA_TIMEOUT_SECONDS",
    )


settings = Settings()
