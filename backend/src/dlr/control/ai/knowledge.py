"""M5.7 Wave C2: DLR's unified read-only KnowledgeSource boundary.

This module owns the *boundary*, not the providers: the three read-only
operations (``list_knowledge_bases`` / ``search_knowledge`` /
``read_knowledge``), the normalized result shapes, the stable error codes and
the read-only/write guards. Concrete sources (e.g. the thin Tencent ima
official OpenAPI adapter in :mod:`dlr.control.ai.ima`) implement the
:class:`KnowledgeSource` contract; the tools layer in ``tools.py`` registers
exactly the three whitelisted tool names and rejects everything else.

Hard guarantees:

- The boundary is read-only *by construction*: the abstract contract exposes
  no write method, and :func:`is_read_only_source` refuses any duck-typed
  object that exposes write-like callables (upload / write / create / delete /
  update / share / sync / ...). There is no runtime registration path from
  request data.
- Results are normalized into bounded dataclasses *before* they can reach the
  model or the browser; every item carries an auditable, non-sensitive
  ``source`` identifier of the form ``<provider>:v1:<id>`` (e.g.
  ``ima:v1:<id>``).
- Errors carry only stable codes (``ks_*``); they never echo queries, item
  ids, endpoint details or any Secret.
- Secret truth (Client ID / API Key / Token) is resolved from DLR Credentials
  at the server-side execution point only. The adapter exposes the values via
  :meth:`KnowledgeSource.redact_values` and the tools layer redacts them by
  value from every summary/result/log path.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger("dlr.ai.knowledge")

# --- Stable error codes (sanitized; never echo request data or Secrets) ------

KS_NOT_CONFIGURED = "ks_not_configured"
KS_CREDENTIAL_INVALID = "ks_credential_invalid"
KS_UNKNOWN_SOURCE = "ks_unknown_source"
KS_CONFIG_INVALID = "ks_config_invalid"
KS_DNS_FAILED = "ks_dns_failed"
KS_UNREACHABLE = "ks_unreachable"
KS_AUTH_FAILED = "ks_auth_failed"
KS_NOT_FOUND = "ks_not_found"
KS_RATE_LIMITED = "ks_rate_limited"
KS_UPSTREAM_ERROR = "ks_upstream_error"
KS_TIMEOUT = "ks_timeout"
KS_RESPONSE_INVALID = "ks_response_invalid"
KS_TOO_LARGE = "ks_too_large"
KS_UNSUPPORTED = "ks_unsupported"

# --- Fixed result bounds (enforced before anything reaches the model/UI) ------

# One knowledge listing may return at most this many items.
MAX_KNOWLEDGE_ITEMS = 20
# Per-string bound for normalized item fields (id / name / title / summary /
# description). Content has its own larger bound below.
MAX_KNOWLEDGE_FIELD_CHARS = 1000
# Bound for the read content of one knowledge item.
MAX_KNOWLEDGE_CONTENT_CHARS = 6000
# Bound for the whole normalized result payload before the tools layer
# applies its own sanitization/truncation budget.
MAX_KNOWLEDGE_RESULT_CHARS = 16000


class KnowledgeSourceError(Exception):
    """Stable, sanitized knowledge-source failure (no request reflection)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KnowledgeBaseSummary:
    """One normalized knowledge base (list result)."""

    id: str
    name: str
    description: str
    item_count: int
    source: str


@dataclass(frozen=True)
class KnowledgeHit:
    """One normalized search hit (search result)."""

    id: str
    title: str
    summary: str
    source: str


@dataclass(frozen=True)
class KnowledgeItem:
    """One normalized knowledge document (read result)."""

    id: str
    title: str
    content: str
    source: str


class KnowledgeSource(ABC):
    """The unified read-only knowledge source contract.

    Concrete sources implement exactly three operations. There is no write,
    upload, delete, share or sync operation anywhere in the boundary.
    """

    @abstractmethod
    def list_knowledge_bases(self) -> list[KnowledgeBaseSummary]:
        """List the knowledge bases of this source (bounded result)."""

    @abstractmethod
    def search_knowledge(
        self, query: str, limit: int, knowledge_base_id: str
    ) -> list[KnowledgeHit]:
        """Search one knowledge base of this source; ``limit`` is already
        clamped by the tool. ``knowledge_base_id`` comes from
        :meth:`list_knowledge_bases` (the official ima API requires it)."""

    @abstractmethod
    def read_knowledge(self, item_id: str) -> KnowledgeItem:
        """Read one knowledge item by its exact id; unknown ids raise
        ``KnowledgeSourceError(ks_not_found)``."""

    def redact_values(self) -> tuple[str, ...]:
        """Secret truth held by this source instance (for by-value redaction).

        The tools layer redacts these values from every summary/result/log
        path. The default returns nothing; credential-backed adapters return
        the resolved Client ID / API Key / Token.
        """
        return ()


class ToolContext(Protocol):
    """The per-execution context the tools layer hands to knowledge sources.

    ``session`` is the request's DB session (used to resolve DLR Credential
    rows inside the Secret Store); ``secret_values`` is the mutable list the
    source appends its credential truth to so the tools layer can redact it
    by value from everything the model / browser / logs see.
    """

    session: Session | None
    secret_values: list[str]


# Write-like method prefixes that a duck-typed object must never expose.
_WRITE_PREFIXES = (
    "upload",
    "write",
    "create",
    "delete",
    "remove",
    "update",
    "share",
    "sync",
    "put",
    "post",
    "save",
    "import",
)


def is_read_only_source(source: object) -> bool:
    """True only for sources without any write-like callable.

    This is the structural half of the read-only boundary: even though the
    abstract contract exposes no write operations, a duck-typed impostor that
    adds one is refused here before it can be used by any tool handler.
    """
    for name in dir(source):
        if not name.startswith("_"):
            lowered = name.lower()
            if any(lowered.startswith(prefix) for prefix in _WRITE_PREFIXES):
                callable_attr = getattr(source, name, None)
                if callable(callable_attr):
                    return False
    return True


def _bounded_str(value: object, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
    if len(value) > max_chars:
        raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
    return value


def _bounded_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
    return value


def _bounded_opt_str(value: object, max_chars: int) -> str:
    """Optional string field: empty strings allowed, oversized rejected."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
    if len(value) > max_chars:
        raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
    return value


def build_source(source_id: str, context: ToolContext | None) -> KnowledgeSource:
    """Resolve one registered knowledge source id to a concrete adapter.

    Unknown ids raise ``ks_unknown_source`` before any execution. The ima
    adapter is built per call so credential truth only exists in memory for
    the duration of one tool execution.
    """
    if source_id == "ima":
        from dlr.control.ai import ima

        session = context.session if context is not None else None
        return ima.build_source(session)
    raise KnowledgeSourceError(KS_UNKNOWN_SOURCE, "unknown knowledge source")


def redact_values_for(source_id: str, session: Session | None) -> tuple[str, ...]:
    """Best-effort credential truth of one source for pre-execution redaction.

    Used by the assist service so the assistant-message echo and every
    summary path can redact the source credential by value even before the
    tool executes. Failures (not configured / missing credential) return an
    empty tuple: the actual tool call still surfaces the stable error.
    """
    if source_id != "ima":
        return ()
    try:
        from dlr.control.ai import ima

        return ima.secret_values(session)
    except KnowledgeSourceError:
        return ()


def _to_result(source: KnowledgeSource) -> None:
    """Write-op guard applied at the tool boundary (defense in depth)."""
    if not is_read_only_source(source):
        raise KnowledgeSourceError(
            KS_UNSUPPORTED,
            "knowledge source exposes write operations and is refused",
        )


def list_knowledge_bases(source_id: str, context: ToolContext | None) -> dict[str, Any]:
    """Tool-facing list operation; returns the normalized tool result dict."""
    source = build_source(source_id, context)
    _to_result(source)
    _collect_redact_values(source, context)
    items = source.list_knowledge_bases()
    payload: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, KnowledgeBaseSummary):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        payload.append(
            {
                "id": _bounded_str(item.id, MAX_KNOWLEDGE_FIELD_CHARS),
                "name": _bounded_str(item.name, MAX_KNOWLEDGE_FIELD_CHARS),
                "description": _bounded_opt_str(item.description, MAX_KNOWLEDGE_FIELD_CHARS),
                "item_count": _bounded_int(item.item_count),
                "source": _bounded_str(item.source, 128),
            }
        )
    return {
        "tool": "list_knowledge_bases",
        "total": len(payload),
        "items": payload,
    }


def search_knowledge(
    source_id: str,
    query: str,
    limit: int,
    knowledge_base_id: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    """Tool-facing search operation; returns the normalized tool result dict."""
    source = build_source(source_id, context)
    _to_result(source)
    _collect_redact_values(source, context)
    hits = source.search_knowledge(query, limit, knowledge_base_id)
    payload: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, KnowledgeHit):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        payload.append(
            {
                "id": _bounded_str(hit.id, MAX_KNOWLEDGE_FIELD_CHARS),
                "title": _bounded_str(hit.title, MAX_KNOWLEDGE_FIELD_CHARS),
                "summary": _bounded_opt_str(hit.summary, MAX_KNOWLEDGE_FIELD_CHARS),
                "source": _bounded_str(hit.source, 128),
            }
        )
    return {
        "tool": "search_knowledge",
        "query": query,
        "limit": limit,
        "total_matches": len(payload),
        "items": payload,
    }


def read_knowledge(source_id: str, item_id: str, context: ToolContext | None) -> dict[str, Any]:
    """Tool-facing read operation; returns the normalized tool result dict."""
    source = build_source(source_id, context)
    _to_result(source)
    _collect_redact_values(source, context)
    item = source.read_knowledge(item_id)
    if not isinstance(item, KnowledgeItem):
        raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
    return {
        "tool": "read_knowledge",
        "item": {
            "id": _bounded_str(item.id, MAX_KNOWLEDGE_FIELD_CHARS),
            "title": _bounded_opt_str(item.title, MAX_KNOWLEDGE_FIELD_CHARS),
            "content": _bounded_str(item.content, MAX_KNOWLEDGE_CONTENT_CHARS),
            "source": _bounded_str(item.source, 128),
        },
    }


def _collect_redact_values(source: KnowledgeSource, context: ToolContext | None) -> None:
    """Append the source's credential truth to the execution context so the
    tools layer can redact it by value from every summary/result/log path."""
    if context is None:
        return
    for value in source.redact_values():
        if value and value not in context.secret_values:
            context.secret_values.append(value)
