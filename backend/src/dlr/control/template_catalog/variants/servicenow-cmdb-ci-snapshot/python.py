"""Bounded ServiceNow cmdb_ci snapshot with optional idempotent CMDB sync."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from urllib import error, parse, request

# ServiceNow 资产采集：可修改的配置集中在这里。
# 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
# 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
# SERVICENOW_USERNAME：ServiceNow 用户名，使用 Basic 认证时配置。
# SERVICENOW_PASSWORD：ServiceNow 密码，使用 Basic 认证时配置。
# SERVICENOW_BEARER_TOKEN：ServiceNow Bearer Token，与 Basic 认证二选一。
# CMDB_TOKEN：目标 CMDB Token，仅同步时配置。
CONFIG = {
    # preview 只采集并返回结果；sync 会写入目标 CMDB，请先配置下方地址和 CMDB_TOKEN 凭据。
    "mode": "preview",
    # 填写 ServiceNow 实例的 HTTPS 地址。
    "instance_url": "https://tenant.example",
    # 填写实例标识，用于区分资产来源。
    "instance_id": "EXAMPLE_TENANT",
    # 读取的资产表，本模板使用 cmdb_ci。
    "table": "cmdb_ci",
    # ServiceNow 查询条件，例如 active=true。
    "encoded_query": "active=true",
    # 需要读取的字段列表。
    "fields": ["sys_id", "name", "sys_class_name", "install_status"],
    # 是否使用 ServiceNow 展示值；false 保留原始值。
    "display_value": False,
    # 单次运行最多读取的页数。
    "max_pages": 20,
    # 单次运行最多返回的记录数。
    "max_records": 5000,
    # 单次运行处理或返回的数据大小上限，单位字节。
    "max_bytes": 8388608,
    # 每次请求的条数，不能超过目标接口限制。
    "page_size": 500,
    # 单次请求超时时间，单位秒。
    "timeout_seconds": 30,
    # 首次读取从 0 开始；sync 必须为 0。
    "offset": 0,
    # 每批处理的记录数。
    "batch_size": 200,
    # 同步范围标识：同一账号、区域和资源范围保持不变。
    "source_scope": "servicenow:EXAMPLE_TENANT:cmdb_ci",
    # 仅 sync 使用：每次新的扫描填写新标识，同一次运行重试保持不变。
    "scan_id": "",
    # 仅 sync 使用：填写目标 CMDB 地址；目标需实现下方代码调用的扫描和批量写入接口。
    "cmdb_base_url": "https://cmdb.example",
}


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = request.build_opener(_NoRedirect())


def _positive(value, default, maximum):
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _headers(context):
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    bearer = context.secrets.get("SERVICENOW_BEARER_TOKEN")
    if bearer:
        return {"Authorization": "Bearer " + bearer}
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    username = context.secrets.get("SERVICENOW_USERNAME")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    password = context.secrets.get("SERVICENOW_PASSWORD")
    if not username or not password:
        raise ValueError("missing_credential")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": "Basic " + token}


def _get_page(context, base, params, deadline, maximum):
    url = (
        parse.urljoin(base.rstrip("/") + "/", "api/now/table/cmdb_ci")
        + "?"
        + parse.urlencode(params)
    )
    headers = _headers(context) | {"Accept": "application/json"}
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("servicenow_timeout")
        try:
            with _NO_REDIRECT_OPENER.open(
                request.Request(url, headers=headers), timeout=remaining
            ) as response:
                payload = response.read(maximum + 1)
                status = response.status
        except error.HTTPError as exc:
            payload = exc.read(maximum + 1)
            status = exc.code
        except (error.URLError, TimeoutError):
            raise ValueError("servicenow_request_failed") from None
        if status == 429 or 500 <= status < 600:
            if attempt == 2:
                raise ValueError("servicenow_retry_limit")
            delay = min(0.1 * 2**attempt, deadline - time.monotonic())
            if delay <= 0:
                raise ValueError("servicenow_timeout")
            time.sleep(delay)
            continue
        if not 200 <= status < 300:
            raise ValueError("servicenow_request_failed")
        if len(payload) > maximum:
            raise ValueError("servicenow_response_too_large")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("servicenow_invalid_json") from None
        result = value.get("result")
        if not isinstance(result, list):
            raise ValueError("servicenow_invalid_result")
        return result, len(payload)
    raise ValueError("servicenow_request_failed")


def _external(account, identifier):
    def component(value):
        return value.replace("%", "%25").replace(":", "%3A")

    return "servicenow:" + component(account) + ":global:cmdb_ci:" + component(identifier)


def _asset(account, record):
    identifier = record.get("sys_id")
    if not isinstance(identifier, str) or not identifier:
        return None

    def display(name):
        value = record.get(name)
        return value.get("display_value") if isinstance(value, dict) else value

    name = display("name") or display("display_name") or identifier
    return {
        "external_key": _external(account, identifier),
        "class": str(display("sys_class_name") or "cmdb_ci"),
        "provider_type": "cmdb_ci",
        "name": str(name),
        "account": account,
        "region": "global",
        "zone": None,
        "status": str(display("install_status")) if display("install_status") is not None else None,
        "tags": {},
        "attributes": {
            key: display(key)
            for key in sorted(record)
            if key not in {"sys_id"}
            and isinstance(display(key), (str, int, float, bool, type(None)))
        },
    }


def _snapshot_result(assets, pages, failure, partial, offset, asset_count=None):
    summary = {
        "assets": len(assets) if asset_count is None else asset_count,
        "relationships": 0,
        "pages": pages,
        "failures": [failure] if failure else [],
    }
    return {
        "schema_version": "dlr-asset-snapshot/v1",
        "assets": assets,
        "relationships": [],
        "summary": summary,
        "partial": partial,
        "checkpoint": {"offset": offset} if partial else None,
    }


def _sync_result(input, assets, pages, failure, offset):
    summary = {
        "assets": len(assets),
        "relationships": 0,
        "pages": pages,
        "failures": [failure] if failure else [],
    }
    return {
        "mode": "sync",
        "scan_id": input["scan_id"],
        "source_scope": input["source_scope"],
        "partial": True,
        "summary": summary,
        "failed": [failure] if failure else ["bounded"],
        "checkpoint": {"offset": offset},
    }


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _candidate_fits(asset_count, asset_item_bytes, pages, offset, max_bytes):
    asset_array_bytes = 2 if asset_count == 0 else asset_item_bytes + asset_count + 1
    shell = _snapshot_result(
        [],
        pages,
        "invalid_source_record",
        True,
        offset,
        asset_count,
    )
    return _encoded_size(shell) - 2 + asset_array_bytes <= max_bytes


def _post(base, path, body, token, idem, deadline):
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Idempotency-Key": idem,
    }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("cmdb_target_error")
    try:
        with _NO_REDIRECT_OPENER.open(
            request.Request(
                parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")),
                data=payload,
                headers=headers,
                method="POST",
            ),
            timeout=remaining,
        ) as response:
            response.read(65_537)
            if not 200 <= response.status < 300:
                raise ValueError("cmdb_target_error")
    except (error.HTTPError, error.URLError, TimeoutError):
        raise ValueError("cmdb_target_error") from None


def _sync(context, input, assets, summary, deadline):
    scan, scope = input.get("scan_id"), input.get("source_scope")
    if not isinstance(scan, str) or not _ID.fullmatch(scan):
        raise ValueError("invalid_scan_id")
    if not isinstance(scope, str) or not _ID.fullmatch(scope):
        raise ValueError("invalid_source_scope")
    base = input.get("cmdb_base_url")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    token = context.secrets.get("CMDB_TOKEN")
    acknowledged_assets = 0
    try:
        target = parse.urlsplit(base) if isinstance(base, str) else None
    except ValueError:
        target = None
    if (
        target is None
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
        or not (
            target.scheme == "https"
            or (target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"})
        )
        or not token
    ):
        raise ValueError("cmdb_target_not_configured")
    common = {"schema_version": "dlr-cmdb-upsert/v1", "source_scope": scope, "scan_id": scan}

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    try:
        begin_idem = digest(["begin", scope, scan])
        _post(
            base,
            "/api/v1/import-scans:begin",
            common
            | {
                "operation": "begin_scan",
                "idempotency_key": begin_idem,
                "provider": "servicenow",
                "catalog_version": "1.0.0",
            },
            token,
            begin_idem,
            deadline,
        )
        batch_size = _positive(input.get("batch_size"), 200, 1000)
        for at in range(0, len(assets), batch_size):
            batch = assets[at : at + batch_size]
            batch_index = at // batch_size
            batch_id = f"assets:servicenow:{scope}:{batch_index:06d}"
            idem = digest(["assets", scope, scan, batch_id])
            _post(
                base,
                f"/api/v1/import-scans/{parse.quote(scan, safe='')}/assets:upsert",
                common
                | {
                    "operation": "upsert_assets",
                    "idempotency_key": idem,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "assets": batch,
                },
                token,
                idem,
                deadline,
            )
            acknowledged_assets += len(batch)
        finish_idem = digest(["finish", scope, scan])
        _post(
            base,
            f"/api/v1/import-scans/{parse.quote(scan, safe='')}:finish",
            common
            | {
                "operation": "finish_scan",
                "idempotency_key": finish_idem,
                "complete": True,
                "summary": summary,
            },
            token,
            finish_idem,
            deadline,
        )
    except ValueError:
        failed_summary = {
            "assets": acknowledged_assets,
            "relationships": 0,
            "pages": summary["pages"],
            "failures": ["target_batch"],
        }
        return {
            "mode": "sync",
            "scan_id": scan,
            "source_scope": scope,
            "partial": True,
            "summary": failed_summary,
            "failed": ["target_batch"],
            "checkpoint": {"scan_id": scan},
        }
    return {
        "mode": "sync",
        "scan_id": scan,
        "source_scope": scope,
        "partial": False,
        "summary": summary,
        "failed": [],
        "checkpoint": None,
    }


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    mode = input.get("mode", "preview")
    if mode not in {"preview", "sync"}:
        raise ValueError("invalid_mode")
    if mode == "sync" and (
        not isinstance(input.get("scan_id"), str) or not isinstance(input.get("source_scope"), str)
    ):
        raise ValueError("stable_scan_identity_required")
    offset = input.get("offset", 0)
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > 2_147_483_647
    ):
        raise ValueError("invalid_offset")
    if mode == "sync" and offset != 0:
        raise ValueError("sync_offset_must_be_zero")
    display_value = input.get("display_value", False)
    if not isinstance(display_value, bool):
        raise ValueError("invalid_display_value")
    encoded_query = input.get("encoded_query", "")
    if not isinstance(encoded_query, str) or len(encoded_query) > 4096:
        raise ValueError("invalid_encoded_query")
    base = input.get("instance_url")
    account = input.get("instance_id")
    try:
        instance = parse.urlsplit(base) if isinstance(base, str) else None
    except ValueError:
        instance = None
    if (
        instance is None
        or instance.scheme != "https"
        or not instance.netloc
        or instance.username is not None
        or instance.password is not None
        or instance.query
        or instance.fragment
        or not isinstance(account, str)
        or not account
        or len(account) > 128
    ):
        raise ValueError("https_instance_url_and_instance_id_required")
    if input.get("table", "cmdb_ci") != "cmdb_ci":
        raise ValueError("only_cmdb_ci_supported")
    fields = input.get("fields", ["sys_id", "name", "sys_class_name", "install_status"])
    if (
        not isinstance(fields, list)
        or len(fields) > 64
        or "sys_id" not in fields
        or not all(isinstance(field, str) and _FIELD.fullmatch(field) for field in fields)
    ):
        raise ValueError("invalid_fields")
    max_pages = _positive(input.get("max_pages"), 20, 200)
    max_records = _positive(input.get("max_records"), 5000, 50000)
    max_bytes = _positive(input.get("max_bytes"), 8_388_608, 16_777_216)
    if max_bytes < 1024:
        raise ValueError("max_bytes_too_small")
    page_size = min(_positive(input.get("page_size"), 500, 10000), max_records)
    timeout = _positive(input.get("timeout_seconds"), 30, 120)
    deadline = time.monotonic() + timeout
    assets = {}
    asset_sizes = {}
    asset_item_bytes = 0
    total_bytes = 0
    pages = 0
    partial = False
    failure = None
    try:
        for _ in range(max_pages):
            params = {
                "sysparm_limit": page_size,
                "sysparm_offset": offset,
                "sysparm_fields": ",".join(fields),
                "sysparm_display_value": str(display_value).lower(),
                "sysparm_exclude_reference_link": "true",
                "sysparm_query": encoded_query,
            }
            remaining_bytes = max_bytes - total_bytes
            if remaining_bytes <= 0:
                partial = True
                break
            records, size = _get_page(context, base, params, deadline, remaining_bytes)
            total_bytes += size
            pages += 1
            if total_bytes > max_bytes:
                partial = True
                break
            processed = 0
            for record in records:
                asset = _asset(account, record) if isinstance(record, dict) else None
                if asset is None:
                    partial = True
                    failure = "invalid_source_record"
                    break
                asset_key = asset["external_key"]
                encoded_asset_size = _encoded_size(asset)
                candidate_count = len(assets) + (asset_key not in assets)
                candidate_item_bytes = (
                    asset_item_bytes - asset_sizes.get(asset_key, 0) + encoded_asset_size
                )
                if candidate_count > max_records:
                    partial = True
                    break
                if not _candidate_fits(
                    candidate_count,
                    candidate_item_bytes,
                    pages,
                    offset + processed + 1,
                    max_bytes,
                ):
                    partial = True
                    failure = "max_bytes_exceeded"
                    break
                assets[asset_key] = asset
                asset_sizes[asset_key] = encoded_asset_size
                asset_item_bytes = candidate_item_bytes
                processed += 1
            offset += processed
            if partial or len(records) < page_size:
                break
            if len(assets) >= max_records:
                partial = True
                break
        else:
            partial = True
    except ValueError:
        partial = True
        failure = "source_read_failed"
    ordered = [assets[key] for key in sorted(assets)]
    if mode == "preview":
        result = _snapshot_result(ordered, pages, failure, partial, offset)
        if _encoded_size(result) > max_bytes:
            raise ValueError("max_bytes_too_small")
        return result
    if partial:
        result = _sync_result(input, ordered, pages, failure, offset)
        if _encoded_size(result) > max_bytes:
            raise ValueError("max_bytes_too_small")
        return result
    summary = {
        "assets": len(ordered),
        "relationships": 0,
        "pages": pages,
        "failures": [],
    }
    result = _sync(context, input, ordered, summary, deadline)
    if _encoded_size(result) > max_bytes:
        raise ValueError("max_bytes_too_small")
    return result
