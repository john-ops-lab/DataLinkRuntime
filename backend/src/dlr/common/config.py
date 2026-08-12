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


settings = Settings()
