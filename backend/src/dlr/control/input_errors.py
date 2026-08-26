"""Stable machine codes shared by the InputConfig and Managed Input APIs."""

from enum import StrEnum


class InputConfigErrorCode(StrEnum):
    """Input and compatibility codes reserved by Issue #127."""

    SOURCE_NOT_AVAILABLE = "input_source_not_available"
    REVISION_CONFLICT = "input_config_revision_conflict"
    NOT_INITIALIZED = "input_config_not_initialized"
    INVALID = "input_invalid"
    EXECUTION_OVERRIDE_NOT_SUPPORTED = "execution_input_override_not_supported"


class ManagedInputErrorCode(StrEnum):
    """Stable machine codes for the B1 upload and staged-artifact boundary."""

    FEATURE_NOT_AVAILABLE = "input_source_not_available"
    INVALID = "input_invalid"
    FILE_TYPE_NOT_ALLOWED = "input_file_type_not_allowed"
    FILE_TOO_LARGE = "input_file_too_large"
    ADAPTER_QUOTA_EXCEEDED = "adapter_input_quota_exceeded"
    PLATFORM_QUOTA_EXCEEDED = "platform_input_quota_exceeded"
    LOW_WATERMARK = "artifact_store_low_watermark"
    STORE_UNAVAILABLE = "artifact_store_unavailable"
    UPLOAD_FAILED = "input_upload_failed"
    UPLOAD_INTERRUPTED = "input_upload_interrupted"
    SESSION_EXPIRED = "upload_session_expired"
    ARTIFACT_NOT_FOUND = "input_artifact_not_found"
    ARTIFACT_NOT_READY = "input_artifact_not_ready"
    ARTIFACT_DELETE_FAILED = "input_artifact_delete_failed"
    CHECKSUM_INVALID = "input_artifact_checksum_mismatch"
