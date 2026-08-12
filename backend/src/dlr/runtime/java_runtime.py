# ruff: noqa: E501
"""Embedded Java runtime support compiled beside each immutable Version."""

SOURCE = r"""
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

class Secrets {
    public String get(String key) { return System.getenv("DLR_SECRET_" + key); }
}

class Logger {
    public void info(Object message) { System.out.println(String.valueOf(message)); }
    public void warn(Object message) { System.err.println(String.valueOf(message)); }
    public void error(Object message) { System.err.println(String.valueOf(message)); }
}

class Context {
    public final Map<String, Object> config;
    public final Secrets secrets = new Secrets();
    public final Logger logger = new Logger();
    Context(Map<String, Object> config) { this.config = config; }
}

public class DlrRuntime {
    public static void main(String[] args) throws Exception {
        Path workspace = Path.of(args[0]);
        Object input = Json.parse(Files.readString(workspace.resolve("input.json")));
        Object configValue = Json.parse(Files.readString(workspace.resolve("runtime_config.json")));
        if (!(configValue instanceof Map<?, ?> rawConfig)) {
            throw new IllegalArgumentException("runtime config must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> config = (Map<String, Object>) rawConfig;
        Object output = new Adapter().handle(new Context(config), input);
        Files.writeString(
            workspace.resolve("output.json"), Json.stringify(output), StandardCharsets.UTF_8
        );
    }
}

final class Json {
    private final String text;
    private int at;
    private Json(String text) { this.text = text; }
    static Object parse(String text) {
        Json parser = new Json(text);
        Object value = parser.value();
        parser.space();
        if (parser.at != text.length()) throw parser.error("unexpected trailing content");
        return value;
    }
    static String stringify(Object value) {
        StringBuilder out = new StringBuilder();
        write(value, out);
        return out.toString();
    }
    private Object value() {
        space();
        if (at >= text.length()) throw error("unexpected end of JSON");
        return switch (text.charAt(at)) {
            case 'n' -> literal("null", null);
            case 't' -> literal("true", Boolean.TRUE);
            case 'f' -> literal("false", Boolean.FALSE);
            case '"' -> string();
            case '[' -> array();
            case '{' -> object();
            default -> number();
        };
    }
    private Object literal(String expected, Object value) {
        if (!text.startsWith(expected, at)) throw error("invalid literal");
        at += expected.length();
        return value;
    }
    private String string() {
        at++;
        StringBuilder out = new StringBuilder();
        while (at < text.length()) {
            char c = text.charAt(at++);
            if (c == '"') return out.toString();
            if (c != '\\') { out.append(c); continue; }
            if (at >= text.length()) throw error("invalid escape");
            char escaped = text.charAt(at++);
            switch (escaped) {
                case '"', '\\', '/' -> out.append(escaped);
                case 'b' -> out.append('\b'); case 'f' -> out.append('\f');
                case 'n' -> out.append('\n'); case 'r' -> out.append('\r');
                case 't' -> out.append('\t');
                case 'u' -> {
                    if (at + 4 > text.length()) throw error("invalid unicode escape");
                    out.append((char) Integer.parseInt(text.substring(at, at + 4), 16)); at += 4;
                }
                default -> throw error("invalid escape");
            }
        }
        throw error("unterminated string");
    }
    private List<Object> array() {
        at++;
        List<Object> values = new ArrayList<>();
        space();
        if (take(']')) return values;
        do { values.add(value()); space(); } while (take(','));
        if (!take(']')) throw error("expected ]");
        return values;
    }
    private Map<String, Object> object() {
        at++;
        Map<String, Object> values = new LinkedHashMap<>();
        space();
        if (take('}')) return values;
        do {
            space();
            if (at >= text.length() || text.charAt(at) != '"') throw error("expected key");
            String key = string(); space();
            if (!take(':')) throw error("expected :");
            values.put(key, value()); space();
        } while (take(','));
        if (!take('}')) throw error("expected }");
        return values;
    }
    private Number number() {
        int start = at;
        if (take('-')) {}
        while (at < text.length() && Character.isDigit(text.charAt(at))) at++;
        boolean decimal = false;
        if (take('.')) { decimal = true; while (at < text.length() && Character.isDigit(text.charAt(at))) at++; }
        if (at < text.length() && (text.charAt(at) == 'e' || text.charAt(at) == 'E')) {
            decimal = true; at++; if (at < text.length() && (text.charAt(at) == '+' || text.charAt(at) == '-')) at++;
            while (at < text.length() && Character.isDigit(text.charAt(at))) at++;
        }
        try { return decimal ? Double.valueOf(text.substring(start, at)) : Long.valueOf(text.substring(start, at)); }
        catch (NumberFormatException error) { throw error("invalid number"); }
    }
    private void space() { while (at < text.length() && Character.isWhitespace(text.charAt(at))) at++; }
    private boolean take(char c) { if (at < text.length() && text.charAt(at) == c) { at++; return true; } return false; }
    private IllegalArgumentException error(String message) { return new IllegalArgumentException(message + " at " + at); }
    private static void write(Object value, StringBuilder out) {
        if (value == null) { out.append("null"); return; }
        if (value instanceof String text) { quote(text, out); return; }
        if (value instanceof Boolean) { out.append(value); return; }
        if (value instanceof Number number) {
            if (number instanceof Double d && !Double.isFinite(d)) throw new IllegalArgumentException("non-finite output number");
            if (number instanceof Float f && !Float.isFinite(f)) throw new IllegalArgumentException("non-finite output number");
            out.append(number); return;
        }
        if (value instanceof Map<?, ?> map) {
            out.append('{'); boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) throw new IllegalArgumentException("output object keys must be strings");
                if (!first) out.append(','); first = false; quote(key, out); out.append(':'); write(entry.getValue(), out);
            }
            out.append('}'); return;
        }
        if (value instanceof Iterable<?> values) {
            out.append('['); boolean first = true;
            for (Object item : values) { if (!first) out.append(','); first = false; write(item, out); }
            out.append(']'); return;
        }
        if (value.getClass().isArray()) {
            out.append('['); int length = java.lang.reflect.Array.getLength(value);
            for (int i = 0; i < length; i++) { if (i > 0) out.append(','); write(java.lang.reflect.Array.get(value, i), out); }
            out.append(']'); return;
        }
        throw new IllegalArgumentException("adapter output is not JSON-serializable: " + value.getClass().getName());
    }
    private static void quote(String text, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\""); case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b"); case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n"); case '\r' -> out.append("\\r"); case '\t' -> out.append("\\t");
                default -> { if (c < 0x20) out.append(String.format("\\u%04x", (int) c)); else out.append(c); }
            }
        }
        out.append('"');
    }
}
"""
