"""Fixed internal failure categories; public errors and raw output stay separate."""

from typing import Literal, cast

from fastapi import HTTPException

FailureStage = Literal[
    "provider_transport",
    "provider_json",
    "provider_envelope",
    "final_json",
    "output_schema",
    "unicode",
    "output_safety",
    "candidate_configuration",
]
FailureReason = Literal[
    "transport_error",
    "response_too_large",
    "invalid_utf8",
    "malformed_json",
    "duplicate_key",
    "non_finite_number",
    "json_limit",
    "json_value_invalid",
    "invalid_shape",
    "incomplete_completion",
    "incomplete_thinking",
    "empty_content",
    "schema_mismatch",
    "invalid_unicode",
    "secret_reflection",
    "requirements_mismatch",
    "runtime_config_mismatch",
    "unexpected_tools",
]


TransportFailureCategory = Literal[
    "http", "dns", "tls", "connection", "timeout", "protocol", "unknown"
]
_TRANSPORT_FAILURE_CATEGORIES = frozenset(
    {"http", "dns", "tls", "connection", "timeout", "protocol", "unknown"}
)


def safe_transport_category(value: object) -> TransportFailureCategory:
    """Only fixed categories may cross the private audit boundary."""
    return cast(
        TransportFailureCategory,
        value if isinstance(value, str) and value in _TRANSPORT_FAILURE_CATEGORIES else "unknown",
    )


def safe_http_status(value: object) -> int | None:
    """Discard booleans, strings and non-HTTP values instead of coercing them."""
    return value if type(value) is int and 100 <= value <= 599 else None


class StrictJsonError(ValueError):
    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason)
        self.reason = reason


class AiResponseInvalid(HTTPException):
    """Carry only fixed private categories without adding public response fields."""

    def __init__(
        self,
        stage: FailureStage,
        reason: FailureReason,
        message: str = "The AI provider returned an invalid response",
    ) -> None:
        super().__init__(
            status_code=502, detail={"code": "ai_response_invalid", "message": message}
        )
        self.stage = stage
        self.reason = reason
