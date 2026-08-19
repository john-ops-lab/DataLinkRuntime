"""M5.7 Wave C2: the thin Tencent ima official OpenAPI read-only adapter.

Official contract source (verified 2026-08 from the official Tencent ima
skill package ``@tencent-adm/ima-skills`` v1.1.9 — publisher ``tencent-ima``,
certified as Tencent Technology (Shenzhen) Co., Ltd. — mirrored on
skillhub.cn and served from the official download domain
``app-dl.ima.qq.com``):

- Base URL: ``https://ima.qq.com``; every call is HTTP POST with a JSON body.
- Auth headers: ``ima-openapi-clientid`` (Client ID), ``ima-openapi-apikey``
  (API Key), ``Content-Type: application/json``.
- Response envelope: ``{"code": 0, "msg": "...", "data": {...}}``; ``code != 0``
  is a business error whose ``msg`` is never echoed into DLR outputs (only
  stable ``ks_*`` codes reach the model / browser / logs).

Read-only mapping — exactly the three KnowledgeSource operations, no write
interface is implemented or registered:

- ``list_knowledge_bases``:
    POST /openapi/wiki/v1/search_knowledge_base
      body {"query": "", "cursor": "", "limit": N}
      -> data.info_list: [{id, name, cover_url}]
    optional enrichment: POST /openapi/wiki/v1/get_knowledge_base
      body {"ids": [...]} -> data.infos: {id: {description, ...}}
- ``search_knowledge``:
    POST /openapi/wiki/v1/search_knowledge
      body {"query": ..., "cursor": "", "knowledge_base_id": ...}
      -> data.info_list: [{media_id, title, parent_folder_id, highlight_content}]
- ``read_knowledge`` (official content read chain):
    POST /openapi/wiki/v1/get_media_info  body {"media_id": ...}
      -> data {media_type, url_info{url, headers}, notebook_ext_info{notebook_id}}
      1) media_type=11 with notebook_ext_info.notebook_id:
           POST /openapi/note/v1/get_doc_content
             body {"note_id": ..., "target_content_format": 0}
           -> data.content
      2) url_info.url: bounded HTTPS fetch of the official media URL (with
         the returned headers); the URL host must be an official media host
         (ima.qq.com / *.myqcloud.com) or an explicitly configured host.
      3) neither -> stable ks_not_found (content not readable via the API).

Credentials: the Client ID / API Key are stored in a DLR ``access_key``
Credential (``access_key_id`` -> Client ID, ``access_key_secret`` -> API Key)
inside the Secret Store and resolved only at the server-side execution point;
they live only inside one adapter instance for the duration of one tool call
and are redacted by value from every summary/result/log path.

Security hardening (unchanged from the C2 boundary): HTTPS + host allowlist +
IP-literal rejection + redirect refusal (SSRF protection), connect/read/total
deadlines that interrupt the external call, bounded response reads, strict
schema/size validation before redaction, and stable ``ks_*`` error codes.
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
    KS_UNSUPPORTED,
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

# Official ima OpenAPI root (same default as the official skill's ima_api.cjs).
DEFAULT_OFFICIAL_BASE_URL = "https://ima.qq.com"

# Official hosts that may be contacted directly (endpoint allowlist).
DEFAULT_OFFICIAL_HOSTS = frozenset(("ima.qq.com",))
# Official media-content hosts that get_media_info may return in url_info.url
# (wildcard suffix match for the COS/CDN storage domain).
OFFICIAL_MEDIA_HOST_SUFFIXES = (".myqcloud.com",)

# Official API paths (read-only subset).
PATH_SEARCH_KNOWLEDGE_BASE = "/openapi/wiki/v1/search_knowledge_base"
PATH_GET_KNOWLEDGE_BASE = "/openapi/wiki/v1/get_knowledge_base"
PATH_SEARCH_KNOWLEDGE = "/openapi/wiki/v1/search_knowledge"
PATH_GET_MEDIA_INFO = "/openapi/wiki/v1/get_media_info"
PATH_GET_DOC_CONTENT = "/openapi/note/v1/get_doc_content"

# Fixed wire-response bound (UTF-8 bytes) for one knowledge request.
MAX_KNOWLEDGE_RESPONSE_BYTES = 512 * 1024

# Media-URL fetch bounds.
MAX_MEDIA_HEADERS = 8
MAX_MEDIA_HEADER_VALUE_CHARS = 512

# Official business error codes -> stable DLR codes. ``msg`` is never echoed.
_BUSINESS_ERROR_CODES: dict[int, str] = {
    20002: KS_RATE_LIMITED,  # apiKey 超过最大限频
    20004: KS_AUTH_FAILED,  # apiKey 鉴权失败
    110012: KS_CONFIG_INVALID,  # 接口无效
    110021: KS_RATE_LIMITED,  # 请求频控
    110030: KS_AUTH_FAILED,  # 无权限
}

_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


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


def _validate_http_url(
    url: str,
    *,
    allow_http: bool,
    allowed_hosts: frozenset[str],
    allow_query: bool,
) -> str:
    """Validate one HTTPS URL against the official host allowlist (SSRF guard).

    Used both for the configured endpoint and for media URLs returned by the
    official ``get_media_info`` response. IP literals are always rejected;
    query strings are only allowed for media URLs (COS signed URLs).
    """
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F or character.isspace() for character in url
    ):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is invalid")
    parts = url_parse.urlsplit(url)
    try:
        _ = parts.port
    except ValueError:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is invalid") from None
    if (
        parts.scheme not in ("https", "http")
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or (bool(parts.query) and not allow_query)
    ):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is invalid")
    hostname = parts.hostname.lower()
    if _is_ip_literal(hostname):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is invalid")
    if not _media_host_allowed(hostname, allowed_hosts):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is not allowed")
    if parts.scheme == "http" and not allow_http:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL must use HTTPS")
    return url


def _media_host_allowed(hostname: str, allowed_hosts: frozenset[str]) -> bool:
    if hostname in DEFAULT_OFFICIAL_HOSTS or hostname in allowed_hosts:
        return True
    return any(hostname.endswith(suffix) for suffix in OFFICIAL_MEDIA_HOST_SUFFIXES)


@dataclass(frozen=True)
class TencentImaKnowledgeSource(KnowledgeSource):
    """The thin official OpenAPI adapter for Tencent ima.

    ``auth`` carries the decrypted DLR Credential fields (client_id /
    api_key) for the duration of one tool execution only; the values are
    returned by :meth:`redact_values` so every summary/result/log path
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
            _validate_http_url(
                self.endpoint,
                allow_http=self.allow_http,
                allowed_hosts=self.allowed_hosts,
                allow_query=False,
            ).rstrip("/"),
        )

    def redact_values(self) -> tuple[str, ...]:
        return tuple(sorted({value for value in self.auth.values() if value}))

    def _src(self, item_id: str) -> str:
        return f"{self._source_prefix}:{item_id}"

    def _headers(self) -> dict[str, str]:
        """Official ima auth headers (contract from the official skill)."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "ima-openapi-clientid": self.auth.get("client_id", ""),
            "ima-openapi-apikey": self.auth.get("api_key", ""),
        }

    def _request_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One bounded, sanitized JSON request.

        Connect/read timeouts interrupt the external call (socket deadline)
        and a total wall-clock deadline is enforced around the whole
        operation; redirects are refused; the response body is size-bounded
        and strict-parsed; every failure maps to a stable ``ks_*`` code.
        """
        deadline = time.monotonic() + self.timeout_seconds
        try:
            data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            request = url_request.Request(url, data=data, headers=headers, method=method)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source request timed out")
            with _NO_REDIRECT_OPENER.open(request, timeout=remaining) as response:
                raw = response.read(MAX_KNOWLEDGE_RESPONSE_BYTES + 1)
                response_headers = dict(response.headers)
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
        return self._unwrap_envelope(parsed, response_headers)

    def _unwrap_envelope(
        self, parsed: dict[str, Any], response_headers: dict[str, str]
    ) -> dict[str, Any]:
        """Validate the official ``{code, msg, data}`` envelope.

        Business errors (``code != 0``) map to stable ``ks_*`` codes; the
        official ``msg`` is never echoed. The response headers are kept so
        the media-URL fetch can reuse the same size/type checks.
        """
        code = parsed.get("code")
        if not isinstance(code, int):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if code != 0:
            stable = _BUSINESS_ERROR_CODES.get(code, KS_UPSTREAM_ERROR)
            raise KnowledgeSourceError(stable, "knowledge source upstream error")
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return data

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", f"{self.endpoint}{path}", self._headers(), payload)

    @staticmethod
    def _field(value: object, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(value) > max_chars:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        return value

    @staticmethod
    def _opt_field(value: object, max_chars: int) -> str:
        """Optional string field: empty strings allowed, oversized rejected."""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(value) > max_chars:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        return value

    @staticmethod
    def _info_list(data: dict[str, Any], field: str) -> list[dict[str, Any]]:
        items = data.get(field)
        if not isinstance(items, list):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(items) > MAX_KNOWLEDGE_ITEMS:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        if any(not isinstance(item, dict) for item in items):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return items

    def list_knowledge_bases(self) -> list[KnowledgeBaseSummary]:
        """POST search_knowledge_base (empty query = all KBs) + optional
        get_knowledge_base description enrichment (official read flow)."""
        data = self._call(
            PATH_SEARCH_KNOWLEDGE_BASE,
            {"query": "", "cursor": "", "limit": MAX_KNOWLEDGE_ITEMS},
        )
        bases = self._info_list(data, "info_list")
        ids = [self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS) for item in bases]
        descriptions: dict[str, str] = {}
        if ids:
            # Enrichment is optional: a failed get_knowledge_base must not
            # fail the listing itself (names alone already identify the KBs).
            try:
                info_data = self._call(PATH_GET_KNOWLEDGE_BASE, {"ids": ids[:20]})
                infos = info_data.get("infos")
                if isinstance(infos, dict):
                    for key, info in infos.items():
                        if isinstance(info, dict):
                            descriptions[key] = self._opt_field(
                                info.get("description"), MAX_KNOWLEDGE_FIELD_CHARS
                            )
            except KnowledgeSourceError:
                logger.info(
                    "ai_knowledge ima op=list enrichment=skipped source=%s",
                    self._source_prefix,
                )
        summaries: list[KnowledgeBaseSummary] = []
        for item in bases:
            item_id = self._field(item.get("id"), MAX_KNOWLEDGE_FIELD_CHARS)
            summaries.append(
                KnowledgeBaseSummary(
                    id=item_id,
                    name=self._field(item.get("name"), MAX_KNOWLEDGE_FIELD_CHARS),
                    description=descriptions.get(item_id, ""),
                    item_count=0,
                    source=self._src(item_id),
                )
            )
        return summaries

    def search_knowledge(
        self, query: str, limit: int, knowledge_base_id: str
    ) -> list[KnowledgeHit]:
        data = self._call(
            PATH_SEARCH_KNOWLEDGE,
            {"query": query, "cursor": "", "knowledge_base_id": knowledge_base_id},
        )
        hits: list[KnowledgeHit] = []
        for item in self._info_list(data, "info_list")[:limit]:
            media_id = self._field(item.get("media_id"), MAX_KNOWLEDGE_FIELD_CHARS)
            hits.append(
                KnowledgeHit(
                    id=media_id,
                    title=self._field(item.get("title"), MAX_KNOWLEDGE_FIELD_CHARS),
                    summary=self._opt_field(
                        item.get("highlight_content"), MAX_KNOWLEDGE_FIELD_CHARS
                    ),
                    source=self._src(media_id),
                )
            )
        return hits

    def read_knowledge(self, item_id: str) -> KnowledgeItem:
        """The official content read chain: get_media_info, then either the
        notes get_doc_content branch (media_type=11) or the bounded media-URL
        fetch branch (url_info). Never a write interface."""
        data = self._call(PATH_GET_MEDIA_INFO, {"media_id": item_id})
        media_type = data.get("media_type")
        if media_type == 11:
            ext = data.get("notebook_ext_info")
            if isinstance(ext, dict):
                notebook_id = ext.get("notebook_id")
                if isinstance(notebook_id, str) and notebook_id.strip():
                    content = self._note_content(notebook_id)
                    return KnowledgeItem(
                        id=item_id,
                        title="",
                        content=self._field(content, MAX_KNOWLEDGE_CONTENT_CHARS),
                        source=self._src(item_id),
                    )
            raise KnowledgeSourceError(
                KS_NOT_FOUND, "knowledge item is not readable through the official API"
            )
        url_info = data.get("url_info")
        if isinstance(url_info, dict):
            url = url_info.get("url")
            if isinstance(url, str) and url.strip():
                return self._media_url_item(item_id, url, url_info.get("headers"))
        raise KnowledgeSourceError(
            KS_NOT_FOUND, "knowledge item is not readable through the official API"
        )

    def _note_content(self, notebook_id: str) -> str:
        """Notes branch: POST /openapi/note/v1/get_doc_content (PLAINTEXT)."""
        data = self._call(
            PATH_GET_DOC_CONTENT,
            {"note_id": notebook_id, "target_content_format": 0},
        )
        content = data.get("content")
        if not isinstance(content, str):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return content

    def _media_url_item(self, item_id: str, url: str, raw_headers: object) -> KnowledgeItem:
        """URL branch: bounded fetch of the official media URL.

        The URL is returned by the official API for the requested media_id
        (never user-supplied), but it is still validated against the official
        media host allowlist and fetched with the same redirect/size/timeout
        guards. Server-provided headers are bounded and passed through.
        """
        validated_url = _validate_http_url(
            url,
            allow_http=self.allow_http,
            allowed_hosts=self.allowed_hosts,
            allow_query=True,
        )
        headers = self._bounded_headers(raw_headers)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            request = url_request.Request(validated_url, headers=headers, method="GET")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source request timed out")
            with _NO_REDIRECT_OPENER.open(request, timeout=remaining) as response:
                raw = response.read(MAX_KNOWLEDGE_RESPONSE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except KnowledgeSourceError:
            raise
        except url_error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                raise KnowledgeSourceError(
                    KS_UNREACHABLE, "knowledge source media redirect refused"
                ) from None
            if error.code in (401, 403):
                raise KnowledgeSourceError(
                    KS_AUTH_FAILED, "knowledge source media rejected"
                ) from None
            if error.code == 404:
                raise KnowledgeSourceError(
                    KS_NOT_FOUND, "knowledge source media not found"
                ) from None
            if error.code == 429:
                raise KnowledgeSourceError(
                    KS_RATE_LIMITED, "knowledge source media rate limited"
                ) from None
            raise KnowledgeSourceError(KS_UPSTREAM_ERROR, "knowledge source media error") from None
        except TimeoutError:
            raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source media timed out") from None
        except (url_error.URLError, http_client.HTTPException, OSError, ValueError) as error:
            if _is_dns_resolution_failure(error):
                raise KnowledgeSourceError(
                    KS_DNS_FAILED, "knowledge source media DNS failed"
                ) from None
            raise KnowledgeSourceError(
                KS_UNREACHABLE, "knowledge source media unreachable"
            ) from None
        if time.monotonic() > deadline:
            raise KnowledgeSourceError(KS_TIMEOUT, "knowledge source media timed out")
        if len(raw) > MAX_KNOWLEDGE_RESPONSE_BYTES:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source media too large")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if (
            media_type
            and not media_type.startswith("text/")
            and media_type
            not in (
                "application/json",
                "application/xml",
                "application/markdown",
                "application/x-markdown",
            )
        ):
            raise KnowledgeSourceError(
                KS_UNSUPPORTED, "knowledge item content is not text-readable"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise KnowledgeSourceError(
                KS_UNSUPPORTED, "knowledge item content is not text-readable"
            ) from None
        return KnowledgeItem(
            id=item_id,
            title="",
            content=self._field(text, MAX_KNOWLEDGE_CONTENT_CHARS),
            source=self._src(item_id),
        )

    def _bounded_headers(self, raw_headers: object) -> dict[str, str]:
        """Bound server-provided media headers (name/value length + count)."""
        if raw_headers is None:
            return {}
        if not isinstance(raw_headers, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        headers: dict[str, str] = {}
        for name, value in raw_headers.items():
            if not isinstance(name, str) or not _HEADER_NAME_PATTERN.fullmatch(name):
                continue
            if not isinstance(value, str) or len(value) > MAX_MEDIA_HEADER_VALUE_CHARS:
                continue
            headers[name] = value
            if len(headers) >= MAX_MEDIA_HEADERS:
                break
        return headers


def _resolve_auth(session: Session | None) -> dict[str, str]:
    """Resolve the ima Credential (Client ID / API Key) from the Secret Store.

    The official contract authenticates with two headers, so a DLR
    ``access_key`` Credential (``access_key_id`` -> Client ID,
    ``access_key_secret`` -> API Key) is required. The plaintext exists only
    in memory for the duration of one tool call and is redacted by value
    from every summary/result/log path.
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
    client_id = fields.get("access_key_id", "")
    api_key = fields.get("access_key_secret", "")
    if not client_id or not api_key:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source Credential must be an access_key type with "
            "Client ID / API Key fields",
        )
    return {"client_id": client_id, "api_key": api_key}


def secret_values(session: Session | None) -> tuple[str, ...]:
    """Credential truth of the configured ima source (for by-value redaction).

    Best-effort: returns an empty tuple when the source is not configured so
    the assist request itself is never blocked by redaction preparation; the
    actual tool call still surfaces the stable error.
    """
    if not settings.dlr_ima_credential_name:
        return ()
    return tuple(sorted({value for value in _resolve_auth(session).values() if value}))


def build_source(session: Session | None) -> TencentImaKnowledgeSource:
    """Build one per-call adapter from the deployment configuration.

    The endpoint defaults to the official base URL ``https://ima.qq.com``;
    an explicitly empty endpoint means the source is not configured
    (``ks_not_configured``) and missing/invalid Credentials map to
    ``ks_credential_invalid`` — both stable and actionable without ever
    echoing config or Secret truth.
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
