"""Bounded tencentcloud inventory Recipe with deterministic preview/sync."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from urllib import error, parse, request

PROVIDER = "tencentcloud"
OPERATIONS = json.loads(
    r"""[["vpc","vpc","vpc.tencentcloudapi.com","DescribeVpcs","2017-03-12","VpcSet",["VpcId"],["VpcName"],[""],[""],[]],["subnet","vpc","vpc.tencentcloudapi.com","DescribeSubnets","2017-03-12","SubnetSet",["SubnetId"],["SubnetName"],["Zone"],[""],[["VpcId","vpc","member_of"]]],["eni","vpc","vpc.tencentcloudapi.com","DescribeNetworkInterfaces","2017-03-12","NetworkInterfaceSet",["NetworkInterfaceId"],["NetworkInterfaceName"],["Zone"],["State"],[["VpcId","vpc","located_in"],["SubnetId","subnet","located_in"]]],["eip","vpc","vpc.tencentcloudapi.com","DescribeAddresses","2017-03-12","AddressSet",["AddressId"],["AddressName","AddressIp"],[""],["AddressStatus"],[["InstanceId","cvm_instance","attached_to"]]],["nat_gateway","vpc","vpc.tencentcloudapi.com","DescribeNatGateways","2017-03-12","NatGatewaySet",["NatGatewayId"],["NatGatewayName"],[""],["State"],[["VpcId","vpc","member_of"]]],["route_table","vpc","vpc.tencentcloudapi.com","DescribeRouteTables","2017-03-12","RouteTableSet",["RouteTableId"],["RouteTableName"],[""],[""],[["VpcId","vpc","member_of"]]],["network_acl","vpc","vpc.tencentcloudapi.com","DescribeNetworkAcls","2017-03-12","NetworkAclSet",["NetworkAclId"],["NetworkAclName"],[""],[""],[["VpcId","vpc","member_of"]]],["ccn","vpc","vpc.tencentcloudapi.com","DescribeCcns","2017-03-12","CcnSet",["CcnId"],["CcnName"],[""],["State"],[]],["vpn_gateway","vpc","vpc.tencentcloudapi.com","DescribeVpnGateways","2017-03-12","VpnGatewaySet",["VpnGatewayId"],["VpnGatewayName"],["Zone"],["State"],[["VpcId","vpc","member_of"]]],["clb","clb","clb.tencentcloudapi.com","DescribeLoadBalancers","2018-03-17","LoadBalancerSet",["LoadBalancerId"],["LoadBalancerName"],["Zone"],["Status"],[["VpcId","vpc","member_of"],["SubnetId","subnet","located_in"]]],["clb_target_group","clb","clb.tencentcloudapi.com","DescribeTargetGroups","2018-03-17","TargetGroupSet",["TargetGroupId"],["TargetGroupName"],[""],[""],[["VpcId","vpc","member_of"]]]]"""
)
_RELATIONS = {"located_in", "attached_to", "protected_by", "member_of", "serves", "routes_to"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


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


def _alicloud(operation, region, page, size, context, timeout):
    from alibabacloud_tea_openapi.client import Client
    from alibabacloud_tea_openapi.utils_models import Config, OpenApiRequest, Params
    from alibabacloud_tea_util.models import RuntimeOptions

    access = context.secrets.get("ALICLOUD_ACCESS_KEY_ID")
    secret = context.secrets.get("ALICLOUD_ACCESS_KEY_SECRET")
    if not access or not secret:
        raise ValueError("missing_credential")
    endpoint = operation[2].replace("cn-hangzhou", region)
    client = Client(
        Config(
            access_key_id=access,
            access_key_secret=secret,
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
    query = {"RegionId": region, "PageNumber": str(page), "PageSize": str(size)}
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


def _tencentcloud(operation, region, page, size, context, timeout, max_bytes):
    access = context.secrets.get("TENCENTCLOUD_SECRET_ID")
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
    token = context.secrets.get("TENCENTCLOUD_TOKEN")
    if token:
        headers["X-TC-Token"] = token
    with _NO_REDIRECT_OPENER.open(
        request.Request("https://" + host, data=body.encode(), headers=headers, method="POST"),
        timeout=timeout,
    ) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("provider_response_too_large")
    payload = json.loads(raw)
    response = payload.get("Response", payload)
    if isinstance(response, dict) and response.get("Error"):
        raise ValueError("provider_api_error")
    return response, len(raw)


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
    base = context.config.get("cmdb_base_url") if isinstance(context.config, dict) else None
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


def _bounded_result(
    mode,
    input,
    assets,
    relationships,
    pages,
    failures,
    partial,
    limit_reached,
    max_bytes,
):
    kept_assets = list(assets)
    kept_relationships = list(relationships)
    kept_failures = list(failures[:50])
    bounded_partial = partial
    bounded_limit = limit_reached

    def build():
        summary = {
            "assets": len(kept_assets),
            "relationships": len(kept_relationships),
            "pages": pages,
            "failures": kept_failures,
        }
        checkpoint = (
            {"failed": kept_failures, "limit_reached": bounded_limit} if bounded_partial else None
        )
        if mode == "preview":
            return {
                "schema_version": "dlr-asset-snapshot/v1",
                "assets": kept_assets,
                "relationships": kept_relationships,
                "summary": summary,
                "partial": bounded_partial,
                "checkpoint": checkpoint,
            }
        return {
            "mode": "sync",
            "scan_id": input["scan_id"],
            "source_scope": input["source_scope"],
            "partial": True,
            "summary": summary,
            "failed": kept_failures or ["bounded"],
            "checkpoint": checkpoint,
        }

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    result = build()
    if size(result) <= max_bytes:
        return result
    if not bounded_partial:
        bounded_partial = True
        bounded_limit = True
        kept_failures = ["bounded"]
        result = build()
    if size(result) > max_bytes and any(isinstance(item, dict) for item in kept_failures):
        kept_failures = list(
            dict.fromkeys(
                str(item.get("error", "bounded")) if isinstance(item, dict) else str(item)
                for item in kept_failures
            )
        )
        result = build()
    while size(result) > max_bytes and kept_relationships:
        kept_relationships.pop()
        bounded_partial = True
        bounded_limit = True
        result = build()
    while size(result) > max_bytes and kept_assets:
        kept_assets.pop()
        bounded_partial = True
        bounded_limit = True
        result = build()
    if size(result) > max_bytes:
        raise ValueError("max_bytes_too_small")
    return result


def handle(context, input):
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
        or not all(isinstance(region, str) and region and len(region) <= 128 for region in regions)
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
    source_bytes = 0
    asset_sizes = {}
    stop = False
    for region in sorted(set(regions)):
        if stop:
            break
        for operation in OPERATIONS:
            if stop:
                break
            page = 1
            try:
                while True:
                    if pages >= max_pages or time.monotonic() >= deadline:
                        partial = True
                        limit_reached = True
                        stop = True
                        break
                    remaining = max(1, int(deadline - time.monotonic()))
                    if fixture is not None:
                        batches = fixture.get(operation[0], [])
                        payload = batches[page - 1] if page <= len(batches) else {}
                        page_bytes = len(
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                        )
                        if page_bytes > max_bytes - source_bytes:
                            partial = True
                            limit_reached = True
                            stop = True
                            break
                        source_bytes += page_bytes
                    elif PROVIDER == "alicloud":
                        payload = _alicloud(operation, region, page, page_size, context, remaining)
                    else:
                        source_max_bytes = min(4_194_304, max_bytes - source_bytes)
                        if source_max_bytes <= 0:
                            partial = True
                            limit_reached = True
                            stop = True
                            break
                        payload, page_bytes = _tencentcloud(
                            operation,
                            region,
                            page,
                            page_size,
                            context,
                            remaining,
                            source_max_bytes,
                        )
                        source_bytes += page_bytes
                    batch = _path(payload, operation[5])
                    batch = batch if isinstance(batch, list) else []
                    pages += 1
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
                    if stop or len(batch) < page_size:
                        break
                    page += 1
            except Exception as exc:
                failures.append(
                    {"region": region, "resource": operation[0], "error": "source_read_failed"}
                )
                partial = True
                if str(exc) == "provider_response_too_large":
                    limit_reached = True
                    stop = True
    assets = sorted(assets_by_key.values(), key=lambda item: item["external_key"])
    relationships = sorted(
        relationships_by_key.values(),
        key=lambda item: (item["from"], item["type"], item["to"]),
    )
    if mode == "preview":
        return _bounded_result(
            mode,
            input,
            assets,
            relationships,
            pages,
            failures,
            partial,
            limit_reached,
            max_bytes,
        )
    summary = {
        "assets": len(assets),
        "relationships": len(relationships),
        "pages": pages,
        "failures": failures[:50],
    }
    if partial:
        return _bounded_result(
            mode,
            input,
            assets,
            relationships,
            pages,
            failures,
            partial,
            limit_reached,
            max_bytes,
        )
    return _sync(context, input, assets, relationships, summary, deadline)
