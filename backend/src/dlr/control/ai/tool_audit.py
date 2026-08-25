"""Bounded, request-correlated persistence boundary for AI tool audit events.

The audit stream deliberately lives outside the ordinary Control ``*.log``
files: it is JSON Lines, owns its size/count rotation in-process, and never
propagates into the root logger. Typed event construction is added at the
orchestrator boundary; this module owns only request correlation and the
dedicated file-handler lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from dlr.common.config import settings
from dlr.control.ai import tools as tools_service

AUDIT_LOGGER_NAME = "dlr.ai.tool-audit"
AUDIT_FILENAME = "ai-tool-audit.jsonl"

_HANDLER_MARKER = "_dlr_ai_tool_audit_handler"
_CONFIGURE_LOCK = threading.RLock()
_SCHEMA_VERSION = 1
_QUERY_HASH_CHARS = 12
_MAX_IDENTIFIER_CHARS = 96
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

AuditToolStatus = Literal["success", "error", "blocked"]
AuditTerminalStatus = Literal["success", "stopped", "error"]
AuditArgumentValue = str | int | dict[str, str | int]


@dataclass(frozen=True)
class AiAuditCorrelation:
    """Non-secret identifiers for one Assist request and browser conversation."""

    request_id: str
    conversation_id: str


@dataclass
class AiToolAuditTrail:
    """Request-local counters and typed emission methods for one Assist.

    The API intentionally accepts no Prompt, attachment, result body, source
    code, reasoning or raw Provider response. Raw tool arguments enter only
    the tool-specific summarizer and are never added to an event directly.
    """

    correlation: AiAuditCorrelation
    adapter_id: int
    started_at: float = 0.0
    attempted_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    blocked_calls: int = 0
    max_round_index: int = 0
    stop_reason: str | None = None
    _terminal_written: bool = False

    def __post_init__(self) -> None:
        if self.started_at <= 0:
            self.started_at = time.monotonic()

    def record_tool_attempt(
        self,
        *,
        round_index: int,
        tool_name: str,
        raw_arguments: str,
        validated_arguments: Mapping[str, object] | None,
        status: AuditToolStatus,
        duration_ms: int,
        result_size: int,
        result_truncated: bool,
        error_code: str | None = None,
        stop_reason: str | None = None,
        redact_values: tuple[str, ...] = (),
    ) -> int:
        """Persist one executed or blocked tool attempt and return its index."""

        self.attempted_calls += 1
        self.max_round_index = max(self.max_round_index, max(0, round_index))
        if status == "success":
            self.successful_calls += 1
        elif status == "error":
            self.failed_calls += 1
        else:
            self.blocked_calls += 1
        if stop_reason is not None:
            self.stop_reason = _safe_code_value(stop_reason)
        _emit_record(
            {
                **self._base_record("tool_attempt"),
                "round": max(0, round_index),
                "call_index": self.attempted_calls,
                "tool": _safe_tool_name(tool_name, redact_values),
                "args_summary": summarize_tool_arguments(
                    tool_name,
                    raw_arguments,
                    validated_arguments,
                    redact_values=redact_values,
                ),
                "status": status,
                "duration_ms": max(0, duration_ms),
                "result_size": max(0, result_size),
                "result_truncated": bool(result_truncated),
                "error_code": _safe_code_value(error_code),
                "stop_reason": _safe_code_value(stop_reason),
            }
        )
        return self.attempted_calls

    def record_guard(
        self,
        *,
        round_index: int,
        stop_reason: str,
        error_code: str | None = None,
    ) -> None:
        """Persist a request guard that is not tied to one Provider tool call."""

        self.max_round_index = max(self.max_round_index, max(0, round_index))
        self.stop_reason = _safe_code_value(stop_reason)
        _emit_record(
            {
                **self._base_record("guard"),
                "round": max(0, round_index),
                "call_index": self.attempted_calls,
                "tool": None,
                "args_summary": {},
                "status": "blocked",
                "duration_ms": 0,
                "result_size": 0,
                "result_truncated": False,
                "error_code": _safe_code_value(error_code),
                "stop_reason": self.stop_reason,
            }
        )

    def finish(
        self,
        *,
        status: AuditTerminalStatus,
        error_code: str | None = None,
    ) -> None:
        """Persist exactly one terminal record for this Assist request."""

        if self._terminal_written:
            return
        self._terminal_written = True
        _emit_record(
            {
                **self._base_record("request_terminal"),
                "round": self.max_round_index,
                "call_index": self.attempted_calls,
                "tool": None,
                "args_summary": {},
                "status": status,
                "duration_ms": max(0, int((time.monotonic() - self.started_at) * 1000)),
                "result_size": 0,
                "result_truncated": False,
                "error_code": _safe_code_value(error_code),
                "stop_reason": self.stop_reason,
                "successful_calls": self.successful_calls,
                "failed_calls": self.failed_calls,
                "blocked_calls": self.blocked_calls,
            }
        )

    def _base_record(self, event_type: str) -> dict[str, object]:
        return {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "schema_version": _SCHEMA_VERSION,
            "event_type": event_type,
            "request_id": self.correlation.request_id,
            "conversation_id": self.correlation.conversation_id,
            "adapter_id": self.adapter_id,
        }


def new_request_correlation(conversation_id: str | None) -> AiAuditCorrelation:
    """Create a unique request id and an old-client request-scoped fallback."""

    request_id = str(uuid.uuid4())
    if conversation_id is None:
        return AiAuditCorrelation(
            request_id=request_id,
            conversation_id=str(uuid.uuid4()),
        )
    parsed = uuid.UUID(conversation_id)
    if parsed.version != 4 or str(parsed) != conversation_id:
        raise ValueError("conversation_id must be a canonical UUID v4")
    return AiAuditCorrelation(request_id=request_id, conversation_id=conversation_id)


def summarize_tool_arguments(
    tool_name: str,
    raw_arguments: str,
    validated_arguments: Mapping[str, object] | None,
    *,
    redact_values: tuple[str, ...] = (),
) -> dict[str, AuditArgumentValue]:
    """Return only the registered tool's approved argument metadata.

    Query text is represented by length and a short SHA-256 digest. Unknown
    tools and invalid argument payloads expose only their UTF-8 byte size.
    Unrecognized keys are always discarded.
    """

    if validated_arguments is None or not tools_service.is_registered_tool(tool_name):
        return {"raw_bytes": len(raw_arguments.encode("utf-8", errors="surrogatepass"))}

    summary: dict[str, AuditArgumentValue] = {}

    def identifier(key: str) -> None:
        value = validated_arguments.get(key)
        if isinstance(value, str):
            summary[key] = _safe_identifier(value, redact_values)

    def query() -> None:
        value = validated_arguments.get("query")
        if isinstance(value, str):
            summary["query_length"] = len(value)
            summary["query_sha256"] = _short_digest(value)

    def limit() -> None:
        value = validated_arguments.get("limit")
        if isinstance(value, int) and not isinstance(value, bool):
            summary["limit"] = value

    if tool_name == "dlr_docs_list":
        identifier("category")
    elif tool_name == "dlr_docs_search":
        query()
        limit()
    elif tool_name == "dlr_docs_read":
        identifier("doc_id")
    elif tool_name == "list_knowledge_bases":
        identifier("source")
    elif tool_name == "search_knowledge":
        identifier("source")
        identifier("knowledge_base_id")
        query()
        limit()
    elif tool_name == "read_knowledge":
        identifier("source")
        identifier("item_id")
    return summary


def _short_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[
        :_QUERY_HASH_CHARS
    ]


def _safe_identifier(
    value: str,
    redact_values: tuple[str, ...],
) -> str | dict[str, str | int]:
    sanitized = tools_service.sanitize_text(
        value,
        None,
        _MAX_IDENTIFIER_CHARS,
        extra_values=redact_values,
    )
    if sanitized == "[REDACTED]" or _SAFE_IDENTIFIER.fullmatch(sanitized):
        return sanitized
    return {"length": len(value), "sha256": _short_digest(value)}


def _safe_tool_name(tool_name: str, redact_values: tuple[str, ...]) -> str:
    if tools_service.is_registered_tool(tool_name):
        return tool_name
    # Unknown names are model-controlled. Preserve correlation without
    # persisting a fabricated name that may contain Prompt or source text.
    sanitized = tools_service.sanitize_text(tool_name, None, 256, redact_values)
    return f"unknown:{_short_digest(sanitized)}"


def _safe_code_value(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _SAFE_CODE.fullmatch(value) else "audit_code_invalid"


def audit_log_path() -> Path:
    """Return the fixed audit path inside Control's persistent log boundary."""

    return Path(settings.platform_log_root).expanduser() / "control" / AUDIT_FILENAME


def configure_ai_tool_audit_logging() -> bool:
    """Attach one bounded JSONL handler to the isolated AI audit logger.

    A missing or unwritable platform-log mount must not prevent Control from
    starting. In that case this returns ``False`` and leaves any previously
    working audit handler in place. Compose provides the persistent mount in
    supported deployments.
    """

    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            mode="a",
            maxBytes=settings.ai_tool_audit_max_bytes,
            backupCount=settings.ai_tool_audit_backup_count,
            encoding="utf-8",
            delay=False,
            errors="backslashreplace",
        )
    except OSError:
        return False

    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_MARKER, True)

    with _CONFIGURE_LOCK:
        logger = logging.getLogger(AUDIT_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False
        for existing in list(logger.handlers):
            if getattr(existing, _HANDLER_MARKER, False):
                logger.removeHandler(existing)
                existing.close()
        logger.addHandler(handler)
    return True


def close_ai_tool_audit_logging() -> None:
    """Close the app-owned handler, primarily for orderly shutdown and tests."""

    with _CONFIGURE_LOCK:
        logger = logging.getLogger(AUDIT_LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()


def _emit_record(record: dict[str, object]) -> None:
    """Write and explicitly flush one complete, deterministic JSON line."""

    try:
        serialized = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with _CONFIGURE_LOCK:
            logger = logging.getLogger(AUDIT_LOGGER_NAME)
            handlers = [
                handler for handler in logger.handlers if getattr(handler, _HANDLER_MARKER, False)
            ]
            if not handlers:
                if not configure_ai_tool_audit_logging():
                    return
                handlers = [
                    handler
                    for handler in logger.handlers
                    if getattr(handler, _HANDLER_MARKER, False)
                ]
            logger.info("%s", serialized)
            for handler in handlers:
                handler.flush()
    except (OSError, TypeError, ValueError):
        # Audit-storage availability must not turn a read-only Assist into a
        # state-changing retry. Supported Compose deployments make this path
        # writable and tests exercise the persisted contract directly.
        return
