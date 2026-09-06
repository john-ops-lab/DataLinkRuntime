import com.jcraft.jsch.ChannelSftp;
import com.jcraft.jsch.HostKeyRepository;
import com.jcraft.jsch.JSch;
import com.jcraft.jsch.JSchException;
import com.jcraft.jsch.Session;
import com.jcraft.jsch.SftpATTRS;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Host-key-verified, root-confined SFTP list and bounded read. */
public class Adapter {
    // SFTP 文件读取：可修改的配置集中在这里。
    // 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    // 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
    // SFTP_USERNAME：SFTP 用户名。
    // SFTP_PASSWORD：SFTP 密码（密码和私钥二选一）。
    // SFTP_PRIVATE_KEY：SFTP 私钥全文（密码和私钥二选一）。
    // SFTP_PRIVATE_KEY_PASSPHRASE：私钥口令，仅加密私钥需要。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // host: SFTP 服务器地址。
        // port: SFTP 端口，默认 22。
        // host_fingerprint_sha256: 填写服务器管理员提供的 SSH 主机公钥 SHA256 指纹，用于确认服务器身份。
        // base_directory: 允许读取的服务器目录。
        // path: 相对于 base_directory 的目录。
        // start_at: 继续读取时填写上次 checkpoint 返回的文件位置；首次留空。
        // suffix: 只匹配此后缀；空字符串表示不按后缀筛选。
        // read_paths: 要读取的相对文件路径；空列表只获取文件清单。
        // max_files: 最多列出的文件数。
        // max_file_bytes: 单个文件读取大小上限，单位字节。
        // max_total_bytes: 读取内容总大小上限，单位字节。
        return (Map<String, Object>) Json.parse("""
            {
              "host": "sftp.example",
              "port": 22,
              "host_fingerprint_sha256": "SHA256:EXAMPLE_HOST_KEY_FINGERPRINT",
              "base_directory": "/exports",
              "path": ".",
              "start_at": null,
              "suffix": ".json",
              "read_paths": [],
              "max_files": 500,
              "max_file_bytes": 1048576,
              "max_total_bytes": 4194304
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
            if (code != null && STABLE_ERRORS.contains(code)) {
                throw new IllegalArgumentException(code);
            }
            throw new IllegalArgumentException("sftp_operation_failed");
        }
    }

    private Object run(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput);
        String host = required(input, "host");
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String username = context.secrets.get("SFTP_USERNAME");
        if (username == null || username.isBlank()) username = required(input, "username");
        String fingerprint = required(input, "host_fingerprint_sha256");
        String baseDirectory = required(input, "base_directory");
        int port = positive(input.get("port"), 22, 65_535);
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String password = context.secrets.get("SFTP_PASSWORD");
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String privateKey = context.secrets.get("SFTP_PRIVATE_KEY");
        if (password == null && privateKey == null) throw new IllegalArgumentException("missing_credential");
        int maxFiles = positive(input.get("max_files"), 500, 5_000);
        int maxFileBytes = positive(input.get("max_file_bytes"), 1_048_576, 8_388_608);
        int maxTotalBytes = positive(input.get("max_total_bytes"), 4_194_304, 16_777_216);
        Object rawStartAt = input.get("start_at");
        if (rawStartAt != null && (!(rawStartAt instanceof String value) || value.isEmpty())) {
            throw new IllegalArgumentException("invalid_start_at");
        }
        String startAt = rawStartAt instanceof String value ? value : null;
        if (maxTotalBytes < 256 || resultSize(
            0, 0, 0, 0, 0, true, checkpoint(startAt, "checkpoint_limit")
        ) > maxTotalBytes) {
            throw new IllegalArgumentException("max_total_bytes_too_small");
        }
        Set<String> requested = strings(input.get("read_paths"));
        if (input.get("suffix") != null && !(input.get("suffix") instanceof String)) {
            throw new IllegalArgumentException("invalid_suffix");
        }
        JSch jsch = new JSch();
        jsch.setHostKeyRepository(new FingerprintRepository(fingerprint));
        if (privateKey != null) {
            // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
            String passphrase = context.secrets.get("SFTP_PRIVATE_KEY_PASSPHRASE");
            jsch.addIdentity(
                "dlr",
                privateKey.getBytes(StandardCharsets.UTF_8),
                null,
                passphrase == null ? null : passphrase.getBytes(StandardCharsets.UTF_8)
            );
        }
        Session session = jsch.getSession(username, host, port);
        session.setConfig("StrictHostKeyChecking", "yes");
        if (password != null) session.setPassword(password);
        ChannelSftp channel = null;
        try {
            session.connect(20_000);
            channel = (ChannelSftp) session.openChannel("sftp");
            channel.connect(20_000);
            String base = normalize(channel.realpath(baseDirectory));
            String directory = normalize(channel.realpath(join(base, String.valueOf(input.getOrDefault("path", ".")))));
            if (!inside(base, directory)) throw new IllegalArgumentException("path_escape");
            List<ListedEntry> entries = new ArrayList<>();
            boolean[] listingPartial = {false};
            Exception[] listingFailure = {null};
            boolean[] startFound = {startAt == null};
            ChannelSftp activeChannel = channel;
            channel.ls(directory, entry -> {
                try {
                    String name = entry.getFilename();
                    if (".".equals(name) || "..".equals(name) || !entry.getAttrs().isReg()) {
                        return ChannelSftp.LsEntrySelector.CONTINUE;
                    }
                    if (input.get("suffix") instanceof String suffix && !name.endsWith(suffix)) {
                        return ChannelSftp.LsEntrySelector.CONTINUE;
                    }
                    String resolved = normalize(activeChannel.realpath(join(directory, name)));
                    if (!inside(base, resolved)) return ChannelSftp.LsEntrySelector.CONTINUE;
                    String relative = resolved.substring(base.equals("/") ? 1 : base.length() + 1);
                    if (!startFound[0]) {
                        if (!relative.equals(startAt)) return ChannelSftp.LsEntrySelector.CONTINUE;
                        startFound[0] = true;
                    }
                    entries.add(new ListedEntry(entry, resolved, relative));
                    if (entries.size() > maxFiles) {
                        listingPartial[0] = true;
                        return ChannelSftp.LsEntrySelector.BREAK;
                    }
                    return ChannelSftp.LsEntrySelector.CONTINUE;
                } catch (Exception error) {
                    listingFailure[0] = error;
                    return ChannelSftp.LsEntrySelector.BREAK;
                }
            });
            if (listingFailure[0] != null) throw listingFailure[0];
            if (!startFound[0]) throw new IllegalArgumentException("invalid_checkpoint");
            List<Map<String, Object>> files = new ArrayList<>();
            List<Map<String, Object>> contents = new ArrayList<>();
            int fileItemBytes = 0;
            int contentItemBytes = 0;
            int totalBytes = 0;
            boolean partial = listingPartial[0];
            Map<String, Object> outputCheckpoint = null;
            boolean stopped = false;
            int processCount = Math.min(entries.size(), maxFiles);
            for (int itemAt = 0; itemAt < processCount; itemAt++) {
                ListedEntry listed = entries.get(itemAt);
                ChannelSftp.LsEntry entry = listed.entry();
                String resolved = listed.resolved();
                String relative = listed.relative();
                Map<String, Object> currentCheckpoint = checkpoint(relative, "output_limit");
                if (resultSize(
                    files.size(), fileItemBytes, contents.size(), contentItemBytes,
                    totalBytes, true, currentCheckpoint
                ) > maxTotalBytes) {
                    currentCheckpoint = checkpoint(startAt, "output_limit");
                }
                String nextPath = itemAt + 1 < entries.size()
                    ? entries.get(itemAt + 1).relative() : null;
                Map<String, Object> afterCheckpoint = nextPath == null
                    ? null : checkpoint(nextPath, "checkpoint_limit");
                Map<String, Object> metadata = Map.of(
                    "path", relative, "size", entry.getAttrs().getSize(),
                    "mtime", entry.getAttrs().getMTime()
                );
                int candidateFileBytes = fileItemBytes + encodedBytes(metadata);
                if (!requested.contains(relative)) {
                    if (resultSize(
                        files.size() + 1, candidateFileBytes,
                        contents.size(), contentItemBytes,
                        totalBytes, nextPath != null || partial, afterCheckpoint
                    ) > maxTotalBytes) {
                        partial = true; outputCheckpoint = currentCheckpoint;
                        stopped = true; break;
                    }
                    files.add(metadata); fileItemBytes = candidateFileBytes;
                    continue;
                }
                long size = entry.getAttrs().getSize();
                if (size > maxFileBytes || totalBytes + size > maxTotalBytes) {
                    Map<String, Object> candidateContent = Map.of(
                        "path", relative, "status", "limit_exceeded", "size", size
                    );
                    int candidateContentBytes = contentItemBytes + encodedBytes(candidateContent);
                    if (resultSize(
                        files.size() + 1, candidateFileBytes,
                        contents.size() + 1, candidateContentBytes,
                        totalBytes, true, afterCheckpoint
                    ) > maxTotalBytes) {
                        partial = true; outputCheckpoint = currentCheckpoint;
                        stopped = true; break;
                    }
                    files.add(metadata); contents.add(candidateContent);
                    fileItemBytes = candidateFileBytes;
                    contentItemBytes = candidateContentBytes;
                    partial = true;
                    continue;
                }
                int readLimit = rawCapacity(
                    relative, maxFileBytes, files.size() + 1, candidateFileBytes,
                    contents.size(), contentItemBytes, totalBytes,
                    afterCheckpoint, maxTotalBytes
                );
                if (size > readLimit) {
                    partial = true; outputCheckpoint = currentCheckpoint;
                    stopped = true; break;
                }
                byte[] bytes;
                try (InputStream stream = channel.get(resolved)) { bytes = readBounded(stream, readLimit); }
                if (bytes == null) {
                    partial = true; outputCheckpoint = currentCheckpoint;
                    stopped = true; break;
                }
                Map<String, Object> candidateContent = Map.of(
                    "path", relative, "status", "read", "bytes", bytes.length,
                    "content_base64", Base64.getEncoder().encodeToString(bytes)
                );
                int candidateContentBytes = contentItemBytes + encodedBytes(candidateContent);
                if (resultSize(
                    files.size() + 1, candidateFileBytes,
                    contents.size() + 1, candidateContentBytes,
                    totalBytes + bytes.length, nextPath != null || partial, afterCheckpoint
                ) > maxTotalBytes) {
                    partial = true; outputCheckpoint = currentCheckpoint;
                    stopped = true; break;
                }
                files.add(metadata); contents.add(candidateContent);
                fileItemBytes = candidateFileBytes;
                contentItemBytes = candidateContentBytes;
                totalBytes += bytes.length;
            }
            if (!stopped && entries.size() > maxFiles) {
                partial = true;
                outputCheckpoint = checkpoint(entries.get(maxFiles).relative(), "max_files");
            }
            Map<String, Object> result = resultValue(
                files, contents, totalBytes, partial, outputCheckpoint, files.size()
            );
            if (encodedBytes(result) > maxTotalBytes) {
                throw new IllegalArgumentException("max_total_bytes_too_small");
            }
            return result;
        } catch (IllegalArgumentException error) {
            throw error;
        } catch (Exception error) {
            throw new IllegalArgumentException("sftp_operation_failed");
        } finally {
            if (channel != null) {
                try { channel.disconnect(); }
                catch (Exception ignored) { /* cleanup must not replace the stable result */ }
            }
            try { session.disconnect(); }
            catch (Exception ignored) { /* cleanup must not replace the stable result */ }
        }
    }

    private static final Set<String> STABLE_ERRORS = Set.of(
        "input_must_be_object", "invalid_host", "invalid_username",
        "invalid_host_fingerprint_sha256", "invalid_base_directory",
        "missing_credential", "path_escape", "invalid_read_paths", "invalid_suffix",
        "invalid_start_at", "invalid_checkpoint", "max_total_bytes_too_small"
    );

    private static Map<String, Object> checkpoint(String startAt, String reason) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("start_at", startAt);
        if (reason != null) value.put("reason", reason);
        return value;
    }

    private static Map<String, Object> resultValue(
        List<Map<String, Object>> files,
        List<Map<String, Object>> contents,
        int totalBytes,
        boolean partial,
        Map<String, Object> checkpoint,
        int fileCount
    ) {
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("files", fileCount); summary.put("bytes_read", totalBytes);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("files", files); result.put("contents", contents);
        result.put("summary", summary); result.put("partial", partial);
        result.put("checkpoint", checkpoint);
        return result;
    }

    private static int resultSize(
        int fileCount,
        int fileItemBytes,
        int contentCount,
        int contentItemBytes,
        int totalBytes,
        boolean partial,
        Map<String, Object> checkpoint
    ) {
        int shell = encodedBytes(resultValue(
            List.of(), List.of(), totalBytes, partial, checkpoint, fileCount
        ));
        return shell - 4
            + arraySize(fileCount, fileItemBytes)
            + arraySize(contentCount, contentItemBytes);
    }

    private static int arraySize(int count, int itemBytes) {
        return count == 0 ? 2 : itemBytes + count + 1;
    }

    private static int readContentSize(String path, int rawBytes) {
        Map<String, Object> shell = new LinkedHashMap<>();
        shell.put("path", path); shell.put("status", "read");
        shell.put("bytes", rawBytes); shell.put("content_base64", "");
        return encodedBytes(shell) + 4 * ((rawBytes + 2) / 3);
    }

    private static int rawCapacity(
        String path,
        int maximum,
        int fileCount,
        int fileItemBytes,
        int contentCount,
        int contentItemBytes,
        int totalBytes,
        Map<String, Object> checkpoint,
        int maxTotalBytes
    ) {
        int low = 0, high = maximum;
        while (low < high) {
            int candidate = low + (high - low + 1) / 2;
            int candidateContentBytes = contentItemBytes + readContentSize(path, candidate);
            if (resultSize(
                fileCount, fileItemBytes, contentCount + 1, candidateContentBytes,
                totalBytes + candidate, true, checkpoint
            ) <= maxTotalBytes) {
                low = candidate;
            } else {
                high = candidate - 1;
            }
        }
        return low;
    }

    private static int encodedBytes(Object value) {
        return Json.stringify(value).getBytes(StandardCharsets.UTF_8).length;
    }

    private static final class FingerprintRepository implements HostKeyRepository {
        private final String expected;
        FingerprintRepository(String expected) { this.expected = expected.replace("SHA256:", "").replace("=", ""); }
        @Override public int check(String host, byte[] key) {
            try {
                String actual = Base64.getEncoder().withoutPadding()
                    .encodeToString(MessageDigest.getInstance("SHA-256").digest(key));
                return MessageDigest.isEqual(actual.getBytes(StandardCharsets.US_ASCII), expected.getBytes(StandardCharsets.US_ASCII))
                    ? OK : CHANGED;
            } catch (Exception error) { return NOT_INCLUDED; }
        }
        @Override public void add(com.jcraft.jsch.HostKey hostkey, com.jcraft.jsch.UserInfo userinfo) {}
        @Override public void remove(String host, String type) {}
        @Override public void remove(String host, String type, byte[] key) {}
        @Override public String getKnownHostsRepositoryID() { return "DLR pinned SHA-256 fingerprint"; }
        @Override public com.jcraft.jsch.HostKey[] getHostKey() { return new com.jcraft.jsch.HostKey[0]; }
        @Override public com.jcraft.jsch.HostKey[] getHostKey(String host, String type) { return new com.jcraft.jsch.HostKey[0]; }
    }
    private static byte[] readBounded(InputStream stream, int maximum) throws Exception { ByteArrayOutputStream output = new ByteArrayOutputStream(); byte[] buffer = new byte[8192]; for (int read; (read = stream.read(buffer)) >= 0;) { if (output.size() + read > maximum) return null; output.write(buffer, 0, read); } return output.toByteArray(); }
    private static String normalize(String value) { String result = value.replaceAll("/+", "/"); while (result.contains("/./")) result = result.replace("/./", "/"); if (result.endsWith("/") && result.length() > 1) result = result.substring(0, result.length() - 1); return result; }
    private static String join(String base, String child) { if (child.contains("..")) throw new IllegalArgumentException("path_escape"); return normalize(base + "/" + child); }
    private static boolean inside(String base, String candidate) { return candidate.equals(base) || candidate.startsWith(base.equals("/") ? "/" : base + "/"); }
    private static Set<String> strings(Object raw) { Set<String> values = new LinkedHashSet<>(); if (raw == null) return values; if (!(raw instanceof List<?> list) || list.size() > 5_000) throw new IllegalArgumentException("invalid_read_paths"); for (Object value : list) { if (!(value instanceof String text)) throw new IllegalArgumentException("invalid_read_paths"); values.add(text); } return values; }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number n && n.intValue() > 0 ? Math.min(n.intValue(), maximum) : fallback; }
    private static String required(Map<String, Object> value, String key) { Object raw = value.get(key); if (!(raw instanceof String text) || text.isEmpty()) throw new IllegalArgumentException("invalid_" + key); return text; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException("input_must_be_object"); return (Map<String, Object>) value; }
    private record ListedEntry(ChannelSftp.LsEntry entry, String resolved, String relative) {}
}
