"""Bounded REST collection pagination with loop detection."""

from __future__ import annotations

import json
import random
import re
import time
from urllib import error, parse, request

# REST 分页采集：可修改的配置集中在这里。
# 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
# 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
# HTTP_BEARER_TOKEN：HTTP Bearer Token，使用此认证时配置。
# HTTP_API_KEY：HTTP API Key，使用此认证时配置。
CONFIG = {
    # 填写实际接口地址；不要在地址中填写密码或 Token。
    "url": "https://api.example/resources",
    # 分页方式：page（页码）、offset（偏移量）、cursor（游标）或 next-url（下一页地址）。
    "strategy": "page",
    # 接口响应中列表的字段路径，例如 items 或 data.items。
    "records_path": "items",
    # 接口响应中下一页游标或地址的字段路径。
    "next_path": "next",
    # 接口接收页码的参数名。
    "page_parameter": "page",
    # 接口接收每页条数的参数名。
    "size_parameter": "page_size",
    # 从第几页开始读取。
    "start_page": 1,
    # 每次请求的条数，不能超过目标接口限制。
    "page_size": 100,
    # 普通请求头；Bearer 认证可增加 "DLR-Auth": "bearer:HTTP_BEARER_TOKEN"。
    # Token 在本适配器凭据绑定中配置。
    "headers": {},
    # 可选认证：例如 {"parameter":"api_key","secret_binding":"HTTP_API_KEY"}。
    # 在本适配器的凭据绑定中配置同名键。
    "query_auth": None,
    # 是否允许下一页跳转到其他站点；建议保留 false。
    "allow_cross_origin_next": False,
    # 单次运行最多读取的页数。
    "max_pages": 20,
    # 单次运行最多返回的记录数。
    "max_records": 10000,
    # 单次运行处理或返回的数据大小上限，单位字节。
    "max_bytes": 4194304,
    # 单次请求超时时间，单位秒。
    "timeout_seconds": 30,
    # 读取请求失败时的最多重试次数。
    "max_retries": 2,
}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = request.build_opener(NoRedirect())
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
_STABLE_ERRORS = frozenset(
    {
        "input_must_be_object",
        "invalid_url",
        "invalid_strategy",
        "invalid_headers",
        "invalid_query_auth",
        "credential_query_collision",
        "direct_credential_query_forbidden",
        "direct_credential_header_forbidden",
        "invalid_auth_scheme",
        "missing_credential",
        "request_timeout",
        "request_failed",
        "response_too_large",
        "retry_limit_exceeded",
        "unexpected_status",
        "invalid_json_response",
        "cross_origin_next_url",
        "records_path_not_array",
        "response_path_missing",
        "pagination_no_progress",
        "offset_not_advancing",
        "pagination_loop_detected",
        "cursor_not_advancing",
    }
)


def _credential_like_name(name: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    return (
        any(marker in compact for marker in _CREDENTIAL_NAME_MARKERS)
        or compact.endswith("auth")
        or compact.endswith("sig")
    )


def _positive(value: object, default: int, maximum: int) -> int:
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _bounded_integer(
    value: object,
    default: int,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise ValueError(error_code)
    return value


def _path(value: object, dotted: str) -> object:
    current = value
    if not dotted:
        return current
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError("response_path_missing")
    return current


def _same_origin(left: str, right: str) -> bool:
    a, b = parse.urlsplit(left), parse.urlsplit(right)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


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


def _checkpoint(strategy: str, page: int, offset: int) -> dict[str, object] | None:
    if strategy == "page":
        return {"strategy": "page", "start_page": page}
    if strategy == "offset":
        return {"strategy": "offset", "start_offset": offset}
    # Cursor and next-URL continuations are opaque and may carry credentials.
    # A redacted token is diagnostic-only and cannot safely resume the scan.
    return None


def _headers(context, raw: object) -> tuple[dict[str, str], set[str], set[str]]:
    if raw is None:
        return {}, set(), set()
    if not isinstance(raw, dict):
        raise ValueError("invalid_headers")
    result = {str(key): str(value) for key, value in raw.items()}
    auth_names = [key for key in result if key.casefold() == "dlr-auth"]
    if len(auth_names) > 1:
        raise ValueError("invalid_headers")
    auth = result.pop(auth_names[0], None) if auth_names else None
    if any(_credential_like_name(key) for key in result):
        raise ValueError("direct_credential_header_forbidden")
    credential_headers: set[str] = set()
    sensitive: set[str] = set()
    if auth is not None:
        if not isinstance(auth, str):
            raise ValueError("invalid_auth_scheme")
        scheme, separator, key = auth.partition(":")
        if not separator:
            raise ValueError("invalid_auth_scheme")
        # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        secret = context.secrets.get(key)
        if not secret:
            raise ValueError("missing_credential")
        if scheme == "bearer":
            injected = f"Bearer {secret}"
            result["Authorization"] = injected
            credential_headers.add("authorization")
        elif scheme.startswith("api-key/") and scheme.removeprefix("api-key/"):
            header_name = scheme.removeprefix("api-key/")
            injected = secret
            result[header_name] = injected
            credential_headers.add(header_name.casefold())
        else:
            raise ValueError("invalid_auth_scheme")
        sensitive.update({secret, injected})
    for name, value in result.items():
        _validate_header(name, value)
    return result, credential_headers, sensitive


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


def _query_auth(context, raw: object) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"parameter", "secret_binding"}:
        raise ValueError("invalid_query_auth")
    parameter = raw.get("parameter")
    binding = raw.get("secret_binding")
    if not isinstance(parameter, str) or not _QUERY_NAME.fullmatch(parameter):
        raise ValueError("invalid_query_auth")
    if not isinstance(binding, str) or not binding:
        raise ValueError("invalid_query_auth")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    secret = context.secrets.get(binding)
    if not secret:
        raise ValueError("missing_credential")
    return parameter, secret


def _apply_query_auth(
    query: dict[str, str],
    query_auth: tuple[str, str] | None,
    *,
    allow_injected: bool,
) -> None:
    if query_auth is None:
        return
    parameter, secret = query_auth
    if parameter in query:
        if allow_injected and query[parameter] == secret:
            return
        raise ValueError("credential_query_collision")
    query[parameter] = secret


def _reject_direct_credential_query(
    query: list[tuple[str, str]],
    query_auth: tuple[str, str] | None = None,
    *,
    allow_injected: bool = False,
) -> None:
    allowed_matches = 0
    for name, value in query:
        if not _credential_like_name(name):
            continue
        if allow_injected and query_auth == (name, value):
            allowed_matches += 1
            continue
        raise ValueError("direct_credential_query_forbidden")
    if allowed_matches > 1:
        raise ValueError("credential_query_collision")


def _records_bytes(records: list[object]) -> int:
    return len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _get_json(
    context,
    url: str,
    headers: dict[str, str],
    deadline: float,
    max_bytes: int,
    retries: int,
) -> tuple[object, int]:
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("request_timeout")
        try:
            try:
                with _OPENER.open(
                    request.Request(url, headers=headers), timeout=remaining
                ) as response:
                    payload = response.read(max_bytes + 1)
                    status = response.status
            except error.HTTPError as exc:
                payload = exc.read(max_bytes + 1)
                status = exc.code
        except Exception:
            if attempt == retries:
                raise ValueError("request_failed") from None
            delay = min(
                0.1 * 2**attempt + random.random() * 0.05,
                1.0,
                deadline - time.monotonic(),
            )
            if delay <= 0:
                raise ValueError("request_timeout") from None
            time.sleep(delay)
            continue
        if len(payload) > max_bytes:
            raise ValueError("response_too_large")
        if status == 429 or 500 <= status < 600:
            if attempt == retries:
                raise ValueError("retry_limit_exceeded")
            delay = min(
                0.1 * 2**attempt + random.random() * 0.05,
                1.0,
                deadline - time.monotonic(),
            )
            if delay <= 0:
                raise ValueError("request_timeout")
            time.sleep(delay)
            continue
        if not 200 <= status < 300:
            raise ValueError("unexpected_status")
        try:
            return json.loads(payload), len(payload)
        except json.JSONDecodeError:
            raise ValueError("invalid_json_response") from None
    raise ValueError("request_failed")


def _handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    raw_url = input.get("url")
    if not isinstance(raw_url, str):
        raise ValueError("invalid_url")
    try:
        origin = parse.urlsplit(raw_url)
    except ValueError:
        raise ValueError("invalid_url") from None
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or origin.username is not None
        or origin.password is not None
        or origin.fragment
    ):
        raise ValueError("invalid_url")
    initial_query = parse.parse_qsl(origin.query, keep_blank_values=True)
    _reject_direct_credential_query(initial_query)
    strategy = input.get("strategy", "page")
    if strategy not in {"page", "offset", "cursor", "next-url"}:
        raise ValueError("invalid_strategy")
    max_pages = _positive(input.get("max_pages"), 20, 500)
    max_records = _positive(input.get("max_records"), 10_000, 100_000)
    max_bytes = _positive(input.get("max_bytes"), 4_194_304, 16_777_216)
    page_size = _positive(input.get("page_size"), 100, 1_000)
    timeout = _positive(input.get("timeout_seconds"), 30, 120)
    deadline = time.monotonic() + timeout
    retries = _bounded_integer(input.get("max_retries"), 2, 0, 5, "invalid_max_retries")
    page = _bounded_integer(input.get("start_page"), 1, 1, 1_000_000, "invalid_start_page")
    offset = _bounded_integer(
        input.get("start_offset"), 0, 0, 1_000_000_000, "invalid_start_offset"
    )
    records_path = str(input.get("records_path", "items"))
    next_path = str(input.get("next_path", "next"))
    headers, credential_headers, sensitive = _headers(context, input.get("headers"))
    query_auth = _query_auth(context, input.get("query_auth"))
    if query_auth is not None:
        query_secret = query_auth[1]
        sensitive.update(
            {
                query_secret,
                parse.quote(query_secret, safe=""),
                parse.quote_plus(query_secret, safe=""),
            }
        )
    records: list[object] = []
    seen_tokens: set[str] = set()
    seen_batches: set[str] = set()
    next_url = raw_url
    cursor: str | None = None
    total_bytes = 0
    pages = 0
    checkpoint = None
    partial = False
    for _ in range(max_pages):
        remaining_bytes = max_bytes - total_bytes
        if remaining_bytes <= 0 or time.monotonic() >= deadline:
            partial = True
            checkpoint = _checkpoint(strategy, page, offset)
            break
        parts = parse.urlsplit(next_url if strategy == "next-url" else raw_url)
        query_pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
        if strategy == "next-url":
            _reject_direct_credential_query(
                query_pairs,
                query_auth,
                allow_injected=True,
            )
        query = dict(query_pairs)
        if strategy == "page":
            query.update(
                {
                    str(input.get("page_parameter", "page")): str(page),
                    str(input.get("size_parameter", "page_size")): str(page_size),
                }
            )
        elif strategy == "offset":
            query.update(
                {
                    str(input.get("offset_parameter", "offset")): str(offset),
                    str(input.get("limit_parameter", "limit")): str(page_size),
                }
            )
        elif strategy == "cursor":
            query[str(input.get("limit_parameter", "limit"))] = str(page_size)
            if cursor is not None:
                query[str(input.get("cursor_parameter", "cursor"))] = cursor
        url = parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), ""))
        cross_origin = not _same_origin(raw_url, url)
        if cross_origin and not bool(input.get("allow_cross_origin_next", False)):
            raise ValueError("cross_origin_next_url")
        if cross_origin and query_auth is not None:
            query.pop(query_auth[0], None)
        _reject_direct_credential_query(
            list(query.items()),
            query_auth,
            allow_injected=not cross_origin and strategy == "next-url",
        )
        if not cross_origin:
            _apply_query_auth(query, query_auth, allow_injected=strategy == "next-url")
        url = parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parse.urlencode(query), ""))
        request_headers = (
            {
                key: value
                for key, value in headers.items()
                if key.casefold() in {"accept", "content-type", "user-agent"}
                and key.casefold() not in credential_headers
            }
            if cross_origin
            else headers
        )
        payload, byte_count = _get_json(
            context, url, request_headers, deadline, remaining_bytes, retries
        )
        pages += 1
        total_bytes += byte_count
        if total_bytes > max_bytes:
            partial = True
            checkpoint = _checkpoint(strategy, page, offset)
            break
        page_records = _path(payload, records_path)
        if not isinstance(page_records, list):
            raise ValueError("records_path_not_array")
        if not page_records:
            break
        safe_records = [_scrub(item, sensitive) for item in page_records]
        fingerprint = json.dumps(safe_records, sort_keys=True, separators=(",", ":"))
        if fingerprint in seen_batches:
            raise ValueError("pagination_no_progress")
        seen_batches.add(fingerprint)
        remaining = max_records - len(records)
        if len(safe_records) > remaining:
            partial = True
            checkpoint = _checkpoint(strategy, page, offset)
            break
        if _records_bytes([*records, *safe_records]) > max_bytes:
            partial = True
            checkpoint = _checkpoint(strategy, page, offset)
            break
        records.extend(safe_records)
        if strategy == "page":
            page += 1
        elif strategy == "offset":
            previous = offset
            offset += len(page_records)
            if offset <= previous:
                raise ValueError("offset_not_advancing")
        else:
            raw_next = _path(payload, next_path)
            if raw_next in (None, ""):
                break
            candidate = (
                parse.urljoin(url, str(raw_next)) if strategy == "next-url" else str(raw_next)
            )
            if candidate in seen_tokens:
                raise ValueError("pagination_loop_detected")
            seen_tokens.add(candidate)
            if strategy == "next-url":
                target = parse.urlsplit(candidate)
                if (
                    target.scheme not in {"http", "https"}
                    or not target.netloc
                    or target.username is not None
                    or target.password is not None
                    or target.fragment
                    or (
                        not _same_origin(raw_url, candidate)
                        and not bool(input.get("allow_cross_origin_next", False))
                    )
                ):
                    raise ValueError("cross_origin_next_url")
                next_url = candidate
            else:
                if cursor == candidate:
                    raise ValueError("cursor_not_advancing")
                cursor = candidate
        if len(records) >= max_records:
            partial = True
            checkpoint = _checkpoint(strategy, page, offset)
            break
    else:
        partial = True
        checkpoint = _checkpoint(strategy, page, offset)
    return {
        "records": records,
        "count": len(records),
        "pages": pages,
        "bytes": total_bytes,
        "partial": partial,
        "checkpoint": checkpoint,
    }


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    try:
        return _handle(context, input)
    except ValueError as error:
        code = str(error)
        if code in _STABLE_ERRORS:
            raise ValueError(code) from None
        raise ValueError("request_failed") from None
    except Exception:
        raise ValueError("request_failed") from None
