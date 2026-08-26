"""Stable machine codes shared by the InputConfig Control API layers."""

from enum import StrEnum


class InputConfigErrorCode(StrEnum):
    """Input and compatibility codes reserved by Issue #127."""

    SOURCE_NOT_AVAILABLE = "input_source_not_available"
    REVISION_CONFLICT = "input_config_revision_conflict"
    NOT_INITIALIZED = "input_config_not_initialized"
    INVALID = "input_invalid"
    EXECUTION_OVERRIDE_NOT_SUPPORTED = "execution_input_override_not_supported"
