"""Non-sensitive Managed Input audit events.

Audit records deliberately accept only identifiers, bounded labels and stable
error codes.  Callers may pass operational values such as storage keys while
compensating for a failed filesystem operation, but those values are ignored
here rather than relying on formatter-level redaction.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("dlr.control.managed_input_audit")

_SAFE_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_MAX_LABEL_LENGTH = 64


def _label(value: object | None) -> str:
    if not isinstance(value, str):
        return "unknown"
    candidate = value[:_MAX_LABEL_LENGTH].casefold()
    return candidate if _SAFE_LABEL.fullmatch(candidate) is not None else "invalid"


def _identifier(value: object | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _code_label(value: object | None) -> str:
    """Keep an omitted stable code distinct from an invalid label."""
    return "none" if value is None else _label(value)


def record_audit_event(
    operation: str,
    outcome: str,
    *,
    actor_kind: str | None = None,
    actor_id: int | None = None,
    adapter_id: int | None = None,
    artifact_id: int | None = None,
    deletion_job_id: int | None = None,
    code: str | None = None,
    **_sensitive_values: Any,
) -> None:
    """Emit one bounded audit event without serializing sensitive values."""
    logger.info(
        "managed_input_audit operation=%s outcome=%s actor_kind=%s actor_id=%s "
        "adapter_id=%s artifact_id=%s deletion_job_id=%s code=%s",
        _label(operation),
        _label(outcome),
        _label(actor_kind),
        _identifier(actor_id),
        _identifier(adapter_id),
        _identifier(artifact_id),
        _identifier(deletion_job_id),
        _code_label(code),
    )


audit_event = record_audit_event

__all__ = ["audit_event", "record_audit_event"]
