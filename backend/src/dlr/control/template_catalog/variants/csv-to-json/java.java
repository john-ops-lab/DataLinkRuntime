import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.Charset;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Bounded CSV to JSON conversion without Managed Input coupling. */
public class Adapter {
    // CSV 转 JSON：可修改的配置集中在这里。
    // 运行时提供待处理的数据或文件；处理规则在下面配置。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // encoding: CSV 字符编码，例如 utf-8 或 utf-8-sig。
        // delimiter: CSV 字段分隔符，默认逗号。
        // header: 是否把首行作为字段名。
        // skip_empty: 是否跳过空行。
        // max_input_bytes: 输入大小上限，单位字节。
        // max_output_bytes: 返回结果大小上限，单位字节。
        // max_rows: 最多读取的行数。
        // max_columns: 最多读取的列数。
        // max_field_bytes: 单个字段大小上限，单位字节。
        return (Map<String, Object>) Json.parse("""
            {
              "encoding": "utf-8-sig",
              "delimiter": ",",
              "header": true,
              "skip_empty": true,
              "max_input_bytes": 2097152,
              "max_output_bytes": 4194304,
              "max_rows": 10000,
              "max_columns": 200,
              "max_field_bytes": 65536
            }
            """);
    }

    public Object handle(Context context, Object rawInput) throws Exception {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        try {
            return run(context, rawInput);
        } catch (Exception error) {
            String code = error.getMessage();
            if (code != null && STABLE_ERRORS.contains(code)) throw new IllegalArgumentException(code);
            throw new IllegalArgumentException("csv_operation_failed");
        }
    }

    private Object run(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput);
        int maxInput = positive(input.get("max_input_bytes"), 2_097_152, 16_777_216);
        int maxOutput = positive(input.get("max_output_bytes"), 4_194_304, 16_777_216);
        int maxRows = positive(input.get("max_rows"), 10_000, 100_000);
        int maxColumns = positive(input.get("max_columns"), 200, 2_000);
        int maxField = positive(input.get("max_field_bytes"), 65_536, 1_048_576);
        Charset charset;
        String encoding = String.valueOf(input.getOrDefault("encoding", "UTF-8-SIG"));
        try {
            charset = encoding.equalsIgnoreCase("UTF-8-SIG")
                ? StandardCharsets.UTF_8 : Charset.forName(encoding);
        } catch (Exception error) {
            throw new IllegalArgumentException("invalid_encoding_or_content");
        }
        byte[] bytes;
        if (input.get("content") instanceof String content) bytes = content.getBytes(charset);
        else if (!context.inputFiles.isEmpty()) {
            InputFile item = context.inputFiles.get(0);
            if (item.sizeBytes > maxInput) throw new IllegalArgumentException("input_too_large");
            bytes = Files.readAllBytes(item.path);
        } else throw new IllegalArgumentException("csv_content_required");
        if (bytes.length > maxInput) throw new IllegalArgumentException("input_too_large");
        String text;
        try {
            text = charset.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes))
                .toString();
        } catch (CharacterCodingException error) {
            throw new IllegalArgumentException("invalid_encoding_or_content");
        }
        if (text.startsWith("\ufeff")) text = text.substring(1);
        String delimiterText = String.valueOf(input.getOrDefault("delimiter", ","));
        if (delimiterText.codePointCount(0, delimiterText.length()) != 1) {
            throw new IllegalArgumentException("invalid_delimiter");
        }
        int delimiter = delimiterText.codePointAt(0);
        List<String> explicitHeaders = null;
        if (input.get("headers") != null) {
            if (!(input.get("headers") instanceof List<?> values)) {
                throw new IllegalArgumentException("invalid_headers");
            }
            explicitHeaders = new ArrayList<>();
            for (Object value : values) {
                if (!(value instanceof String name)) throw new IllegalArgumentException("invalid_headers");
                explicitHeaders.add(name);
            }
            validateHeaders(explicitHeaders, maxColumns, maxField);
        }
        final List<String> initialHeaders = explicitHeaders;
        final class Accumulator implements RowConsumer {
            private List<String> headers = initialHeaders;
            private final List<Object> rows = new ArrayList<>();
            private int outputBytes = 2;
            private Map<String, Object> checkpoint;

            @Override
            public boolean accept(List<String> row, int physicalRow) {
                if (!Boolean.FALSE.equals(input.get("skip_empty"))
                    && row.stream().allMatch(String::isBlank)) return true;
                if (headers == null && !Boolean.FALSE.equals(input.get("header"))) {
                    headers = row;
                    validateHeaders(headers, maxColumns, maxField);
                    return true;
                }
                Object item;
                if (headers == null) item = row;
                else {
                    if (row.size() > headers.size()) {
                        throw new IllegalArgumentException("row_has_extra_columns");
                    }
                    Map<String, Object> value = new LinkedHashMap<>();
                    for (int column = 0; column < headers.size(); column++) {
                        value.put(headers.get(column), column < row.size() ? row.get(column) : null);
                    }
                    item = value;
                }
                int encoded = Json.stringify(item).getBytes(StandardCharsets.UTF_8).length
                    + (rows.isEmpty() ? 0 : 1);
                if (rows.size() >= maxRows || outputBytes + encoded > maxOutput) {
                    checkpoint = Map.of("next_physical_row", physicalRow);
                    return false;
                }
                rows.add(item);
                outputBytes += encoded;
                return true;
            }
        }
        Accumulator accumulator = new Accumulator();
        parse(text, delimiter, maxColumns, maxField, accumulator);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("rows", accumulator.rows);
        result.put("count", accumulator.rows.size());
        result.put("partial", accumulator.checkpoint != null);
        result.put("checkpoint", accumulator.checkpoint);
        result.put("encoding", charset.name());
        result.put("delimiter", delimiterText);
        return result;
    }

    private static final Set<String> STABLE_ERRORS = Set.of(
        "input_must_be_object", "input_too_large", "csv_content_required",
        "invalid_encoding_or_content", "invalid_delimiter", "invalid_headers",
        "invalid_or_duplicate_header", "invalid_quote", "unterminated_quote",
        "column_limit_exceeded", "field_limit_exceeded", "row_has_extra_columns"
    );

    @FunctionalInterface
    private interface RowConsumer {
        boolean accept(List<String> row, int physicalRow);
    }

    private static void parse(
        String text, int delimiter, int maxColumns, int maxField, RowConsumer consumer
    ) {
        List<String> row = new ArrayList<>();
        StringBuilder field = new StringBuilder();
        int fieldBytes = 0;
        boolean quoted = false;
        int physicalRow = 0;
        for (int at = 0; at < text.length();) {
            int value = text.codePointAt(at);
            int width = Character.charCount(value);
            if (quoted) {
                if (value == '"' && at + 1 < text.length() && text.charAt(at + 1) == '"') {
                    field.append('"');
                    fieldBytes = checkedFieldBytes(fieldBytes, '"', maxField);
                    at += 2;
                    continue;
                } else if (value == '"') quoted = false;
                else {
                    field.appendCodePoint(value);
                    fieldBytes = checkedFieldBytes(fieldBytes, value, maxField);
                }
            } else if (value == '"') {
                if (!field.isEmpty()) throw new IllegalArgumentException("invalid_quote");
                quoted = true;
            } else if (value == delimiter) {
                row.add(field.toString());
                if (row.size() > maxColumns) {
                    throw new IllegalArgumentException("column_limit_exceeded");
                }
                field.setLength(0);
                fieldBytes = 0;
            } else if (value == '\n') {
                row.add(field.toString());
                if (row.size() > maxColumns) {
                    throw new IllegalArgumentException("column_limit_exceeded");
                }
                physicalRow++;
                if (!consumer.accept(row, physicalRow)) return;
                row = new ArrayList<>();
                field.setLength(0);
                fieldBytes = 0;
            } else if (value == '\r' && at + width < text.length()
                && text.codePointAt(at + width) == '\n') {
                // CR in a CRLF record terminator is not part of the field.
            } else {
                field.appendCodePoint(value);
                fieldBytes = checkedFieldBytes(fieldBytes, value, maxField);
            }
            at += width;
        }
        if (quoted) throw new IllegalArgumentException("unterminated_quote");
        if (!field.isEmpty() || !row.isEmpty()) {
            row.add(field.toString());
            if (row.size() > maxColumns) {
                throw new IllegalArgumentException("column_limit_exceeded");
            }
            consumer.accept(row, physicalRow + 1);
        }
    }

    private static int checkedFieldBytes(int current, int codePoint, int maximum) {
        int next = current + (codePoint <= 0x7f ? 1 : codePoint <= 0x7ff ? 2
            : codePoint <= 0xffff ? 3 : 4);
        if (next > maximum) throw new IllegalArgumentException("field_limit_exceeded");
        return next;
    }

    private static void validateHeaders(List<String> headers, int maxColumns, int maxField) {
        if (headers.isEmpty() || new LinkedHashSet<>(headers).size() != headers.size()
            || headers.stream().anyMatch(String::isBlank)) {
            throw new IllegalArgumentException("invalid_or_duplicate_header");
        }
        if (headers.size() > maxColumns) {
            throw new IllegalArgumentException("column_limit_exceeded");
        }
        if (headers.stream().anyMatch(
            value -> value.getBytes(StandardCharsets.UTF_8).length > maxField
        )) {
            throw new IllegalArgumentException("field_limit_exceeded");
        }
    }

    private static int positive(Object raw, int fallback, int maximum) {
        return raw instanceof Number number && number.intValue() > 0
            ? Math.min(number.intValue(), maximum) : fallback;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> object(Object raw) {
        if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException("input_must_be_object");
        return (Map<String, Object>) value;
    }
}
