import java.lang.reflect.Array;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.Statement;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/** Bounded read-only MySQL snapshot. */
public class Adapter {
    // MySQL 数据查询：可修改的配置集中在这里。
    // 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    // 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
    // MYSQL_DSN：MySQL 连接字符串（包含账号密码）。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // sql: 填写一条 SELECT；动态值通过 params 绑定，不要拼接用户输入。
        // params: 按 SQL 占位符顺序填写参数。
        // max_rows: 最多读取的行数。
        // max_output_bytes: 返回结果大小上限，单位字节。
        // max_cell_bytes: 单个单元格大小上限，单位字节。
        // batch_size: 每批处理的记录数。
        // timeout_seconds: 单次请求超时时间，单位秒。
        return (Map<String, Object>) Json.parse("""
            {
              "sql": "SELECT id, name FROM example_items WHERE updated_at >= ?",
              "params": [
                "2026-01-01T00:00:00Z"
              ],
              "max_rows": 5000,
              "max_output_bytes": 4194304,
              "max_cell_bytes": 1048576,
              "batch_size": 500,
              "timeout_seconds": 30
            }
            """);
    }

    public Object handle(Context context, Object rawInput) {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        Map<String, Object> input = object(rawInput);
        String sql = checkedQuery(input.get("sql"));
        Object rawParams = input.get("params");
        if (rawParams != null && !(rawParams instanceof List<?>)) {
            throw new IllegalArgumentException("params_must_be_array");
        }
        List<?> params = rawParams instanceof List<?> values ? values : List.of();
        if (params.size() > 64) throw new IllegalArgumentException("params_must_be_array");
        int maxRows = positive(input.get("max_rows"), 5_000, 100_000);
        int batchSize = positive(input.get("batch_size"), 500, 5_000);
        int maxOutput = positive(input.get("max_output_bytes"), 4_194_304, 16_777_216);
        int maxCell = positive(input.get("max_cell_bytes"), 1_048_576, 8_388_608);
        int timeout = positive(input.get("timeout_seconds"), 30, 300);
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String dsn = context.secrets.get("MYSQL_DSN");
        if (dsn == null || dsn.isBlank()) throw new IllegalArgumentException("missing_credential");
        List<Map<String, Object>> rows = new ArrayList<>();
        Connection connection = null;
        String failureCode = "database_connection_failed";
        int outputBytes = 2;
        boolean partial = false;
        try {
            connection = DriverManager.getConnection(dsn);
            failureCode = "database_query_failed";
            connection.setReadOnly(true);
            connection.setAutoCommit(false);
            try (Statement control = connection.createStatement()) {
                control.execute("SET SESSION TRANSACTION READ ONLY");
                control.execute("SET time_zone = '+00:00'");
                control.execute("SET SESSION MAX_EXECUTION_TIME=" + timeout * 1000);
            }

            try (PreparedStatement statement = connection.prepareStatement(
                sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY
            )) {
                statement.setQueryTimeout(timeout);
                statement.setFetchSize(batchSize);
                statement.setMaxRows(maxRows + 1);
                for (int at = 0; at < params.size(); at++) statement.setObject(at + 1, params.get(at));
                try (ResultSet result = statement.executeQuery()) {
                    ResultSetMetaData metadata = result.getMetaData();
                    List<String> names = columnNames(metadata);
                    while (result.next()) {
                        if (rows.size() >= maxRows) {
                            partial = true;
                            break;
                        }
                        Map<String, Object> row = new LinkedHashMap<>();
                        for (int column = 1; column <= metadata.getColumnCount(); column++) {
                            Object value;
                            try {
                                value = normalizeCell(databaseValue(result, metadata, column), 0);
                            } catch (UnsupportedCell error) {
                                partial = true;
                                break;
                            }
                            if (Json.stringify(value).getBytes(StandardCharsets.UTF_8).length > maxCell) {
                                partial = true;
                                break;
                            }
                            row.put(names.get(column - 1), value);
                        }
                        if (partial) break;
                        int encodedBytes = Json.stringify(row).getBytes(StandardCharsets.UTF_8).length
                            + (rows.isEmpty() ? 0 : 1);
                        if (outputBytes + encodedBytes > maxOutput) {
                            partial = true;
                            break;
                        }
                        rows.add(row);
                        outputBytes += encodedBytes;
                    }
                }
            }
            connection.rollback();
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("rows", rows); result.put("count", rows.size());
            result.put("partial", partial);
            result.put("checkpoint", partial
                ? Map.of("row_offset", rows.size()) : null);
            return result;
        } catch (Exception ignored) {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("rows", rows); result.put("count", rows.size());
            result.put("partial", true); result.put("error", failureCode);
            return result;
        } finally {
            closeSafely(connection);
        }
    }

    private static void closeSafely(Connection connection) {
        if (connection == null) return;
        try { if (!connection.getAutoCommit()) connection.rollback(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
        try { connection.close(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
    }

    private static List<String> columnNames(ResultSetMetaData metadata) throws Exception {
        List<String> names = new ArrayList<>();
        Set<String> unique = new HashSet<>();
        for (int column = 1; column <= metadata.getColumnCount(); column++) {
            String name = metadata.getColumnLabel(column);
            if (!unique.add(name)) throw new IllegalArgumentException("duplicate_column_label");
            names.add(name);
        }
        return names;
    }

    private static Object databaseValue(
        ResultSet result, ResultSetMetaData metadata, int column
    ) throws Exception {
        String typeName = metadata.getColumnTypeName(column);
        if (typeName != null && typeName.equalsIgnoreCase("timestamptz")) {
            return result.getObject(column, java.time.OffsetDateTime.class);
        }
        if (typeName != null && typeName.equalsIgnoreCase("timetz")) {
            return result.getObject(column, java.time.OffsetTime.class);
        }
        return switch (metadata.getColumnType(column)) {
            case java.sql.Types.DATE -> result.getObject(column, java.time.LocalDate.class);
            case java.sql.Types.TIME -> result.getObject(column, java.time.LocalTime.class);
            case java.sql.Types.TIME_WITH_TIMEZONE ->
                result.getObject(column, java.time.OffsetTime.class);
            case java.sql.Types.TIMESTAMP ->
                result.getObject(column, java.time.LocalDateTime.class);
            case java.sql.Types.TIMESTAMP_WITH_TIMEZONE ->
                result.getObject(column, java.time.OffsetDateTime.class);
            default -> result.getObject(column);
        };
    }

    private static Object normalizeCell(Object value, int depth) {
        if (depth > 32) throw new UnsupportedCell();
        if (value == null || value instanceof String || value instanceof Boolean
            || value instanceof Byte || value instanceof Short || value instanceof Integer
            || value instanceof Long) return value;
        if (value instanceof Character character) return character.toString();
        if (value instanceof BigDecimal || value instanceof BigInteger || value instanceof UUID) {
            return value.toString();
        }
        if (value instanceof Float number) {
            if (!Float.isFinite(number)) throw new UnsupportedCell();
            return number;
        }
        if (value instanceof Double number) {
            if (!Double.isFinite(number)) throw new UnsupportedCell();
            return number;
        }
        if (value instanceof java.sql.Timestamp timestamp) {
            return timestamp.toLocalDateTime().toString();
        }
        if (value instanceof java.sql.Date sqlDate) return sqlDate.toLocalDate().toString();
        if (value instanceof java.sql.Time sqlTime) return sqlTime.toLocalTime().toString();
        if (value instanceof java.time.temporal.TemporalAccessor temporal) {
            return temporal.toString().replaceFirst("Z$", "+00:00");
        }
        if (value instanceof java.util.Date legacyDate) {
            return legacyDate.toInstant().toString().replaceFirst("Z$", "+00:00");
        }
        if (value instanceof byte[] bytes) {
            return Map.of("$binary_base64", Base64.getEncoder().encodeToString(bytes));
        }
        if (value instanceof ByteBuffer buffer) {
            ByteBuffer copy = buffer.duplicate();
            byte[] bytes = new byte[copy.remaining()];
            copy.get(bytes);
            return Map.of("$binary_base64", Base64.getEncoder().encodeToString(bytes));
        }
        if (value instanceof Map<?, ?> rawMap) {
            Map<String, Object> normalized = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : rawMap.entrySet()) {
                if (!(entry.getKey() instanceof String key)) throw new UnsupportedCell();
                normalized.put(key, normalizeCell(entry.getValue(), depth + 1));
            }
            return normalized;
        }
        if (value instanceof Iterable<?> values) {
            List<Object> normalized = new ArrayList<>();
            for (Object item : values) normalized.add(normalizeCell(item, depth + 1));
            return normalized;
        }
        if (value.getClass().isArray()) {
            List<Object> normalized = new ArrayList<>();
            for (int at = 0; at < Array.getLength(value); at++) {
                normalized.add(normalizeCell(Array.get(value, at), depth + 1));
            }
            return normalized;
        }
        throw new UnsupportedCell();
    }

    private static final class UnsupportedCell extends RuntimeException {}

    private static String checkedQuery(Object raw) {
        if (!(raw instanceof String sql) || !sql.matches("(?is)^\\s*select\\b.*")
            || sql.contains(";")
            || sql.matches("(?is).*\\b(insert|update|delete|merge|call|execute|create|alter|drop|truncate|copy)\\b.*")) {
            throw new IllegalArgumentException("single_select_required");
        }
        return sql;
    }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number number && number.intValue() > 0 ? Math.min(number.intValue(), maximum) : fallback; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException("input_must_be_object"); return (Map<String, Object>) value; }
}
