"""Stable machine codes shared by the InputConfig Control API layers."""

from enum import StrEnum


class InputConfigErrorCode(StrEnum):
    """Input and compatibility codes reserved by Issue #127."""

    SOURCE_NOT_AVAILABLE = "input_source_not_available"
    REVISION_CONFLICT = "input_config_revision_conflict"
    INVALID = "input_invalid"
    EXECUTION_OVERRIDE_NOT_SUPPORTED = "execution_input_override_not_supported"


INPUT_CONFIG_ERROR_CODES = frozenset(code.value for code in InputConfigErrorCode)
