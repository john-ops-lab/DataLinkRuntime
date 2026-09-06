"""Bounded alicloud inventory Recipe with deterministic preview/sync."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from urllib import error, parse, request

# 阿里云网络资源：可修改的配置集中在这里。
# 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
# 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
# ALICLOUD_ACCESS_KEY_ID：阿里云 AccessKey ID。
# ALICLOUD_ACCESS_KEY_SECRET：阿里云 AccessKey Secret。
# ALICLOUD_SECURITY_TOKEN：阿里云临时凭据 Token，仅使用临时凭据时配置。
# CMDB_TOKEN：目标 CMDB Token，仅同步时配置。
CONFIG = {
    # preview 只采集并返回结果；sync 会写入目标 CMDB，请先配置下方地址和 CMDB_TOKEN 凭据。
    "mode": "preview",
    # 填写云账号标识，用于区分不同账号的资产；不是密码。
    "account": "EXAMPLE_ACCOUNT",
    # 填写需要采集的区域 ID，可配置多个区域。
    "regions": ["cn-hangzhou"],
    # 单次运行最多读取的页数。
    "max_pages": 50,
    # 单次运行最多返回的记录数。
    "max_records": 5000,
    # 单次运行处理或返回的数据大小上限，单位字节。
    "max_bytes": 8388608,
    # 每次请求的条数，不能超过目标接口限制。
    "page_size": 100,
    # 单次请求超时时间，单位秒。
    "timeout_seconds": 30,
    # 每批处理的记录数。
    "batch_size": 200,
    # 同步范围标识：同一账号、区域和资源范围保持不变。
    "source_scope": "alicloud:EXAMPLE_ACCOUNT:cn-hangzhou",
    # 仅 sync 使用：每次新的扫描填写新标识，同一次运行重试保持不变。
    "scan_id": "",
    # 仅 sync 使用：填写目标 CMDB 地址；目标需实现下方代码调用的扫描和批量写入接口。
    "cmdb_base_url": "https://cmdb.example",
}


PROVIDER = "alicloud"
OPERATIONS = json.loads(
    r"""[["vpc","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeVpcs","2016-04-28","Vpcs.Vpc",["VpcId"],["VpcName"],[""],["Status"],[],["numbered","PageSize",1,50,"PageNumber","","",""]],["vswitch","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeVSwitches","2016-04-28","VSwitches.VSwitch",["VSwitchId"],["VSwitchName"],["ZoneId"],["Status"],[["VpcId","vpc","member_of"]],["numbered","PageSize",1,50,"PageNumber","","",""]],["eip","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeEipAddresses","2016-04-28","EipAddresses.EipAddress",["AllocationId"],["Name","IpAddress"],["Zone"],["Status"],[["InstanceId","ecs_instance","attached_to"]],["numbered","PageSize",1,100,"PageNumber","","",""]],["nat_gateway","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeNatGateways","2016-04-28","NatGateways.NatGateway",["NatGatewayId"],["Name"],[""],["Status"],[["VpcId","vpc","member_of"],["VSwitchId","vswitch","located_in"]],["numbered","PageSize",1,50,"PageNumber","","",""]],["network_acl","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeNetworkAcls","2016-04-28","NetworkAcls.NetworkAcl",["NetworkAclId"],["NetworkAclName"],[""],["Status"],[["VpcId","vpc","member_of"]],["numbered","PageSize",1,50,"PageNumber","","",""]],["vpn_gateway","vpc","vpc.cn-hangzhou.aliyuncs.com","DescribeVpnGateways","2016-04-28","VpnGateways.VpnGateway",["VpnGatewayId"],["Name"],[""],["Status"],[["VpcId","vpc","member_of"]],["numbered","PageSize",1,50,"PageNumber","","",""]],["certificate","cas","cas.aliyuncs.com","ListUserCertificateOrder","2020-04-07","CertificateOrderList",["CertificateId","OrderId"],["Name","Domain"],[""],["Status"],[],["current-page","ShowSize",1,50,"CurrentPage","","",""]]]"""
)
_RELATIONS = {"located_in", "attached_to", "protected_by", "member_of", "serves", "routes_to"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_REGION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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


def _alicloud_endpoint(template, region):
    marker = ".cn-hangzhou.aliyuncs.com"
    return (
        f"{template.removesuffix(marker)}.{region}.aliyuncs.com"
        if template.endswith(marker)
        else template
    )


def _path(value, dotted):
    current = value
    if not dotted:
        return current
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _first(record, fields):
    for field in fields:
        value = _path(record, field)
        if value not in (None, "", []):
            return value
    return None


def _values(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for nested in value.values():
            if isinstance(nested, list):
                return nested
        return []
    return [value]


def _external(account, region, kind, identifier):
    return ":".join(
        (
            PROVIDER,
            parse.quote(account, safe="-._"),
            parse.quote(region or "global", safe="-._"),
            kind,
            parse.quote(str(identifier), safe="-._"),
        )
    )


def _normalize(operation, record, account, region):
    identifier = _first(record, operation[6])
    if not isinstance(identifier, str) or not identifier.strip():
        return None, []
    key = _external(account, region, operation[0], identifier)
    zone = _first(record, operation[8])
    status = _first(record, operation[9])
    name = _first(record, operation[7])
    asset = {
        "external_key": key,
        "class": operation[0],
        "provider_type": operation[3],
        "name": str(name or identifier),
        "account": account,
        "region": region,
        "zone": str(zone) if zone is not None else None,
        "status": str(status) if status is not None else None,
        "tags": {},
        "attributes": {"source_action": operation[3]},
    }
    relations = []
    for field, target_type, relation_type in operation[10]:
        if relation_type not in _RELATIONS:
            continue
        for target in _values(_path(record, field)):
            if isinstance(target, str) and target.strip():
                relations.append(
                    {
                        "from": key,
                        "type": relation_type,
                        "to": _external(account, region, target_type, target),
                    }
                )
    return asset, relations


def _alicloud_page_request(operation, region, page, token, requested_size):
    pagination = operation[11]
    kind, size_name = pagination[0], pagination[1]
    query = {"RegionId": region}
    if kind != "none":
        effective_size = max(pagination[2], min(requested_size, pagination[3]))
        query[size_name] = str(effective_size)
        if kind in {"numbered", "current-page"}:
            query[pagination[4]] = str(page)
        elif token is not None:
            query[pagination[4]] = token
        if pagination[6]:
            query[pagination[6]] = pagination[7]
    else:
        effective_size = 0
    return query, effective_size


def _alicloud_continuation(operation, payload, batch_size, effective_size, seen_tokens):
    pagination = operation[11]
    kind = pagination[0]
    if kind == "none":
        return False, None
    if kind in {"numbered", "current-page"}:
        return batch_size >= effective_size, None
    next_token = _path(payload, pagination[5])
    if next_token in (None, ""):
        return False, None
    if not isinstance(next_token, str) or len(next_token) > 4096 or next_token in seen_tokens:
        raise ValueError("source_pagination_no_progress")
    seen_tokens.add(next_token)
    return True, next_token


def _alicloud(operation, region, query, context, timeout):
    from alibabacloud_tea_openapi.client import Client
    from alibabacloud_tea_openapi.utils_models import Config, OpenApiRequest, Params
    from alibabacloud_tea_util.models import RuntimeOptions

    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    access = context.secrets.get("ALICLOUD_ACCESS_KEY_ID")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    secret = context.secrets.get("ALICLOUD_ACCESS_KEY_SECRET")
    if not access or not secret:
        raise ValueError("missing_credential")
    endpoint = _alicloud_endpoint(operation[2], region)
    client = Client(
        Config(
            access_key_id=access,
            access_key_secret=secret,
            # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
            security_token=context.secrets.get("ALICLOUD_SECURITY_TOKEN"),
            endpoint=endpoint,
            region_id=region,
            protocol="HTTPS",
            read_timeout=timeout * 1000,
            connect_timeout=min(timeout, 20) * 1000,
        )
    )
    params = Params(
        action=operation[3],
        version=operation[4],
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        body_type="json",
        req_body_type="json",
        style="RPC",
    )
    result = client.call_api(
        params,
        OpenApiRequest(query=query),
        RuntimeOptions(
            read_timeout=timeout * 1000, connect_timeout=min(timeout, 20) * 1000, autoretry=False
        ),
    )
    return result.get("body", result)


def _tc3_key(secret, date, service):
    date_key = hmac.new(("TC3" + secret).encode(), date.encode(), hashlib.sha256).digest()
    service_key = hmac.new(date_key, service.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"tc3_request", hashlib.sha256).digest()


def _tencentcloud(operation, region, page, size, context, timeout):
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    access = context.secrets.get("TENCENTCLOUD_SECRET_ID")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    secret = context.secrets.get("TENCENTCLOUD_SECRET_KEY")
    if not access or not secret:
        raise ValueError("missing_credential")
    service, host, action, version = operation[1], operation[2], operation[3], operation[4]
    body = json.dumps({"Offset": (page - 1) * size, "Limit": size}, separators=(",", ":"))
    timestamp = int(datetime.now(UTC).timestamp())
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    hashed = hashlib.sha256(body.encode()).hexdigest()
    canonical = "\n".join(
        (
            "POST",
            "/",
            "",
            "content-type:application/json; charset=utf-8\nhost:"
            + host.lower()
            + "\nx-tc-action:"
            + action.lower()
            + "\n",
            "content-type;host;x-tc-action",
            hashed,
        )
    )
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        ("TC3-HMAC-SHA256", str(timestamp), scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    signature = hmac.new(
        _tc3_key(secret, date, service), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={access}/{scope}, "
        f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    token = context.secrets.get("TENCENTCLOUD_TOKEN")
    if token:
        headers["X-TC-Token"] = token
    with _NO_REDIRECT_OPENER.open(
        request.Request("https://" + host, data=body.encode(), headers=headers, method="POST"),
        timeout=timeout,
    ) as response:
        payload = json.loads(response.read(4_194_305))
    response = payload.get("Response", payload)
    if isinstance(response, dict) and response.get("Error"):
        raise ValueError("provider_api_error")
    return response


def _post_json(base, path, body, token, idem, deadline):
    url = parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "Idempotency-Key": idem,
    }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("cmdb_target_error")
    try:
        with _NO_REDIRECT_OPENER.open(
            request.Request(url, data=payload, headers=headers, method="POST"),
            timeout=remaining,
        ) as response:
            if not 200 <= response.status < 300:
                raise ValueError("cmdb_target_error")
            response.read(65_537)
    except (error.HTTPError, error.URLError, TimeoutError):
        raise ValueError("cmdb_target_error") from None


def _sync(context, input, assets, relationships, summary, deadline):
    scan_id, scope = input.get("scan_id"), input.get("source_scope")
    if not isinstance(scan_id, str) or not _ID.fullmatch(scan_id):
        raise ValueError("invalid_scan_id")
    if not isinstance(scope, str) or not _ID.fullmatch(scope):
        raise ValueError("invalid_source_scope")
    base = input.get("cmdb_base_url")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    token = context.secrets.get("CMDB_TOKEN")
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
    common = {"schema_version": "dlr-cmdb-upsert/v1", "source_scope": scope, "scan_id": scan_id}

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    try:
        begin_idem = digest(["begin", scope, scan_id])
        _post_json(
            base,
            "/api/v1/import-scans:begin",
            common
            | {
                "operation": "begin_scan",
                "idempotency_key": begin_idem,
                "provider": PROVIDER,
                "catalog_version": "1.0.0",
            },
            token,
            begin_idem,
            deadline,
        )
        batch_size = _positive(input.get("batch_size"), 200, 1000)
        for phase, values, suffix in (
            ("assets", assets, "assets:upsert"),
            ("relationships", relationships, "relationships:upsert"),
        ):
            for index in range(0, len(values), batch_size):
                batch = values[index : index + batch_size]
                batch_index = index // batch_size
                batch_id = f"{phase}:{PROVIDER}:{scope}:{batch_index:06d}"
                idem = digest([phase, scope, scan_id, batch_id])
                body = common | {
                    "operation": f"upsert_{phase}",
                    "idempotency_key": idem,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    phase: batch,
                }
                _post_json(
                    base,
                    f"/api/v1/import-scans/{parse.quote(scan_id, safe='')}/{suffix}",
                    body,
                    token,
                    idem,
                    deadline,
                )
        finish_idem = digest(["finish", scope, scan_id])
        _post_json(
            base,
            f"/api/v1/import-scans/{parse.quote(scan_id, safe='')}:finish",
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
        return {
            "mode": "sync",
            "scan_id": scan_id,
            "source_scope": scope,
            "partial": True,
            "summary": summary,
            "failed": ["target_batch"],
            "checkpoint": {"scan_id": scan_id},
        }
    return {
        "mode": "sync",
        "scan_id": scan_id,
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
    if mode == "sync":
        if not isinstance(input.get("scan_id"), str) or not _ID.fullmatch(input["scan_id"]):
            raise ValueError("invalid_scan_id")
        if not isinstance(input.get("source_scope"), str) or not _ID.fullmatch(
            input["source_scope"]
        ):
            raise ValueError("invalid_source_scope")
    account = input.get("account")
    regions = input.get("regions")
    if (
        not isinstance(account, str)
        or not account
        or len(account) > 256
        or not isinstance(regions, list)
        or not regions
        or len(regions) > 32
        or len(set(regions)) != len(regions)
        or not all(
            isinstance(region, str) and len(region) <= 128 and _REGION.fullmatch(region)
            for region in regions
        )
    ):
        raise ValueError("account_and_regions_required")
    max_pages = _positive(input.get("max_pages"), 50, 100)
    max_records = _positive(input.get("max_records"), 5000, 50000)
    max_bytes = _positive(input.get("max_bytes"), 8_388_608, 16_777_216)
    if max_bytes < 1024:
        raise ValueError("max_bytes_too_small")
    page_size = _positive(input.get("page_size"), 100, 1000)
    timeout = _positive(input.get("timeout_seconds"), 30, 120)
    deadline = time.monotonic() + timeout
    fixture = input.get("fixture_pages")
    if fixture is not None and not isinstance(fixture, dict):
        raise ValueError("invalid_fixture_pages")
    assets_by_key = {}
    relationships_by_key = {}
    failures = []
    pages = 0
    partial = False
    limit_reached = False
    collected_bytes = 512
    asset_sizes = {}
    stop = False
    for region in sorted(set(regions)):
        if stop:
            break
        for operation in OPERATIONS:
            if stop:
                break
            page = 1
            token = None
            seen_tokens = set()
            try:
                while True:
                    if pages >= max_pages or time.monotonic() >= deadline:
                        partial = True
                        limit_reached = True
                        stop = True
                        break
                    remaining = max(1, int(deadline - time.monotonic()))
                    query, effective_page_size = _alicloud_page_request(
                        operation, region, page, token, page_size
                    )
                    if fixture is not None:
                        batches = fixture.get(operation[0], [])
                        payload = batches[page - 1] if page <= len(batches) else {}
                    elif PROVIDER == "alicloud":
                        payload = _alicloud(operation, region, query, context, remaining)
                    else:
                        payload = _tencentcloud(
                            operation, region, page, page_size, context, remaining
                        )
                    batch = _path(payload, operation[5])
                    batch = batch if isinstance(batch, list) else []
                    pages += 1
                    should_continue, next_token = _alicloud_continuation(
                        operation, payload, len(batch), effective_page_size, seen_tokens
                    )
                    for record in batch:
                        if not isinstance(record, dict):
                            failures.append(
                                {
                                    "region": region,
                                    "resource": operation[0],
                                    "error": "invalid_source_record",
                                }
                            )
                            partial = True
                            stop = True
                            break
                        asset, relations = _normalize(operation, record, account, region)
                        if asset is None:
                            failures.append(
                                {
                                    "region": region,
                                    "resource": operation[0],
                                    "error": "invalid_source_record",
                                }
                            )
                            partial = True
                            stop = True
                            break
                        asset_key = asset["external_key"]
                        if asset_key not in assets_by_key and len(assets_by_key) >= max_records:
                            partial = True
                            limit_reached = True
                            stop = True
                            break
                        encoded_asset_size = (
                            len(
                                json.dumps(
                                    asset, ensure_ascii=False, separators=(",", ":")
                                ).encode()
                            )
                            + 1
                        )
                        relation_updates = []
                        seen_relations = set()
                        for relation in relations:
                            relation_key = (
                                relation["from"],
                                relation["type"],
                                relation["to"],
                            )
                            if relation_key in seen_relations:
                                continue
                            seen_relations.add(relation_key)
                            encoded_relation_size = (
                                len(
                                    json.dumps(
                                        relation,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ).encode()
                                )
                                + 1
                            )
                            relation_updates.append((relation_key, relation, encoded_relation_size))
                        next_size = (
                            collected_bytes
                            - asset_sizes.get(asset_key, 0)
                            + encoded_asset_size
                            + sum(
                                size
                                for key, _relation, size in relation_updates
                                if key not in relationships_by_key
                            )
                        )
                        if next_size > max_bytes:
                            partial = True
                            limit_reached = True
                            stop = True
                            break
                        collected_bytes = next_size
                        asset_sizes[asset_key] = encoded_asset_size
                        assets_by_key[asset["external_key"]] = asset
                        for relation_key, relation, _encoded_relation_size in relation_updates:
                            relationships_by_key[relation_key] = relation
                    if stop or not should_continue:
                        break
                    token = next_token
                    page += 1
            except Exception:
                failures.append(
                    {"region": region, "resource": operation[0], "error": "source_read_failed"}
                )
                partial = True
    assets = sorted(assets_by_key.values(), key=lambda item: item["external_key"])
    relationships = sorted(
        relationships_by_key.values(),
        key=lambda item: (item["from"], item["type"], item["to"]),
    )
    summary = {
        "assets": len(assets),
        "relationships": len(relationships),
        "pages": pages,
        "failures": failures[:50],
    }
    checkpoint = {"failed": failures[:50], "limit_reached": limit_reached} if partial else None
    if mode == "preview":
        return {
            "schema_version": "dlr-asset-snapshot/v1",
            "assets": assets,
            "relationships": relationships,
            "summary": summary,
            "partial": partial,
            "checkpoint": checkpoint,
        }
    if partial:
        return {
            "mode": "sync",
            "scan_id": input["scan_id"],
            "source_scope": input["source_scope"],
            "partial": True,
            "summary": summary,
            "failed": failures[:50],
            "checkpoint": checkpoint,
        }
    return _sync(context, input, assets, relationships, summary, deadline)
