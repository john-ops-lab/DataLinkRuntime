"""Public contracts for the B0 Managed Input policy API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# These values are the single application-level copy of the database policy
# bounds.  The migration and model repeat them in SQL so the database remains
# the final enforcement layer for direct writes and concurrent requests.
DEFAULT_RETENTION_SECONDS = 86_400
DEFAULT_MAX_FILE_BYTES = 104_857_600
DEFAULT_PLATFORM_QUOTA_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_ADAPTER_QUOTA_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_CUSTOM_RETENTION_SECONDS = 2_592_000
DEFAULT_MIN_FREE_SPACE_BYTES = 1024 * 1024 * 1024
DEFAULT_STAGED_TTL_SECONDS = 3_600

MIN_DEFAULT_RETENTION_SECONDS = 3_600
MAX_DEFAULT_RETENTION_SECONDS = 2_592_000
MIN_FILE_BYTES = 1 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MIN_QUOTA_BYTES = 1 * 1024 * 1024
MAX_QUOTA_BYTES = 10 * 1024 * 1024 * 1024 * 1024
MIN_CUSTOM_RETENTION_SECONDS = 3_600
MAX_CUSTOM_RETENTION_SECONDS = 31_536_000
MIN_FREE_SPACE_BYTES = 64 * 1024 * 1024
MAX_FREE_SPACE_BYTES = 1024 * 1024 * 1024 * 1024
MIN_STAGED_TTL_SECONDS = 300
MAX_STAGED_TTL_SECONDS = 86_400


class ManagedInputSettingsUpdate(BaseModel):
    """Complete replacement payload for the administrator policy singleton."""

    model_config = ConfigDict(extra="forbid")

    default_retention_seconds: int = Field(
        ge=MIN_DEFAULT_RETENTION_SECONDS, le=MAX_DEFAULT_RETENTION_SECONDS
    )
    max_file_bytes: int = Field(ge=MIN_FILE_BYTES, le=MAX_FILE_BYTES)
    platform_quota_bytes: int = Field(ge=MIN_QUOTA_BYTES, le=MAX_QUOTA_BYTES)
    adapter_quota_bytes: int = Field(ge=MIN_QUOTA_BYTES, le=MAX_QUOTA_BYTES)
    allow_manual_delete: bool
    max_custom_retention_seconds: int = Field(
        ge=MIN_CUSTOM_RETENTION_SECONDS, le=MAX_CUSTOM_RETENTION_SECONDS
    )
    min_free_space_bytes: int = Field(ge=MIN_FREE_SPACE_BYTES, le=MAX_FREE_SPACE_BYTES)
    staged_ttl_seconds: int = Field(ge=MIN_STAGED_TTL_SECONDS, le=MAX_STAGED_TTL_SECONDS)

    @model_validator(mode="after")
    def validate_cross_field_invariants(self) -> "ManagedInputSettingsUpdate":
        if self.adapter_quota_bytes > self.platform_quota_bytes:
            raise ValueError("adapter quota cannot exceed platform quota")
        if self.max_custom_retention_seconds < self.default_retention_seconds:
            raise ValueError("custom retention limit cannot be below default retention")
        return self


class ManagedInputAdapterUsage(BaseModel):
    """Non-sensitive usage and quota facts for one Adapter."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: int
    actual_bytes: int
    reserved_bytes: int
    total_bytes: int
    quota_bytes: int
    over_quota: bool


class ManagedInputUsage(BaseModel):
    """Current platform and per-Adapter capacity usage."""

    model_config = ConfigDict(extra="forbid")

    platform_actual_bytes: int
    platform_reserved_bytes: int
    platform_total_bytes: int
    adapters: list[ManagedInputAdapterUsage] = Field(default_factory=list)


class ManagedInputSettingsResponse(ManagedInputSettingsUpdate):
    """Safe settings response; deployment paths and tokens are excluded."""

    id: int
    usage: ManagedInputUsage
    over_quota: bool
    platform_over_quota: bool
    adapter_over_quota: list[int] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ManagedInputCapabilityResponse(BaseModel):
    """Minimum business-user capability facts for the Managed Input UI.

    Keep this resource deliberately smaller than the administrator settings
    response: only business-form retention bounds are included; deployment
    paths, quota usage and credentials are not part of a capability check.
    """

    model_config = ConfigDict(extra="forbid")

    managed_files_enabled: bool
    ready: bool
    default_retention_seconds: int
    max_custom_retention_seconds: int
    allow_manual_delete: bool
    allowed_extensions: list[str] = Field(min_length=1)


class ManagedInputArtifactResponse(BaseModel):
    """Safe metadata for one Adapter-owned staged Artifact."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: Literal["STAGED"]
    created_at: datetime
    expires_at: datetime | None
