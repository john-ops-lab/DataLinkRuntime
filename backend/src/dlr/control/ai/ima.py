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
      -> data.info_list: [{kb_id, kb_name, cover_url, content_count?}]
         (legacy id/name accepted; missing content_count remains unknown)
    optional enrichment: POST /openapi/wiki/v1/get_knowledge_base
      body {"ids": [...]} -> data.infos keyed by the normalized kb_id/id values from those ids
- ``search_knowledge``:
    POST /openapi/wiki/v1/search_knowledge
      body {"query": ..., "cursor": "", "knowledge_base_id": ...}
      -> data.info_list: [{media_id, title, parent_folder_id, highlight_content}]
      The official request has no limit field; DLR keeps the upstream order
      and locally retains the requested top N within the 512 KiB page bound.
- ``read_knowledge`` (official content read chain):
    POST /openapi/wiki/v1/get_media_info  body {"media_id": ...}
      -> data {media_type, url_info{url, headers}, notebook_ext_info{notebook_id}}
      1) media_type=11 with notebook_ext_info.notebook_id:
           POST /openapi/note/v1/get_doc_content
             body {"note_id": ..., "target_content_format": 0}
           -> data.content
      2) url_info.url: bounded HTTPS fetch of the official media URL (with
         the returned headers); the URL host must be an official media host
         (ima.qq.com / *.myqcloud.com / exact mp.weixin.qq.com) or an
         explicitly configured host. WeChat is media-only, never an API host.
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
from html.parser import HTMLParser
from http import client as http_client
from typing import Any
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.ai import providers
from dlr.control.ai.knowledge import (
    KS_AUTH_FAILED,
    KS_CONFIG_INVALID,
    KS_CREDENTIAL_INVALID,
    KS_DNS_FAILED,
    KS_FULL_TEXT_UNAVAILABLE,
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
    KnowledgeSearchResult,
    KnowledgeSource,
    KnowledgeSourceError,
)
from dlr.control.models import Credential
from dlr.control.services import knowledge_source as knowledge_source_service
from dlr.control.services import secrets as secrets_service

logger = logging.getLogger("dlr.ai.knowledge")

# Official ima OpenAPI root (same default as the official skill's ima_api.cjs).
DEFAULT_OFFICIAL_BASE_URL = "https://ima.qq.com"

# Official hosts that may be contacted directly (endpoint allowlist).
DEFAULT_OFFICIAL_HOSTS = frozenset(("ima.qq.com",))
# ``get_media_info`` may return a public WeChat article as the readable media
# for an item.  This exact host is accepted only for the media-URL branch; it
# is deliberately not an allowed ima API endpoint.
OFFICIAL_MEDIA_EXACT_HOSTS = frozenset(("mp.weixin.qq.com",))
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
MAX_KNOWLEDGE_CONTENT_COUNT = 2_147_483_647

# Official business error codes -> stable DLR codes. ``msg`` is never echoed.
_BUSINESS_ERROR_CODES: dict[int, str] = {
    20002: KS_RATE_LIMITED,  # apiKey 超过最大限频
    20004: KS_AUTH_FAILED,  # apiKey 鉴权失败
    110012: KS_CONFIG_INVALID,  # 接口无效
    110021: KS_RATE_LIMITED,  # 请求频控
    110030: KS_AUTH_FAILED,  # 无权限
}

# These codes mean that a search result cannot be upgraded to full text only
# when returned by the official get_media_info/get_doc_content read chain.
# 210005 is NOTE_NOT_OWNER in the official skill mapping. 220030 was
# live-observed in 2026-08 for subscribed-base get_media_info access and is not
# claimed as a published API-wide contract.
_FULL_TEXT_UNAVAILABLE_CODES = frozenset((210005, 220030))
_FULL_TEXT_READ_PATHS = frozenset((PATH_GET_MEDIA_INFO, PATH_GET_DOC_CONTENT))

_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BLOCKED_MEDIA_HEADERS = frozenset(
    (
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "ima-openapi-apikey",
        "ima-openapi-clientid",
        "proxy-authorization",
        "transfer-encoding",
    )
)
_PUBLIC_MEDIA_AUTH_HEADERS = frozenset(("authorization", "cookie"))


class _VisibleHTMLTextParser(HTMLParser):
    """Extract visible UTF-8 text without executing or retaining markup."""

    _SKIPPED_ELEMENTS = frozenset(("script", "style", "noscript", "svg"))

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.casefold()
        if lowered in self._SKIPPED_ELEMENTS:
            self._skip_stack.append(lowered)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack and tag.casefold() == self._skip_stack[-1]:
            self._skip_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self._skip_stack and data.strip():
            self.parts.append(data)


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
    allow_official_media_hosts: bool = False,
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
    if not _host_allowed(
        hostname,
        allowed_hosts,
        allow_official_media_hosts=allow_official_media_hosts,
    ):
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL is not allowed")
    if parts.scheme == "http" and not allow_http:
        raise KnowledgeSourceError(KS_CONFIG_INVALID, "knowledge source URL must use HTTPS")
    return url


def _host_allowed(
    hostname: str,
    allowed_hosts: frozenset[str],
    *,
    allow_official_media_hosts: bool,
) -> bool:
    if hostname in DEFAULT_OFFICIAL_HOSTS or hostname in allowed_hosts:
        return True
    if not allow_official_media_hosts:
        return False
    if hostname in OFFICIAL_MEDIA_EXACT_HOSTS:
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
        *,
        full_text_read: bool = False,
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
        except KnowledgeSourceError:
            raise
        except http_client.IncompleteRead as error:
            # The connection dropped before the full body arrived (a server
            # closing with unread request data can RST the client mid-read).
            # When the partial body already exceeds the size bound, the
            # response is still deterministically too large; otherwise the
            # incomplete transport cannot be trusted and maps to unreachable.
            partial = error.partial or b""
            if len(partial) > MAX_KNOWLEDGE_RESPONSE_BYTES:
                raise KnowledgeSourceError(
                    KS_TOO_LARGE, "knowledge source response too large"
                ) from None
            raise KnowledgeSourceError(KS_UNREACHABLE, "knowledge source unreachable") from None
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
        return self._unwrap_envelope(parsed, full_text_read=full_text_read)

    def _unwrap_envelope(
        self, parsed: dict[str, Any], *, full_text_read: bool = False
    ) -> dict[str, Any]:
        """Validate the official ``{code, msg, data}`` envelope.

        Business errors (``code != 0``) map to stable ``ks_*`` codes; the
        official ``msg`` is never echoed.
        """
        code = parsed.get("code")
        if not isinstance(code, int):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if code != 0:
            stable = (
                KS_FULL_TEXT_UNAVAILABLE
                if full_text_read and code in _FULL_TEXT_UNAVAILABLE_CODES
                else _BUSINESS_ERROR_CODES.get(code, KS_UPSTREAM_ERROR)
            )
            raise KnowledgeSourceError(stable, "knowledge source upstream error")
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return data

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"{self.endpoint}{path}",
            self._headers(),
            payload,
            full_text_read=path in _FULL_TEXT_READ_PATHS,
        )

    @staticmethod
    def _field(value: object, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if len(value) > max_chars:
            raise KnowledgeSourceError(KS_TOO_LARGE, "knowledge source response too large")
        return value

    def _preferred_field(
        self,
        item: dict[str, Any],
        preferred: str,
        fallback: str,
    ) -> str:
        """Normalize one upstream field while preserving strict validation.

        Tencent ima currently returns ``kb_id`` / ``kb_name`` while older
        responses use ``id`` / ``name``. A present, non-empty preferred field
        wins; only a missing or empty preferred field falls back to the legacy
        field. The selected value still goes through ``_field`` so malformed
        or oversized values never enter the normalized boundary.
        """
        value = item.get(preferred)
        if value is None or value == "":
            value = item.get(fallback)
        return self._field(value, MAX_KNOWLEDGE_FIELD_CHARS)

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

    @staticmethod
    def _search_info_list(
        data: dict[str, Any], field: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Return only the locally consumed top hits from one bounded page.

        ima's official search request has no ``limit`` field and the live API
        can return far more than DLR's 20-item listing bound.  The 512 KiB
        wire bound is authoritative for the full page; after parsing it, only
        the first local ``limit`` entries cross the normalization boundary.
        Every entry must at least be an object so malformed tail data cannot
        inflate the reported trajectory count. Only consumed entries undergo
        the deeper field-level validation needed for normalization.
        """
        items = data.get(field)
        if not isinstance(items, list):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        if any(not isinstance(item, dict) for item in items):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return items[:limit], len(items)

    def _search_page_is_end(self, data: dict[str, Any]) -> bool | None:
        is_end = data.get("is_end")
        if is_end is not None and not isinstance(is_end, bool):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        next_cursor = data.get("next_cursor")
        if next_cursor is not None:
            self._opt_field(next_cursor, MAX_KNOWLEDGE_FIELD_CHARS)
        return is_end

    @staticmethod
    def _content_count(item: dict[str, Any] | None) -> int | None:
        if item is None or "content_count" not in item or item.get("content_count") is None:
            return None
        value = item.get("content_count")
        if isinstance(value, str):
            # Live ima responses encode this field as a JSON decimal string.
            # Only the canonical unsigned form is accepted: no signs,
            # whitespace, fractions, leading zeroes or unbounded integers.
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", value):
                raise KnowledgeSourceError(
                    KS_RESPONSE_INVALID, "malformed knowledge source response"
                )
            value = int(value)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_KNOWLEDGE_CONTENT_COUNT
        ):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        return value

    def list_knowledge_bases(self) -> list[KnowledgeBaseSummary]:
        """POST search_knowledge_base (empty query = all KBs) + optional
        get_knowledge_base description enrichment (official read flow)."""
        data = self._call(
            PATH_SEARCH_KNOWLEDGE_BASE,
            {"query": "", "cursor": "", "limit": MAX_KNOWLEDGE_ITEMS},
        )
        bases = self._info_list(data, "info_list")
        ids = [self._preferred_field(item, "kb_id", "id") for item in bases]
        details: dict[str, dict[str, Any]] = {}
        if ids:
            # Enrichment is optional: a failed get_knowledge_base must not
            # fail the listing itself (names alone already identify the KBs).
            try:
                info_data = self._call(PATH_GET_KNOWLEDGE_BASE, {"ids": ids[:20]})
                infos = info_data.get("infos")
                if isinstance(infos, dict):
                    for key, info in infos.items():
                        if isinstance(key, str) and isinstance(info, dict):
                            # Validate enrichment only when it is consumed;
                            # the whole enrichment call remains optional.
                            self._opt_field(info.get("description"), MAX_KNOWLEDGE_FIELD_CHARS)
                            self._content_count(info)
                            details[key] = info
            except KnowledgeSourceError:
                logger.info(
                    "ai_knowledge ima op=list enrichment=skipped source=%s",
                    self._source_prefix,
                )
        summaries: list[KnowledgeBaseSummary] = []
        for item in bases:
            item_id = self._preferred_field(item, "kb_id", "id")
            detail = details.get(item_id)
            item_count = self._content_count(item)
            if item_count is None:
                item_count = self._content_count(detail)
            summaries.append(
                KnowledgeBaseSummary(
                    id=item_id,
                    name=self._preferred_field(item, "kb_name", "name"),
                    description=(
                        self._opt_field(detail.get("description"), MAX_KNOWLEDGE_FIELD_CHARS)
                        if detail is not None
                        else ""
                    ),
                    item_count=item_count,
                    source=self._src(item_id),
                )
            )
        return summaries

    def search_knowledge(
        self, query: str, limit: int, knowledge_base_id: str
    ) -> KnowledgeSearchResult:
        data = self._call(
            PATH_SEARCH_KNOWLEDGE,
            {"query": query, "cursor": "", "knowledge_base_id": knowledge_base_id},
        )
        hits: list[KnowledgeHit] = []
        items, returned_matches = self._search_info_list(data, "info_list", limit)
        is_end = self._search_page_is_end(data)
        for item in items:
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
        return KnowledgeSearchResult(
            hits=hits,
            returned_matches=returned_matches,
            is_end=is_end,
        )

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
            allow_official_media_hosts=True,
        )
        hostname = url_parse.urlsplit(validated_url).hostname
        headers = self._bounded_headers(
            raw_headers,
            allow_origin_auth=hostname not in OFFICIAL_MEDIA_EXACT_HOSTS,
        )
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
        except http_client.IncompleteRead as error:
            partial = error.partial or b""
            if len(partial) > MAX_KNOWLEDGE_RESPONSE_BYTES:
                raise KnowledgeSourceError(
                    KS_TOO_LARGE, "knowledge source media too large"
                ) from None
            raise KnowledgeSourceError(
                KS_UNREACHABLE, "knowledge source media unreachable"
            ) from None
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
                "application/xhtml+xml",
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
        if media_type in ("text/html", "application/xhtml+xml"):
            parser = _VisibleHTMLTextParser()
            try:
                parser.feed(text)
                parser.close()
            except (ValueError, RecursionError):
                raise KnowledgeSourceError(
                    KS_RESPONSE_INVALID, "malformed knowledge source response"
                ) from None
            text = " ".join(" ".join(parser.parts).split())
            if len(text) > MAX_KNOWLEDGE_CONTENT_CHARS:
                text = f"{text[: MAX_KNOWLEDGE_CONTENT_CHARS - 1]}…"
        return KnowledgeItem(
            id=item_id,
            title="",
            content=self._field(text, MAX_KNOWLEDGE_CONTENT_CHARS),
            source=self._src(item_id),
        )

    def _bounded_headers(self, raw_headers: object, *, allow_origin_auth: bool) -> dict[str, str]:
        """Bound official media headers without leaking DLR credentials.

        ima/COS and explicitly configured media hosts may require an
        upstream-provided Authorization or Cookie signature. Public WeChat
        articles do not receive either. Hop-by-hop, host-routing, proxy auth,
        ima OpenAPI auth, compression negotiation and any value containing
        the actual DLR Client ID/API Key are always refused.
        """
        if raw_headers is None:
            return {}
        if not isinstance(raw_headers, dict):
            raise KnowledgeSourceError(KS_RESPONSE_INVALID, "malformed knowledge source response")
        headers: dict[str, str] = {}
        for name, value in raw_headers.items():
            if not isinstance(name, str) or not _HEADER_NAME_PATTERN.fullmatch(name):
                continue
            lowered_name = name.casefold()
            if lowered_name in _BLOCKED_MEDIA_HEADERS:
                continue
            if not allow_origin_auth and lowered_name in _PUBLIC_MEDIA_AUTH_HEADERS:
                continue
            if not isinstance(value, str) or len(value) > MAX_MEDIA_HEADER_VALUE_CHARS:
                continue
            if any(secret and secret in value for secret in self.auth.values()):
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
    if session is None:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        )
    config = knowledge_source_service.effective_ima_config(session)
    if not config.enabled or not config.endpoint:
        raise KnowledgeSourceError(KS_NOT_CONFIGURED, "knowledge source is not configured")
    credential = (
        session.get(Credential, config.credential_id)
        if config.credential_id is not None
        else session.scalar(select(Credential).where(Credential.name == config.credential_name))
    )
    if credential is None:
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires a bound Credential",
        )
    if credential.type != "access_key":
        raise KnowledgeSourceError(
            KS_CREDENTIAL_INVALID,
            "knowledge source requires an access_key Credential",
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
    try:
        return tuple(sorted({value for value in _resolve_auth(session).values() if value}))
    except KnowledgeSourceError:
        return ()


def build_source(session: Session | None) -> TencentImaKnowledgeSource:
    """Build one per-call adapter from the effective product/env configuration.

    A saved database row supplies the product configuration.  Without one,
    the existing environment-variable configuration remains compatible.  The
    official endpoint is the product default; a custom endpoint is a
    deployment-only environment override and is never accepted from the API.
    """
    config = knowledge_source_service.effective_ima_config(session)
    if not config.enabled or not config.endpoint:
        raise KnowledgeSourceError(
            KS_NOT_CONFIGURED,
            "knowledge source is not configured",
        )
    auth = _resolve_auth(session)
    return TencentImaKnowledgeSource(
        endpoint=config.endpoint,
        auth=auth,
        timeout_seconds=config.timeout_seconds,
        allowed_hosts=_parse_allowed_hosts(config.allowed_hosts),
        allow_http=config.allow_http,
    )
