/** Bounded alicloud inventory Recipe with deterministic preview/sync. */
import crypto from "node:crypto";
import OpenApi, { Config, OpenApiRequest, Params } from "@alicloud/openapi-client";
import { RuntimeOptions } from "@alicloud/tea-util";
const PROVIDER = "alicloud";
const REGION = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const OPERATIONS = [["ecs_instance","ecs","ecs.cn-hangzhou.aliyuncs.com","DescribeInstances","2014-05-26","Instances.Instance",["InstanceId"],["InstanceName"],["ZoneId"],["Status"],[["VpcAttributes.VpcId","vpc","located_in"],["VpcAttributes.VSwitchId","vswitch","located_in"],["SecurityGroupIds.SecurityGroupId","security_group","protected_by"]],["next-token","MaxResults",10,100,"NextToken","NextToken","",""]],["ecs_disk","ecs","ecs.cn-hangzhou.aliyuncs.com","DescribeDisks","2014-05-26","Disks.Disk",["DiskId"],["DiskName"],["ZoneId"],["Status"],[["InstanceId","ecs_instance","attached_to"]],["next-token","MaxResults",10,100,"NextToken","NextToken","",""]],["ecs_eni","ecs","ecs.cn-hangzhou.aliyuncs.com","DescribeNetworkInterfaces","2014-05-26","NetworkInterfaceSets.NetworkInterfaceSet",["NetworkInterfaceId"],["NetworkInterfaceName"],["ZoneId"],["Status"],[["InstanceId","ecs_instance","attached_to"],["VpcId","vpc","located_in"],["VSwitchId","vswitch","located_in"]],["next-token","MaxResults",10,100,"NextToken","NextToken","",""]],["ecs_image","ecs","ecs.cn-hangzhou.aliyuncs.com","DescribeImages","2014-05-26","Images.Image",["ImageId"],["ImageName"],[""],["Status"],[],["numbered","PageSize",1,100,"PageNumber","","",""]],["ess_scaling_group","ess","ess.aliyuncs.com","DescribeScalingGroups","2014-08-28","ScalingGroups.ScalingGroup",["ScalingGroupId"],["ScalingGroupName"],[""],["LifecycleState"],[["VpcId","vpc","located_in"]],["numbered","PageSize",1,50,"PageNumber","","",""]]];

function positive(v,d,m){return Number.isInteger(v)&&v>0?Math.min(v,m):d;}
function aliEndpoint(template,region){const marker=".cn-hangzhou.aliyuncs.com";return template.endsWith(marker)?`${template.slice(0,-marker.length)}.${region}.aliyuncs.com`:template;}
function aliPageRequest(op,region,page,token,requestedSize){const pagination=op[11],kind=pagination[0];const query={RegionId:region};let effectiveSize=0;if(kind!=="none"){effectiveSize=Math.max(pagination[2],Math.min(requestedSize,pagination[3]));query[pagination[1]]=String(effectiveSize);if(["numbered","current-page"].includes(kind))query[pagination[4]]=String(page);else if(token!==null)query[pagination[4]]=token;if(pagination[6])query[pagination[6]]=pagination[7];}return[query,effectiveSize];}
function aliContinuation(op,payload,batchSize,effectiveSize,seenTokens){const pagination=op[11],kind=pagination[0];if(kind==="none")return[false,null];if(["numbered","current-page"].includes(kind))return[batchSize>=effectiveSize,null];const nextToken=path(payload,pagination[5]);if(nextToken===null||nextToken===undefined||nextToken==="")return[false,null];if(typeof nextToken!=="string"||nextToken.length>4096||seenTokens.has(nextToken))throw new Error("source_pagination_no_progress");seenTokens.add(nextToken);return[true,nextToken];}
function path(v,p){let c=v;if(!p)return c;for(const k of p.split(".")){if(c&&typeof c==="object"&&Object.hasOwn(c,k))c=c[k];else return null;}return c;}
function first(r,fs){for(const f of fs){const v=path(r,f);if(v!==null&&v!==undefined&&v!==""&&!(Array.isArray(v)&&v.length===0))return v;}return null;}
function values(v){if(v===null||v===undefined||v==="")return[];if(Array.isArray(v))return v;if(typeof v==="object"){for(const n of Object.values(v))if(Array.isArray(n))return n;return[];}return[v];}
function external(account,region,type,id){return [PROVIDER,account,region||"global",type,String(id)].map((v,i)=>i===0?String(v):encodeURIComponent(String(v))).join(":");}
function normalize(op,record,account,region){const id=first(record,op[6]);if(typeof id!=="string"||id.trim()==="")return[null,[]];const key=external(account,region,op[0],id);const asset={external_key:key,class:op[0],provider_type:op[3],name:String(first(record,op[7])??id),account,region,zone:first(record,op[8])??null,status:first(record,op[9])??null,tags:{},attributes:{source_action:op[3]}};const relationships=[];for(const [field,targetType,type] of op[10])for(const target of values(path(record,field)))if(typeof target==="string"&&target.trim()!=="")relationships.push({from:key,type,to:external(account,region,targetType,target)});return[asset,relationships];}
async function ali(op,region,query,context,timeout){const accessKeyId=context.secrets.get("ALICLOUD_ACCESS_KEY_ID"),accessKeySecret=context.secrets.get("ALICLOUD_ACCESS_KEY_SECRET");if(!accessKeyId||!accessKeySecret)throw new Error("missing_credential");const client=new OpenApi(new Config({accessKeyId,accessKeySecret,securityToken:context.secrets.get("ALICLOUD_SECURITY_TOKEN")??undefined,endpoint:aliEndpoint(op[2],region),regionId:region,protocol:"HTTPS",readTimeout:timeout*1000,connectTimeout:Math.min(timeout,20)*1000}));const params=new Params({action:op[3],version:op[4],protocol:"HTTPS",pathname:"/",method:"POST",authType:"AK",bodyType:"json",reqBodyType:"json",style:"RPC"});const result=await client.callApi(params,new OpenApiRequest({query}),new RuntimeOptions({readTimeout:timeout*1000,connectTimeout:Math.min(timeout,20)*1000,autoretry:false}));return result.body??result;}
function hmac(key,value){return crypto.createHmac("sha256",key).update(value).digest();}
async function tc3(op,region,page,size,context,timeout){const access=context.secrets.get("TENCENTCLOUD_SECRET_ID"),secret=context.secrets.get("TENCENTCLOUD_SECRET_KEY");if(!access||!secret)throw new Error("missing_credential");const [service,host,action,version]=op.slice(1,5);const body=JSON.stringify({Offset:(page-1)*size,Limit:size});const timestamp=Math.floor(Date.now()/1000),date=new Date(timestamp*1000).toISOString().slice(0,10);const hash=crypto.createHash("sha256").update(body).digest("hex");const canonical=["POST","/","","content-type:application/json; charset=utf-8\nhost:"+host.toLowerCase()+"\nx-tc-action:"+action.toLowerCase()+"\n","content-type;host;x-tc-action",hash].join("\n");const scope=`${date}/${service}/tc3_request`;const toSign=["TC3-HMAC-SHA256",String(timestamp),scope,crypto.createHash("sha256").update(canonical).digest("hex")].join("\n");const key=hmac(hmac(hmac(Buffer.from("TC3"+secret),date),service),"tc3_request");const signature=crypto.createHmac("sha256",key).update(toSign).digest("hex");const headers={"Content-Type":"application/json; charset=utf-8","Host":host,"X-TC-Action":action,"X-TC-Version":version,"X-TC-Timestamp":String(timestamp),"X-TC-Region":region,"Authorization":`TC3-HMAC-SHA256 Credential=${access}/${scope}, SignedHeaders=content-type;host;x-tc-action, Signature=${signature}`};const token=context.secrets.get("TENCENTCLOUD_TOKEN");if(token)headers["X-TC-Token"]=token;const response=await fetch("https://"+host,{method:"POST",headers,body,redirect:"manual",signal:AbortSignal.timeout(timeout*1000)});if(!response.ok)throw new Error("provider_api_error");const payload=await response.json();if(payload.Response?.Error)throw new Error("provider_api_error");return payload.Response??payload;}
function digest(value){return crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");}
async function post(base, pathName, body, token, idem, deadline) { const remaining = deadline - Date.now(); if (remaining <= 0) throw new Error("request_timeout"); const response = await fetch(new URL(pathName, base.endsWith("/") ? base : base + "/"), { method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}`, "Idempotency-Key": idem }, body: JSON.stringify(body), redirect: "manual", signal: AbortSignal.timeout(remaining) }); if (!response.ok) { await response.body?.cancel(); throw new Error("cmdb_target_error"); } await response.body?.cancel(); }
async function sync(context, input, assets, relationships, summary, deadline) {
  const { scan_id: scanId, source_scope: scope } = input;
  const identity = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/;
  if (typeof scanId !== "string" || !identity.test(scanId)) throw new Error("invalid_scan_id");
  if (typeof scope !== "string" || !identity.test(scope)) throw new Error("invalid_source_scope");
  const rawBase = context.config?.cmdb_base_url;
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
export async function handle(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const mode = input.mode ?? "preview";
  if (!["preview", "sync"].includes(mode)) throw new Error("invalid_mode");
  if (mode === "sync" && (typeof input.scan_id !== "string" || typeof input.source_scope !== "string")) {
    throw new Error("stable_scan_identity_required");
  }
  if (typeof input.account !== "string" || !input.account || input.account.length > 256
      || !Array.isArray(input.regions) || input.regions.length === 0 || input.regions.length > 32
      || new Set(input.regions).size !== input.regions.length
      || input.regions.some((value) => typeof value !== "string" || value.length > 128 || !REGION.test(value))) {
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
  let pages = 0; let partial = false; let limitReached = false;
  scan: for (const region of [...new Set(input.regions)].sort()) {
    for (const op of OPERATIONS) {
      try {
        let token = null; const seenTokens = new Set();
        for (let pageNumber = 1; ; pageNumber += 1) {
          const remaining = Math.ceil((deadline - Date.now()) / 1000);
          if (remaining <= 0 || pages >= maxPages) {
            partial = true; limitReached = true; break scan;
          }
          const [query, effectivePageSize] = aliPageRequest(op, region, pageNumber, token, pageSize);
          const payload = input.fixture_pages
            ? (input.fixture_pages[op[0]]?.[pageNumber - 1] ?? {})
            : PROVIDER === "alicloud"
              ? await ali(op, region, query, context, remaining)
              : await tc3(op, region, pageNumber, pageSize, context, remaining);
          const batch = path(payload, op[5]);
          const list = Array.isArray(batch) ? batch : [];
          pages += 1;
          const [shouldContinue, nextToken] = aliContinuation(
            op, payload, list.length, effectivePageSize, seenTokens,
          );
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
          if (!shouldContinue) break;
          token = nextToken;
        }
      } catch {
        failures.push({ region, resource: op[0], error: "source_read_failed" });
        partial = true;
      }
    }
  }
  const assets = [...assetMap.values()].sort((left, right) => left.external_key.localeCompare(right.external_key));
  const relationships = [...relationMap.values()]
    .sort((left, right) => [left.from, left.type, left.to].join("\0").localeCompare(
      [right.from, right.type, right.to].join("\0")));
  const summary = { assets: assets.length, relationships: relationships.length, pages, failures: failures.slice(0, 50) };
  const checkpoint = partial ? { failed: failures.slice(0, 50), limit_reached: limitReached } : null;
  if (mode === "preview") {
    return { schema_version: "dlr-asset-snapshot/v1", assets, relationships, summary, partial, checkpoint };
  }
  if (partial || Date.now() >= deadline) {
    return {
      mode: "sync", scan_id: input.scan_id, source_scope: input.source_scope,
      partial: true, summary, failed: failures.length ? failures.slice(0, 50) : ["bounded"], checkpoint,
    };
  }
  return sync(context, input, assets, relationships, summary, deadline);
}
