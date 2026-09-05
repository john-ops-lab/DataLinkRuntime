import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Bounded ServiceNow cmdb_ci snapshot with optional idempotent CMDB sync. */
public class Adapter {
    public Object handle(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        String mode = String.valueOf(input.getOrDefault("mode", "preview"));
        if (!List.of("preview", "sync").contains(mode)) throw new IllegalArgumentException("invalid_mode");
        if ("sync".equals(mode)) validateIdentity(input);
        int offset = nonNegativeInt(input.get("offset"), "invalid_offset");
        if ("sync".equals(mode) && offset != 0) throw new IllegalArgumentException("sync_offset_must_be_zero");
        Object rawDisplayValue = input.getOrDefault("display_value", false);
        if (!(rawDisplayValue instanceof Boolean displayValue)) {
            throw new IllegalArgumentException("invalid_display_value");
        }
        Object rawEncodedQuery = input.getOrDefault("encoded_query", "");
        if (!(rawEncodedQuery instanceof String encodedQuery)
            || encodedQuery.codePointCount(0, encodedQuery.length()) > 4096) {
            throw new IllegalArgumentException("invalid_encoded_query");
        }
        URI base;
        try { base = URI.create(required(input, "instance_url")); }
        catch (RuntimeException error) {
            throw new IllegalArgumentException("https_instance_url_required");
        }
        String account = required(input, "instance_id");
        if (!"https".equals(base.getScheme()) || base.getHost() == null || base.getUserInfo() != null
            || base.getQuery() != null || base.getFragment() != null || account.length() > 128) {
            throw new IllegalArgumentException("https_instance_url_required");
        }
        if (!"cmdb_ci".equals(input.getOrDefault("table", "cmdb_ci"))) throw new IllegalArgumentException("only_cmdb_ci_supported");
        List<?> fields = input.get("fields") instanceof List<?> values
            ? values : List.of("sys_id", "name", "sys_class_name", "install_status");
        if (fields.size() > 64 || !fields.contains("sys_id") || fields.stream().anyMatch(value -> !(value instanceof String text) || !text.matches("[A-Za-z][A-Za-z0-9_.]{0,127}"))) throw new IllegalArgumentException("invalid_fields");
        int maxPages = positive(input.get("max_pages"), 20, 200), maxRecords = positive(input.get("max_records"), 5_000, 50_000), maxBytes = positive(input.get("max_bytes"), 8_388_608, 16_777_216), pageSize = Math.min(positive(input.get("page_size"), 500, 10_000), maxRecords), timeout = positive(input.get("timeout_seconds"), 30, 120);
        if (maxBytes < 1_024) throw new IllegalArgumentException("max_bytes_too_small");
        long deadline = System.nanoTime() + Duration.ofSeconds(timeout).toNanos();
        Map<String, Map<String, Object>> assets = new LinkedHashMap<>();
        Map<String, Integer> assetSizes = new LinkedHashMap<>();
        int assetItemBytes = 0;
        int totalBytes = 0, pages = 0; boolean partial = false; String failure = null;
        boolean sourceComplete = false;
        try {
            for (int pageAt = 0; pageAt < maxPages; pageAt++) {
                String query = "sysparm_limit=" + pageSize + "&sysparm_offset=" + offset
                    + "&sysparm_fields=" + encode(fields.stream().map(String::valueOf).collect(java.util.stream.Collectors.joining(",")))
                    + "&sysparm_display_value=" + displayValue
                    + "&sysparm_exclude_reference_link=true&sysparm_query=" + encode(encodedQuery);
                URI url = base.resolve("/api/now/table/cmdb_ci?" + query);
                int remainingBytes = maxBytes - totalBytes;
                if (remainingBytes <= 0) { partial = true; break; }
                Page page = getPage(context, url, remainingBytes, deadline);
                totalBytes += page.bytes;
                pages++;
                if (totalBytes > maxBytes) { partial = true; break; }
                int processed = 0;
                for (Object value : page.records) {
                    Map<String, Object> mapped = value instanceof Map<?, ?>
                        ? asset(account, object(value, "invalid_record")) : null;
                    if (mapped == null) { partial = true; failure = "invalid_source_record"; break; }
                    String assetKey = String.valueOf(mapped.get("external_key"));
                    int assetBytes = encodedBytes(mapped);
                    int candidateCount = assets.size() + (assets.containsKey(assetKey) ? 0 : 1);
                    int candidateItemBytes = assetItemBytes
                        - assetSizes.getOrDefault(assetKey, 0) + assetBytes;
                    if (candidateCount > maxRecords) { partial = true; break; }
                    if (!candidateFits(
                        candidateCount, candidateItemBytes, pages,
                        offset + processed + 1, maxBytes
                    )) {
                        partial = true; failure = "max_bytes_exceeded"; break;
                    }
                    assets.put(assetKey, mapped); assetSizes.put(assetKey, assetBytes);
                    assetItemBytes = candidateItemBytes; processed++;
                }
                offset += processed;
                if (partial || page.records.size() < pageSize) {
                    sourceComplete = !partial;
                    break;
                }
                if (assets.size() >= maxRecords) { partial = true; break; }
            }
            if (pages == maxPages && !sourceComplete) partial = true;
        } catch (Exception error) { partial = true; failure = "source_read_failed"; }
        List<Map<String, Object>> ordered = new ArrayList<>(assets.values());
        ordered.sort(Comparator.comparing(value -> String.valueOf(value.get("external_key"))));
        if ("preview".equals(mode)) {
            Map<String, Object> result = snapshotResult(ordered, pages, failure, partial, offset);
            if (encodedBytes(result) > maxBytes) {
                throw new IllegalArgumentException("max_bytes_too_small");
            }
            return result;
        }
        if (partial) {
            Map<String, Object> result = partialSyncResult(input, ordered, pages, failure, offset);
            if (encodedBytes(result) > maxBytes) {
                throw new IllegalArgumentException("max_bytes_too_small");
            }
            return result;
        }
        Map<String, Object> summary = summary(ordered.size(), pages, null);
        Object result = sync(context, input, ordered, summary, deadline);
        if (encodedBytes(result) > maxBytes) {
            throw new IllegalArgumentException("max_bytes_too_small");
        }
        return result;
    }

    private static Map<String, Object> summary(int assets, int pages, String failure) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("assets", assets); value.put("relationships", 0); value.put("pages", pages);
        value.put("failures", failure == null ? List.of() : List.of(failure));
        return value;
    }

    private static Map<String, Object> snapshotResult(
        List<Map<String, Object>> assets,
        int pages,
        String failure,
        boolean partial,
        int offset
    ) {
        return snapshotResult(assets, pages, failure, partial, offset, assets.size());
    }

    private static Map<String, Object> snapshotResult(
        List<Map<String, Object>> assets,
        int pages,
        String failure,
        boolean partial,
        int offset,
        int assetCount
    ) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("schema_version", "dlr-asset-snapshot/v1");
        result.put("assets", assets); result.put("relationships", List.of());
        result.put("summary", summary(assetCount, pages, failure));
        result.put("partial", partial);
        result.put("checkpoint", partial ? Map.of("offset", offset) : null);
        return result;
    }

    private static Map<String, Object> partialSyncResult(
        Map<String, Object> input,
        List<Map<String, Object>> assets,
        int pages,
        String failure,
        int offset
    ) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("mode", "sync"); result.put("scan_id", input.get("scan_id"));
        result.put("source_scope", input.get("source_scope")); result.put("partial", true);
        result.put("summary", summary(assets.size(), pages, failure));
        result.put("failed", failure == null ? List.of("bounded") : List.of(failure));
        result.put("checkpoint", Map.of("offset", offset));
        return result;
    }

    private static boolean candidateFits(
        int assetCount,
        int assetItemBytes,
        int pages,
        int offset,
        int maxBytes
    ) {
        int assetArrayBytes = assetCount == 0 ? 2 : assetItemBytes + assetCount + 1;
        Map<String, Object> shell = snapshotResult(
            List.of(), pages, "invalid_source_record", true, offset, assetCount
        );
        return encodedBytes(shell) - 2 + assetArrayBytes <= maxBytes;
    }

    private static int encodedBytes(Object value) {
        return Json.stringify(value).getBytes(StandardCharsets.UTF_8).length;
    }

    private static Page getPage(Context context, URI url, int maximum, long deadline) throws Exception {
        Map<String, String> headers = auth(context); headers.put("Accept", "application/json");
        for (int attempt = 0; attempt < 3; attempt++) {
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) throw new IllegalArgumentException("servicenow_timeout");
            HttpRequest.Builder builder = HttpRequest.newBuilder(url).GET().timeout(Duration.ofNanos(remaining));
            headers.forEach(builder::header);
            HttpResponse<InputStream> response = HttpClient.newBuilder()
                .followRedirects(HttpClient.Redirect.NEVER).build()
                .send(builder.build(), HttpResponse.BodyHandlers.ofInputStream());
            int status = response.statusCode();
            if (status == 429 || status >= 500) {
                response.body().close();
                if (attempt == 2) throw new IllegalArgumentException("servicenow_retry_limit");
                long delay = Math.min(100L << attempt, Math.max(0, (deadline - System.nanoTime()) / 1_000_000L));
                if (delay <= 0) throw new IllegalArgumentException("servicenow_timeout");
                Thread.sleep(delay); continue;
            }
            if (status < 200 || status >= 300) { response.body().close(); throw new IllegalArgumentException("servicenow_request_failed"); }
            byte[] bytes;
            try (InputStream stream = response.body()) { bytes = stream.readNBytes(maximum + 1); }
            if (bytes.length > maximum) throw new IllegalArgumentException("servicenow_response_too_large");
            Map<String, Object> payload = object(Json.parse(new String(bytes, StandardCharsets.UTF_8)), "servicenow_invalid_json");
            if (!(payload.get("result") instanceof List<?> records)) throw new IllegalArgumentException("servicenow_invalid_result");
            return new Page(records, bytes.length);
        }
        throw new IllegalArgumentException("servicenow_request_failed");
    }
    private static Map<String, String> auth(Context context) {
        String bearer = context.secrets.get("SERVICENOW_BEARER_TOKEN");
        if (bearer != null) return new LinkedHashMap<>(Map.of("Authorization", "Bearer " + bearer));
        String user = context.secrets.get("SERVICENOW_USERNAME"), password = context.secrets.get("SERVICENOW_PASSWORD");
        if (user == null || password == null) throw new IllegalArgumentException("missing_credential");
        return new LinkedHashMap<>(Map.of("Authorization", "Basic " + Base64.getEncoder().encodeToString((user + ":" + password).getBytes(StandardCharsets.UTF_8))));
    }
    private static Map<String, Object> asset(String account, Map<String, Object> record) {
        Object id = record.get("sys_id"); if (!(id instanceof String text) || text.isEmpty()) return null;
        Object name = display(record, "name"); if (name == null) name = display(record, "display_name"); if (name == null) name = id;
        Map<String, Object> attributes = new LinkedHashMap<>();
        record.keySet().stream().sorted().forEach(key -> { if (!"sys_id".equals(key)) { Object value = display(record, key); if (value == null || value instanceof String || value instanceof Number || value instanceof Boolean) attributes.put(key, value); } });
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("external_key", "servicenow:" + keyComponent(account) + ":global:cmdb_ci:" + keyComponent(text));
        asset.put("class", String.valueOf(display(record, "sys_class_name") == null ? "cmdb_ci" : display(record, "sys_class_name")));
        asset.put("provider_type", "cmdb_ci"); asset.put("name", String.valueOf(name)); asset.put("account", account);
        asset.put("region", "global"); asset.put("zone", null);
        asset.put("status", display(record, "install_status") == null ? null : String.valueOf(display(record, "install_status")));
        asset.put("tags", Map.of()); asset.put("attributes", attributes); return asset;
    }
    private static Object display(Map<String, Object> record, String key) { Object value = record.get(key); return value instanceof Map<?, ?> map ? map.get("display_value") : value; }
    private static Object sync(Context context, Map<String, Object> input, List<Map<String, Object>> assets, Map<String, Object> summary, long deadline) throws Exception {
        String scan = required(input, "scan_id"), scope = required(input, "source_scope");
        String base = context.config.get("cmdb_base_url") instanceof String value ? value : null, token = context.secrets.get("CMDB_TOKEN");
        URI target;
        try { target = base == null ? null : URI.create(base); } catch (Exception error) { target = null; }
        String host = target == null ? null : target.getHost();
        boolean loopback = List.of("localhost", "127.0.0.1", "::1").contains(host);
        if (target == null || token == null || host == null || target.getUserInfo() != null
            || target.getRawQuery() != null || target.getFragment() != null
            || !("https".equals(target.getScheme()) || ("http".equals(target.getScheme()) && loopback))) {
            throw new IllegalArgumentException("cmdb_target_not_configured");
        }
        Map<String, Object> common = new LinkedHashMap<>(); common.put("schema_version", "dlr-cmdb-upsert/v1"); common.put("source_scope", scope); common.put("scan_id", scan);
        int acknowledgedAssets = 0;
        try {
            String beginIdempotency = digest(List.of("begin", scope, scan));
            Map<String, Object> begin = new LinkedHashMap<>(common);
            begin.put("operation", "begin_scan"); begin.put("idempotency_key", beginIdempotency);
            begin.put("provider", "servicenow"); begin.put("catalog_version", "1.0.0");
            post(base, "/api/v1/import-scans:begin", begin, token, beginIdempotency, deadline);
            int size = positive(input.get("batch_size"), 200, 1000);
            for (int at = 0; at < assets.size(); at += size) {
                int batchIndex = at / size;
                List<Map<String, Object>> batch = assets.subList(at, Math.min(assets.size(), at + size));
                String batchId = "assets:servicenow:" + scope + ":" + String.format("%06d", batchIndex);
                String idempotency = digest(List.of("assets", scope, scan, batchId));
                Map<String, Object> body = new LinkedHashMap<>(common);
                body.put("operation", "upsert_assets"); body.put("idempotency_key", idempotency);
                body.put("batch_id", batchId); body.put("batch_index", batchIndex); body.put("assets", batch);
                post(base, "/api/v1/import-scans/" + encode(scan) + "/assets:upsert", body, token, idempotency, deadline);
                acknowledgedAssets += batch.size();
            }
            String finishIdempotency = digest(List.of("finish", scope, scan));
            Map<String, Object> finish = new LinkedHashMap<>(common);
            finish.put("operation", "finish_scan"); finish.put("idempotency_key", finishIdempotency);
            finish.put("complete", true); finish.put("summary", summary);
            post(base, "/api/v1/import-scans/" + encode(scan) + ":finish", finish, token, finishIdempotency, deadline);
        } catch (Exception error) {
            Map<String, Object> failedSummary = summary(acknowledgedAssets, ((Number) summary.get("pages")).intValue(), "target_batch");
            Map<String, Object> failed = new LinkedHashMap<>(); failed.put("mode", "sync"); failed.put("scan_id", scan); failed.put("source_scope", scope); failed.put("partial", true); failed.put("summary", failedSummary); failed.put("failed", List.of("target_batch")); failed.put("checkpoint", Map.of("scan_id", scan)); return failed;
        }
        Map<String, Object> done = new LinkedHashMap<>(); done.put("mode", "sync"); done.put("scan_id", scan); done.put("source_scope", scope); done.put("partial", false); done.put("summary", summary); done.put("failed", List.of()); done.put("checkpoint", null); return done;
    }
    private static void post(String base, String path, Object body, String token, String idem, long deadline) throws Exception { long remaining = deadline - System.nanoTime(); if (remaining <= 0) throw new IllegalArgumentException("cmdb_target_error"); HttpRequest value = HttpRequest.newBuilder(URI.create((base.endsWith("/") ? base.substring(0, base.length() - 1) : base) + path)).timeout(Duration.ofNanos(remaining)).header("Authorization", "Bearer " + token).header("Content-Type", "application/json").header("Idempotency-Key", idem).POST(HttpRequest.BodyPublishers.ofString(Json.stringify(body))).build(); HttpResponse<InputStream> response = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).build().send(value, HttpResponse.BodyHandlers.ofInputStream()); response.body().close(); if (response.statusCode() < 200 || response.statusCode() >= 300) throw new IllegalArgumentException("cmdb_target_error"); }
    private static String digest(Object value) { try { return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Json.stringify(value).getBytes(StandardCharsets.UTF_8))); } catch (Exception error) { throw new IllegalStateException(error); } }
    private static void validateIdentity(Map<String, Object> input) { for (String key : List.of("scan_id", "source_scope")) if (!required(input, key).matches("[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")) throw new IllegalArgumentException("invalid_" + key); }
    private static String keyComponent(String value) { return value.replace("%", "%25").replace(":", "%3A"); }
    private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20"); }
    private static int nonNegativeInt(Object raw, String code) {
        if (raw == null) return 0;
        if (!(raw instanceof Number number)) throw new IllegalArgumentException(code);
        double value = number.doubleValue();
        if (!Double.isFinite(value) || value != Math.rint(value) || value < 0 || value > Integer.MAX_VALUE) {
            throw new IllegalArgumentException(code);
        }
        return (int) value;
    }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number n && n.intValue() > 0 ? Math.min(n.intValue(), maximum) : fallback; }
    private static String required(Map<String, Object> value, String key) { Object raw = value.get(key); if (!(raw instanceof String text) || text.isBlank()) throw new IllegalArgumentException("invalid_" + key); return text; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw, String code) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException(code); return (Map<String, Object>) value; }
    private record Page(List<?> records, int bytes) {}
}
