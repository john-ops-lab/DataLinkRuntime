"""M5.7 Wave C2: the thin Tencent ima official OpenAPI read-only adapter.

Research status (2026-08, official sources only): Tencent ima (ima.qq.com)
is an official AI knowledge-assistant product, but no official ima OpenAPI
documentation, no open-platform console and no general MCP endpoint are
currently discoverable from official sources (``ima.qq.com/open`` returns
404; ``open.ima.qq.com`` / ``doc.ima.qq.com`` do not resolve; there is no
``ima`` product under cloud.tencent.com and no ima module in the Tencent
Cloud SDK). Community write-ups are deliberately NOT treated as contract.

Therefore this module implements the *official OpenAPI adapter seam*:

- The adapter is a thin, bounded HTTPS JSON client for exactly the three
  read-only operations of the unified KnowledgeSource boundary
  (:mod:`dlr.control.ai.knowledge`), served over DLR's normalized read-only
  wire protocol v1 (documented below and implemented by the fake official
  service used in tests and compose-smoke).
- All security-critical behavior is real and final: HTTPS + official host
  allowlist + IP-literal rejection + redirect refusal (SSRF protection),
  connect/read/total deadlines that interrupt the external call, bounded
  response reads, strict schema/size validation *before* redaction, and
  stable ``ks_*`` error codes that never reflect request data or Secrets.
- The only seam left for the confirmed official ima OpenAPI contract is the
  request mapping (endpoint paths, auth headers/signature, response field
  mapping) in the three operations below. Client ID / API Key / Token are
  resolved from DLR Credentials (Secret Store) at the server-side execution
  point, live only inside one adapter instance for the duration of one tool
  call, and are redacted by value from every summary/result/log path.

Normalized read-only wire protocol v1 (served by the fake official service):

- ``GET {root}/v1/knowledge/bases?limit=N``
      -> 200 ``{"total": N, "items": [{"id","name","description","item_count"}]}``
- ``POST {root}/v1/knowledge/search``  body ``{"query","limit"}``
      -> 200 ``{"total": N, "items": [{"id","title","summary"}]}``
- ``GET {root}/v1/knowledge/items/{id}``
      -> 200 ``{"item": {"id","title","content"}}``
- errors: JSON body with a non-200 HTTP status (401/403/404/429/5xx ...)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from http import client as http_client
from typing import Any
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.ai import providers
from dlr.control.ai.knowledge import (
    KS_AUTH_FAILED,
    KS_CONFIG_INVALID,
    KS_CREDENTIAL_INVALID,
    KS_DNS_FAILED,
    KS_NOT_CONFIGURED,
    KS_NOT_FOUND,
    KS_RATE_LIMITED,
    KS_RESPONSE_INVALID,
    KS_TIMEOUT,
    KS_TOO_LARGE,
    KS_UNREACHABLE,
    KS_UPSTREAM_ERROR,
    MAX_KNOWLEDGE_CONTENT_CHARS,
    MAX_KNOWLEDGE_FIELD_CHARS,
    MAX_KNOWLEDGE_ITEMS,
    KnowledgeBaseSummary,
    KnowledgeHit,
    KnowledgeItem,
    KnowledgeSource,
    KnowledgeSourceError,
)
from dlr.control.models import Credential
from dlr.control.services import secrets as secrets_service

logger = logging.getLogger("dlr.ai.knowledge")

# Official host allowlist default. Only hosts explicitly listed here (or
# added by the deployment for a fake/test service on a private network) may
# ever be contacted; IP literals are always rejected.
DEFAULT_OFFICIAL_HOSTS = frozenset(("ima.qq.com",))

# Fixed wire-response bound (UTF-8 bytes) for one knowledge request.
MAX_KNOWLEDGE_RESPONSE_BYTES = 512 * 1024

# Auth fields accepted from a DLR Credential (mapping seam: the official ima
# contract determines which of these are actually required/sent).
AUTH_FIELDS = (
    "token",
    "client_id",
    "api_key",
    "access_key_id",
    "access_key_secret",
    "username",
    "password",
    "secret",
)

_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _is_dns_resolution_failure(error: BaseException) -> bool:
    return isinstance(error, socket.gaierror) or isinstance(
        getattr(error, "reason", None), socket.gaierror
    )


class _NoRedirectHandler(url_request.HTTPRedirectHandler):
    """Never follow redirects: an official knowledge endpoint that redirects
    is refused instead of forwarding the request (and its auth) anywhere."""

    def redirect_request(
        self,
        req: url_request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = url_request.build_opener(_NoRedirectHandler())


def _parse_allowed_hosts(raw: str) -> frozenset[str]:
    hosts = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not hosts:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source host allowlist is empty")
    for host in hosts:
        if not _HOSTNAME_PATTERN.fullmatch(host) or _is_ip_literal(host):
            raise KnowledgeSourceError(
                KS_CONFIG_INVALID, "knowledge source host allowlist contains an invalid host"
            )
    return frozenset(hosts)


def _validate_endpoint(
    endpoint: str,
    *,
    allow_http: bool,
    allowed_hosts: frozenset[str],
) -> str:
    """Validate the configured official endpoint (SSRF guard).

    The endpoint must be HTTPS (HTTP only through the explicit test/smoke
    escape hatch), must not carry credentials/query/fragment, and its
    hostname must be a plain registered hostname listed in the official host
    allowlist. IP literals are always rejected so the tool can never target
    internal addresses.
    """
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F or character.isspace()
        for character in endpoint
    ):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source endpoint is invalid")
    parts = url_parse.urlsplit(endpoint)
    try:
        _ = parts.port
    except ValueError:
        raise KnowledgeSourceError(
            KS_CONFIG_INVALID, "knowledge source endpoint is invalid"
        ) from None
    if (
        parts.scheme not in ("https", "http")
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or bool(parts.query)
        or bool(parts.fragment)
    ):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source endpoint is invalid")
    hostname = parts.hostname.lower()
    if _is_ip_literal(hostname):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source endpoint is invalid")
    if hostname not in allowed_hosts:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source endpoint is not allowed")
    if parts.scheme == "http" and not allow_http:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source endpoint must use HTTPS")
    if parts.scheme == "http":
        logger.info(
            "ai_knowledge endpoint_scheme=http host=%s (explicit test/smoke escape hatch)",
            hostname,
        )
    return endpoint.rstrip("/")


@dataclass(frozen=True)
class TencentImaKnowledgeSource(KnowledgeSource):
    """The thin official OpenAPI adapter for Tencent ima.

    ``auth`` carries the decrypted DLR Credential fields (token / client_id /
    api_key / ...) for the duration of one tool execution only; the values
    are returned by :meth:`redact_values` so every summary/result/log path
    redacts them by value.
    """

    endpoint: str
    auth: dict[str, str]
    timeout_seconds: float
    allowed_hosts: frozenset[str] = DEFAULT_OFFICIAL_HOSTS
    allow_http: bool = False
    _source_prefix: str = "ima:v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "endpoint",
            _validate_endpoint(
                self.endpoint,
                allow_http=self.allow_http,
                allowed_hosts=self.allowed_hosts,
            ),
        )

    def redact_values(self) -> tuple[str, ...]:
        return tuple(sorted({value for value in self.auth.values() if value}))

    def _src(self, item_id: str) -> str:
        return f"{self._source_prefix}:{item_id}"

    def _headers(self) -> dict[str, str]:
        """Official-adapter auth mapping seam (pending confirmed contract)."""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.auth.get("token"):
            headers["Authorization"] = f"Bearer {self.auth['token']}"
        if self.auth.get("client_id"):
            headers["X-Client-Id"] = self.auth["client_id"]
        if self.auth.get("api_key"):
            headers["X-Api-Key"] = self.auth["api_key"]
        if self.auth.get("access_key_id"):
            headers["X-Access-Key-Id"] = self.auth["access_key_id"]
        if self.auth.get("access_key_secret"):
            headers["X-Access-Key-Secret"] = self.auth["access_key_secret"]
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One bounded, sanitized JSON request against the official endpoint.

        Connect/read timeouts interrupt the external call (socket deadline)
        and a total wall-clock deadline is enforced around the whole
        operation; redirects are refused; the response body is size-bounded
        and strict-parsed; every failure maps to a stable ``ks_*`` code.
        """
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        url = f"{self.endpoint}{path}"
        try:
            data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            request = url_request.Request(url, data=data, headers=self._headers(), method=method)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source request timed out")
            with _NO_REDIRECT_OPENER.open(request, timeout=remaining) as response:
                raw = response.read(MAX_KNOWLEDGE_RESPONSE_BYTES + 1)
        except KnowledgeSourceError:
            raise
        except url_error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                raise KnowledgeSourceError(
                    KS_UNREACHABLE, "knowledge source redirect refused"
                ) from None
            if error.code in (401, 403):
                raise KnowledgeSourceError(
                    KS_AUTH_FAILED, "knowledge source rejected the credential"
                ) from None
            if error.code == 404:
                raise KnowledgeSourceError(
                    KS_NOT_FOUND, "knowledge source item not found"
                ) from None
            if error.code == 429:
                raise KnowledgeSourceError(
                    KS_RATE_LIMITED, "knowledge source rate limit exceeded"
                ) from None
            raise KnowledgeSourceError(
                KS_UPSTREAM_ERROR, "knowledge source upstream error"
            ) from None
        except TimeoutError:
            raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source request timed out") from None
        except (url_error.URLError, http_client.HTTPException, OSError, ValueError) as error:
            if _is_dns_resolution_failure(error):
                raise KnowledgeSourceError(
                    KS_DNS_FAILED, "knowledge source DNS resolution failed"
                ) from None
            raise KnowledgeSourceError(KS_UNREACHABLE, "knowledge source unreachable") from None
        if time.monotonic() > deadline:
            raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source request timed out")
        if len(raw) > MAX_KNOWLEDGE_RESPONSE_BYTES:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        try:
            parsed = providers.load_json_strict(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise KnowledgeSourceError(
                KS_RESPONSE_INVALID, "knowledge source returned invalid JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return parsed

    def _items(self, response: dict[str, Any], field: str) -> list[dict[str, Any]]:
        items = response.get(field)
        if not isinstance(items, list):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(items) > MAX_KNOWLEDGE_ITEMS:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        if any(not isinstance(item, dict) for item in items):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return items

    @staticmethod
    def _field(value: object, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(value) > max_chars:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        return value

    @staticmethod
    def _count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return value

    def list_knowledge_bases(self) -> list[KnowledgeBaseSummary]:
        path = f"/v1/knowledge/bases?limit={MAX_KNOWLEDGE_ITEMS}"
        response = self._request_json("GET", path)
        return [
            KnowledgeBaseSummary(
                id=self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS),
                name=self._field(item.get("name"), MAX_KNOWLEDGE_FIELD_CHARS),
                description=self._field(item.get("description"), MAX_KNOWLEDGE_FIELD_CHARS),
                item_count=self._count(item.get("item_count")),
                source=self._src(self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS)),
            )
            for item in self._items(response, "items")
        ]

    def search_knowledge(self, query: str, limit: int) -> list[KnowledgeHit]:
        response = self._request_json(
            "POST", "/v1/knowledge/search", {"query": query, "limit": limit}
        )
        return [
            KnowledgeHit(
                id=self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS),
                title=self._field(item.get("title"), MAX_KNOWLEDGE_FIELD_CHARS),
                summary=self._field(item.get("summary"), MAX_KNOWLEDGE_FIELD_CHARS),
                source=self._src(self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS)),
            )
            for item in self._items(response, "items")
        ]

    def read_knowledge(self, item_id: str) -> KnowledgeItem:
        path = f"/v1/knowledge/items/{quote(item_id, safe='')}"
        response = self._request_json("GET", path)
        item = response.get("item")
        if not isinstance(item, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return KnowledgeItem(
            id=self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS),
            title=self._field(item.get("title"), MAX_KNOWLEDGE_FIELD_CHARS),
            content=self._field(item.get("content"), MAX_KNOWLEDGE_CONTENT_CHARS),
            source=self._src(self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS)),
        )


def _resolve_auth(session: Session | None) -> dict[str, str]:
    """Resolve the ima Credential fields from the Secret Store.

    The plaintext exists only in memory for the duration of one tool call
    and is redacted by value from every summary/result/log path.
    """
    credential_name = settings.dlr_ima_credential_name
    if not credential_name:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        )
    if session is None:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        )
    credential = session.scalar(select(Credential).where(Credential.name == credential_name))
    if credential is None:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        )
    try:
        fields = secrets_service.decrypt_fields(credential.ciphertext)
    except HTTPException:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        ) from None
    auth = {key: value for key, value in fields.items() if key in AUTH_FIELDS and value}
    if not auth:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source Credential carries no usable auth field",
        )
    return auth


def secret_values(session: Session | None) -> tuple[str, ...]:
    """Credential truth of the configured ima source (for by-value redaction).

    Best-effort: returns an empty tuple when the source is not configured so
    the assist request itself is never blocked by redaction preparation; the
    actual tool call still surfaces the stable error.
    """
    if not settings.dlr_ima_endpoint or not settings.dlr_ima_credential_name:
        return ()
    return tuple(sorted({value for value in _resolve_auth(session).values() if value}))


def build_source(session: Session | None) -> TencentImaKnowledgeSource:
    """Build one per-call adapter from the deployment configuration.

    Raises ``ks_not_configured`` when no official endpoint is configured and
    ``ks_credential_invalid`` for missing/invalid Credentials — both stable
    and actionable without ever echoing config or Secret truth.
    """
    endpoint = settings.dlr_ima_endpoint
    if not endpoint:
        raise KnowledgeSourceError(
            KS_NOT_CONFIGURED,
            "knowledge source is not configured",
        )
    auth = _resolve_auth(session)
    return TencentImaKnowledgeSource(
        endpoint=endpoint,
        auth=auth,
        timeout_seconds=settings.dlr_ima_timeout_seconds,
        allowed_hosts=_parse_allowed_hosts(settings.dlr_ima_allowed_hosts),
        allow_http=settings.dlr_ima_allow_http,
    )
