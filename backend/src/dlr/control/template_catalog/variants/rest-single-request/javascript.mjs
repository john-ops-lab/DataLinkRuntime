// REST 接口请求：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// HTTP_BASIC_CREDENTIAL：HTTP Basic 认证，值为 username:password。
// HTTP_BEARER_TOKEN：HTTP Bearer Token，使用此认证时配置。
// HTTP_API_KEY：HTTP API Key，使用此认证时配置。
const CONFIG = {
  // 填写实际接口地址；不要在地址中填写密码或 Token。
  "url": "https://api.example/resources",
  // 请求方法：GET 读取；POST、PUT、PATCH、DELETE 可能修改远端数据。
  "method": "GET",
  // 普通查询参数；认证参数使用 query_auth 从凭据读取。
  "query": {},
  // 可选认证：例如 {"parameter":"api_key","secret_binding":"HTTP_API_KEY"}；在本适配器的凭据绑定中配置同名键。
  "query_auth": null,
  // 普通请求头；Bearer 认证可增加 "DLR-Auth": "bearer:HTTP_BEARER_TOKEN"，Token 在本适配器凭据绑定中配置。
  "headers": {"Accept":"application/json"},
  // POST、PUT、PATCH 的请求内容；GET 通常留空。
  "body": null,
  // 返回内容类型：json 或 text。
  "response_type": "json",
  // 允许的 HTTP 状态码列表；按目标接口调整。
  "allowed_statuses": [200],
  // 单次请求超时时间，单位秒。
  "timeout_seconds": 30,
  // HTTP 响应大小上限，单位字节。
  "max_response_bytes": 1048576,
  // 最多跟随的同站点跳转次数。
  "max_redirects": 3,
};

/** Bounded single HTTP request Recipe for DLR. */

const METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const SIDE_EFFECT_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const RESTRICTED_HEADERS = new Set([
  "connection", "content-length", "host", "keep-alive", "proxy-connection",
  "te", "trailer", "transfer-encoding", "upgrade",
]);
const CREDENTIAL_NAME_MARKERS = [
  "accesskey", "apikey", "authorization", "authentication", "clientsecret", "cookie",
  "credential", "password", "privatekey", "secret", "signature", "token",
];

function credentialLikeName(name) {
  const compact = name.toLowerCase().replace(/[^a-z0-9]/g, "");
  return CREDENTIAL_NAME_MARKERS.some((marker) => compact.includes(marker))
    || compact.endsWith("auth") || compact.endsWith("sig");
}

function positiveInt(value, fallback, maximum) {
  return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}

function secret(context, key) {
  if (typeof key !== "string" || key.length === 0) throw new Error("invalid_secret_key");
  // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
  const value = context.secrets.get(key);
  if (!value) throw new Error("missing_credential");
  return value;
}

function validateHeader(name, value) {
  const normalized = name.toLowerCase();
  if (!HEADER_NAME.test(name) || RESTRICTED_HEADERS.has(normalized) || normalized.startsWith("proxy-")
      || value.includes("\r") || value.includes("\n")) throw new Error("request_failed");
}

function headersFor(context, raw) {
  if (raw === undefined || raw === null) return { headers: {}, sensitive: new Set() };
  if (typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid_headers");
  const headers = Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, String(value)]));
  const authNames = Object.keys(headers).filter((key) => key.toLowerCase() === "dlr-auth");
  if (authNames.length > 1) throw new Error("invalid_headers");
  const auth = authNames.length === 1 ? headers[authNames[0]] : undefined;
  if (authNames.length === 1) delete headers[authNames[0]];
  if (Object.keys(headers).some(credentialLikeName)) {
    throw new Error("direct_credential_header_forbidden");
  }
  const sensitive = new Set();
  if (auth) {
    const splitAt = auth.indexOf(":");
    const scheme = auth.slice(0, splitAt);
    const value = secret(context, auth.slice(splitAt + 1));
    let injected;
    if (scheme === "bearer") injected = headers.Authorization = `Bearer ${value}`;
    else if (scheme === "basic") injected = headers.Authorization = `Basic ${Buffer.from(value).toString("base64")}`;
    else if (scheme.startsWith("api-key/")) injected = headers[scheme.slice(8)] = value;
    else throw new Error("invalid_auth_scheme");
    sensitive.add(value); sensitive.add(injected);
  }
  for (const [name, value] of Object.entries(headers)) validateHeader(name, value);
  return { headers, sensitive };
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

function requestFailure(method) {
  return {
    ok: false,
    error: "request_failed",
    side_effect_uncertain: SIDE_EFFECT_METHODS.has(method),
    retried: false,
  };
}

function queryAuthFor(context, raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== "object" || Array.isArray(raw)
      || Object.keys(raw).sort().join(",") !== "parameter,secret_binding"
      || typeof raw.parameter !== "string"
      || !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(raw.parameter)) {
    throw new Error("invalid_query_auth");
  }
  return { parameter: raw.parameter, secret: secret(context, raw.secret_binding) };
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
  for (const [name, value] of url.searchParams) {
    if (credentialLikeName(name)
        && !(allowInjected && queryAuth && name === queryAuth.parameter && value === queryAuth.secret)) {
      throw new Error("direct_credential_query_forbidden");
    }
  }
}

async function readBounded(response, maximum) {
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maximum) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    joined.set(chunk, at);
    at += chunk.byteLength;
  }
  return joined;
}

export async function handle(context, input) {
  if (input === undefined || input === null) input = {};
  if (typeof input !== "object" || Array.isArray(input)) throw new Error("输入必须是 JSON 对象");
  input = { ...CONFIG, ...input };
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const method = String(input.method ?? "GET").toUpperCase();
  if (!METHODS.has(method)) throw new Error("unsupported_method");
  let url;
  try { url = new URL(input.url); } catch { throw new Error("invalid_url"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash) {
    throw new Error("invalid_url");
  }
  rejectDirectCredentialQuery(url);
  if (input.query !== undefined) {
    if (!input.query || typeof input.query !== "object" || Array.isArray(input.query)) throw new Error("invalid_query");
    for (const [key, value] of Object.entries(input.query).sort(([a], [b]) => a.localeCompare(b))) {
      if (!/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(key)
          || typeof value !== "string" || value.length > 4096) throw new Error("invalid_query");
      if (credentialLikeName(key)) throw new Error("direct_credential_query_forbidden");
      url.searchParams.set(key, value);
    }
  }
  const queryAuth = queryAuthFor(context, input.query_auth);
  const sensitive = new Set();
  if (queryAuth) {
    sensitive.add(queryAuth.secret);
    sensitive.add(encodeURIComponent(queryAuth.secret));
    sensitive.add(new URLSearchParams([["value", queryAuth.secret]]).toString().slice(6));
  }
  applyQueryAuth(url, queryAuth, false);
  let headers;
  try {
    const headerResult = headersFor(context, input.headers);
    headers = headerResult.headers;
    for (const value of headerResult.sensitive) sensitive.add(value);
  }
  catch (error) {
    if (!(error instanceof Error) || error.message !== "request_failed") throw error;
    return requestFailure(method);
  }
  let body;
  try {
    if (input.body !== undefined && input.body !== null) {
      if (input.content_type === "text/plain") body = String(input.body);
      else {
        body = JSON.stringify(input.body);
        headers["Content-Type"] ??= input.content_type ?? "application/json";
        validateHeader("Content-Type", headers["Content-Type"]);
      }
    }
  } catch { return requestFailure(method); }
  const maximum = positiveInt(input.max_response_bytes, 1_048_576, 8_388_608);
  const timeout = positiveInt(input.timeout_seconds, 30, 120);
  const maxRedirects = positiveInt(input.max_redirects, 3, 10);
  const rawAllowed = input.allowed_statuses ?? Array.from({ length: 100 }, (_, at) => 200 + at);
  if (!Array.isArray(rawAllowed) || rawAllowed.length === 0
      || rawAllowed.some((status) => !Number.isInteger(status) || status < 100 || status > 599)) {
    throw new Error("invalid_allowed_statuses");
  }
  const allowed = new Set(rawAllowed);
  const origin = url.origin;
  let response;
  let target = url;
  try {
    for (let redirects = 0; ; redirects += 1) {
      response = await fetch(target, {
        method,
        headers,
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(timeout * 1000),
      });
      if (![301, 302, 303, 307, 308].includes(response.status)) break;
      if (SIDE_EFFECT_METHODS.has(method)) throw new Error("side_effect_redirect_forbidden");
      if (redirects >= maxRedirects) throw new Error("redirect_limit_exceeded");
      const location = response.headers.get("location");
      if (!location) throw new Error("redirect_without_location");
      target = new URL(location, target);
      if (target.origin !== origin) throw new Error("cross_origin_redirect");
      rejectDirectCredentialQuery(target, queryAuth, true);
      applyQueryAuth(target, queryAuth, true);
    }
  } catch (error) {
    const known = new Set([
      "credential_query_collision", "redirect_limit_exceeded", "redirect_without_location",
      "cross_origin_redirect", "direct_credential_query_forbidden", "side_effect_redirect_forbidden",
    ]);
    const failure = requestFailure(method);
    failure.error = error instanceof Error && known.has(error.message) ? error.message : "request_failed";
    return failure;
  }
  try {
    if (!allowed.has(response.status)) {
      await response.body?.cancel();
      return {
        ok: false,
        error: "unexpected_status",
        status: response.status,
        side_effect_uncertain: SIDE_EFFECT_METHODS.has(method),
        retried: false,
      };
    }
    const bytes = await readBounded(response, maximum);
    if (bytes === null) {
      return { ok: true, status: response.status, partial: true, bytes_read: maximum, response: null };
    }
    const text = new TextDecoder().decode(bytes);
    let value = text;
    if (response.headers.get("content-type")?.includes("application/json") || input.response_type === "json") {
      try { value = JSON.parse(text); } catch { return { ok: false, error: "invalid_json_response", status: response.status }; }
    }
    const cleaned = scrub(value, sensitive);
    if (new TextEncoder().encode(JSON.stringify(cleaned)).byteLength > maximum) {
      return { ok: true, status: response.status, partial: true, bytes_read: bytes.byteLength, response: null };
    }
    return {
      ok: true,
      status: response.status,
      content_type: response.headers.get("content-type"),
      partial: false,
      bytes_read: bytes.byteLength,
      response: cleaned,
      side_effect_warning: SIDE_EFFECT_METHODS.has(method),
    };
  } catch {
    return requestFailure(method);
  }
}
