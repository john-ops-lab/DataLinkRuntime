import java.time.Instant;
import java.time.OffsetDateTime;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Finite RFC 6901 mapping, filtering, sorting and de-duplication. */
public class Adapter {
    // JSON 字段整理：可修改的配置集中在这里。
    // 运行时提供待处理的数据或文件；处理规则在下面配置。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // mappings: 字段映射规则；source/pointer 指定原字段路径，target 指定结果字段名。
        // filters: 按字段值筛选；空列表表示不过滤。
        // sort: 排序字段和方向：asc 升序、desc 降序。
        // dedupe_by: 按此字段去重。
        // max_records: 单次运行最多返回的记录数。
        // max_fields: 最多处理的字段数。
        // max_output_bytes: 返回结果大小上限，单位字节。
        return (Map<String, Object>) Json.parse("""
            {
              "mappings": [
                {
                  "pointer": "/profile/name",
                  "target": "name",
                  "type": "string",
                  "default": ""
                },
                {
                  "pointer": "/id",
                  "target": "id",
                  "type": "string"
                }
              ],
              "filters": [],
              "sort": {
                "field": "name",
                "direction": "asc"
              },
              "dedupe_by": "id",
              "max_records": 10000,
              "max_fields": 200,
              "max_output_bytes": 4194304
            }
            """);
    }

    private static final int MAX_POINTER_BYTES = 1_024;
    private static final int MAX_POINTER_TOKENS = 64;
    private static final int MAX_POINTER_TOKEN_BYTES = 256;
    private static final int MAX_TARGET_BYTES = 256;
    private static final Set<String> CONVERSIONS = Set.of(
        "string", "integer", "number", "boolean", "datetime"
    );

    public Object handle(Context context, Object rawInput) {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        List<?> rawRecords = list(input.get("records"), "records_required");
        List<?> mappings = list(input.get("mappings"), "mappings_required");
        int maxRecords = bounded(input, "max_records", 10_000, 100_000);
        int maxFields = bounded(input, "max_fields", 200, 1_000);
        int maxOutputBytes = bounded(input, "max_output_bytes", 4_194_304, 16_777_216);
        if (mappings.size() > maxFields) throw new IllegalArgumentException("invalid_max_fields");
        List<?> filters = list(input.getOrDefault("filters", List.of()), "invalid_filters");
        if (filters.size() > maxFields) throw new IllegalArgumentException("invalid_filter");
        for (Object rawRule : filters) {
            Map<String, Object> rule = object(rawRule, "invalid_filter");
            Object operation = rule.getOrDefault("op", "equals");
            if (!validPointer(rule.get("pointer"))
                || !("equals".equals(operation) || "exists".equals(operation))
                || "exists".equals(operation) && rule.containsKey("value")
                    && !(rule.get("value") instanceof Boolean)) {
                throw new IllegalArgumentException("invalid_filter");
            }
        }
        Object dedupeRaw = input.get("dedupe_by");
        if (dedupeRaw != null && !validTarget(dedupeRaw)) {
            throw new IllegalArgumentException("invalid_dedupe");
        }
        String dedupeField = dedupeRaw instanceof String value ? value : null;
        Object sortRaw = input.get("sort");
        Map<String, Object> sort = sortRaw == null ? null : object(sortRaw, "invalid_sort");
        if (sort != null && (!validTarget(sort.get("field"))
            || !("asc".equals(sort.getOrDefault("direction", "asc"))
                || "desc".equals(sort.get("direction"))))) {
            throw new IllegalArgumentException("invalid_sort");
        }
        String sortField = sort == null ? null : (String) sort.get("field");
        boolean descending = sort != null && "desc".equals(sort.get("direction"));
        Set<String> seen = new LinkedHashSet<>();
        List<Candidate> candidates = new ArrayList<>();
        int outputBytes = 2;
        boolean outputLimited = false;
        List<?> sourceRecords = rawRecords.subList(0, Math.min(rawRecords.size(), maxRecords));
        for (int ordinal = 0; ordinal < sourceRecords.size(); ordinal++) {
            Object record = sourceRecords.get(ordinal);
            boolean accepted = true;
            for (Object rawRule : filters) {
                if (!matches(record, object(rawRule, "invalid_filter"))) { accepted = false; break; }
            }
            if (!accepted) continue;
            Map<String, Object> output = new LinkedHashMap<>();
            Map<String, Integer> fieldSizes = new LinkedHashMap<>();
            int objectBytes = 2;
            boolean recordLimited = false;
            Object sortValue = null;
            Object dedupeValue = null;
            for (Object rawMapping : mappings) {
                Map<String, Object> mapping = object(rawMapping, "invalid_mapping");
                Object sourceRaw = mapping.get("pointer");
                Object targetRaw = mapping.get("target");
                Object conversion = mapping.get("type");
                if (!(sourceRaw instanceof String) || !validTarget(targetRaw)
                    || conversion != null && (!(conversion instanceof String)
                        || !CONVERSIONS.contains(conversion))) {
                    throw new IllegalArgumentException("invalid_mapping");
                }
                if (!validPointer(sourceRaw)) throw new IllegalArgumentException("invalid_json_pointer");
                String source = (String) sourceRaw;
                String target = (String) targetRaw;
                Object value;
                try { value = pointer(record, source); }
                catch (IllegalArgumentException error) {
                    if (!"missing_pointer".equals(error.getMessage())) throw error;
                    if (!mapping.containsKey("default")) continue;
                    value = mapping.get("default");
                }
                try {
                    Object converted = convert(value, (String) conversion);
                    if (target.equals(sortField)) sortValue = converted;
                    if (target.equals(dedupeField)) dedupeValue = converted;
                    if (recordLimited) continue;
                    int fieldSize = serializedBytes(target) + 1 + serializedBytes(converted);
                    Integer previousSize = fieldSizes.get(target);
                    int nextObjectBytes = objectBytes - (previousSize == null ? 0 : previousSize)
                        + fieldSize;
                    if (previousSize == null && !fieldSizes.isEmpty()) nextObjectBytes++;
                    if (nextObjectBytes > maxOutputBytes) {
                        recordLimited = true;
                        if (sortField == null) break;
                        continue;
                    }
                    output.put(target, converted);
                    fieldSizes.put(target, fieldSize);
                    objectBytes = nextObjectBytes;
                }
                catch (RuntimeException error) {
                    throw new IllegalArgumentException("conversion_failed");
                }
            }
            if (dedupeField != null && !seen.add(canonicalJson(
                recordLimited ? dedupeValue : output.get(dedupeField)
            ))) continue;
            if (recordLimited) {
                outputLimited = true;
                if (sortField == null) break;
            }
            Map<String, Object> candidateOutput = recordLimited ? null : output;
            int encodedBytes = recordLimited ? maxOutputBytes + 1 : objectBytes;
            if (sortField == null) {
                int addition = encodedBytes + (candidates.isEmpty() ? 0 : 1);
                if (outputBytes + addition > maxOutputBytes) {
                    outputLimited = true;
                    break;
                }
                candidates.add(new Candidate(output, encodedBytes, ordinal, ""));
                outputBytes += addition;
                continue;
            }

            Candidate candidate = new Candidate(
                candidateOutput, encodedBytes, ordinal,
                sortKey(recordLimited ? sortValue : output.get(sortField))
            );
            int low = 0;
            int high = candidates.size();
            while (low < high) {
                int middle = (low + high) >>> 1;
                Candidate existing = candidates.get(middle);
                int comparison = candidate.order.compareTo(existing.order);
                boolean before = comparison == 0
                    ? ordinal < existing.ordinal
                    : descending ? comparison > 0 : comparison < 0;
                if (before) high = middle;
                else low = middle + 1;
            }
            candidates.add(low, candidate);
            int boundedBytes = 2;
            for (int at = 0; at < candidates.size(); at++) {
                Candidate item = candidates.get(at);
                int addition = item.encodedBytes + (at == 0 ? 0 : 1);
                if (boundedBytes + addition > maxOutputBytes) {
                    item.output = null;
                    candidates.subList(at + 1, candidates.size()).clear();
                    outputLimited = true;
                    break;
                }
                boundedBytes += addition;
            }
        }
        List<Map<String, Object>> bounded = candidates.stream()
            .filter(item -> item.output != null).map(item -> item.output).toList();
        boolean inputLimited = rawRecords.size() > maxRecords;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("records", bounded);
        result.put("count", bounded.size());
        result.put("partial", inputLimited || outputLimited);
        result.put("checkpoint", inputLimited
            ? Map.of("reason", "input_limit", "next_index", maxRecords)
            : outputLimited ? Map.of("reason", "output_limit", "emitted", bounded.size()) : null);
        return result;
    }

    private static final class Candidate {
        private Map<String, Object> output;
        private final int encodedBytes;
        private final int ordinal;
        private final String order;

        private Candidate(Map<String, Object> output, int encodedBytes, int ordinal, String order) {
            this.output = output;
            this.encodedBytes = encodedBytes;
            this.ordinal = ordinal;
            this.order = order;
        }
    }

    private static Object pointer(Object value, String path) {
        if (!validPointer(path)) throw new IllegalArgumentException("invalid_json_pointer");
        if (path.isEmpty()) return value;
        Object current = value;
        for (String raw : path.substring(1).split("/", -1)) {
            String token = raw.replace("~1", "/").replace("~0", "~");
            if (current instanceof List<?> values && token.matches("[0-9]+")) {
                if (token.length() > 10) throw new IllegalArgumentException("missing_pointer");
                long parsed = Long.parseLong(token);
                if (parsed > Integer.MAX_VALUE) throw new IllegalArgumentException("missing_pointer");
                int index = (int) parsed;
                if (index >= values.size()) throw new IllegalArgumentException("missing_pointer");
                current = values.get(index);
            } else if (current instanceof Map<?, ?> map && map.containsKey(token)) current = map.get(token);
            else throw new IllegalArgumentException("missing_pointer");
        }
        return current;
    }

    private static Object convert(Object value, String kind) {
        if (kind == null) return value;
        return switch (kind) {
            case "string" -> String.valueOf(value);
            case "integer" -> {
                if (value instanceof Boolean) throw new IllegalArgumentException("invalid_integer");
                long converted;
                if (value instanceof String text) {
                    if (text.length() > 32 || !text.matches("-?(?:0|[1-9][0-9]*)")) {
                        throw new IllegalArgumentException("invalid_integer");
                    }
                    converted = Long.parseLong(text);
                } else if (value instanceof Number number) {
                    double decimal = number.doubleValue();
                    if (!Double.isFinite(decimal) || decimal != Math.rint(decimal)) {
                        throw new IllegalArgumentException("invalid_integer");
                    }
                    converted = number.longValue();
                } else throw new IllegalArgumentException("invalid_integer");
                if (Math.abs((double) converted) > 9_007_199_254_740_991D) {
                    throw new IllegalArgumentException("invalid_integer");
                }
                yield converted;
            }
            case "number" -> {
                if (value instanceof Boolean) throw new IllegalArgumentException("invalid_number");
                if (!(value instanceof Number) && (!(value instanceof String text)
                    || text.length() > 128
                    || !text.matches("-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"))) {
                    throw new IllegalArgumentException("invalid_number");
                }
                double converted = Double.parseDouble(String.valueOf(value));
                if (!Double.isFinite(converted)) throw new IllegalArgumentException("invalid_number");
                yield converted;
            }
            case "boolean" -> {
                if (Boolean.TRUE.equals(value) || "true".equals(value) || "1".equals(value)
                    || Long.valueOf(1).equals(value)) yield true;
                if (Boolean.FALSE.equals(value) || "false".equals(value) || "0".equals(value)
                    || Long.valueOf(0).equals(value)) yield false;
                throw new IllegalArgumentException("invalid_boolean");
            }
            case "datetime" -> {
                if (!(value instanceof String text)) throw new IllegalArgumentException("invalid_datetime");
                try { yield OffsetDateTime.parse(text).toInstant().toString(); }
                catch (RuntimeException error) { yield Instant.parse(text).toString(); }
            }
            default -> throw new IllegalArgumentException("unsupported_conversion");
        };
    }

    private static boolean matches(Object item, Map<String, Object> rule) {
        Object value = null;
        boolean exists = true;
        try { value = pointer(item, required(rule, "pointer")); }
        catch (IllegalArgumentException error) {
            if (!"missing_pointer".equals(error.getMessage())) throw error;
            exists = false;
        }
        String op = String.valueOf(rule.getOrDefault("op", "equals"));
        if ("exists".equals(op)) return exists == !Boolean.FALSE.equals(rule.get("value"));
        if ("equals".equals(op)) return exists && jsonEquals(value, rule.get("value"));
        throw new IllegalArgumentException("unsupported_filter");
    }

    private static String sortKey(Object value) {
        return value == null ? "1" : "0" + canonicalJson(value);
    }

    private static boolean jsonEquals(Object left, Object right) {
        if (left instanceof Boolean || right instanceof Boolean) return left instanceof Boolean
            && right instanceof Boolean && left.equals(right);
        if (left instanceof Number || right instanceof Number) return left instanceof Number a
            && right instanceof Number b && a.doubleValue() == b.doubleValue();
        if (left instanceof List<?> || right instanceof List<?>) {
            if (!(left instanceof List<?> a) || !(right instanceof List<?> b)
                || a.size() != b.size()) return false;
            for (int at = 0; at < a.size(); at++) {
                if (!jsonEquals(a.get(at), b.get(at))) return false;
            }
            return true;
        }
        if (left instanceof Map<?, ?> || right instanceof Map<?, ?>) {
            if (!(left instanceof Map<?, ?> a) || !(right instanceof Map<?, ?> b)
                || !a.keySet().equals(b.keySet())) return false;
            for (Object key : a.keySet()) {
                if (!jsonEquals(a.get(key), b.get(key))) return false;
            }
            return true;
        }
        return java.util.Objects.equals(left, right);
    }

    private static String canonicalJson(Object value) {
        return Json.stringify(canonicalize(value));
    }

    private static Object canonicalize(Object value) {
        if (value instanceof Map<?, ?> map) {
            List<String> keys = new ArrayList<>();
            for (Object key : map.keySet()) {
                if (!(key instanceof String text)) {
                    throw new IllegalArgumentException("output_not_json");
                }
                keys.add(text);
            }
            keys.sort(String::compareTo);
            Map<String, Object> result = new LinkedHashMap<>();
            for (String key : keys) result.put(key, canonicalize(map.get(key)));
            return result;
        }
        if (value instanceof List<?> values) {
            List<Object> result = new ArrayList<>();
            for (Object item : values) result.add(canonicalize(item));
            return result;
        }
        return value;
    }

    private static boolean validPointer(Object raw) {
        if (!(raw instanceof String path)
            || path.getBytes(StandardCharsets.UTF_8).length > MAX_POINTER_BYTES) return false;
        if (path.isEmpty()) return true;
        if (!path.startsWith("/")) return false;
        String[] tokens = path.substring(1).split("/", -1);
        if (tokens.length > MAX_POINTER_TOKENS) return false;
        for (String rawToken : tokens) {
            if (rawToken.matches(".*~(?:[^01]|$).*")
                || rawToken.replace("~1", "/").replace("~0", "~")
                    .getBytes(StandardCharsets.UTF_8).length > MAX_POINTER_TOKEN_BYTES) return false;
        }
        return true;
    }

    private static boolean validTarget(Object raw) {
        if (!(raw instanceof String target)) return false;
        int bytes = target.getBytes(StandardCharsets.UTF_8).length;
        if (bytes < 1 || bytes > MAX_TARGET_BYTES) return false;
        return target.chars().noneMatch(character -> character < 0x20);
    }

    private static int serializedBytes(Object value) {
        try {
            return Json.stringify(value).getBytes(StandardCharsets.UTF_8).length;
        } catch (RuntimeException | StackOverflowError error) {
            throw new IllegalArgumentException("output_not_json");
        }
    }

    private static int bounded(Map<String, Object> input, String key, int fallback, int maximum) {
        if (!input.containsKey(key)) return fallback;
        Object raw = input.get(key);
        if (!(raw instanceof Number number)) throw new IllegalArgumentException("invalid_limits");
        double decimal = number.doubleValue();
        if (!Double.isFinite(decimal) || decimal != Math.rint(decimal)
            || decimal < 1 || decimal > maximum) throw new IllegalArgumentException("invalid_limits");
        return (int) decimal;
    }

    private static String required(Map<String, Object> value, String key) {
        Object raw = value.get(key);
        if (!(raw instanceof String text) || text.isEmpty()) throw new IllegalArgumentException("invalid_" + key);
        return text;
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
