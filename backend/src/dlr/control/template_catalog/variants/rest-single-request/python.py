"""Bounded single HTTP request Recipe for DLR."""

from __future__ import annotations

import base64
import json
import re
from urllib import error, parse, request

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_SIDE_EFFECT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_QUERY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RESTRICTED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_CREDENTIAL_NAME_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "authentication",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "signature",
    "token",
)
_SAFE_REQUEST_ERRORS = {
    "credential_query_collision",
    "cross_origin_redirect",
    "direct_credential_query_forbidden",
    "redirect_limit_exceeded",
    "redirect_without_location",
    "side_effect_redirect_forbidden",
}


def _credential_like_name(name: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    return (
        any(marker in compact for marker in _CREDENTIAL_NAME_MARKERS)
        or compact.endswith("auth")
        or compact.endswith("sig")
    )


class SameOriginRedirect(request.HTTPRedirectHandler):
    def __init__(
        self,
        origin: tuple[str, str],
        max_redirects: int,
        query_auth: tuple[str, str] | None,
    ) -> None:
        self.origin = origin
        self.max_redirects = max_redirects
        self.query_auth = query_auth
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if req.get_method() != "GET":
            raise ValueError("side_effect_redirect_forbidden")
        target = parse.urlsplit(parse.urljoin(req.full_url, newurl))
        if (target.scheme, target.netloc) != self.origin:
            raise ValueError("cross_origin_redirect")
        self.count += 1
        if self.count > self.max_redirects:
            raise ValueError("redirect_limit_exceeded")
        values = parse.parse_qsl(target.query, keep_blank_values=True)
        if any(
            _credential_like_name(key)
            and not (
                self.query_auth is not None
                and key == self.query_auth[0]
                and value == self.query_auth[1]
            )
            for key, value in values
        ):
            raise ValueError("direct_credential_query_forbidden")
        if self.query_auth is not None:
            parameter, secret = self.query_auth
            matches = [value for key, value in values if key == parameter]
            if matches and matches != [secret]:
                raise ValueError("credential_query_collision")
            if not matches:
                values.append((parameter, secret))
            target = target._replace(query=parse.urlencode(values))
            newurl = parse.urlunsplit(target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _positive_int(value: object, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return min(value, maximum)


def _secret(context, key: object) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("invalid_secret_key")
    value = context.secrets.get(key)
    if not value:
        raise ValueError("missing_credential")
    return value


def _validate_header(name: str, value: str) -> None:
    normalized = name.casefold()
    if (
        not _HEADER_NAME.fullmatch(name)
        or normalized in _RESTRICTED_HEADERS
        or normalized.startswith("proxy-")
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("request_failed")


def _headers(context, raw: object) -> tuple[dict[str, str], set[str]]:
    if raw is None:
        return {}, set()
    if not isinstance(raw, dict):
        raise ValueError("invalid_headers")
    result = {str(key): str(value) for key, value in raw.items()}
    auth_names = [key for key in result if key.casefold() == "dlr-auth"]
    if len(auth_names) > 1:
        raise ValueError("invalid_headers")
    auth = result.pop(auth_names[0], None) if auth_names else None
    if any(_credential_like_name(key) for key in result):
        raise ValueError("direct_credential_header_forbidden")
    sensitive: set[str] = set()
    if auth:
        scheme, _, key = auth.partition(":")
        secret = _secret(context, key)
        if scheme == "bearer":
            injected = f"Bearer {secret}"
            result["Authorization"] = injected
        elif scheme == "basic":
            encoded = base64.b64encode(secret.encode()).decode()
            injected = f"Basic {encoded}"
            result["Authorization"] = injected
        elif scheme.startswith("api-key/"):
            injected = secret
            result[scheme.removeprefix("api-key/")] = injected
        else:
            raise ValueError("invalid_auth_scheme")
        sensitive.update({secret, injected})
    for name, value in result.items():
        _validate_header(name, value)
    return result, sensitive


def _query_auth(context, raw: object) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"parameter", "secret_binding"}:
        raise ValueError("invalid_query_auth")
    parameter = raw.get("parameter")
    binding = raw.get("secret_binding")
    if not isinstance(parameter, str) or not _QUERY_NAME.fullmatch(parameter):
        raise ValueError("invalid_query_auth")
    return parameter, _secret(context, binding)


def _body(payload: object, content_type: str | None) -> tuple[bytes | None, str | None]:
    if content_type is not None and not isinstance(content_type, str):
        raise ValueError("request_failed")
    if payload is None:
        return None, content_type
    if content_type == "text/plain":
        return str(payload).encode(), content_type
    try:
        value = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (OverflowError, TypeError, ValueError):
        raise ValueError("request_failed") from None
    return value, content_type or "application/json"


def _scrub(value: object, sensitive: set[str]) -> object:
    if isinstance(value, str):
        for secret in sorted(sensitive, key=len, reverse=True):
            if secret:
                size = len(secret.encode("utf-8"))
                marker = "<redacted>" if size >= len("<redacted>") else "*" * size
                value = value.replace(secret, marker)
        return value
    if isinstance(value, list):
        return [_scrub(item, sensitive) for item in value]
    if isinstance(value, dict):
        return {
            str(_scrub(str(key), sensitive)): _scrub(item, sensitive) for key, item in value.items()
        }
    return value


def handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    method = str(input.get("method", "GET")).upper()
    if method not in _METHODS:
        raise ValueError("unsupported_method")
    raw_url = input.get("url")
    if not isinstance(raw_url, str):
        raise ValueError("invalid_url")
    try:
        url_parts = parse.urlsplit(raw_url)
    except ValueError:
        raise ValueError("invalid_url") from None
    if (
        url_parts.scheme not in {"http", "https"}
        or not url_parts.netloc
        or url_parts.username is not None
        or url_parts.password is not None
        or bool(url_parts.fragment)
    ):
        raise ValueError("invalid_url")
    query = input.get("query", {})
    if not isinstance(query, dict):
        raise ValueError("invalid_query")
    query_items = list(parse.parse_qsl(url_parts.query, keep_blank_values=True))
    if any(_credential_like_name(key) for key, _value in query_items):
        raise ValueError("direct_credential_query_forbidden")
    for key, value in sorted(query.items()):
        if (
            not isinstance(key, str)
            or not _QUERY_NAME.fullmatch(key)
            or not isinstance(value, str)
            or len(value) > 4096
        ):
            raise ValueError("invalid_query")
        if _credential_like_name(key):
            raise ValueError("direct_credential_query_forbidden")
        query_items.append((key, value))
    query_auth = _query_auth(context, input.get("query_auth"))
    sensitive = set()
    if query_auth is not None:
        query_secret = query_auth[1]
        sensitive.update(
            {
                query_secret,
                parse.quote(query_secret, safe=""),
                parse.quote_plus(query_secret, safe=""),
            }
        )
    if query_auth is not None:
        parameter, secret = query_auth
        if any(key == parameter for key, _value in query_items):
            raise ValueError("credential_query_collision")
        query_items.append((parameter, secret))
    query_string = parse.urlencode(query_items)
    url = parse.urlunsplit(
        (url_parts.scheme, url_parts.netloc, url_parts.path or "/", query_string, "")
    )
    max_bytes = _positive_int(input.get("max_response_bytes"), 1_048_576, 8_388_608)
    timeout = _positive_int(input.get("timeout_seconds"), 30, 120)
    max_redirects = _positive_int(input.get("max_redirects"), 3, 10)
    try:
        headers, header_secrets = _headers(context, input.get("headers"))
        sensitive.update(header_secrets)
        body, content_type = _body(input.get("body"), input.get("content_type"))
    except ValueError as exc:
        if str(exc) != "request_failed":
            raise
        return {
            "ok": False,
            "error": "request_failed",
            "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
            "retried": False,
        }
    if content_type:
        headers.setdefault("Content-Type", content_type)
        try:
            _validate_header("Content-Type", headers["Content-Type"])
        except ValueError:
            return {
                "ok": False,
                "error": "request_failed",
                "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
                "retried": False,
            }
    allowed = input.get("allowed_statuses", list(range(200, 300)))
    if (
        not isinstance(allowed, list)
        or not allowed
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and 100 <= item <= 599
            for item in allowed
        )
    ):
        raise ValueError("invalid_allowed_statuses")
    try:
        redirect = SameOriginRedirect(
            (url_parts.scheme, url_parts.netloc), max_redirects, query_auth
        )
        opener = request.build_opener(redirect)
        with opener.open(
            request.Request(url, data=body, headers=headers, method=method), timeout=timeout
        ) as response:
            payload = response.read(max_bytes + 1)
            status = response.status
            response_type = response.headers.get_content_type()
    except error.HTTPError as exc:
        try:
            payload = exc.read(max_bytes + 1)
            status = exc.code
            response_type = exc.headers.get_content_type()
        except Exception:
            return {
                "ok": False,
                "error": "request_failed",
                "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
                "retried": False,
            }
    except ValueError as exc:
        raw_category = exc.args[0] if exc.args else None
        category = raw_category if raw_category in _SAFE_REQUEST_ERRORS else "request_failed"
        return {
            "ok": False,
            "error": category,
            "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
            "retried": False,
        }
    except (error.URLError, TimeoutError):
        return {
            "ok": False,
            "error": "request_failed",
            "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
            "retried": False,
        }
    except Exception:
        return {
            "ok": False,
            "error": "request_failed",
            "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
            "retried": False,
        }
    if status not in allowed:
        return {
            "ok": False,
            "error": "unexpected_status",
            "status": status,
            "side_effect_uncertain": method in _SIDE_EFFECT_METHODS,
            "retried": False,
        }
    if len(payload) > max_bytes:
        return {
            "ok": True,
            "status": status,
            "partial": True,
            "bytes_read": max_bytes,
            "response": None,
        }
    text = payload.decode("utf-8", errors="replace")
    if response_type == "application/json" or input.get("response_type") == "json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_json_response", "status": status}
    else:
        value = text
    value = _scrub(value, sensitive)
    output_bytes = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if output_bytes > max_bytes:
        return {
            "ok": True,
            "status": status,
            "partial": True,
            "bytes_read": len(payload),
            "response": None,
        }
    return {
        "ok": True,
        "status": status,
        "content_type": response_type,
        "partial": False,
        "bytes_read": len(payload),
        "response": value,
        "side_effect_warning": method in _SIDE_EFFECT_METHODS,
    }
