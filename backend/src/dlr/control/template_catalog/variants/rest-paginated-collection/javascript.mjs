/** Bounded REST collection pagination with loop detection. */

const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const RESTRICTED_HEADERS = new Set([
  "connection", "content-length", "host", "keep-alive", "proxy-connection",
  "te", "trailer", "transfer-encoding", "upgrade",
]);
const CREDENTIAL_NAME_MARKERS = [
  "accesskey", "apikey", "authorization", "authentication", "clientsecret", "cookie",
  "credential", "password", "privatekey", "secret", "signature", "token",
];
const STABLE_ERRORS = new Set([
  "input_must_be_object", "invalid_url", "invalid_strategy", "invalid_headers",
  "invalid_query_auth", "credential_query_collision", "direct_credential_query_forbidden",
  "direct_credential_header_forbidden", "invalid_auth_scheme", "missing_credential",
  "request_timeout", "request_failed", "response_too_large", "retry_limit_exceeded",
  "unexpected_status", "invalid_json_response", "cross_origin_next_url",
  "records_path_not_array", "response_path_missing", "pagination_no_progress",
  "offset_not_advancing", "pagination_loop_detected", "cursor_not_advancing",
]);

function credentialLikeName(name) {
  const compact = name.toLowerCase().replace(/[^a-z0-9]/g, "");
  return CREDENTIAL_NAME_MARKERS.some((marker) => compact.includes(marker))
    || compact.endsWith("auth") || compact.endsWith("sig");
}

function positive(value, fallback, maximum) {
  return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}
function boundedInteger(value, fallback, minimum, maximum, errorCode) {
  if (value === undefined || value === null) return fallback;
  if (!Number.isInteger(value) || value < minimum || value > maximum) throw new Error(errorCode);
  return value;
}
function path(value, dotted) {
  let current = value;
  if (!dotted) return current;
  for (const part of dotted.split(".")) {
    if (current && typeof current === "object" && Object.hasOwn(current, part)) current = current[part];
    else throw new Error("response_path_missing");
  }
  return current;
}
function validateHeader(name, value) {
  const normalized = name.toLowerCase();
  if (!HEADER_NAME.test(name) || RESTRICTED_HEADERS.has(normalized) || normalized.startsWith("proxy-")
      || value.includes("\r") || value.includes("\n")) throw new Error("request_failed");
}
function checkpointValue(strategy, page, offset) {
  if (strategy === "page") return { strategy: "page", start_page: page };
  if (strategy === "offset") return { strategy: "offset", start_offset: offset };
  // Cursor and next-URL continuations are opaque and may carry credentials.
  // A redacted token is diagnostic-only and cannot safely resume the scan.
  return null;
}
function headersFor(context, raw) {
  if (raw === undefined || raw === null) {
    return { headers: {}, credentialHeaders: new Set(), sensitive: new Set() };
  }
  if (typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid_headers");
  const headers = Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, String(value)]));
  const authNames = Object.keys(headers).filter((key) => key.toLowerCase() === "dlr-auth");
  if (authNames.length > 1) throw new Error("invalid_headers");
  const auth = authNames.length === 1 ? headers[authNames[0]] : undefined;
  if (authNames.length === 1) delete headers[authNames[0]];
  if (Object.keys(headers).some(credentialLikeName)) {
    throw new Error("direct_credential_header_forbidden");
  }
  const credentialHeaders = new Set();
  const sensitive = new Set();
  if (auth !== undefined) {
    if (typeof auth !== "string" || !auth.includes(":")) throw new Error("invalid_auth_scheme");
    const splitAt = auth.indexOf(":");
    const scheme = auth.slice(0, splitAt);
    const value = context.secrets.get(auth.slice(splitAt + 1));
    if (!value) throw new Error("missing_credential");
    if (scheme === "bearer") {
      const injected = `Bearer ${value}`;
      headers.Authorization = injected;
      credentialHeaders.add("authorization");
      sensitive.add(value); sensitive.add(injected);
    } else if (scheme.startsWith("api-key/") && scheme.length > 8) {
      const headerName = scheme.slice(8);
      headers[headerName] = value;
      credentialHeaders.add(headerName.toLowerCase());
      sensitive.add(value);
    }
    else throw new Error("invalid_auth_scheme");
  }
  for (const [name, value] of Object.entries(headers)) validateHeader(name, value);
  return { headers, credentialHeaders, sensitive };
}
function scrub(value, sensitive) {
  if (typeof value === "string") {
    for (const secret of [...sensitive].filter(Boolean).sort((a, b) => b.length - a.length)) {
      const size = new TextEncoder().encode(secret).byteLength;
      const marker = size >= 10 ? "<redacted>" : "*".repeat(size);
      value = value.split(secret).join(marker);
    }
    return value;
  }
  if (Array.isArray(value)) return value.map((item) => scrub(item, sensitive));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(
      ([key, item]) => [scrub(key, sensitive), scrub(item, sensitive)],
    ));
  }
  return value;
}
function queryAuthFor(context, raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== "object" || Array.isArray(raw)
      || Object.keys(raw).sort().join(",") !== "parameter,secret_binding"
      || typeof raw.parameter !== "string"
      || !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(raw.parameter)
      || typeof raw.secret_binding !== "string" || raw.secret_binding.length === 0) {
    throw new Error("invalid_query_auth");
  }
  const value = context.secrets.get(raw.secret_binding);
  if (!value) throw new Error("missing_credential");
  return { parameter: raw.parameter, secret: value };
}
function applyQueryAuth(url, queryAuth, allowInjected) {
  if (!queryAuth) return;
  if (url.searchParams.has(queryAuth.parameter)) {
    if (allowInjected && url.searchParams.getAll(queryAuth.parameter).length === 1
        && url.searchParams.get(queryAuth.parameter) === queryAuth.secret) return;
    throw new Error("credential_query_collision");
  }
  url.searchParams.set(queryAuth.parameter, queryAuth.secret);
}
function rejectDirectCredentialQuery(url, queryAuth = null, allowInjected = false) {
  let allowedMatches = 0;
  for (const [name, value] of url.searchParams.entries()) {
    if (!credentialLikeName(name)) continue;
    if (allowInjected && queryAuth && name === queryAuth.parameter && value === queryAuth.secret) {
      allowedMatches += 1;
      continue;
    }
    throw new Error("direct_credential_query_forbidden");
  }
  if (allowedMatches > 1) throw new Error("credential_query_collision");
}
function recordsBytes(records) {
  return new TextEncoder().encode(JSON.stringify(records)).byteLength;
}
async function boundedJson(response, maximum) {
  if (!response.body) throw new Error("empty_response");
  const reader = response.body.getReader();
  const chunks = []; let total = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maximum) { await reader.cancel(); throw new Error("response_too_large"); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total); let at = 0;
  for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.byteLength; }
  try { return [JSON.parse(new TextDecoder().decode(bytes)), total]; }
  catch { throw new Error("invalid_json_response"); }
}
async function getJson(url, headers, deadline, maxBytes, retries) {
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("request_timeout");
    let response;
    try {
      response = await fetch(url, { headers, redirect: "manual", signal: AbortSignal.timeout(remaining) });
    } catch {
      if (attempt === retries) throw new Error("request_failed");
      const delay = Math.min(100 * 2 ** attempt + Math.floor(Math.random() * 50), 1000, deadline - Date.now());
      if (delay <= 0) throw new Error("request_timeout");
      await new Promise((resolve) => setTimeout(resolve, delay));
      continue;
    }
    if (response.status === 429 || response.status >= 500) {
      await response.body?.cancel();
      if (attempt === retries) throw new Error("retry_limit_exceeded");
      const delay = Math.min(100 * 2 ** attempt + Math.floor(Math.random() * 50), 1000, deadline - Date.now());
      if (delay <= 0) throw new Error("request_timeout");
      await new Promise((resolve) => setTimeout(resolve, delay));
      continue;
    }
    if (response.status < 200 || response.status >= 300) {
      await response.body?.cancel(); throw new Error("unexpected_status");
    }
    return boundedJson(response, maxBytes);
  }
  throw new Error("request_failed");
}

async function run(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  let base;
  try { base = new URL(input.url); } catch { throw new Error("invalid_url"); }
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.hash) {
    throw new Error("invalid_url");
  }
  rejectDirectCredentialQuery(base);
  const strategy = input.strategy ?? "page";
  if (!["page", "offset", "cursor", "next-url"].includes(strategy)) throw new Error("invalid_strategy");
  const maxPages = positive(input.max_pages, 20, 500);
  const maxRecords = positive(input.max_records, 10_000, 100_000);
  const maxBytes = positive(input.max_bytes, 4_194_304, 16_777_216);
  const pageSize = positive(input.page_size, 100, 1_000);
  const timeout = positive(input.timeout_seconds, 30, 120);
  const deadline = Date.now() + timeout * 1000;
  const retries = boundedInteger(input.max_retries, 2, 0, 5, "invalid_max_retries");
  let page = boundedInteger(input.start_page, 1, 1, 1_000_000, "invalid_start_page");
  let offset = boundedInteger(input.start_offset, 0, 0, 1_000_000_000, "invalid_start_offset");
  const { headers, credentialHeaders, sensitive } = headersFor(context, input.headers);
  const queryAuth = queryAuthFor(context, input.query_auth);
  if (queryAuth) {
    sensitive.add(queryAuth.secret);
    sensitive.add(encodeURIComponent(queryAuth.secret));
    sensitive.add(new URLSearchParams([["value", queryAuth.secret]]).toString().slice(6));
  }
  const records = []; const seen = new Set(); const seenBatches = new Set();
  let nextUrl = new URL(base); let cursor = null;
  let totalBytes = 0; let checkpoint = null; let partial = false; let pages = 0;
  let completed = false;
  for (let iteration = 0; iteration < maxPages; iteration += 1) {
    const remainingBytes = maxBytes - totalBytes;
    if (remainingBytes <= 0 || Date.now() >= deadline) {
      partial = true; checkpoint = checkpointValue(strategy, page, offset); break;
    }
    const url = strategy === "next-url" ? new URL(nextUrl) : new URL(base);
    if (strategy === "page") {
      url.searchParams.set(input.page_parameter ?? "page", String(page));
      url.searchParams.set(input.size_parameter ?? "page_size", String(pageSize));
    } else if (strategy === "offset") {
      url.searchParams.set(input.offset_parameter ?? "offset", String(offset));
      url.searchParams.set(input.limit_parameter ?? "limit", String(pageSize));
    } else if (strategy === "cursor") {
      url.searchParams.set(input.limit_parameter ?? "limit", String(pageSize));
      if (cursor !== null) url.searchParams.set(input.cursor_parameter ?? "cursor", cursor);
    }
    const crossOrigin = url.origin !== base.origin;
    if (crossOrigin && input.allow_cross_origin_next !== true) throw new Error("cross_origin_next_url");
    if (crossOrigin && queryAuth) url.searchParams.delete(queryAuth.parameter);
    rejectDirectCredentialQuery(url, queryAuth, !crossOrigin && strategy === "next-url");
    if (!crossOrigin) applyQueryAuth(url, queryAuth, strategy === "next-url");
    const requestHeaders = crossOrigin
      ? Object.fromEntries(Object.entries(headers).filter(([key]) =>
        ["accept", "content-type", "user-agent"].includes(key.toLowerCase())
        && !credentialHeaders.has(key.toLowerCase())))
      : headers;
    const [payload, size] = await getJson(url, requestHeaders, deadline, remainingBytes, retries);
    pages += 1;
    totalBytes += size;
    if (totalBytes > maxBytes) {
      partial = true; checkpoint = checkpointValue(strategy, page, offset); break;
    }
    const batch = path(payload, input.records_path ?? "items");
    if (!Array.isArray(batch)) throw new Error("records_path_not_array");
    if (batch.length === 0) { completed = true; break; }
    const safeBatch = batch.map((item) => scrub(item, sensitive));
    const fingerprint = JSON.stringify(safeBatch);
    if (seenBatches.has(fingerprint)) throw new Error("pagination_no_progress");
    seenBatches.add(fingerprint);
    const remaining = maxRecords - records.length;
    if (safeBatch.length > remaining) {
      partial = true; checkpoint = checkpointValue(strategy, page, offset); break;
    }
    if (recordsBytes([...records, ...safeBatch]) > maxBytes) {
      partial = true; checkpoint = checkpointValue(strategy, page, offset); break;
    }
    records.push(...safeBatch);
    if (strategy === "page") page += 1;
    else if (strategy === "offset") {
      const previous = offset; offset += batch.length;
      if (offset <= previous) throw new Error("offset_not_advancing");
    } else {
      const rawNext = path(payload, input.next_path ?? "next");
      if (rawNext === null || rawNext === undefined || rawNext === "") {
        completed = true; break;
      }
      const candidate = strategy === "next-url" ? new URL(String(rawNext), url).toString() : String(rawNext);
      if (seen.has(candidate)) throw new Error("pagination_loop_detected");
      seen.add(candidate);
      if (strategy === "next-url") {
        const parsed = new URL(candidate);
        if (parsed.username || parsed.password || parsed.hash
            || (parsed.origin !== base.origin && input.allow_cross_origin_next !== true)) {
          throw new Error("cross_origin_next_url");
        }
        nextUrl = parsed;
      } else {
        if (candidate === cursor) throw new Error("cursor_not_advancing");
        cursor = candidate;
      }
    }
    if (records.length >= maxRecords) {
      partial = true; checkpoint = checkpointValue(strategy, page, offset); break;
    }
  }
  if (!completed && !partial && pages === maxPages) {
    partial = true; checkpoint ??= checkpointValue(strategy, page, offset);
  }
  return { records, count: records.length, pages, bytes: totalBytes, partial, checkpoint };
}

export async function handle(context, input) {
  try {
    return await run(context, input);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (STABLE_ERRORS.has(code)) throw new Error(code);
    throw new Error("request_failed");
  }
}
