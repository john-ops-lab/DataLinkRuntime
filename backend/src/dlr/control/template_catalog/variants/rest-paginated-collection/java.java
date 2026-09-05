import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

/** Bounded REST collection pagination with loop detection. */
public class Adapter {
    public Object handle(Context context, Object rawInput) throws Exception {
        try {
            return run(context, rawInput);
        } catch (Exception error) {
            String code = error.getMessage();
            if (code != null && STABLE_ERRORS.contains(code)) {
                throw new IllegalArgumentException(code);
            }
            throw new IllegalArgumentException("request_failed");
        }
    }

    private Object run(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        URI base;
        try { base = URI.create(required(input, "url")); }
        catch (IllegalArgumentException error) { throw new IllegalArgumentException("invalid_url"); }
        if (!List.of("http", "https").contains(base.getScheme()) || base.getHost() == null
            || base.getUserInfo() != null || base.getFragment() != null) {
            throw new IllegalArgumentException("invalid_url");
        }
        rejectDirectCredentialQuery(base, null, false);
        String strategy = String.valueOf(input.getOrDefault("strategy", "page"));
        if (!List.of("page", "offset", "cursor", "next-url").contains(strategy)) {
            throw new IllegalArgumentException("invalid_strategy");
        }
        int maxPages = positive(input.get("max_pages"), 20, 500);
        int maxRecords = positive(input.get("max_records"), 10_000, 100_000);
        int maxBytes = positive(input.get("max_bytes"), 4_194_304, 16_777_216);
        int pageSize = positive(input.get("page_size"), 100, 1_000);
        int timeout = positive(input.get("timeout_seconds"), 30, 120);
        int retries = boundedInteger(input.get("max_retries"), 2, 0, 5, "invalid_max_retries");
        int page = boundedInteger(
            input.get("start_page"), 1, 1, 1_000_000, "invalid_start_page"
        );
        int offset = boundedInteger(
            input.get("start_offset"), 0, 0, 1_000_000_000, "invalid_start_offset"
        );
        long deadline = System.nanoTime() + Duration.ofSeconds(timeout).toNanos();
        HeaderSet headerSet = headers(context, input.get("headers"));
        Map<String, String> headers = headerSet.values();
        String[] queryAuth = queryAuth(context, input.get("query_auth"));
        if (queryAuth != null) {
            headerSet.sensitive().add(queryAuth[1]);
            headerSet.sensitive().add(encode(queryAuth[1]));
            headerSet.sensitive().add(URLEncoder.encode(queryAuth[1], StandardCharsets.UTF_8));
        }
        HttpClient client = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER)
            .connectTimeout(Duration.ofSeconds(timeout)).build();
        List<Object> records = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        Set<String> seenBatches = new LinkedHashSet<>();
        URI nextUrl = base;
        String cursor = null;
        int totalBytes = 0;
        Map<String, Object> checkpoint = null;
        boolean partial = false;
        int pages = 0;
        boolean completed = false;
        for (int iteration = 0; iteration < maxPages; iteration++) {
            int remainingBytes = maxBytes - totalBytes;
            if (remainingBytes <= 0 || System.nanoTime() >= deadline) {
                partial = true; checkpoint = checkpoint(strategy, page, offset); break;
            }
            URI url = withPagination("next-url".equals(strategy) ? nextUrl : base, strategy, input, pageSize, page, offset, cursor);
            boolean crossOrigin = !sameOrigin(base, url);
            if (crossOrigin && !Boolean.TRUE.equals(input.get("allow_cross_origin_next"))) {
                throw new IllegalArgumentException("cross_origin_next_url");
            }
            if (crossOrigin) {
                url = withoutQueryParameter(url, queryAuth == null ? null : queryAuth[0]);
                rejectDirectCredentialQuery(url, null, false);
            } else {
                rejectDirectCredentialQuery(url, queryAuth, "next-url".equals(strategy));
                url = withQueryAuth(url, queryAuth, "next-url".equals(strategy));
            }
            Map<String, String> requestHeaders = new LinkedHashMap<>(headers);
            if (crossOrigin) requestHeaders.keySet().removeIf(key -> !Set.of(
                "accept", "content-type", "user-agent"
            ).contains(key.toLowerCase()) || headerSet.credentialNames().contains(key.toLowerCase()));
            Page response = getJson(client, url, requestHeaders, deadline, remainingBytes, retries);
            pages++;
            totalBytes += response.bytes;
            if (totalBytes > maxBytes) {
                partial = true; checkpoint = checkpoint(strategy, page, offset); break;
            }
            Object rawBatch = path(response.payload, String.valueOf(input.getOrDefault("records_path", "items")));
            if (!(rawBatch instanceof List<?> batch)) throw new IllegalArgumentException("records_path_not_array");
            if (batch.isEmpty()) { completed = true; break; }
            List<Object> safeBatch = new ArrayList<>();
            for (Object value : batch) safeBatch.add(scrub(value, headerSet.sensitive()));
            if (!seenBatches.add(Json.stringify(safeBatch))) {
                throw new IllegalArgumentException("pagination_no_progress");
            }
            int remaining = maxRecords - records.size();
            if (safeBatch.size() > remaining) {
                partial = true; checkpoint = checkpoint(strategy, page, offset); break;
            }
            List<Object> candidateRecords = new ArrayList<>(records);
            candidateRecords.addAll(safeBatch);
            if (recordsBytes(candidateRecords) > maxBytes) {
                partial = true; checkpoint = checkpoint(strategy, page, offset); break;
            }
            records.addAll(safeBatch);
            if ("page".equals(strategy)) page++;
            else if ("offset".equals(strategy)) {
                int previous = offset; offset += batch.size();
                if (offset <= previous) throw new IllegalArgumentException("offset_not_advancing");
            } else {
                Object rawNext = path(response.payload, String.valueOf(input.getOrDefault("next_path", "next")));
                if (rawNext == null || String.valueOf(rawNext).isEmpty()) {
                    completed = true; break;
                }
                String candidate = "next-url".equals(strategy)
                    ? url.resolve(String.valueOf(rawNext)).toString() : String.valueOf(rawNext);
                if (!seen.add(candidate)) throw new IllegalArgumentException("pagination_loop_detected");
                if ("next-url".equals(strategy)) {
                    nextUrl = URI.create(candidate);
                    if (nextUrl.getUserInfo() != null || nextUrl.getFragment() != null
                        || (!sameOrigin(base, nextUrl)
                        && !Boolean.TRUE.equals(input.get("allow_cross_origin_next")))) {
                        throw new IllegalArgumentException("cross_origin_next_url");
                    }
                } else {
                    if (candidate.equals(cursor)) throw new IllegalArgumentException("cursor_not_advancing");
                    cursor = candidate;
                }
            }
            if (records.size() >= maxRecords) {
                partial = true; checkpoint = checkpoint(strategy, page, offset); break;
            }
        }
        if (!completed && !partial && pages == maxPages) {
            partial = true;
            if (checkpoint == null) checkpoint = checkpoint(strategy, page, offset);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("records", records); result.put("count", records.size());
        result.put("pages", pages); result.put("bytes", totalBytes);
        result.put("partial", partial); result.put("checkpoint", checkpoint);
        return result;
    }

    private static final String HEADER_NAME = "[!#$%&'*+\\-.^_`|~0-9A-Za-z]+";
    private static final Set<String> RESTRICTED_HEADERS = Set.of(
        "connection", "content-length", "host", "keep-alive", "proxy-connection",
        "te", "trailer", "transfer-encoding", "upgrade"
    );
    private static final Set<String> CREDENTIAL_NAME_MARKERS = Set.of(
        "accesskey", "apikey", "authorization", "authentication", "clientsecret", "cookie",
        "credential", "password", "privatekey", "secret", "signature", "token"
    );
    private static final Set<String> STABLE_ERRORS = Set.of(
        "input_must_be_object", "invalid_url", "invalid_strategy", "invalid_headers",
        "invalid_query_auth", "credential_query_collision", "direct_credential_query_forbidden",
        "direct_credential_header_forbidden", "invalid_auth_scheme", "missing_credential",
        "request_timeout", "request_failed", "response_too_large", "retry_limit_exceeded",
        "unexpected_status", "invalid_json_response", "cross_origin_next_url",
        "records_path_not_array", "response_path_missing", "pagination_no_progress",
        "offset_not_advancing", "pagination_loop_detected", "cursor_not_advancing"
    );

    private static Page getJson(HttpClient client, URI url, Map<String, String> headers, long deadline, int maximum, int retries) throws Exception {
        for (int attempt = 0; attempt <= retries; attempt++) {
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) throw new IllegalArgumentException("request_timeout");
            HttpResponse<InputStream> response;
            try {
                HttpRequest.Builder builder = HttpRequest.newBuilder(url).GET()
                    .timeout(Duration.ofNanos(remaining));
                headers.forEach(builder::header);
                response = client.send(builder.build(), HttpResponse.BodyHandlers.ofInputStream());
            }
            catch (Exception error) {
                if (attempt == retries) throw new IllegalArgumentException("request_failed");
                retryDelay(attempt, deadline); continue;
            }
            int status = response.statusCode();
            if (status == 429 || status >= 500) {
                closeSafely(response.body());
                if (attempt == retries) throw new IllegalArgumentException("retry_limit_exceeded");
                retryDelay(attempt, deadline); continue;
            }
            if (status < 200 || status >= 300) {
                closeSafely(response.body());
                throw new IllegalArgumentException("unexpected_status");
            }
            byte[] bytes;
            InputStream stream = response.body();
            try { bytes = stream.readNBytes(maximum + 1); }
            catch (Exception error) { throw new IllegalArgumentException("request_failed"); }
            finally { closeSafely(stream); }
            if (bytes.length > maximum) throw new IllegalArgumentException("response_too_large");
            return new Page(Json.parse(new String(bytes, StandardCharsets.UTF_8)), bytes.length);
        }
        throw new IllegalArgumentException("request_failed");
    }

    private static void retryDelay(int attempt, long deadline) throws InterruptedException {
        long remaining = deadline - System.nanoTime();
        if (remaining <= 0) throw new IllegalArgumentException("request_timeout");
        long delay = Math.min(Duration.ofMillis(Math.min(100L << attempt, 1_000L)).toNanos(), remaining);
        TimeUnit.NANOSECONDS.sleep(delay);
    }

    private static void closeSafely(AutoCloseable resource) {
        if (resource == null) return;
        try { resource.close(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
    }

    private static URI withPagination(URI base, String strategy, Map<String, Object> input, int size, int page, int offset, String cursor) {
        Map<String, String> query = query(base.getRawQuery());
        if ("page".equals(strategy)) {
            query.put(String.valueOf(input.getOrDefault("page_parameter", "page")), String.valueOf(page));
            query.put(String.valueOf(input.getOrDefault("size_parameter", "page_size")), String.valueOf(size));
        } else if ("offset".equals(strategy)) {
            query.put(String.valueOf(input.getOrDefault("offset_parameter", "offset")), String.valueOf(offset));
            query.put(String.valueOf(input.getOrDefault("limit_parameter", "limit")), String.valueOf(size));
        } else if ("cursor".equals(strategy)) {
            query.put(String.valueOf(input.getOrDefault("limit_parameter", "limit")), String.valueOf(size));
            if (cursor != null) query.put(String.valueOf(input.getOrDefault("cursor_parameter", "cursor")), cursor);
        }
        String encoded = query.entrySet().stream().sorted(Map.Entry.comparingByKey())
            .map(entry -> encode(entry.getKey()) + "=" + encode(entry.getValue()))
            .collect(java.util.stream.Collectors.joining("&"));
        return URI.create(base.getScheme() + "://" + base.getAuthority()
            + (base.getRawPath() == null || base.getRawPath().isEmpty() ? "/" : base.getRawPath())
            + (encoded.isEmpty() ? "" : "?" + encoded));
    }

    private static Map<String, String> query(String raw) {
        Map<String, String> result = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) return result;
        for (String pair : raw.split("&")) {
            String[] parts = pair.split("=", 2);
            result.put(java.net.URLDecoder.decode(parts[0], StandardCharsets.UTF_8),
                parts.length == 2 ? java.net.URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : "");
        }
        return result;
    }

    private static String[] queryAuth(Context context, Object raw) {
        if (raw == null) return null;
        Map<String, Object> value = object(raw, "invalid_query_auth");
        if (!value.keySet().equals(Set.of("parameter", "secret_binding"))) {
            throw new IllegalArgumentException("invalid_query_auth");
        }
        String parameter = required(value, "parameter");
        if (!parameter.matches("[A-Za-z][A-Za-z0-9_.-]{0,63}")) {
            throw new IllegalArgumentException("invalid_query_auth");
        }
        String binding = required(value, "secret_binding");
        String secret = context.secrets.get(binding);
        if (secret == null || secret.isEmpty()) {
            throw new IllegalArgumentException("missing_credential");
        }
        return new String[] {parameter, secret};
    }

    private static URI withQueryAuth(URI uri, String[] queryAuth, boolean allowInjected) {
        if (queryAuth == null) return uri;
        String raw = uri.getRawQuery();
        int matching = 0;
        boolean exact = false;
        if (raw != null && !raw.isEmpty()) {
            for (String pair : raw.split("&")) {
                String[] parts = pair.split("=", 2);
                String name = java.net.URLDecoder.decode(parts[0], StandardCharsets.UTF_8);
                if (!name.equals(queryAuth[0])) continue;
                matching++;
                String value = parts.length == 2
                    ? java.net.URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : "";
                exact = value.equals(queryAuth[1]);
            }
        }
        if (matching > 0) {
            if (allowInjected && matching == 1 && exact) return uri;
            throw new IllegalArgumentException("credential_query_collision");
        }
        String pair = encode(queryAuth[0]) + "=" + encode(queryAuth[1]);
        return rebuildQuery(uri, raw == null || raw.isEmpty() ? pair : raw + "&" + pair);
    }

    private static URI withoutQueryParameter(URI uri, String parameter) {
        if (parameter == null || uri.getRawQuery() == null || uri.getRawQuery().isEmpty()) return uri;
        List<String> retained = new ArrayList<>();
        for (String pair : uri.getRawQuery().split("&")) {
            String[] parts = pair.split("=", 2);
            String name = java.net.URLDecoder.decode(parts[0], StandardCharsets.UTF_8);
            if (!name.equals(parameter)) retained.add(pair);
        }
        return rebuildQuery(uri, String.join("&", retained));
    }

    private static URI rebuildQuery(URI uri, String rawQuery) {
        return URI.create(uri.getScheme() + "://" + uri.getRawAuthority()
            + (uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath())
            + (rawQuery == null || rawQuery.isEmpty() ? "" : "?" + rawQuery));
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
        int allowedMatches = 0;
        for (String pair : raw.split("&")) {
            String[] parts = pair.split("=", 2);
            String name = java.net.URLDecoder.decode(parts[0], StandardCharsets.UTF_8);
            if (!credentialLikeName(name)) continue;
            String value = parts.length == 2
                ? java.net.URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : "";
            if (allowInjected && queryAuth != null
                && name.equals(queryAuth[0]) && value.equals(queryAuth[1])) {
                allowedMatches++;
                continue;
            }
            throw new IllegalArgumentException("direct_credential_query_forbidden");
        }
        if (allowedMatches > 1) {
            throw new IllegalArgumentException("credential_query_collision");
        }
    }

    private static int recordsBytes(List<Object> records) {
        return Json.stringify(records).getBytes(StandardCharsets.UTF_8).length;
    }

    private static Object path(Object value, String dotted) {
        Object current = value;
        if (dotted.isEmpty()) return current;
        for (String part : dotted.split("\\.")) {
            if (!(current instanceof Map<?, ?> map) || !map.containsKey(part)) {
                throw new IllegalArgumentException("response_path_missing");
            }
            current = map.get(part);
        }
        return current;
    }
    private static Map<String, Object> checkpoint(String strategy, int page, int offset) {
        if (!"page".equals(strategy) && !"offset".equals(strategy)) return null;
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("strategy", strategy);
        value.put("page".equals(strategy) ? "start_page" : "start_offset",
            "page".equals(strategy) ? page : offset);
        return value;
    }
    private static boolean sameOrigin(URI a, URI b) {
        return a.getScheme().equalsIgnoreCase(b.getScheme()) && a.getHost().equalsIgnoreCase(b.getHost())
            && port(a) == port(b);
    }
    private static int port(URI value) { return value.getPort() >= 0 ? value.getPort() : "https".equals(value.getScheme()) ? 443 : 80; }
    private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20"); }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number n && n.intValue() > 0 ? Math.min(n.intValue(), maximum) : fallback; }
    private static int boundedInteger(
        Object raw, int fallback, int minimum, int maximum, String code
    ) {
        if (raw == null) return fallback;
        if (!(raw instanceof Number number)) throw new IllegalArgumentException(code);
        double value = number.doubleValue();
        if (!Double.isFinite(value) || value != Math.rint(value)
            || value < minimum || value > maximum) {
            throw new IllegalArgumentException(code);
        }
        return (int) value;
    }
    private static String required(Map<String, Object> value, String key) { Object raw = value.get(key); if (!(raw instanceof String text) || text.isEmpty()) throw new IllegalArgumentException("invalid_" + key); return text; }
    private static HeaderSet headers(Context context, Object raw) {
        Map<String, String> result = new LinkedHashMap<>();
        Set<String> credentialNames = new LinkedHashSet<>();
        Set<String> sensitive = new LinkedHashSet<>();
        if (raw == null) return new HeaderSet(result, credentialNames, sensitive);
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
            String secret = context.secrets.get(auth.substring(split + 1));
            if (secret == null || secret.isEmpty()) throw new IllegalArgumentException("missing_credential");
            if ("bearer".equals(scheme)) {
                String injected = "Bearer " + secret;
                result.put("Authorization", injected);
                credentialNames.add("authorization");
                sensitive.add(secret); sensitive.add(injected);
            }
            else if (scheme.startsWith("api-key/") && scheme.length() > 8) {
                String headerName = scheme.substring(8);
                result.put(headerName, secret);
                credentialNames.add(headerName.toLowerCase());
                sensitive.add(secret);
            } else throw new IllegalArgumentException("invalid_auth_scheme");
        }
        for (Map.Entry<String, String> entry : result.entrySet()) {
            validateHeader(entry.getKey(), entry.getValue());
        }
        return new HeaderSet(result, credentialNames, sensitive);
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
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw, String code) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException(code); return (Map<String, Object>) value; }
    private record Page(Object payload, int bytes) {}
    private record HeaderSet(
        Map<String, String> values,
        Set<String> credentialNames,
        Set<String> sensitive
    ) {}
}
