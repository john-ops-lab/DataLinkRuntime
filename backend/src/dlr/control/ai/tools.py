"""M5.7 Wave C1: DLR's explicit read-only Tool registry and bounded dispatcher.

This module owns the whitelist, the strict argument schemas, the fixed
execution bounds and the sanitization boundary for the controlled Tool Call
loop. It is deliberately NOT a general agent runtime: there is no way to
register a tool at runtime from request data, every registered tool is
read-only by construction, and the dispatcher executes tool calls strictly
sequentially (concurrency is fixed at 1).

Bounds (all fixed module constants, enforced before any provider round trip):

- ``MAX_TOOL_CALLS_PER_ASSIST``: total individual tool calls per assist.
- ``MAX_TOOL_ROUNDS``: provider round trips that carry tool calls.
- ``MAX_TOOL_RESULT_CHARS``: sanitized result sent back to the model per call.
- ``MAX_TOOL_RESULT_TOTAL_CHARS``: accumulated sanitized result budget.
- ``MAX_TOOL_ARGS_CHARS``: per-string argument bound (schema-level).
- ``MAX_TOOL_SUMMARY_CHARS``: UI summary bound for args and results.
- ``TOOL_TIMEOUT_SECONDS``: wall-clock bound for one tool execution.
- ``MAX_TOOL_CONCURRENCY``: fixed at 1 (sequential).

Every executed call logs one line with only tool name / status / duration /
size / stable error code — never arguments, results or any sensitive data.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dlr.control.ai import dlr_docs

logger = logging.getLogger("dlr.ai.tools")

# --- Fixed bounds ------------------------------------------------------------

MAX_TOOL_CALLS_PER_ASSIST = 8
MAX_TOOL_ROUNDS = 4
MAX_TOOL_RESULT_CHARS = 4000
MAX_TOOL_RESULT_TOTAL_CHARS = 16000
MAX_TOOL_ARGS_CHARS = 500
MAX_TOOL_SUMMARY_CHARS = 400
TOOL_TIMEOUT_SECONDS = 10.0
MAX_TOOL_CONCURRENCY = 1

_TRUNCATION_SUFFIX = "\n…[DLR 工具结果已截断]"
_REDACTED = "[REDACTED]"

# Stable per-call error codes (never echo args or results).
CODE_UNKNOWN_TOOL = "ai_tool_unknown"
CODE_ARGS_INVALID = "ai_tool_args_invalid"
CODE_TIMEOUT = "ai_tool_timeout"
CODE_FAILED = "ai_tool_failed"

# Common secret shapes redacted wherever tool args/results could be rendered
# (browser, model context or error messages). The round's API key is also
# redacted explicitly by value; this list is a second, pattern-based layer.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
)


# Argument validators: strict schemas (extra keys rejected) plus per-string
# length bounds, so malformed or oversized arguments are rejected before any
# execution and never echo back into error summaries.
@dataclass
class ToolSpec:
    """One whitelisted read-only tool.

    Not frozen on purpose: the registry is module-private and tests swap
    handlers to exercise timeout/failure paths. ``_TOOLS`` is never mutated
    from request data.
    """

    """One whitelisted read-only tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolExecution:
    """The sanitized outcome of one tool call."""

    tool_name: str
    status: str  # "success" | "error"
    args_summary: str
    result_summary: str
    model_content: str
    error_code: str | None
    duration_ms: int
    result_truncated: bool
    result_size: int
    source: str | None


def _bounded_text(value: str, max_chars: int) -> str:
    """Deterministic length bound; the truncation suffix is never appended
    to strings that already fit, and the suffix itself is cut to the bound."""
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX


def sanitize_text(value: str, api_key: str | None, max_chars: int) -> str:
    """Redact secrets and clamp length.

    Applied on every path that could reach the model, the browser, logs or
    errors. The round's API key is redacted by exact value; the pattern list
    catches common token shapes. Truncation is deterministic (head kept) so
    identical inputs always produce identical outputs.
    """
    sanitized = value
    if api_key:
        sanitized = sanitized.replace(api_key, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    return _bounded_text(sanitized, max_chars)


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return _REDACTED


def _summary_text(value: object, api_key: str | None) -> str:
    """Bounded, sanitized, deterministic string form for model/UI use."""
    return sanitize_text(_stringify(value), api_key, MAX_TOOL_SUMMARY_CHARS)


def _docs_entry_payload(entry: dlr_docs.DocEntry, *, include_content: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": entry.id,
        "title": entry.title,
        "category": entry.category,
        "summary": entry.summary,
        "source": entry.source,
    }
    if include_content:
        payload["content"] = entry.content
    return payload


def _handler_docs_list(args: dict[str, Any]) -> dict[str, Any]:
    category = args.get("category") or None
    entries = dlr_docs.list_entries(category)
    return {
        "tool": "dlr_docs_list",
        "total": len(entries),
        "items": [_docs_entry_payload(entry, include_content=False) for entry in entries],
    }


def _handler_docs_search(args: dict[str, Any]) -> dict[str, Any]:
    query = args["query"]
    limit = min(max(1, int(args.get("limit", 5))), 10)
    entries = dlr_docs.search_entries(query, limit)
    return {
        "tool": "dlr_docs_search",
        "query": query,
        "limit": limit,
        "total_matches": len(entries),
        "items": [_docs_entry_payload(entry, include_content=False) for entry in entries],
    }


def _handler_docs_read(args: dict[str, Any]) -> dict[str, Any]:
    entry = dlr_docs.get_entry(args["doc_id"])
    if entry is None:
        raise ToolFailure(CODE_ARGS_INVALID, "unknown dlr docs id")
    return {"tool": "dlr_docs_read", "item": _docs_entry_payload(entry, include_content=True)}


class ToolFailure(Exception):
    """Stable, sanitized tool execution failure (no args/result reflection)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Argument validators: strict schemas (extra keys rejected) plus per-string
# length bounds, so malformed or oversized arguments are rejected before any
# execution and never echo back into error summaries.
class ToolArgsValidator:
    """Bounded strict-schema validation for one tool's arguments."""

    def __init__(self, parameters: dict[str, Any]) -> None:
        self.parameters = parameters

    def validate(self, raw_arguments: str) -> dict[str, Any]:
        """Parse and validate raw JSON arguments.

        Raises ``ToolFailure(ai_tool_args_invalid)`` without reflecting the
        offending input for malformed JSON, non-object arguments, unknown
        keys, wrong types or oversized strings.
        """
        try:
            parsed = json.loads(raw_arguments, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError, RecursionError):
            raise ToolFailure(CODE_ARGS_INVALID, "tool arguments must be a JSON object") from None
        if not isinstance(parsed, dict):
            raise ToolFailure(CODE_ARGS_INVALID, "tool arguments must be a JSON object")
        self._check_unknown_keys(parsed)
        validated: dict[str, Any] = {}
        for key, spec in self.parameters["properties"].items():
            if key not in parsed:
                if key in self.parameters.get("required", []):
                    raise ToolFailure(CODE_ARGS_INVALID, "missing required tool argument")
                continue
            validated[key] = self._check_value(key, spec, parsed[key])
        return validated

    def _check_unknown_keys(self, parsed: dict[str, Any]) -> None:
        allowed = set(self.parameters.get("properties", {}))
        if any(key not in allowed for key in parsed):
            raise ToolFailure(CODE_ARGS_INVALID, "unknown tool argument")

    def _check_value(self, key: str, spec: dict[str, Any], value: object) -> Any:
        expected = spec.get("type")
        if expected == "string":
            if not isinstance(value, str):
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            if len(value) > MAX_TOOL_ARGS_CHARS:
                raise ToolFailure(CODE_ARGS_INVALID, "tool argument too long")
            return value
        if expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            return value
        if expected in ("number", "boolean"):
            if isinstance(value, bool) and expected != "boolean":
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            if expected == "boolean" and not isinstance(value, bool):
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            return value
        if expected == "object":
            if not isinstance(value, dict):
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            return value
        if expected == "array":
            if not isinstance(value, list):
                raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
            return value
        if expected is None and spec.get("nullable"):
            if value is None:
                return None
            raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument type")
        raise ToolFailure(CODE_ARGS_INVALID, "invalid tool argument")  # defensive


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


_TOOLS: dict[str, ToolSpec] = {
    "dlr_docs_list": ToolSpec(
        name="dlr_docs_list",
        description=(
            "List the app-shipped DLR platform help documents. Optional category "
            "filter ('runtime' or 'platform'). Read-only, deterministic and bounded."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional category filter"},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_handler_docs_list,
    ),
    "dlr_docs_search": ToolSpec(
        name="dlr_docs_search",
        description=(
            "Search the app-shipped DLR platform help documents by query text. "
            "Returns at most 10 bounded entry summaries with a dlr-docs:v1 source. "
            "Read-only and deterministic."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text, e.g. 'runtime contract' or 'secrets'",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (1-10, default 5)",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_handler_docs_search,
    ),
    "dlr_docs_read": ToolSpec(
        name="dlr_docs_read",
        description=(
            "Read one app-shipped DLR platform help document by its exact id "
            "(from dlr_docs_list / dlr_docs_search). Bounded content with a "
            "dlr-docs:v1 source identifier. Read-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Exact document id"},
            },
            "required": ["doc_id"],
            "additionalProperties": False,
        },
        handler=_handler_docs_read,
    ),
}


def tools_payload() -> list[dict[str, Any]]:
    """OpenAI Chat Completions ``tools`` definitions for the whitelist."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in _TOOLS.values()
    ]


def tool_names() -> tuple[str, ...]:
    return tuple(sorted(_TOOLS))


def is_registered_tool(name: str) -> bool:
    """Whitelist check; unknown tools are rejected before any execution."""
    return name in _TOOLS


def _execute_one(
    spec: ToolSpec,
    validated_args: dict[str, Any],
    api_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Run one handler with a wall-clock timeout (sequential executor)."""
    started = time.monotonic()
    result: dict[str, Any] = {}
    truncated = False
    try:
        # The executor is deliberately a single worker: MAX_TOOL_CONCURRENCY
        # is fixed at 1 and each call gets its own timeout budget.
        output = spec.handler(validated_args)
        result = output if isinstance(output, dict) else {"value": output}
    except ToolFailure:
        raise
    except Exception:
        raise ToolFailure(CODE_FAILED, "tool execution failed") from None
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms > TOOL_TIMEOUT_SECONDS * 1000:
        raise ToolFailure(CODE_TIMEOUT, "tool execution timed out")
    # Sanitize the raw handler output before it can reach the model.
    result = _sanitize_result(result, api_key)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        result = _truncate_result(result, api_key)
        truncated = True
    return result, truncated


def _sanitize_result(result: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    """Deep-sanitize one tool result: redact secrets and clamp string length."""
    sanitized: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, str):
            sanitized[key] = sanitize_text(value, api_key, MAX_TOOL_RESULT_CHARS)
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_result(value, api_key)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_result(item, api_key) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def _truncate_result(result: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    """Deterministically cut an oversized sanitized result to the bound."""
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True)
    bounded = _bounded_text(raw, MAX_TOOL_RESULT_CHARS)
    return {"value": bounded, "truncated": True}


def _error_execution(
    tool_name: str,
    args_summary: str,
    error_code: str,
    duration_ms: int,
) -> ToolExecution:
    """One sanitized error execution; the model only ever sees stable codes."""
    return ToolExecution(
        tool_name=tool_name,
        status="error",
        args_summary=args_summary,
        result_summary="",
        model_content=json.dumps(
            {
                "ok": False,
                "error_code": error_code,
                "error": "tool call rejected or failed",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        error_code=error_code,
        duration_ms=duration_ms,
        result_truncated=False,
        result_size=0,
        source=None,
    )


def execute_tool_call(
    tool_name: str,
    raw_arguments: str,
    api_key: str | None,
) -> ToolExecution:
    """Validate, bound and execute ONE whitelisted read-only tool call.

    Unknown / unregistered / write tools are rejected without execution
    (``ai_tool_unknown``). Malformed or oversized arguments are rejected
    without execution (``ai_tool_args_invalid``). Timeouts and handler
    failures produce stable error results. Everything that could reach the
    model, the browser or logs is sanitized and length-bounded.
    """
    started = time.monotonic()
    spec = _TOOLS.get(tool_name)
    if spec is None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "ai_tool tool=%s status=error code=%s duration_ms=%d size=0",
            _bounded_text(tool_name, 64),
            CODE_UNKNOWN_TOOL,
            elapsed_ms,
        )
        return _error_execution(
            _bounded_text(tool_name, 64),
            "",
            CODE_UNKNOWN_TOOL,
            elapsed_ms,
        )
    try:
        validated_args = ToolArgsValidator(spec.parameters).validate(raw_arguments)
    except ToolFailure as error:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "ai_tool tool=%s status=error code=%s duration_ms=%d size=0",
            spec.name,
            error.code,
            elapsed_ms,
        )
        return _error_execution(
            spec.name,
            _summary_text(raw_arguments, api_key),
            error.code,
            elapsed_ms,
        )
    try:
        result, truncated = _execute_one(spec, validated_args, api_key)
    except ToolFailure as error:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "ai_tool tool=%s status=error code=%s duration_ms=%d size=0",
            spec.name,
            error.code,
            elapsed_ms,
        )
        return _error_execution(
            spec.name,
            _summary_text(validated_args, api_key),
            error.code,
            elapsed_ms,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    content = json.dumps(result, ensure_ascii=False, sort_keys=True)
    size = len(content.encode())
    logger.info(
        "ai_tool tool=%s status=success duration_ms=%d size=%d truncated=%s",
        spec.name,
        elapsed_ms,
        size,
        str(truncated).lower(),
    )
    source = None
    if isinstance(result.get("item"), dict):
        source = result["item"].get("source")
    if isinstance(result.get("items"), list) and result["items"]:
        first = result["items"][0]
        if isinstance(first, dict):
            source = first.get("source")
    if not isinstance(source, str) or len(source) > 128:
        source = None
    return ToolExecution(
        tool_name=spec.name,
        status="success",
        args_summary=_summary_text(validated_args, api_key),
        result_summary=_bounded_text(content, MAX_TOOL_SUMMARY_CHARS),
        model_content=content,
        error_code=None,
        duration_ms=elapsed_ms,
        result_truncated=truncated,
        result_size=size,
        source=source,
    )


def tool_result_content(execution: ToolExecution) -> str:
    """The sanitized tool result message sent back to the model."""
    return execution.model_content


def check_budget(calls_used: int, round_index: int, incoming_calls: int) -> None:
    """Raise ValueError when a round would exceed the fixed call/round budget.

    The check is deterministic and happens before any execution of the
    incoming round, so a looping model can never spend unbounded time.
    """
    if calls_used + incoming_calls > MAX_TOOL_CALLS_PER_ASSIST:
        raise ValueError("tool call budget exceeded")
    if round_index > MAX_TOOL_ROUNDS:
        raise ValueError("tool round budget exceeded")


def truncation_suffix() -> str:
    return _TRUNCATION_SUFFIX
