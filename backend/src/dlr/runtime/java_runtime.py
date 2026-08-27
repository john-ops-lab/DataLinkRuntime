# ruff: noqa: E501
"""Embedded Java runtime support compiled beside each immutable Version."""

SOURCE = r"""
import java.nio.charset.StandardCharsets;
import java.nio.file.LinkOption;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

class Secrets {
    public String get(String key) { return System.getenv("DLR_SECRET_" + key); }
}

class Logger {
    public void info(Object message) { System.out.println("[INFO] " + message); }
    public void warn(Object message) { System.err.println("[WARN] " + message); }
    public void error(Object message) { System.err.println("[ERROR] " + message); }
}

final class InputFile {
    public final int ordinal;
    public final Path path;
    public final String originalName;
    public final String contentType;
    public final long sizeBytes;
    public final String sha256;

    InputFile(
        int ordinal,
        Path path,
        String originalName,
        String contentType,
        long sizeBytes,
        String sha256
    ) {
        this.ordinal = ordinal;
        this.path = path;
        this.originalName = originalName;
        this.contentType = contentType;
        this.sizeBytes = sizeBytes;
        this.sha256 = sha256;
    }
}

class Context {
    public final Map<String, Object> config;
    public final List<InputFile> inputFiles;
    public final Secrets secrets = new Secrets();
    public final Logger logger = new Logger();
    Context(Map<String, Object> config) { this(config, List.of()); }
    Context(Map<String, Object> config, List<InputFile> inputFiles) {
        this.config = config;
        this.inputFiles = List.copyOf(inputFiles);
    }
}

final class InputManifestException extends Exception {
    final String code;
    InputManifestException(String code) {
        super(code);
        this.code = code;
    }
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
        List<InputFile> inputFiles;
        try {
            inputFiles = readInputFiles(workspace);
        } catch (InputManifestException error) {
            System.err.println("DLR_INPUT_ERROR:" + error.code);
            System.exit(1);
            return;
        }
        Object output = new Adapter().handle(new Context(config, inputFiles), input);
        Files.writeString(
            workspace.resolve("output.json"), Json.stringify(output), StandardCharsets.UTF_8
        );
    }

    private static List<InputFile> readInputFiles(Path workspace) throws InputManifestException {
        if (!workspace.isAbsolute() || !workspace.getFileName().toString().matches("dlr-exec-[1-9][0-9]*")) {
            throw new InputManifestException("input_artifact_not_ready");
        }
        long executionId;
        try {
            executionId = Long.parseLong(workspace.getFileName().toString().substring(9));
        } catch (NumberFormatException error) {
            throw new InputManifestException("input_artifact_not_ready");
        }
        Path inputDirectory = workspace.resolve("input");
        Path manifestPath = workspace.resolve("input_manifest.json");
        Object manifestValue;
        try {
            if (Files.isSymbolicLink(inputDirectory)
                || !Files.isDirectory(inputDirectory, LinkOption.NOFOLLOW_LINKS)
                || Files.isSymbolicLink(manifestPath)
                || !Files.isRegularFile(manifestPath, LinkOption.NOFOLLOW_LINKS)) {
                throw new InputManifestException("input_artifact_not_ready");
            }
            manifestValue = Json.parse(Files.readString(manifestPath));
        } catch (InputManifestException error) {
            throw error;
        } catch (Exception error) {
            throw new InputManifestException("input_artifact_not_ready");
        }
        if (!(manifestValue instanceof Map<?, ?> rawManifest)
            || rawManifest.size() != 2
            || !rawManifest.containsKey("execution_id")
            || !rawManifest.containsKey("files")
            || !(rawManifest.get("files") instanceof List<?> rawFiles)
            || rawFiles.size() > 8) {
            throw new InputManifestException("input_artifact_not_ready");
        }
        Long manifestExecutionId = integerValue(rawManifest.get("execution_id"));
        if (manifestExecutionId == null || manifestExecutionId != executionId) {
            throw new InputManifestException("input_artifact_not_ready");
        }
        List<InputFile> result = new ArrayList<>();
        for (int expectedOrdinal = 0; expectedOrdinal < rawFiles.size(); expectedOrdinal++) {
            Object rawValue = rawFiles.get(expectedOrdinal);
            if (!(rawValue instanceof Map<?, ?> rawFile)
                || rawFile.size() != 7
                || !rawFile.keySet().containsAll(List.of(
                    "artifact_id", "ordinal", "mount_name", "original_filename",
                    "content_type", "size_bytes", "sha256"))) {
                throw new InputManifestException("input_artifact_not_ready");
            }
            Object artifactIdValue = rawFile.get("artifact_id");
            Object ordinalValue = rawFile.get("ordinal");
            Object mountValue = rawFile.get("mount_name");
            Object originalNameValue = rawFile.get("original_filename");
            Object contentTypeValue = rawFile.get("content_type");
            Object sizeValue = rawFile.get("size_bytes");
            Object shaValue = rawFile.get("sha256");
            Long artifactId = integerValue(artifactIdValue);
            Long ordinal = integerValue(ordinalValue);
            Long sizeBytes = integerValue(sizeValue);
            if (artifactId == null || artifactId <= 0
                || ordinal == null || ordinal != expectedOrdinal
                || ordinal < 0 || ordinal > 7
                || !(mountValue instanceof String mountName)
                || !mountName.matches("input-[0-9]{2}")
                || Integer.parseInt(mountName.substring(6)) != expectedOrdinal
                || !(originalNameValue instanceof String originalName)
                || !(contentTypeValue instanceof String contentType)
                || sizeBytes == null || sizeBytes < 0
                || !(shaValue instanceof String sha256) || !sha256.matches("[0-9a-f]{64}")) {
                throw new InputManifestException("input_artifact_not_ready");
            }
            Path target = inputDirectory.resolve(mountName).normalize();
            if (!target.isAbsolute() || !target.getParent().equals(inputDirectory)) {
                throw new InputManifestException("input_artifact_not_ready");
            }
            verifyInputFile(target, sizeBytes, sha256);
            result.add(new InputFile(
                expectedOrdinal, target, originalName, contentType, sizeBytes, sha256
            ));
        }
        return List.copyOf(result);
    }

    private static Long integerValue(Object value) {
        if (!(value instanceof Number number)) return null;
        double decimal = number.doubleValue();
        if (!Double.isFinite(decimal) || decimal != Math.rint(decimal)
            || decimal < Long.MIN_VALUE || decimal > Long.MAX_VALUE) {
            return null;
        }
        long integer = number.longValue();
        return (double) integer == decimal ? integer : null;
    }

    private static void verifyInputFile(Path target, long expectedSize, String expectedSha)
        throws InputManifestException {
        try {
            if (Files.isSymbolicLink(target)
                || !Files.isRegularFile(target, LinkOption.NOFOLLOW_LINKS)) {
                throw new InputManifestException("input_artifact_not_ready");
            }
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long actualSize = 0;
            try (var stream = Files.newInputStream(target, LinkOption.NOFOLLOW_LINKS)) {
                byte[] buffer = new byte[1024 * 1024];
                int count;
                while ((count = stream.read(buffer)) != -1) {
                    actualSize += count;
                    digest.update(buffer, 0, count);
                }
            }
            String actualSha = HexFormat.of().formatHex(digest.digest());
            if (actualSize != expectedSize || !actualSha.equals(expectedSha)) {
                throw new InputManifestException("input_artifact_checksum_mismatch");
            }
        } catch (InputManifestException error) {
            throw error;
        } catch (Exception error) {
            throw new InputManifestException("input_artifact_not_ready");
        }
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
