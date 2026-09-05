import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.time.format.DateTimeFormatter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Pure webhook payload validation and normalization. */
public class Adapter {
    private static final List<String> DANGEROUS_TARGET_SEGMENTS = List.of(
        "__proto__", "prototype", "constructor"
    );
    private static final int MAX_PATH_LENGTH = 256;
    private static final int MAX_PATH_SEGMENTS = 32;
    private static final int MIN_OUTPUT_BYTES = 128;

    public Object handle(Context context, Object rawInput) {
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        Map<String, Object> payload = object(input.get("payload"), "payload_must_be_object");
        List<?> required = list(input.getOrDefault("required", List.of()), "invalid_required");
        List<?> mappings = list(input.getOrDefault("mappings", List.of()), "invalid_mappings");
        int maxFields = boundedInt(input, "max_fields", 200, 1, 1000, "invalid_max_fields");
        int maxInputBytes = boundedInt(
            input, "max_input_bytes", 1_048_576, 1, 8_388_608, "invalid_max_input_bytes"
        );
        int maxOutputBytes = boundedInt(
            input, "max_output_bytes", 2_097_152, MIN_OUTPUT_BYTES, 8_388_608,
            "invalid_max_output_bytes"
        );
        int maxDepth = boundedInt(input, "max_depth", 32, 1, 64, "invalid_max_depth");
        if (required.size() > maxFields) throw new IllegalArgumentException("invalid_required");
        if (mappings.size() > maxFields) throw new IllegalArgumentException("invalid_mappings");
        for (Object value : required) {
            if (!validPath(value, false)) throw new IllegalArgumentException("invalid_required");
        }
        if (exceedsDepth(payload, maxDepth)) throw new IllegalArgumentException("payload_too_deep");
        if (serializedBytes(payload) > maxInputBytes) throw new IllegalArgumentException("payload_too_large");
        List<Map<String, String>> errors = new ArrayList<>();
        for (int index = 0; index < required.size(); index++) {
            String path = (String) required.get(index);
            try {
                if (readPath(payload, path) == null) throw new IllegalArgumentException("missing");
            } catch (IllegalArgumentException error) {
                errors.add(Map.of("field", "required[" + index + "]", "code", "required"));
            }
        }
        Map<String, Object> normalized = new LinkedHashMap<>();
        int normalizedBudget = 2;
        int assignedMappings = 0;
        int validResultOverhead = serializedBytes(
            result(true, new LinkedHashMap<>(), List.of(), false)
        ) - 2;
        for (int index = 0; index < mappings.size(); index++) {
            Object rawMapping = mappings.get(index);
            String field = "mappings[" + index + "]";
            Map<String, Object> mapping;
            try {
                mapping = object(rawMapping, "invalid_mapping");
            } catch (IllegalArgumentException error) {
                errors.add(Map.of("field", field, "code", "invalid_mapping"));
                continue;
            }
            Object sourceRaw = mapping.get("source");
            Object targetRaw = mapping.get("target");
            Object requiredRaw = mapping.get("required");
            Object conversion = mapping.get("type");
            if (!validPath(sourceRaw, false) || !validPath(targetRaw, true)
                || requiredRaw != null && !(requiredRaw instanceof Boolean)
                || conversion != null && !"datetime".equals(conversion)) {
                errors.add(Map.of("field", field, "code", "invalid_mapping"));
                continue;
            }
            String source = (String) sourceRaw;
            String target = (String) targetRaw;
            try {
                Object value;
                try {
                    value = readPath(payload, source);
                } catch (IllegalArgumentException error) {
                    if (mapping.containsKey("default")) value = mapping.get("default");
                    else if (Boolean.TRUE.equals(requiredRaw)) {
                        errors.add(Map.of("field", field + ".source", "code", "missing"));
                        continue;
                    }
                    else continue;
                }
                if ("datetime".equals(conversion)) value = utc(value);
                Map<String, Object> candidate = new LinkedHashMap<>();
                assign(candidate, target, value);
                int addition = serializedBytes(candidate) - 2 + (assignedMappings == 0 ? 0 : 1);
                if (validResultOverhead + normalizedBudget + addition > maxOutputBytes) {
                    errors.add(Map.of("field", "", "code", "output_limit"));
                    break;
                }
                assign(normalized, target, value);
                normalizedBudget += addition;
                assignedMappings++;
            } catch (IllegalArgumentException error) {
                errors.add(Map.of("field", field, "code", "invalid_value"));
            }
        }
        return boundedResult(errors, normalized, maxFields, maxOutputBytes);
    }

    private static Object readPath(Object value, String path) {
        Object current = value;
        for (String segment : path.split("\\.", -1)) {
            if (segment.isEmpty() || !(current instanceof Map<?, ?> map) || !map.containsKey(segment)) {
                throw new IllegalArgumentException("missing_path");
            }
            current = map.get(segment);
        }
        return current;
    }

    private static boolean validPath(Object raw, boolean target) {
        if (!(raw instanceof String path) || path.isEmpty() || path.length() > MAX_PATH_LENGTH) {
            return false;
        }
        String[] segments = path.split("\\.", -1);
        if (segments.length > MAX_PATH_SEGMENTS) return false;
        for (String segment : segments) {
            if (!segment.matches("[A-Za-z0-9_-]{1,64}")
                || target && DANGEROUS_TARGET_SEGMENTS.contains(segment)) return false;
        }
        return true;
    }

    private static String utc(Object value) {
        if (!(value instanceof String text)) throw new IllegalArgumentException("invalid_timestamp");
        Instant instant;
        try {
            instant = OffsetDateTime.parse(text).toInstant();
        } catch (DateTimeParseException error) {
            try { instant = Instant.parse(text); }
            catch (DateTimeParseException ignored) {
                throw new IllegalArgumentException("invalid_timestamp");
            }
        }
        return DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'")
            .withZone(ZoneOffset.UTC).format(instant);
    }

    private static boolean exceedsDepth(Object value, int maximum) {
        Deque<DepthValue> stack = new ArrayDeque<>();
        stack.push(new DepthValue(value, 0));
        while (!stack.isEmpty()) {
            DepthValue item = stack.pop();
            Object current = item.value();
            if (!(current instanceof Map<?, ?>) && !(current instanceof List<?>)) continue;
            int currentDepth = item.parentDepth() + 1;
            if (currentDepth > maximum) return true;
            Iterable<?> children = current instanceof Map<?, ?> map ? map.values() : (List<?>) current;
            for (Object child : children) stack.push(new DepthValue(child, currentDepth));
        }
        return false;
    }

    private static int serializedBytes(Object value) {
        try {
            return Json.stringify(value).getBytes(StandardCharsets.UTF_8).length;
        } catch (RuntimeException | StackOverflowError error) {
            throw new IllegalArgumentException("payload_too_large");
        }
    }

    private static Map<String, Object> boundedResult(
        List<Map<String, String>> errors,
        Map<String, Object> normalized,
        int maxFields,
        int maxOutputBytes
    ) {
        if (errors.isEmpty()) {
            Map<String, Object> successful = result(true, normalized, List.of(), false);
            if (serializedBytes(successful) <= maxOutputBytes) return successful;
            errors = List.of(Map.of("field", "", "code", "output_limit"));
        }

        List<Map<String, String>> selected = new ArrayList<>();
        for (Map<String, String> error : errors.subList(0, Math.min(errors.size(), maxFields))) {
            List<Map<String, String>> candidateErrors = new ArrayList<>(selected);
            candidateErrors.add(error);
            if (serializedBytes(result(false, null, candidateErrors, true)) > maxOutputBytes) break;
            selected.add(error);
        }
        boolean partial = selected.size() < errors.size();
        Map<String, Object> bounded = result(false, null, selected, partial);
        if (!partial && serializedBytes(bounded) > maxOutputBytes) {
            selected.remove(selected.size() - 1);
            bounded = result(false, null, selected, true);
        }
        return bounded;
    }

    private static Map<String, Object> result(
        boolean valid, Object data, List<? extends Map<String, String>> errors, boolean partial
    ) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("valid", valid);
        result.put("data", data);
        result.put("errors", errors);
        result.put("partial", partial);
        return result;
    }

    private record DepthValue(Object value, int parentDepth) {}

    @SuppressWarnings("unchecked")
    private static void assign(Map<String, Object> target, String path, Object value) {
        String[] segments = path.split("\\.", -1);
        if (java.util.Arrays.stream(segments).anyMatch(DANGEROUS_TARGET_SEGMENTS::contains)) {
            throw new IllegalArgumentException("invalid_target_path");
        }
        Map<String, Object> current = target;
        for (int at = 0; at < segments.length - 1; at++) {
            if (segments[at].isEmpty()) throw new IllegalArgumentException("invalid_target_path");
            Object child = current.computeIfAbsent(segments[at], ignored -> new LinkedHashMap<>());
            if (!(child instanceof Map<?, ?> map)) throw new IllegalArgumentException("target_path_conflict");
            current = (Map<String, Object>) map;
        }
        if (segments.length == 0 || segments[segments.length - 1].isEmpty()) {
            throw new IllegalArgumentException("invalid_target_path");
        }
        current.put(segments[segments.length - 1], value);
    }

    private static int boundedInt(
        Map<String, Object> input,
        String key,
        int fallback,
        int minimum,
        int maximum,
        String code
    ) {
        if (!input.containsKey(key)) return fallback;
        Object raw = input.get(key);
        if (!(raw instanceof Number number)) throw new IllegalArgumentException(code);
        double decimal = number.doubleValue();
        if (!Double.isFinite(decimal) || decimal != Math.rint(decimal)
            || decimal < minimum || decimal > maximum) throw new IllegalArgumentException(code);
        return (int) decimal;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> object(Object raw, String code) {
        if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException(code);
        return (Map<String, Object>) value;
    }

    private static List<?> list(Object raw, String code) {
        if (!(raw instanceof List<?> values)) throw new IllegalArgumentException(code);
        return values;
    }
}
