// TypeScript 参考模板；外部 JSON 的动态字段显式保留，Context 使用平台类型。
import type { Context } from "dlr";

// SFTP 文件读取：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// SFTP_USERNAME：SFTP 用户名。
// SFTP_PASSWORD：SFTP 密码（密码和私钥二选一）。
// SFTP_PRIVATE_KEY：SFTP 私钥全文（密码和私钥二选一）。
// SFTP_PRIVATE_KEY_PASSPHRASE：私钥口令，仅加密私钥需要。
const CONFIG: Record<string, any> = {
    // SFTP 服务器地址。
    "host": "sftp.example",
    // SFTP 端口，默认 22。
    "port": 22,
    // 填写服务器管理员提供的 SSH 主机公钥 SHA256 指纹，用于确认服务器身份。
    "host_fingerprint_sha256": "SHA256:EXAMPLE_HOST_KEY_FINGERPRINT",
    // 允许读取的服务器目录。
    "base_directory": "/exports",
    // 相对于 base_directory 的目录。
    "path": ".",
    // 继续读取时填写上次 checkpoint 返回的文件位置；首次留空。
    "start_at": null,
    // 只匹配此后缀；空字符串表示不按后缀筛选。
    "suffix": ".json",
    // 要读取的相对文件路径；空列表只获取文件清单。
    "read_paths": [],
    // 最多列出的文件数。
    "max_files": 500,
    // 单个文件读取大小上限，单位字节。
    "max_file_bytes": 1048576,
    // 读取内容总大小上限，单位字节。
    "max_total_bytes": 4194304,
};
/** Host-key-verified, root-confined SFTP list and bounded read. */
import path from "node:path/posix";
import SftpClient from "ssh2-sftp-client";
const STABLE_ERRORS = new Set<any>([
    "input_must_be_object", "host_username_fingerprint_and_base_required",
    "missing_credential", "host_key_mismatch", "path_escape",
    "invalid_read_paths", "invalid_suffix", "invalid_start_at",
    "invalid_checkpoint", "max_total_bytes_too_small",
]);
function positive(value: any, fallback: any, maximum: any): any {
    return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}
function inside(base: any, candidate: any): any {
    return candidate === base || candidate.startsWith(`${base.replace(/\/$/, "")}/`);
}
async function readBounded(client: any, remotePath: any, maximum: any): Promise<any> {
    const stream = client.createReadStream(remotePath, {
        highWaterMark: Math.min(65536, maximum + 1),
    });
    const chunks: any[] = [];
    let total = 0;
    for await (const raw of stream) {
        const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
        if (total + chunk.length > maximum) {
            stream.destroy();
            return null;
        }
        chunks.push(chunk);
        total += chunk.length;
    }
    return Buffer.concat(chunks, total);
}
function encodedSize(value: any): any { return Buffer.byteLength(JSON.stringify(value), "utf8"); }
function arraySize(count: any, itemBytes: any): any { return count === 0 ? 2 : itemBytes + count + 1; }
function checkpoint(startAt: any, reason: any = null): any {
    const value: Record<string, any> = { start_at: startAt };
    if (reason !== null)
        value.reason = reason;
    return value;
}
function resultValue(files: any, contents: any, totalBytes: any, partial: any, valueCheckpoint: any, fileCount: any = files.length): any {
    return {
        files, contents, summary: { files: fileCount, bytes_read: totalBytes },
        partial, checkpoint: valueCheckpoint,
    };
}
function resultSize(fileCount: any, fileItemBytes: any, contentCount: any, contentItemBytes: any, totalBytes: any, partial: any, valueCheckpoint: any): any {
    const shell = resultValue([], [], totalBytes, partial, valueCheckpoint, fileCount);
    return encodedSize(shell) - 4
        + arraySize(fileCount, fileItemBytes)
        + arraySize(contentCount, contentItemBytes);
}
function readContentSize(filePath: any, rawBytes: any): any {
    return encodedSize({ path: filePath, status: "read", bytes: rawBytes, content_base64: "" })
        + 4 * Math.ceil(rawBytes / 3);
}
function rawCapacity(filePath: any, maximum: any, fileCount: any, fileItemBytes: any, contentCount: any, contentItemBytes: any, totalBytes: any, valueCheckpoint: any, maxTotalBytes: any): any {
    let low = 0;
    let high = maximum;
    while (low < high) {
        const candidate = Math.floor((low + high + 1) / 2);
        const candidateContentBytes = contentItemBytes + readContentSize(filePath, candidate);
        if (resultSize(fileCount, fileItemBytes, contentCount + 1, candidateContentBytes, totalBytes + candidate, true, valueCheckpoint) <= maxTotalBytes)
            low = candidate;
        else
            high = candidate - 1;
    }
    return low;
}
function sftpCall(sftp: any, method: any, ...args: any[]): any {
    return new Promise((resolve: any, reject: any) => {
        sftp[method](...args, (error: any, value: any) => {
            if (error)
                reject(error);
            else
                resolve(value);
        });
    });
}
async function listBounded(client: any, directory: any, base: any, suffix: any, startAt: any, maximum: any): Promise<any> {
    const sftp = client.sftp;
    if (!sftp || typeof sftp.opendir !== "function" || typeof sftp.readdir !== "function") {
        throw new Error("sftp_operation_failed");
    }
    let handle: any;
    const entries: any[] = [];
    let partial = false;
    let startFound = startAt === null;
    try {
        handle = await sftpCall(sftp, "opendir", directory);
        while (!partial) {
            let batch: any;
            try {
                batch = await sftpCall(sftp, "readdir", handle);
            }
            catch (error: any) {
                if (error && error.code === 1)
                    break;
                throw error;
            }
            if (batch === false || batch === undefined)
                break;
            if (!Array.isArray(batch))
                throw new Error("sftp_operation_failed");
            if (batch.length === 0)
                break;
            for (const raw of batch) {
                const name = raw?.filename;
                const attrs = raw?.attrs;
                if (typeof name !== "string" || name === "." || name === "..")
                    continue;
                if (!attrs || !Number.isInteger(attrs.mode)
                    || (attrs.mode & 0o170000) !== 0o100000)
                    continue;
                if (suffix !== undefined && !name.endsWith(suffix))
                    continue;
                const resolved = path.normalize(await client.realPath(path.join(directory, name)));
                if (!inside(base, resolved))
                    continue;
                const relative = path.relative(base, resolved);
                if (!startFound) {
                    if (relative !== startAt)
                        continue;
                    startFound = true;
                }
                entries.push({
                    name, resolved, relative,
                    size: Number(attrs.size), modifyTime: Number(attrs.mtime) * 1000,
                });
                if (entries.length > maximum) {
                    partial = true;
                    break;
                }
            }
        }
    }
    finally {
        if (handle !== undefined && typeof sftp.close === "function") {
            try {
                await sftpCall(sftp, "close", handle);
            }
            catch { /* bounded listing cleanup */ }
        }
    }
    if (!startFound)
        throw new Error("invalid_checkpoint");
    return [entries, partial];
}
async function run(context: Context, input: any): Promise<any> {
    if (!input || typeof input !== "object" || Array.isArray(input))
        throw new Error("input_must_be_object");
    for (const key of ["host", "host_fingerprint_sha256", "base_directory"]) {
        if (typeof input[key] !== "string" || input[key].length === 0)
            throw new Error("host_username_fingerprint_and_base_required");
    }
    // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    const username = context.secrets.get("SFTP_USERNAME") ?? input.username;
    if (typeof username !== "string" || username.length === 0)
        throw new Error("host_username_fingerprint_and_base_required");
    // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    const password = context.secrets.get("SFTP_PASSWORD");
    // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    const privateKey = context.secrets.get("SFTP_PRIVATE_KEY");
    if (!password && !privateKey)
        throw new Error("missing_credential");
    const maxFiles = positive(input.max_files, 500, 5000);
    const maxFileBytes = positive(input.max_file_bytes, 1048576, 8388608);
    const maxTotalBytes = positive(input.max_total_bytes, 4194304, 16777216);
    const startAt = input.start_at ?? null;
    if (startAt !== null && (typeof startAt !== "string" || startAt.length === 0)) {
        throw new Error("invalid_start_at");
    }
    if (maxTotalBytes < 256 || resultSize(0, 0, 0, 0, 0, true, checkpoint(startAt, "checkpoint_limit")) > maxTotalBytes)
        throw new Error("max_total_bytes_too_small");
    if (input.read_paths !== undefined
        && (!Array.isArray(input.read_paths) || input.read_paths.length > 5000
            || input.read_paths.some((value: any) => typeof value !== "string"))) {
        throw new Error("invalid_read_paths");
    }
    if (input.suffix !== undefined && typeof input.suffix !== "string") {
        throw new Error("invalid_suffix");
    }
    const client = new SftpClient();
    const expected = input.host_fingerprint_sha256.replace(/^SHA256:/, "");
    let hostKeyChecked = false;
    let hostKeyMatched = false;
    try {
        await client.connect({
            host: input.host,
            port: positive(input.port, 22, 65535),
            username,
            password: password ?? undefined,
            privateKey: privateKey ?? undefined,
            passphrase: privateKey
                // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
                ? (context.secrets.get("SFTP_PRIVATE_KEY_PASSPHRASE") ?? undefined)
                : undefined,
            readyTimeout: 20000,
            hostHash: "sha256",
            hostVerifier: (actual: any) => {
                hostKeyChecked = true;
                const normalized = /^[0-9a-f]{64}$/i.test(actual)
                    ? Buffer.from(actual, "hex").toString("base64").replace(/=+$/, "")
                    : actual.replace(/^SHA256:/, "").replace(/=+$/, "");
                hostKeyMatched = normalized === expected.replace(/=+$/, "");
                return hostKeyMatched;
            },
        });
        const base = path.normalize(await client.realPath(input.base_directory));
        const directory = path.normalize(await client.realPath(path.join(base, input.path ?? ".")));
        if (!inside(base, directory))
            throw new Error("path_escape");
        const requested = new Set<any>(input.read_paths ?? []);
        const files: any[] = [];
        const contents: any[] = [];
        let fileItemBytes = 0;
        let contentItemBytes = 0;
        let totalBytes = 0;
        let partial = false;
        let outputCheckpoint: any = null;
        let stopped = false;
        const [entries, listingPartial] = await listBounded(client, directory, base, input.suffix, startAt, maxFiles);
        partial = listingPartial;
        for (let itemAt = 0; itemAt < Math.min(entries.length, maxFiles); itemAt += 1) {
            const entry = entries[itemAt];
            const relative = entry.relative;
            let currentCheckpoint = checkpoint(relative, "output_limit");
            const fallbackCheckpoint = checkpoint(startAt, "output_limit");
            if (resultSize(files.length, fileItemBytes, contents.length, contentItemBytes, totalBytes, true, currentCheckpoint) > maxTotalBytes)
                currentCheckpoint = fallbackCheckpoint;
            const nextPath = itemAt + 1 < entries.length ? entries[itemAt + 1].relative : null;
            const afterCheckpoint = nextPath === null ? null : checkpoint(nextPath, "checkpoint_limit");
            const metadata: Record<string, any> = { path: relative, size: entry.size, mtime: entry.modifyTime };
            const candidateFileBytes = fileItemBytes + encodedSize(metadata);
            let candidateContent: any = null;
            if (!requested.has(relative)) {
                if (resultSize(files.length + 1, candidateFileBytes, contents.length, contentItemBytes, totalBytes, nextPath !== null || partial, afterCheckpoint) > maxTotalBytes) {
                    partial = true;
                    outputCheckpoint = currentCheckpoint;
                    stopped = true;
                    break;
                }
                files.push(metadata);
                fileItemBytes = candidateFileBytes;
                continue;
            }
            if (entry.size > maxFileBytes || totalBytes + entry.size > maxTotalBytes) {
                candidateContent = { path: relative, status: "limit_exceeded", size: entry.size };
            }
            if (candidateContent !== null) {
                const candidateContentBytes = contentItemBytes + encodedSize(candidateContent);
                if (resultSize(files.length + 1, candidateFileBytes, contents.length + 1, candidateContentBytes, totalBytes, true, afterCheckpoint) > maxTotalBytes) {
                    partial = true;
                    outputCheckpoint = currentCheckpoint;
                    stopped = true;
                    break;
                }
                files.push(metadata);
                contents.push(candidateContent);
                fileItemBytes = candidateFileBytes;
                contentItemBytes = candidateContentBytes;
                partial = true;
                continue;
            }
            const readLimit = rawCapacity(relative, maxFileBytes, files.length + 1, candidateFileBytes, contents.length, contentItemBytes, totalBytes, afterCheckpoint, maxTotalBytes);
            if (entry.size > readLimit) {
                partial = true;
                outputCheckpoint = currentCheckpoint;
                stopped = true;
                break;
            }
            const data = await readBounded(client, entry.resolved, readLimit);
            if (data === null) {
                partial = true;
                outputCheckpoint = currentCheckpoint;
                stopped = true;
                break;
            }
            candidateContent = {
                path: relative, status: "read", bytes: data.length,
                content_base64: data.toString("base64"),
            };
            const candidateContentBytes = contentItemBytes + encodedSize(candidateContent);
            if (resultSize(files.length + 1, candidateFileBytes, contents.length + 1, candidateContentBytes, totalBytes + data.length, nextPath !== null || partial, afterCheckpoint) > maxTotalBytes) {
                partial = true;
                outputCheckpoint = currentCheckpoint;
                stopped = true;
                break;
            }
            files.push(metadata);
            contents.push(candidateContent);
            fileItemBytes = candidateFileBytes;
            contentItemBytes = candidateContentBytes;
            totalBytes += data.length;
        }
        if (!stopped && entries.length > maxFiles) {
            partial = true;
            outputCheckpoint = checkpoint(entries[maxFiles].relative, "max_files");
        }
        const result = resultValue(files, contents, totalBytes, partial, outputCheckpoint);
        if (encodedSize(result) > maxTotalBytes)
            throw new Error("max_total_bytes_too_small");
        return result;
    }
    catch (error: any) {
        if (hostKeyChecked && !hostKeyMatched)
            throw new Error("host_key_mismatch");
        if (error instanceof Error && STABLE_ERRORS.has(error.message))
            throw error;
        throw new Error("sftp_operation_failed");
    }
    finally {
        try {
            await client.end();
        }
        catch { /* cleanup must not replace the stable result */ }
    }
}
export async function handle(context: Context, input: any): Promise<any> {
    if (input === undefined || input === null)
        input = {};
    if (typeof input !== "object" || Array.isArray(input))
        throw new Error("输入必须是 JSON 对象");
    input = { ...CONFIG, ...input };
    try {
        return await run(context, input);
    }
    catch (error: any) {
        const code = error instanceof Error ? error.message : "";
        if (STABLE_ERRORS.has(code))
            throw new Error(code);
        throw new Error("sftp_operation_failed");
    }
}
