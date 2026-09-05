/** Bounded ServiceNow cmdb_ci snapshot with optional idempotent CMDB sync. */
import crypto from "node:crypto";

function positive(value, fallback, maximum) { return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback; }
function digest(value) { return crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex"); }
function headers(context) {
  const bearer = context.secrets.get("SERVICENOW_BEARER_TOKEN");
  if (bearer) return { Authorization: `Bearer ${bearer}` };
  const username = context.secrets.get("SERVICENOW_USERNAME");
  const password = context.secrets.get("SERVICENOW_PASSWORD");
  if (!username || !password) throw new Error("missing_credential");
  return { Authorization: `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}` };
}
async function boundedBytes(response, maximum, tooLargeCode) {
  const body = response.body;
  if (!body) throw new Error("servicenow_request_failed");
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maximum) {
    await body.cancel().catch(() => {});
    throw new Error(tooLargeCode);
  }
  const reader = body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const take = Math.min(value.byteLength, Math.max(0, maximum + 1 - size));
      if (take > 0) {
        chunks.push(value.slice(0, take));
        size += take;
      }
      if (take < value.byteLength || size > maximum) {
        await reader.cancel().catch(() => {});
        throw new Error(tooLargeCode);
      }
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}
async function getPage(context, url, maximum, deadline) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("servicenow_timeout");
    const response = await fetch(url, { headers: { ...headers(context), Accept: "application/json" }, redirect: "manual", signal: AbortSignal.timeout(remaining) });
    if (response.status === 429 || response.status >= 500) {
      await response.body?.cancel();
      if (attempt === 2) throw new Error("servicenow_retry_limit");
      const delay = Math.min(100 * 2 ** attempt, deadline - Date.now());
      if (delay <= 0) throw new Error("servicenow_timeout");
      await new Promise((resolve) => setTimeout(resolve, delay)); continue;
    }
    if (!response.ok) { await response.body?.cancel(); throw new Error("servicenow_request_failed"); }
    const bytes = await boundedBytes(response, maximum, "servicenow_response_too_large");
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    if (!Array.isArray(payload.result)) throw new Error("servicenow_invalid_result");
    return [payload.result, bytes.length];
  }
  throw new Error("servicenow_request_failed");
}
function display(record, key) { const value = record[key]; return value && typeof value === "object" && !Array.isArray(value) ? value.display_value : value; }
function keyComponent(value) { return value.replaceAll("%", "%25").replaceAll(":", "%3A"); }
function asset(account, record) {
  const id = record.sys_id;
  if (typeof id !== "string" || !id) return null;
  const attributes = {};
  for (const key of Object.keys(record).sort()) {
    if (key === "sys_id") continue;
    const value = display(record, key);
    if (value === null || ["string", "number", "boolean"].includes(typeof value)) attributes[key] = value;
  }
  return {
    external_key: `servicenow:${keyComponent(account)}:global:cmdb_ci:${keyComponent(id)}`,
    class: String(display(record, "sys_class_name") ?? "cmdb_ci"),
    provider_type: "cmdb_ci",
    name: String(display(record, "name") ?? display(record, "display_name") ?? id),
    account, region: "global", zone: null,
    status: display(record, "install_status") == null ? null : String(display(record, "install_status")),
    tags: {}, attributes,
  };
}
function snapshotResult(assets, pages, failure, partial, offset, assetCount = assets.length) {
  const summary = {
    assets: assetCount, relationships: 0, pages,
    failures: failure ? [failure] : [],
  };
  return {
    schema_version: "dlr-asset-snapshot/v1", assets, relationships: [],
    summary, partial, checkpoint: partial ? { offset } : null,
  };
}
function partialSyncResult(input, assets, pages, failure, offset) {
  const summary = {
    assets: assets.length, relationships: 0, pages,
    failures: failure ? [failure] : [],
  };
  return {
    mode: "sync", scan_id: input.scan_id, source_scope: input.source_scope,
    partial: true, summary, failed: failure ? [failure] : ["bounded"],
    checkpoint: { offset },
  };
}
function encodedSize(value) { return Buffer.byteLength(JSON.stringify(value), "utf8"); }
function candidateFits(assetCount, assetItemBytes, pages, offset, maxBytes) {
  const assetArrayBytes = assetCount === 0 ? 2 : assetItemBytes + assetCount + 1;
  const shell = snapshotResult([], pages, "invalid_source_record", true, offset, assetCount);
  return encodedSize(shell) - 2 + assetArrayBytes <= maxBytes;
}
async function post(base, path, body, token, idem, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) throw new Error("cmdb_target_error");
  const response = await fetch(new URL(path, base.endsWith("/") ? base : base + "/"), {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Idempotency-Key": idem },
    body: JSON.stringify(body), redirect: "manual", signal: AbortSignal.timeout(remaining),
  });
  if (!response.ok) { await response.body?.cancel(); throw new Error("cmdb_target_error"); }
  await response.body?.cancel();
}
async function sync(context, input, assets, summary, deadline) {
  const scan = input.scan_id, scope = input.source_scope;
  const valid = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/;
  if (typeof scan !== "string" || !valid.test(scan)) throw new Error("invalid_scan_id");
  if (typeof scope !== "string" || !valid.test(scope)) throw new Error("invalid_source_scope");
  const base = context.config?.cmdb_base_url, token = context.secrets.get("CMDB_TOKEN");
  let target;
  try { target = new URL(base); } catch { throw new Error("cmdb_target_not_configured"); }
  if (!token || target.username || target.password || target.search || target.hash
      || !(target.protocol === "https:" || (target.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)))) {
    throw new Error("cmdb_target_not_configured");
  }
  const common = { schema_version: "dlr-cmdb-upsert/v1", source_scope: scope, scan_id: scan };
  let acknowledgedAssets = 0;
  try {
    const beginIdem = digest(["begin", scope, scan]);
    await post(base, "/api/v1/import-scans:begin", { ...common, operation: "begin_scan", idempotency_key: beginIdem, provider: "servicenow", catalog_version: "1.0.0" }, token, beginIdem, deadline);
    const size = positive(input.batch_size, 200, 1000);
    for (let at = 0; at < assets.length; at += size) {
      const batch = assets.slice(at, at + size), batchIndex = at / size;
      const batchId = `assets:servicenow:${scope}:${String(batchIndex).padStart(6, "0")}`;
      const idem = digest(["assets", scope, scan, batchId]);
      await post(base, `/api/v1/import-scans/${encodeURIComponent(scan)}/assets:upsert`, { ...common, operation: "upsert_assets", idempotency_key: idem, batch_id: batchId, batch_index: batchIndex, assets: batch }, token, idem, deadline);
      acknowledgedAssets += batch.length;
    }
    const finishIdem = digest(["finish", scope, scan]);
    await post(base, `/api/v1/import-scans/${encodeURIComponent(scan)}:finish`, { ...common, operation: "finish_scan", idempotency_key: finishIdem, complete: true, summary }, token, finishIdem, deadline);
  } catch {
    const failedSummary = { assets: acknowledgedAssets, relationships: 0, pages: summary.pages, failures: ["target_batch"] };
    return { mode: "sync", scan_id: scan, source_scope: scope, partial: true, summary: failedSummary, failed: ["target_batch"], checkpoint: { scan_id: scan } };
  }
  return { mode: "sync", scan_id: scan, source_scope: scope, partial: false, summary, failed: [], checkpoint: null };
}

export async function handle(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const mode = input.mode ?? "preview";
  if (!["preview", "sync"].includes(mode)) throw new Error("invalid_mode");
  if (mode === "sync" && (typeof input.scan_id !== "string" || typeof input.source_scope !== "string")) throw new Error("stable_scan_identity_required");
  const rawOffset = input.offset ?? 0;
  if (!Number.isInteger(rawOffset) || rawOffset < 0 || rawOffset > 2147483647) throw new Error("invalid_offset");
  let offset = rawOffset;
  if (mode === "sync" && offset !== 0) throw new Error("sync_offset_must_be_zero");
  const displayValue = input.display_value ?? false;
  if (typeof displayValue !== "boolean") throw new Error("invalid_display_value");
  const encodedQuery = input.encoded_query ?? "";
  if (typeof encodedQuery !== "string" || [...encodedQuery].length > 4096) throw new Error("invalid_encoded_query");
  let base;
  try { base = new URL(input.instance_url); } catch { throw new Error("https_instance_url_and_instance_id_required"); }
  if (base.protocol !== "https:" || base.username || base.password || base.search || base.hash
      || typeof input.instance_id !== "string" || !input.instance_id || input.instance_id.length > 128) {
    throw new Error("https_instance_url_and_instance_id_required");
  }
  if ((input.table ?? "cmdb_ci") !== "cmdb_ci") throw new Error("only_cmdb_ci_supported");
  const fields = input.fields ?? ["sys_id", "name", "sys_class_name", "install_status"];
  if (!Array.isArray(fields) || fields.length > 64 || !fields.includes("sys_id") || fields.some((field) => typeof field !== "string" || !/^[A-Za-z][A-Za-z0-9_.]{0,127}$/.test(field))) throw new Error("invalid_fields");
  const maxPages = positive(input.max_pages, 20, 200), maxRecords = positive(input.max_records, 5000, 50000), maxBytes = positive(input.max_bytes, 8388608, 16777216), pageSize = Math.min(positive(input.page_size, 500, 10000), maxRecords), timeout = positive(input.timeout_seconds, 30, 120);
  if (maxBytes < 1024) throw new Error("max_bytes_too_small");
  const deadline = Date.now() + timeout * 1000;
  const assets = new Map(), assetSizes = new Map();
  let assetItemBytes = 0, totalBytes = 0, pages = 0, partial = false, failure = null;
  let sourceComplete = false;
  try {
    for (let pageAt = 0; pageAt < maxPages; pageAt += 1) {
      const url = new URL("/api/now/table/cmdb_ci", base);
      url.searchParams.set("sysparm_limit", String(pageSize)); url.searchParams.set("sysparm_offset", String(offset));
      url.searchParams.set("sysparm_fields", fields.join(",")); url.searchParams.set("sysparm_display_value", String(displayValue));
      url.searchParams.set("sysparm_exclude_reference_link", "true"); url.searchParams.set("sysparm_query", encodedQuery);
      const remainingBytes = maxBytes - totalBytes;
      if (remainingBytes <= 0) { partial = true; break; }
      const [records, size] = await getPage(context, url, remainingBytes, deadline);
      totalBytes += size;
      pages += 1;
      if (totalBytes > maxBytes) { partial = true; break; }
      let processed = 0;
      for (const record of records) {
        const mapped = record && typeof record === "object" ? asset(input.instance_id, record) : null;
        if (!mapped) { partial = true; failure = "invalid_source_record"; break; }
        const assetBytes = encodedSize(mapped), existingBytes = assetSizes.get(mapped.external_key) ?? 0;
        const candidateCount = assets.size + (assets.has(mapped.external_key) ? 0 : 1);
        const candidateItemBytes = assetItemBytes - existingBytes + assetBytes;
        if (candidateCount > maxRecords) { partial = true; break; }
        if (!candidateFits(
          candidateCount, candidateItemBytes, pages, offset + processed + 1, maxBytes,
        )) {
          partial = true; failure = "max_bytes_exceeded"; break;
        }
        assets.set(mapped.external_key, mapped); assetSizes.set(mapped.external_key, assetBytes);
        assetItemBytes = candidateItemBytes;
        processed += 1;
      }
      offset += processed;
      if (partial || records.length < pageSize) {
        sourceComplete = !partial;
        break;
      }
      if (assets.size >= maxRecords) { partial = true; break; }
    }
    if (pages === maxPages && !sourceComplete) partial = true;
  } catch { partial = true; failure = "source_read_failed"; }
  const ordered = [...assets.values()].sort((a, b) => a.external_key.localeCompare(b.external_key));
  if (mode === "preview") {
    const result = snapshotResult(ordered, pages, failure, partial, offset);
    if (encodedSize(result) > maxBytes) throw new Error("max_bytes_too_small");
    return result;
  }
  if (partial) {
    const result = partialSyncResult(input, ordered, pages, failure, offset);
    if (encodedSize(result) > maxBytes) throw new Error("max_bytes_too_small");
    return result;
  }
  const summary = { assets: ordered.length, relationships: 0, pages, failures: [] };
  const result = await sync(context, input, ordered, summary, deadline);
  if (encodedSize(result) > maxBytes) throw new Error("max_bytes_too_small");
  return result;
}
