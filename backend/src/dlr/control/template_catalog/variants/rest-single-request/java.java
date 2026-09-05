import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** Bounded single HTTP request Recipe for DLR. */
public class Adapter {
    // REST 接口请求：可修改的配置集中在这里。
    // 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    // 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
    // HTTP_BASIC_CREDENTIAL：HTTP Basic 认证，值为 username:password。
    // HTTP_BEARER_TOKEN：HTTP Bearer Token，使用此认证时配置。
    // HTTP_API_KEY：HTTP API Key，使用此认证时配置。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // url: 填写实际接口地址；不要在地址中填写密码或 Token。
        // method: 请求方法：GET 读取；POST、PUT、PATCH、DELETE 可能修改远端数据。
        // query: 普通查询参数；认证参数使用 query_auth 从凭据读取。
        // query_auth: 可选认证：例如 {"parameter":"api_key","secret_binding":"HTTP_API_KEY"}；在本适配器的凭据绑定中配置同名键。
        // headers: 普通请求头；Bearer 认证可增加 "DLR-Auth": "bearer:HTTP_BEARER_TOKEN"，Token 在本适配器凭据绑定中配置。
        // body: POST、PUT、PATCH 的请求内容；GET 通常留空。
        // response_type: 返回内容类型：json 或 text。
        // allowed_statuses: 允许的 HTTP 状态码列表；按目标接口调整。
        // timeout_seconds: 单次请求超时时间，单位秒。
        // max_response_bytes: HTTP 响应大小上限，单位字节。
        // max_redirects: 最多跟随的同站点跳转次数。
        return (Map<String, Object>) Json.parse("""
            {
              "url": "https://api.example/resources",
              "method": "GET",
              "query": {},
              "query_auth": null,
              "headers": {
                "Accept": "application/json"
              },
              "body": null,
              "response_type": "json",
              "allowed_statuses": [
                200
              ],
              "timeout_seconds": 30,
              "max_response_bytes": 1048576,
              "max_redirects": 3
            }
            """);
    }

    private static final Set<String> METHODS = Set.of("GET", "POST", "PUT", "PATCH", "DELETE");
    private static final Set<String> SIDE_EFFECTS = Set.of("POST", "PUT", "PATCH", "DELETE");
    private static final String HEADER_NAME = "[!#$%&'*+\\-.^_`|~0-9A-Za-z]+";
    private static final Set<String> RESTRICTED_HEADERS = Set.of(
        "connection", "content-length", "host", "keep-alive", "proxy-connection",
        "te", "trailer", "transfer-encoding", "upgrade"
    );
    private static final Set<String> CREDENTIAL_NAME_MARKERS = Set.of(
        "accesskey", "apikey", "authorization", "authentication", "clientsecret", "cookie",
        "credential", "password", "privatekey", "secret", "signature", "token"
    );

    public Object handle(Context context, Object rawInput) throws Exception {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        String method = String.valueOf(input.getOrDefault("method", "GET")).toUpperCase();
        if (!METHODS.contains(method)) throw new IllegalArgumentException("unsupported_method");
        URI initial;
        try { initial = URI.create(requiredString(input, "url")); }
        catch (IllegalArgumentException error) { throw new IllegalArgumentException("invalid_url"); }
        if (!List.of("http", "https").contains(initial.getScheme()) || initial.getHost() == null
            || initial.getUserInfo() != null || initial.getFragment() != null) {
            throw new IllegalArgumentException("invalid_url");
        }
        rejectDirectCredentialQuery(initial, null, false);
        URI target = withQuery(initial, input.get("query"));
        String[] queryAuth = queryAuth(context, input.get("query_auth"));
        Set<String> sensitive = new LinkedHashSet<>();
        if (queryAuth != null) {
            sensitive.add(queryAuth[1]);
            sensitive.add(encode(queryAuth[1]));
            sensitive.add(java.net.URLEncoder.encode(queryAuth[1], StandardCharsets.UTF_8));
        }
        target = withQueryAuth(target, queryAuth, false);
        Map<String, String> headers;
        try {
            HeaderSet headerSet = headers(context, input.get("headers"));
            headers = headerSet.values();
            sensitive.addAll(headerSet.sensitive());
        } catch (IllegalArgumentException error) {
            if (!"request_failed".equals(error.getMessage())) throw error;
            return requestFailure(method);
        }
        Object bodyValue = input.get("body");
        String body;
        try {
            body = bodyValue == null ? null
                : "text/plain".equals(input.get("content_type")) ? String.valueOf(bodyValue)
                : Json.stringify(bodyValue);
        } catch (RuntimeException error) {
            return requestFailure(method);
        }
        if (body != null) headers.putIfAbsent(
            "Content-Type", String.valueOf(input.getOrDefault("content_type", "application/json"))
        );
        try {
            if (headers.containsKey("Content-Type")) {
                validateHeader("Content-Type", headers.get("Content-Type"));
            }
        } catch (IllegalArgumentException error) {
            return requestFailure(method);
        }
        int timeout = positiveInt(input.get("timeout_seconds"), 30, 120);
        int maximum = positiveInt(input.get("max_response_bytes"), 1_048_576, 8_388_608);
        int maxRedirects = positiveInt(input.get("max_redirects"), 3, 10);
        Set<Long> allowed = longSet(input.get("allowed_statuses"));
        HttpResponse<InputStream> response = null;
        try {
            HttpClient client = HttpClient.newBuilder()
                .followRedirects(HttpClient.Redirect.NEVER)
                .connectTimeout(Duration.ofSeconds(timeout))
                .build();
            for (int redirects = 0; ; redirects++) {
                HttpRequest.Builder builder = HttpRequest.newBuilder(target)
                    .timeout(Duration.ofSeconds(timeout));
                headers.forEach(builder::header);
                builder.method(method, body == null
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(body));
                response = client.send(builder.build(), HttpResponse.BodyHandlers.ofInputStream());
                if (!List.of(301, 302, 303, 307, 308).contains(response.statusCode())) break;
                closeSafely(response.body());
                if (SIDE_EFFECTS.contains(method)) {
                    throw new IllegalArgumentException("side_effect_redirect_forbidden");
                }
                if (redirects >= maxRedirects) throw new IllegalArgumentException("redirect_limit_exceeded");
                String location = response.headers().firstValue("location")
                    .orElseThrow(() -> new IllegalArgumentException("redirect_without_location"));
                URI next = target.resolve(location);
                if (!sameOrigin(initial, next)) throw new IllegalArgumentException("cross_origin_redirect");
                rejectDirectCredentialQuery(next, queryAuth, true);
                target = withQueryAuth(next, queryAuth, true);
            }
        } catch (Exception error) {
            Map<String, Object> failure = new LinkedHashMap<>();
            failure.put("ok", false);
            String message = error instanceof IllegalArgumentException ? error.getMessage() : null;
            failure.put("error", message != null && Set.of(
                "credential_query_collision", "redirect_limit_exceeded", "redirect_without_location",
                "cross_origin_redirect", "direct_credential_query_forbidden",
                "side_effect_redirect_forbidden"
            ).contains(message) ? message : "request_failed");
            failure.put("side_effect_uncertain", SIDE_EFFECTS.contains(method));
            failure.put("retried", false);
            return failure;
        }
        int status = response.statusCode();
        if (!allowed.isEmpty() && !allowed.contains((long) status)) {
            closeSafely(response.body());
            return Map.of(
                "ok", false, "error", "unexpected_status", "status", status,
                "side_effect_uncertain", SIDE_EFFECTS.contains(method), "retried", false
            );
        }
        byte[] bytes;
        InputStream stream = response.body();
        try {
            bytes = stream.readNBytes(maximum + 1);
        } catch (Exception error) {
            return requestFailure(method);
        } finally {
            closeSafely(stream);
        }
        if (bytes.length > maximum) {
            Map<String, Object> limited = new LinkedHashMap<>();
            limited.put("ok", true); limited.put("status", status);
            limited.put("partial", true); limited.put("bytes_read", maximum);
            limited.put("response", null); return limited;
        }
        String text = new String(bytes, StandardCharsets.UTF_8);
        Object value = text;
        String contentType = response.headers().firstValue("content-type").orElse("");
        if (contentType.contains("application/json") || "json".equals(input.get("response_type"))) {
            try {
                value = Json.parse(text);
            } catch (RuntimeException error) {
                return Map.of("ok", false, "error", "invalid_json_response", "status", status);
            }
        }
        Object cleaned = scrub(value, sensitive);
        if (Json.stringify(cleaned).getBytes(StandardCharsets.UTF_8).length > maximum) {
            Map<String, Object> limited = new LinkedHashMap<>();
            limited.put("ok", true); limited.put("status", status);
            limited.put("partial", true); limited.put("bytes_read", bytes.length);
            limited.put("response", null); return limited;
        }
        Map<String, Object> output = new LinkedHashMap<>();
        output.put("ok", true);
        output.put("status", status);
        output.put("content_type", contentType);
        output.put("partial", false);
        output.put("bytes_read", bytes.length);
        output.put("response", cleaned);
        output.put("side_effect_warning", SIDE_EFFECTS.contains(method));
        return output;
    }

    private static Map<String, Object> requestFailure(String method) {
        return Map.of(
            "ok", false, "error", "request_failed",
            "side_effect_uncertain", SIDE_EFFECTS.contains(method), "retried", false
        );
    }

    private static void closeSafely(AutoCloseable resource) {
        if (resource == null) return;
        try { resource.close(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
    }

    private static URI withQuery(URI uri, Object raw) {
        if (raw == null) return uri;
        Map<String, Object> query = object(raw, "invalid_query");
        List<String> pairs = new ArrayList<>();
        if (uri.getRawQuery() != null && !uri.getRawQuery().isEmpty()) pairs.add(uri.getRawQuery());
        query.entrySet().stream().sorted(Map.Entry.comparingByKey()).forEach(entry -> {
            if (!entry.getKey().matches("[A-Za-z][A-Za-z0-9_.-]{0,63}")
                || !(entry.getValue() instanceof String value) || value.length() > 4096) {
                throw new IllegalArgumentException("invalid_query");
            }
            if (credentialLikeName(entry.getKey())) {
                throw new IllegalArgumentException("direct_credential_query_forbidden");
            }
            pairs.add(encode(entry.getKey()) + "=" + encode(value));
        });
        return URI.create(uri.getScheme() + "://" + uri.getAuthority()
            + (uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath())
            + (pairs.isEmpty() ? "" : "?" + String.join("&", pairs)));
    }

    private static String[] queryAuth(Context context, Object raw) {
        if (raw == null) return null;
        Map<String, Object> value = object(raw, "invalid_query_auth");
        if (!value.keySet().equals(Set.of("parameter", "secret_binding"))) {
            throw new IllegalArgumentException("invalid_query_auth");
        }
        String parameter = requiredString(value, "parameter");
        if (!parameter.matches("[A-Za-z][A-Za-z0-9_.-]{0,63}")) {
            throw new IllegalArgumentException("invalid_query_auth");
        }
        String binding = requiredString(value, "secret_binding");
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String secret = context.secrets.get(binding);
        if (secret == null || secret.isEmpty()) throw new IllegalArgumentException("missing_credential");
        return new String[] {parameter, secret};
    }

    private static URI withQueryAuth(URI uri, String[] queryAuth, boolean allowInjected) {
        if (queryAuth == null) return uri;
        String parameter = encode(queryAuth[0]);
        String expected = parameter + "=" + encode(queryAuth[1]);
        List<String> pairs = new ArrayList<>();
        if (uri.getRawQuery() != null && !uri.getRawQuery().isEmpty()) {
            for (String pair : uri.getRawQuery().split("&")) {
                if (allowInjected && pair.equals(expected)
                    && uri.getRawQuery().indexOf(expected) == uri.getRawQuery().lastIndexOf(expected)) {
                    return uri;
                }
                if (pair.equals(parameter) || pair.startsWith(parameter + "=")) {
                    throw new IllegalArgumentException("credential_query_collision");
                }
                pairs.add(pair);
            }
        }
        pairs.add(expected);
        return URI.create(uri.getScheme() + "://" + uri.getRawAuthority()
            + (uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath())
            + "?" + String.join("&", pairs));
    }

    private static String encode(String value) {
        return java.net.URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }

    private static boolean credentialLikeName(String name) {
        String compact = name.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]", "");
        return CREDENTIAL_NAME_MARKERS.stream().anyMatch(compact::contains)
            || compact.endsWith("auth") || compact.endsWith("sig");
    }

    private static void rejectDirectCredentialQuery(
        URI uri,
        String[] queryAuth,
        boolean allowInjected
    ) {
        String raw = uri.getRawQuery();
        if (raw == null || raw.isEmpty()) return;
        for (String pair : raw.split("&")) {
            String[] parts = pair.split("=", 2);
            String name = java.net.URLDecoder.decode(parts[0], StandardCharsets.UTF_8);
            String value = parts.length == 2
                ? java.net.URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : "";
            if (credentialLikeName(name)
                && !(allowInjected && queryAuth != null
                    && name.equals(queryAuth[0]) && value.equals(queryAuth[1]))) {
                throw new IllegalArgumentException("direct_credential_query_forbidden");
            }
        }
    }

    private static HeaderSet headers(Context context, Object raw) {
        Map<String, String> result = new LinkedHashMap<>();
        Set<String> sensitive = new LinkedHashSet<>();
        if (raw == null) return new HeaderSet(result, sensitive);
        for (Map.Entry<String, Object> entry : object(raw, "invalid_headers").entrySet()) {
            result.put(entry.getKey(), String.valueOf(entry.getValue()));
        }
        List<String> authNames = result.keySet().stream()
            .filter(name -> "dlr-auth".equals(name.toLowerCase(Locale.ROOT))).toList();
        if (authNames.size() > 1) throw new IllegalArgumentException("invalid_headers");
        String auth = authNames.isEmpty() ? null : result.remove(authNames.get(0));
        if (result.keySet().stream().anyMatch(Adapter::credentialLikeName)) {
            throw new IllegalArgumentException("direct_credential_header_forbidden");
        }
        if (auth != null) {
            int split = auth.indexOf(':');
            if (split <= 0) throw new IllegalArgumentException("invalid_auth_scheme");
            String scheme = auth.substring(0, split);
            // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
            String secret = context.secrets.get(auth.substring(split + 1));
            if (secret == null || secret.isEmpty()) throw new IllegalArgumentException("missing_credential");
            String injected;
            if ("bearer".equals(scheme)) {
                injected = "Bearer " + secret; result.put("Authorization", injected);
            } else if ("basic".equals(scheme)) {
                injected = "Basic " + Base64.getEncoder().encodeToString(secret.getBytes(StandardCharsets.UTF_8));
                result.put("Authorization", injected);
            } else if (scheme.startsWith("api-key/")) {
                injected = secret; result.put(scheme.substring(8), injected);
            }
            else throw new IllegalArgumentException("invalid_auth_scheme");
            sensitive.add(secret); sensitive.add(injected);
        }
        for (Map.Entry<String, String> entry : result.entrySet()) {
            validateHeader(entry.getKey(), entry.getValue());
        }
        return new HeaderSet(result, sensitive);
    }

    private static Object scrub(Object raw, Set<String> sensitive) {
        if (raw instanceof String text) {
            List<String> ordered = sensitive.stream().filter(value -> !value.isEmpty())
                .sorted((left, right) -> Integer.compare(right.length(), left.length())).toList();
            for (String secret : ordered) {
                int size = secret.getBytes(StandardCharsets.UTF_8).length;
                String marker = size >= 10 ? "<redacted>" : "*".repeat(size);
                text = text.replace(secret, marker);
            }
            return text;
        }
        if (raw instanceof List<?> list) {
            List<Object> result = new ArrayList<>();
            for (Object value : list) result.add(scrub(value, sensitive));
            return result;
        }
        if (raw instanceof Map<?, ?> map) {
            Map<String, Object> result = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                result.put((String) scrub(String.valueOf(entry.getKey()), sensitive),
                    scrub(entry.getValue(), sensitive));
            }
            return result;
        }
        return raw;
    }

    private static void validateHeader(String name, String value) {
        String normalized = name.toLowerCase();
        if (!name.matches(HEADER_NAME) || RESTRICTED_HEADERS.contains(normalized)
            || normalized.startsWith("proxy-")
            || value.contains("\r") || value.contains("\n")) {
            throw new IllegalArgumentException("request_failed");
        }
    }

    private static boolean sameOrigin(URI left, URI right) {
        return left.getScheme().equalsIgnoreCase(right.getScheme())
            && left.getHost().equalsIgnoreCase(right.getHost())
            && effectivePort(left) == effectivePort(right);
    }

    private static int effectivePort(URI uri) {
        return uri.getPort() >= 0 ? uri.getPort() : "https".equals(uri.getScheme()) ? 443 : 80;
    }

    private static int positiveInt(Object value, int fallback, int maximum) {
        if (!(value instanceof Number number)) return fallback;
        int parsed = number.intValue();
        return parsed > 0 ? Math.min(parsed, maximum) : fallback;
    }

    private static Set<Long> longSet(Object raw) {
        if (raw == null) {
            return java.util.stream.LongStream.range(200, 300).boxed()
                .collect(java.util.stream.Collectors.toUnmodifiableSet());
        }
        if (!(raw instanceof List<?> values) || values.isEmpty()
            || values.stream().anyMatch(value -> !(value instanceof Number number)
                || number.longValue() < 100 || number.longValue() > 599)) {
            throw new IllegalArgumentException("invalid_allowed_statuses");
        }
        return values.stream().map(Number.class::cast).map(Number::longValue)
            .collect(java.util.stream.Collectors.toUnmodifiableSet());
    }

    private static String requiredString(Map<String, Object> value, String key) {
        Object raw = value.get(key);
        if (!(raw instanceof String text) || text.isBlank()) throw new IllegalArgumentException("invalid_" + key);
        return text;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> object(Object raw, String code) {
        if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException(code);
        return (Map<String, Object>) value;
    }

    private record HeaderSet(Map<String, String> values, Set<String> sensitive) {}
}
