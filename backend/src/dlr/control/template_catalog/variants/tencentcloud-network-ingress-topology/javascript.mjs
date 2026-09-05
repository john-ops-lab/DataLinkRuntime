// 腾讯云网络资源：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// TENCENTCLOUD_SECRET_ID：腾讯云 SecretId。
// TENCENTCLOUD_SECRET_KEY：腾讯云 SecretKey。
// TENCENTCLOUD_TOKEN：腾讯云临时凭据 Token，仅使用临时凭据时配置。
// CMDB_TOKEN：目标 CMDB Token，仅同步时配置。
const CONFIG = {
  // preview 只采集并返回结果；sync 会写入目标 CMDB，请先配置下方地址和 CMDB_TOKEN 凭据。
  "mode": "preview",
  // 填写云账号标识，用于区分不同账号的资产；不是密码。
  "account": "EXAMPLE_ACCOUNT",
  // 填写需要采集的区域 ID，可配置多个区域。
  "regions": ["ap-guangzhou"],
  // 单次运行最多读取的页数。
  "max_pages": 50,
  // 单次运行最多返回的记录数。
  "max_records": 5000,
  // 单次运行处理或返回的数据大小上限，单位字节。
  "max_bytes": 8388608,
  // 每次请求的条数，不能超过目标接口限制。
  "page_size": 100,
  // 单次请求超时时间，单位秒。
  "timeout_seconds": 30,
  // 每批处理的记录数。
  "batch_size": 200,
  // 同步范围标识：同一账号、区域和资源范围保持不变。
  "source_scope": "tencentcloud:EXAMPLE_ACCOUNT:ap-guangzhou",
  // 仅 sync 使用：每次新的扫描填写新标识，同一次运行重试保持不变。
  "scan_id": "",
  // 仅 sync 使用：填写目标 CMDB 地址；目标需实现下方代码调用的扫描和批量写入接口。
  "cmdb_base_url": "https://cmdb.example",
};

/** Bounded tencentcloud inventory Recipe with deterministic preview/sync. */
import crypto from "node:crypto";

const PROVIDER = "tencentcloud";
const OPERATIONS = [["vpc","vpc","vpc.tencentcloudapi.com","DescribeVpcs","2017-03-12","VpcSet",["VpcId"],["VpcName"],[""],[""],[]],["subnet","vpc","vpc.tencentcloudapi.com","DescribeSubnets","2017-03-12","SubnetSet",["SubnetId"],["SubnetName"],["Zone"],[""],[["VpcId","vpc","member_of"]]],["eni","vpc","vpc.tencentcloudapi.com","DescribeNetworkInterfaces","2017-03-12","NetworkInterfaceSet",["NetworkInterfaceId"],["NetworkInterfaceName"],["Zone"],["State"],[["VpcId","vpc","located_in"],["SubnetId","subnet","located_in"]]],["eip","vpc","vpc.tencentcloudapi.com","DescribeAddresses","2017-03-12","AddressSet",["AddressId"],["AddressName","AddressIp"],[""],["AddressStatus"],[["InstanceId","cvm_instance","attached_to"]]],["nat_gateway","vpc","vpc.tencentcloudapi.com","DescribeNatGateways","2017-03-12","NatGatewaySet",["NatGatewayId"],["NatGatewayName"],[""],["State"],[["VpcId","vpc","member_of"]]],["route_table","vpc","vpc.tencentcloudapi.com","DescribeRouteTables","2017-03-12","RouteTableSet",["RouteTableId"],["RouteTableName"],[""],[""],[["VpcId","vpc","member_of"]]],["network_acl","vpc","vpc.tencentcloudapi.com","DescribeNetworkAcls","2017-03-12","NetworkAclSet",["NetworkAclId"],["NetworkAclName"],[""],[""],[["VpcId","vpc","member_of"]]],["ccn","vpc","vpc.tencentcloudapi.com","DescribeCcns","2017-03-12","CcnSet",["CcnId"],["CcnName"],[""],["State"],[]],["vpn_gateway","vpc","vpc.tencentcloudapi.com","DescribeVpnGateways","2017-03-12","VpnGatewaySet",["VpnGatewayId"],["VpnGatewayName"],["Zone"],["State"],[["VpcId","vpc","member_of"]]],["clb","clb","clb.tencentcloudapi.com","DescribeLoadBalancers","2018-03-17","LoadBalancerSet",["LoadBalancerId"],["LoadBalancerName"],["Zone"],["Status"],[["VpcId","vpc","member_of"],["SubnetId","subnet","located_in"]]],["clb_target_group","clb","clb.tencentcloudapi.com","DescribeTargetGroups","2018-03-17","TargetGroupSet",["TargetGroupId"],["TargetGroupName"],[""],[""],[["VpcId","vpc","member_of"]]]];

function positive(v,d,m){return Number.isInteger(v)&&v>0?Math.min(v,m):d;}
function path(v,p){let c=v;if(!p)return c;for(const k of p.split(".")){if(c&&typeof c==="object"&&Object.hasOwn(c,k))c=c[k];else return null;}return c;}
function first(r,fs){for(const f of fs){const v=path(r,f);if(v!==null&&v!==undefined&&v!==""&&!(Array.isArray(v)&&v.length===0))return v;}return null;}
function values(v){if(v===null||v===undefined||v==="")return[];if(Array.isArray(v))return v;if(typeof v==="object"){for(const n of Object.values(v))if(Array.isArray(n))return n;return[];}return[v];}
function external(account,region,type,id){return [PROVIDER,account,region||"global",type,String(id)].map((v,i)=>i===0?String(v):encodeURIComponent(String(v))).join(":");}
function normalize(op,record,account,region){const id=first(record,op[6]);if(typeof id!=="string"||id.trim()==="")return[null,[]];const key=external(account,region,op[0],id);const asset={external_key:key,class:op[0],provider_type:op[3],name:String(first(record,op[7])??id),account,region,zone:first(record,op[8])??null,status:first(record,op[9])??null,tags:{},attributes:{source_action:op[3]}};const relationships=[];for(const [field,targetType,type] of op[10])for(const target of values(path(record,field)))if(typeof target==="string"&&target.trim()!=="")relationships.push({from:key,type,to:external(account,region,targetType,target)});return[asset,relationships];}
function hmac(key,value){return crypto.createHmac("sha256",key).update(value).digest();}
async function boundedJson(response, maximum) {
  const stream = response.body;
  if (!stream) throw new Error("provider_api_error");
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maximum) {
    await stream.cancel().catch(() => {});
    throw new Error("provider_response_too_large");
  }
  const reader = stream.getReader(); const chunks = []; let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const take = Math.min(value.byteLength, Math.max(0, maximum + 1 - size));
      if (take > 0) { chunks.push(value.slice(0, take)); size += take; }
      if (take < value.byteLength || size > maximum) {
        await reader.cancel().catch(() => {});
        throw new Error("provider_response_too_large");
      }
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return { payload: JSON.parse(new TextDecoder().decode(bytes)), bytes: size };
}
// 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
async function tc3(op,region,page,size,context,timeout,maximum){const access=context.secrets.get("TENCENTCLOUD_SECRET_ID"),secret=context.secrets.get("TENCENTCLOUD_SECRET_KEY");if(!access||!secret)throw new Error("missing_credential");const [service,host,action,version]=op.slice(1,5);const body=JSON.stringify({Offset:(page-1)*size,Limit:size});const timestamp=Math.floor(Date.now()/1000),date=new Date(timestamp*1000).toISOString().slice(0,10);const hash=crypto.createHash("sha256").update(body).digest("hex");const canonical=["POST","/","","content-type:application/json; charset=utf-8\nhost:"+host.toLowerCase()+"\nx-tc-action:"+action.toLowerCase()+"\n","content-type;host;x-tc-action",hash].join("\n");const scope=`${date}/${service}/tc3_request`;const toSign=["TC3-HMAC-SHA256",String(timestamp),scope,crypto.createHash("sha256").update(canonical).digest("hex")].join("\n");const key=hmac(hmac(hmac(Buffer.from("TC3"+secret),date),service),"tc3_request");const signature=crypto.createHmac("sha256",key).update(toSign).digest("hex");const headers={"Content-Type":"application/json; charset=utf-8","Host":host,"X-TC-Action":action,"X-TC-Version":version,"X-TC-Timestamp":String(timestamp),"X-TC-Region":region,"Authorization":`TC3-HMAC-SHA256 Credential=${access}/${scope}, SignedHeaders=content-type;host;x-tc-action, Signature=${signature}`};const token=context.secrets.get("TENCENTCLOUD_TOKEN");if(token)headers["X-TC-Token"]=token;const response=await fetch("https://"+host,{method:"POST",headers,body,redirect:"manual",signal:AbortSignal.timeout(timeout*1000)});if(!response.ok){await response.body?.cancel();throw new Error("provider_api_error");}const pageValue=await boundedJson(response,maximum);if(pageValue.payload.Response?.Error)throw new Error("provider_api_error");return {payload:pageValue.payload.Response??pageValue.payload,bytes:pageValue.bytes};}
function digest(value){return crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");}
async function post(base, pathName, body, token, idem, deadline) { const remaining = deadline - Date.now(); if (remaining <= 0) throw new Error("request_timeout"); const response = await fetch(new URL(pathName, base.endsWith("/") ? base : base + "/"), { method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}`, "Idempotency-Key": idem }, body: JSON.stringify(body), redirect: "manual", signal: AbortSignal.timeout(remaining) }); if (!response.ok) { await response.body?.cancel(); throw new Error("cmdb_target_error"); } await response.body?.cancel(); }
async function sync(context, input, assets, relationships, summary, deadline) {
  const { scan_id: scanId, source_scope: scope } = input;
  const identity = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/;
  if (typeof scanId !== "string" || !identity.test(scanId)) throw new Error("invalid_scan_id");
  if (typeof scope !== "string" || !identity.test(scope)) throw new Error("invalid_source_scope");
  const rawBase = input.cmdb_base_url;
  // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
  const token = context.secrets.get("CMDB_TOKEN");
  let target;
  try { target = new URL(rawBase); } catch { throw new Error("cmdb_target_not_configured"); }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname);
  if (!token || target.username || target.password || target.search || target.hash
      || (target.protocol !== "https:" && !(target.protocol === "http:" && loopback))) {
    throw new Error("cmdb_target_not_configured");
  }
  const base = target.toString();
  const common = { schema_version: "dlr-cmdb-upsert/v1", source_scope: scope, scan_id: scanId };
  try {
    const beginIdem = digest(["begin", scope, scanId]);
    await post(base, "/api/v1/import-scans:begin", {
      ...common, operation: "begin_scan", idempotency_key: beginIdem,
      provider: PROVIDER, catalog_version: "1.0.0",
    }, token, beginIdem, deadline);
    const batchSize = positive(input.batch_size, 200, 1000);
    for (const [phase, items, suffix] of [
      ["assets", assets, "assets:upsert"],
      ["relationships", relationships, "relationships:upsert"],
    ]) {
      for (let at = 0; at < items.length; at += batchSize) {
        const batch = items.slice(at, at + batchSize);
        const batchIndex = at / batchSize;
        const batchId = `${phase}:${PROVIDER}:${scope}:${String(batchIndex).padStart(6, "0")}`;
        const idem = digest([phase, scope, scanId, batchId]);
        await post(base, `/api/v1/import-scans/${encodeURIComponent(scanId)}/${suffix}`, {
          ...common, operation: `upsert_${phase}`, idempotency_key: idem,
          batch_id: batchId, batch_index: batchIndex, [phase]: batch,
        }, token, idem, deadline);
      }
    }
    const finishIdem = digest(["finish", scope, scanId]);
    await post(base, `/api/v1/import-scans/${encodeURIComponent(scanId)}:finish`, {
      ...common, operation: "finish_scan", idempotency_key: finishIdem,
      complete: true, summary,
    }, token, finishIdem, deadline);
  } catch {
    return {
      mode: "sync", scan_id: scanId, source_scope: scope, partial: true,
      summary, failed: ["target_batch"], checkpoint: { scan_id: scanId },
    };
  }
  return {
    mode: "sync", scan_id: scanId, source_scope: scope, partial: false,
    summary, failed: [], checkpoint: null,
  };
}
function boundedResult(mode,input,assets,relationships,pages,failures,partial,limitReached,maxBytes) {
  const keptAssets = [...assets]; const keptRelationships = [...relationships];
  let keptFailures = failures.slice(0, 50); let boundedPartial = partial; let boundedLimit = limitReached;
  const build = () => {
    const summary = {
      assets: keptAssets.length, relationships: keptRelationships.length,
      pages, failures: keptFailures,
    };
    const checkpoint = boundedPartial
      ? { failed: keptFailures, limit_reached: boundedLimit }
      : null;
    if (mode === "preview") {
      return {
        schema_version: "dlr-asset-snapshot/v1", assets: keptAssets,
        relationships: keptRelationships, summary, partial: boundedPartial, checkpoint,
      };
    }
    return {
      mode: "sync", scan_id: input.scan_id, source_scope: input.source_scope,
      partial: true, summary, failed: keptFailures.length ? keptFailures : ["bounded"], checkpoint,
    };
  };
  const size = (value) => Buffer.byteLength(JSON.stringify(value), "utf8");
  let result = build();
  if (size(result) <= maxBytes) return result;
  if (!boundedPartial) {
    boundedPartial = true; boundedLimit = true; keptFailures = ["bounded"]; result = build();
  }
  if (size(result) > maxBytes && keptFailures.some((item) => item && typeof item === "object")) {
    keptFailures = [...new Set(keptFailures.map((item) =>
      item && typeof item === "object" ? String(item.error ?? "bounded") : String(item)))];
    result = build();
  }
  while (size(result) > maxBytes && keptRelationships.length) {
    keptRelationships.pop(); boundedPartial = true; boundedLimit = true; result = build();
  }
  while (size(result) > maxBytes && keptAssets.length) {
    keptAssets.pop(); boundedPartial = true; boundedLimit = true; result = build();
  }
  if (size(result) > maxBytes) throw new Error("max_bytes_too_small");
  return result;
}
export async function handle(context, input) {
  if (input === undefined || input === null) input = {};
  if (typeof input !== "object" || Array.isArray(input)) throw new Error("输入必须是 JSON 对象");
  input = { ...CONFIG, ...input };
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const mode = input.mode ?? "preview";
  if (!["preview", "sync"].includes(mode)) throw new Error("invalid_mode");
  if (mode === "sync" && (typeof input.scan_id !== "string" || typeof input.source_scope !== "string")) {
    throw new Error("stable_scan_identity_required");
  }
  if (typeof input.account !== "string" || !input.account || input.account.length > 256
      || !Array.isArray(input.regions) || input.regions.length === 0 || input.regions.length > 32
      || new Set(input.regions).size !== input.regions.length
      || input.regions.some((value) => typeof value !== "string" || !value || value.length > 128)) {
    throw new Error("account_and_regions_required");
  }
  const maxPages = positive(input.max_pages, 50, 100);
  const maxRecords = positive(input.max_records, 5000, 50000);
  const maxBytes = positive(input.max_bytes, 8_388_608, 16_777_216);
  if (maxBytes < 1024) throw new Error("max_bytes_too_small");
  const pageSize = positive(input.page_size, 100, 1000);
  const timeout = positive(input.timeout_seconds, 30, 120);
  const deadline = Date.now() + timeout * 1000;
  if (input.fixture_pages !== undefined
      && (!input.fixture_pages || typeof input.fixture_pages !== "object" || Array.isArray(input.fixture_pages))) {
    throw new Error("invalid_fixture_pages");
  }
  const assetMap = new Map(); const relationMap = new Map(); const failures = [];
  let pages = 0; let sourceBytes = 0; let partial = false; let limitReached = false;
  scan: for (const region of [...new Set(input.regions)].sort()) {
    for (const op of OPERATIONS) {
      try {
        for (let pageNumber = 1; ; pageNumber += 1) {
          const remaining = Math.ceil((deadline - Date.now()) / 1000);
          if (remaining <= 0 || pages >= maxPages) {
            partial = true; limitReached = true; break scan;
          }
          let payload;
          if (input.fixture_pages) {
            payload = input.fixture_pages[op[0]]?.[pageNumber - 1] ?? {};
            const pageBytes = Buffer.byteLength(JSON.stringify(payload), "utf8");
            if (pageBytes > maxBytes - sourceBytes) {
              partial = true; limitReached = true; break scan;
            }
            sourceBytes += pageBytes;
          } else {
            const responseLimit = Math.min(4_194_304, maxBytes - sourceBytes);
            if (responseLimit <= 0) { partial = true; limitReached = true; break scan; }
            const pageValue = await tc3(op, region, pageNumber, pageSize, context, remaining, responseLimit);
            payload = pageValue.payload;
            sourceBytes += pageValue.bytes;
          }
          const batch = path(payload, op[5]);
          const list = Array.isArray(batch) ? batch : [];
          pages += 1;
          for (const record of list) {
            if (!record || typeof record !== "object" || Array.isArray(record)) {
              failures.push({ region, resource: op[0], error: "invalid_source_record" });
              partial = true; break scan;
            }
            const [asset, relations] = normalize(op, record, input.account, region);
            if (!asset) {
              failures.push({ region, resource: op[0], error: "invalid_source_record" });
              partial = true; break scan;
            }
            const candidateAssets = new Map(assetMap); candidateAssets.set(asset.external_key, asset);
            const candidateRelations = new Map(relationMap);
            for (const relation of relations) {
              candidateRelations.set([relation.from, relation.type, relation.to].join("\0"), relation);
            }
            const candidateSize = Buffer.byteLength(JSON.stringify({
              assets: [...candidateAssets.values()], relationships: [...candidateRelations.values()],
            }), "utf8");
            if (candidateAssets.size > maxRecords || candidateSize > maxBytes) {
              partial = true; limitReached = true; break;
            }
            assetMap.clear(); for (const [key, value] of candidateAssets) assetMap.set(key, value);
            relationMap.clear(); for (const [key, value] of candidateRelations) relationMap.set(key, value);
          }
          if (limitReached) break scan;
          if (list.length < pageSize) break;
        }
      } catch (error) {
        failures.push({ region, resource: op[0], error: "source_read_failed" });
        partial = true;
        if (error instanceof Error && error.message === "provider_response_too_large") {
          limitReached = true; break scan;
        }
      }
    }
  }
  const assets = [...assetMap.values()].sort((left, right) => left.external_key.localeCompare(right.external_key));
  const relationships = [...relationMap.values()]
    .sort((left, right) => [left.from, left.type, left.to].join("\0").localeCompare(
      [right.from, right.type, right.to].join("\0")));
  if (mode === "preview") {
    return boundedResult(
      mode, input, assets, relationships, pages, failures, partial, limitReached, maxBytes,
    );
  }
  if (partial || Date.now() >= deadline) {
    return boundedResult(
      mode, input, assets, relationships, pages, failures, true, limitReached, maxBytes,
    );
  }
  const summary = { assets: assets.length, relationships: relationships.length, pages, failures: [] };
  return sync(context, input, assets, relationships, summary, deadline);
}
