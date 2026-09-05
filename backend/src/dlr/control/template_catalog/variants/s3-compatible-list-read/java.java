import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URI;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Request;

/** Bounded S3-compatible list and optional range reads. */
public class Adapter {
    public Object handle(Context context, Object rawInput) throws Exception {
        try {
            return run(context, rawInput);
        } catch (Exception error) {
            String code = error.getMessage();
            if (code != null && STABLE_ERRORS.contains(code)) {
                throw new IllegalArgumentException(code);
            }
            throw new IllegalArgumentException("s3_operation_failed");
        }
    }

    private Object run(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput);
        String bucket = required(input, "bucket");
        String access = context.secrets.get("S3_ACCESS_KEY_ID");
        String secret = context.secrets.get("S3_SECRET_ACCESS_KEY");
        if (access == null || secret == null) throw new IllegalArgumentException("missing_credential");
        Set<String> requested = new LinkedHashSet<>();
        if (input.get("read_keys") instanceof List<?> values) {
            if (values.size() > 1_000) throw new IllegalArgumentException("invalid_read_keys");
            for (Object value : values) {
                if (!(value instanceof String key)) throw new IllegalArgumentException("invalid_read_keys");
                requested.add(key);
            }
        } else if (input.containsKey("read_keys") && input.get("read_keys") != null) {
            throw new IllegalArgumentException("invalid_read_keys");
        }
        Object rawContinuation = input.get("continuation_token");
        if (rawContinuation != null && (!(rawContinuation instanceof String value)
            || value.isEmpty())) {
            throw new IllegalArgumentException("invalid_continuation_token");
        }
        String continuation = rawContinuation instanceof String value ? value : null;
        Object rawObjectOffset = input.getOrDefault("object_offset", 0);
        if (!(rawObjectOffset instanceof Number number)
            || number.intValue() < 0 || number.intValue() > 1_000) {
            throw new IllegalArgumentException("invalid_object_offset");
        }
        int objectOffset = number.intValue();
        int maxTotalBytes = positive(input.get("max_total_bytes"), 4_194_304, 16_777_216);
        if (maxTotalBytes < 256 || resultSize(
            0, 0, 0, 0, 0, 0, true,
            checkpoint(continuation, objectOffset, "checkpoint_limit")
        ) > maxTotalBytes) {
            throw new IllegalArgumentException("max_total_bytes_too_small");
        }
        String session = context.secrets.get("S3_SESSION_TOKEN");
        var credentials = session == null
            ? AwsBasicCredentials.create(access, secret)
            : AwsSessionCredentials.create(access, secret, session);
        S3Client.Builder builder = S3Client.builder()
            .region(Region.of(String.valueOf(input.getOrDefault("region", "us-east-1"))))
            .credentialsProvider(StaticCredentialsProvider.create(credentials))
            .forcePathStyle(!Boolean.FALSE.equals(input.get("force_path_style")));
        if (input.get("endpoint") instanceof String endpoint) {
            URI uri = URI.create(endpoint);
            boolean loopback = "localhost".equalsIgnoreCase(uri.getHost())
                || "127.0.0.1".equals(uri.getHost()) || "::1".equals(uri.getHost());
            if (uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null
                || uri.getFragment() != null || !("https".equals(uri.getScheme())
                || ("http".equals(uri.getScheme()) && loopback))) {
                throw new IllegalArgumentException("invalid_endpoint");
            }
            builder.endpointOverride(uri);
        } else if (input.containsKey("endpoint") && input.get("endpoint") != null) {
            throw new IllegalArgumentException("invalid_endpoint");
        }
        int maxObjects = positive(input.get("max_objects"), 1_000, 10_000);
        int maxPages = positive(input.get("max_pages"), 20, 200);
        int maxObjectBytes = positive(input.get("max_object_bytes"), 1_048_576, 8_388_608);
        List<Map<String, Object>> objects = new ArrayList<>();
        List<Map<String, Object>> contents = new ArrayList<>();
        int objectItemBytes = 0;
        int contentItemBytes = 0;
        int totalBytes = 0;
        int pages = 0;
        boolean partial = false;
        Map<String, Object> outputCheckpoint = null;
        boolean stopped = false;
        S3Client client = null;
        try {
            client = builder.build();
            while (pages < maxPages && !stopped) {
                String pageToken = continuation;
                int pageOffset = objectOffset;
                var response = client.listObjectsV2(ListObjectsV2Request.builder()
                    .bucket(bucket).prefix(String.valueOf(input.getOrDefault("prefix", "")))
                    .maxKeys(Math.min(1_000, maxObjects))
                    .continuationToken(pageToken).build());
                pages++;
                if (pageOffset > response.contents().size()) {
                    throw new IllegalArgumentException("invalid_checkpoint");
                }
                for (int itemAt = pageOffset; itemAt < response.contents().size(); itemAt++) {
                    var item = response.contents().get(itemAt);
                    Map<String, Object> currentCheckpoint = checkpoint(
                        pageToken, itemAt, "output_limit"
                    );
                    Map<String, Object> afterCheckpoint = checkpoint(
                        pageToken, itemAt + 1, "checkpoint_limit"
                    );
                    if (objects.size() >= maxObjects) {
                        partial = true;
                        outputCheckpoint = checkpoint(pageToken, itemAt, "max_objects");
                        stopped = true;
                        break;
                    }
                    String key = item.key();
                    long size = item.size();
                    Map<String, Object> metadata = new LinkedHashMap<>();
                    metadata.put("key", key); metadata.put("size", size);
                    metadata.put("etag", item.eTag() == null ? "" : item.eTag().replace("\"", ""));
                    metadata.put("lastModified", item.lastModified() == null ? null : item.lastModified().toString());
                    int candidateObjectBytes = objectItemBytes + encodedBytes(metadata);
                    Map<String, Object> candidateContent = null;
                    if (size > maxObjectBytes || totalBytes + size > maxTotalBytes) {
                        if (requested.contains(key)) {
                            candidateContent = new LinkedHashMap<>();
                            candidateContent.put("key", key);
                            candidateContent.put("status", "limit_exceeded");
                            candidateContent.put("size", size);
                        }
                    }
                    if (candidateContent != null) {
                        int candidateContentBytes = contentItemBytes + encodedBytes(candidateContent);
                        if (resultSize(
                            objects.size() + 1, candidateObjectBytes,
                            contents.size() + 1, candidateContentBytes,
                            totalBytes, pages, true, afterCheckpoint
                        ) > maxTotalBytes) {
                            partial = true; outputCheckpoint = currentCheckpoint;
                            stopped = true; break;
                        }
                        objects.add(metadata); contents.add(candidateContent);
                        objectItemBytes = candidateObjectBytes;
                        contentItemBytes = candidateContentBytes;
                        partial = true;
                        continue;
                    }
                    if (!requested.contains(key)) {
                        if (resultSize(
                            objects.size() + 1, candidateObjectBytes,
                            contents.size(), contentItemBytes,
                            totalBytes, pages, true, afterCheckpoint
                        ) > maxTotalBytes) {
                            partial = true; outputCheckpoint = currentCheckpoint;
                            stopped = true; break;
                        }
                        objects.add(metadata); objectItemBytes = candidateObjectBytes;
                        continue;
                    }
                    int readLimit = rawCapacity(
                        key, maxObjectBytes, objects.size() + 1, candidateObjectBytes,
                        contents.size(), contentItemBytes, totalBytes, pages,
                        afterCheckpoint, maxTotalBytes
                    );
                    if (size > readLimit) {
                        partial = true; outputCheckpoint = currentCheckpoint;
                        stopped = true; break;
                    }
                    byte[] bytes = new byte[0];
                    if (readLimit > 0) {
                        GetObjectRequest get = GetObjectRequest.builder().bucket(bucket).key(key)
                            .range("bytes=0-" + (readLimit - 1)).build();
                        ResponseInputStream<?> objectStream = null;
                        try {
                            objectStream = client.getObject(get);
                            bytes = readBounded(objectStream, readLimit);
                        } finally {
                            closeSafely(objectStream);
                        }
                        if (bytes == null) {
                            partial = true; outputCheckpoint = currentCheckpoint;
                            stopped = true; break;
                        }
                    }
                    candidateContent = new LinkedHashMap<>();
                    candidateContent.put("key", key); candidateContent.put("status", "read");
                    candidateContent.put("bytes", bytes.length);
                    candidateContent.put("content_base64", Base64.getEncoder().encodeToString(bytes));
                    int candidateContentBytes = contentItemBytes + encodedBytes(candidateContent);
                    if (resultSize(
                        objects.size() + 1, candidateObjectBytes,
                        contents.size() + 1, candidateContentBytes,
                        totalBytes + bytes.length, pages, true, afterCheckpoint
                    ) > maxTotalBytes) {
                        partial = true; outputCheckpoint = currentCheckpoint;
                        stopped = true; break;
                    }
                    objects.add(metadata); contents.add(candidateContent);
                    objectItemBytes = candidateObjectBytes;
                    contentItemBytes = candidateContentBytes;
                    totalBytes += bytes.length;
                }
                if (stopped) break;
                boolean truncated = Boolean.TRUE.equals(response.isTruncated());
                String nextContinuation = response.nextContinuationToken();
                if (!truncated) {
                    continuation = null; objectOffset = 0; break;
                }
                if (nextContinuation == null || nextContinuation.isEmpty()) {
                    partial = true;
                    outputCheckpoint = checkpoint(
                        pageToken, response.contents().size(), "missing_token"
                    );
                    break;
                }
                Map<String, Object> nextCheckpoint = checkpoint(nextContinuation, 0, null);
                if (resultSize(
                    objects.size(), objectItemBytes, contents.size(), contentItemBytes,
                    totalBytes, pages, true, nextCheckpoint
                ) > maxTotalBytes) {
                    partial = true;
                    outputCheckpoint = checkpoint(
                        pageToken, response.contents().size(), "output_limit"
                    );
                    break;
                }
                continuation = nextContinuation;
                objectOffset = 0;
            }
        } finally {
            closeSafely(client);
        }
        if (continuation != null && outputCheckpoint == null) {
            partial = true;
            outputCheckpoint = checkpoint(continuation, objectOffset, null);
        }
        Map<String, Object> result = resultValue(
            objects, contents, totalBytes, pages, partial, outputCheckpoint, objects.size()
        );
        if (encodedBytes(result) > maxTotalBytes) {
            throw new IllegalArgumentException("max_total_bytes_too_small");
        }
        return result;
    }

    private static final Set<String> STABLE_ERRORS = Set.of(
        "input_must_be_object", "bucket_required", "invalid_endpoint",
        "missing_credential", "invalid_read_keys", "invalid_continuation_token",
        "invalid_object_offset", "invalid_checkpoint", "max_total_bytes_too_small"
    );

    private static Map<String, Object> checkpoint(
        String token,
        int objectOffset,
        String reason
    ) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("continuation_token", token);
        value.put("object_offset", objectOffset);
        if (reason != null) value.put("reason", reason);
        return value;
    }

    private static Map<String, Object> resultValue(
        List<Map<String, Object>> objects,
        List<Map<String, Object>> contents,
        int totalBytes,
        int pages,
        boolean partial,
        Map<String, Object> checkpoint,
        int objectCount
    ) {
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("objects", objectCount);
        summary.put("bytes_read", totalBytes);
        summary.put("pages", pages);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("objects", objects); result.put("contents", contents);
        result.put("summary", summary); result.put("partial", partial);
        result.put("checkpoint", checkpoint);
        return result;
    }

    private static int resultSize(
        int objectCount,
        int objectItemBytes,
        int contentCount,
        int contentItemBytes,
        int totalBytes,
        int pages,
        boolean partial,
        Map<String, Object> checkpoint
    ) {
        int shell = encodedBytes(resultValue(
            List.of(), List.of(), totalBytes, pages, partial, checkpoint, objectCount
        ));
        return shell - 4
            + arraySize(objectCount, objectItemBytes)
            + arraySize(contentCount, contentItemBytes);
    }

    private static int arraySize(int count, int itemBytes) {
        return count == 0 ? 2 : itemBytes + count + 1;
    }

    private static int readContentSize(String key, int rawBytes) {
        Map<String, Object> shell = new LinkedHashMap<>();
        shell.put("key", key); shell.put("status", "read");
        shell.put("bytes", rawBytes); shell.put("content_base64", "");
        return encodedBytes(shell) + 4 * ((rawBytes + 2) / 3);
    }

    private static int rawCapacity(
        String key,
        int maximum,
        int objectCount,
        int objectItemBytes,
        int contentCount,
        int contentItemBytes,
        int totalBytes,
        int pages,
        Map<String, Object> checkpoint,
        int maxTotalBytes
    ) {
        int low = 0, high = maximum;
        while (low < high) {
            int candidate = low + (high - low + 1) / 2;
            int candidateContentBytes = contentItemBytes + readContentSize(key, candidate);
            if (resultSize(
                objectCount, objectItemBytes, contentCount + 1, candidateContentBytes,
                totalBytes + candidate, pages, true, checkpoint
            ) <= maxTotalBytes) {
                low = candidate;
            } else {
                high = candidate - 1;
            }
        }
        return low;
    }

    private static int encodedBytes(Object value) {
        return Json.stringify(value).getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
    }

    private static void closeSafely(AutoCloseable resource) {
        if (resource == null) return;
        try { resource.close(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
    }

    private static byte[] readBounded(InputStream stream, int maximum) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        for (int read; (read = stream.read(buffer)) >= 0;) {
            if (output.size() + read > maximum) return null;
            output.write(buffer, 0, read);
        }
        return output.toByteArray();
    }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number n && n.intValue() > 0 ? Math.min(n.intValue(), maximum) : fallback; }
    private static String required(Map<String, Object> value, String key) { Object raw = value.get(key); if (!(raw instanceof String text) || text.isEmpty()) throw new IllegalArgumentException(key + "_required"); return text; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException("input_must_be_object"); return (Map<String, Object>) value; }
}
