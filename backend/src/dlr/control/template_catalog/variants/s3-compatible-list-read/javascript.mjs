// S3 对象读取：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// S3_ACCESS_KEY_ID：S3 Access Key ID。
// S3_SECRET_ACCESS_KEY：S3 Secret Access Key。
// S3_SESSION_TOKEN：S3 临时凭据 Token，仅使用临时凭据时配置。
const CONFIG = {
  // S3 兼容服务地址。
  "endpoint": "https://storage.example",
  // 存储桶所在区域 ID。
  "region": "example-region-1",
  // 填写存储桶名称。
  "bucket": "example-bucket",
  // 只列出此前缀下的对象；空字符串表示整个存储桶。
  "prefix": "exports/",
  // 继续读取时填写上次 checkpoint 返回的分页标记；首次留空。
  "continuation_token": null,
  // 同一页内继续读取的位置，首次为 0。
  "object_offset": 0,
  // 填写要读取的对象键；空列表只获取对象清单。
  "read_keys": [],
  // 兼容私有 S3 服务时通常使用 true。
  "force_path_style": true,
  // 单次运行最多读取的页数。
  "max_pages": 20,
  // 最多列出的对象数。
  "max_objects": 1000,
  // 单个对象读取大小上限，单位字节。
  "max_object_bytes": 1048576,
  // 读取内容总大小上限，单位字节。
  "max_total_bytes": 4194304,
};

/** Bounded S3-compatible list and optional range reads. */
import {
  GetObjectCommand,
  ListObjectsV2Command,
  S3Client,
} from "@aws-sdk/client-s3";

const STABLE_ERRORS = new Set([
  "input_must_be_object", "bucket_required", "invalid_endpoint",
  "missing_credential", "invalid_read_keys", "invalid_continuation_token",
  "invalid_object_offset", "invalid_checkpoint", "max_total_bytes_too_small",
]);

function positive(value, fallback, maximum) {
  return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}
function validatedEndpoint(value) {
  if (value === undefined || value === null) return undefined;
  let endpoint;
  try { endpoint = new URL(value); } catch { throw new Error("invalid_endpoint"); }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(endpoint.hostname);
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash
      || (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && loopback))) {
    throw new Error("invalid_endpoint");
  }
  return endpoint.toString();
}
async function boundedBody(body, maximum) {
  const chunks = []; let size = 0;
  for await (const chunk of body) {
    const bytes = Buffer.from(chunk); size += bytes.length;
    if (size > maximum) return null;
    chunks.push(bytes);
  }
  return Buffer.concat(chunks);
}
function encodedSize(value) { return Buffer.byteLength(JSON.stringify(value), "utf8"); }
function arraySize(count, itemBytes) { return count === 0 ? 2 : itemBytes + count + 1; }
function checkpoint(token, objectOffset, reason = null) {
  const value = { continuation_token: token, object_offset: objectOffset };
  if (reason !== null) value.reason = reason;
  return value;
}
function resultValue(objects, contents, totalBytes, pages, partial, valueCheckpoint, objectCount = objects.length) {
  return {
    objects, contents,
    summary: { objects: objectCount, bytes_read: totalBytes, pages },
    partial, checkpoint: valueCheckpoint,
  };
}
function resultSize(objectCount, objectItemBytes, contentCount, contentItemBytes, totalBytes, pages, partial, valueCheckpoint) {
  const shell = resultValue([], [], totalBytes, pages, partial, valueCheckpoint, objectCount);
  return encodedSize(shell) - 4
    + arraySize(objectCount, objectItemBytes)
    + arraySize(contentCount, contentItemBytes);
}
function readContentSize(key, rawBytes) {
  return encodedSize({ key, status: "read", bytes: rawBytes, content_base64: "" })
    + 4 * Math.ceil(rawBytes / 3);
}
function rawCapacity(key, maximum, objectCount, objectItemBytes, contentCount, contentItemBytes, totalBytes, pages, valueCheckpoint, maxTotalBytes) {
  let low = 0; let high = maximum;
  while (low < high) {
    const candidate = Math.floor((low + high + 1) / 2);
    const candidateContentBytes = contentItemBytes + readContentSize(key, candidate);
    if (resultSize(
      objectCount, objectItemBytes, contentCount + 1, candidateContentBytes,
      totalBytes + candidate, pages, true, valueCheckpoint,
    ) <= maxTotalBytes) low = candidate;
    else high = candidate - 1;
  }
  return low;
}

async function run(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  if (typeof input.bucket !== "string" || input.bucket.length === 0) throw new Error("bucket_required");
  const endpoint = validatedEndpoint(input.endpoint);
  // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
  const accessKeyId = context.secrets.get("S3_ACCESS_KEY_ID");
  // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
  const secretAccessKey = context.secrets.get("S3_SECRET_ACCESS_KEY");
  if (!accessKeyId || !secretAccessKey) throw new Error("missing_credential");
  if (input.read_keys !== undefined
      && (!Array.isArray(input.read_keys) || input.read_keys.length > 1_000
        || input.read_keys.some((key) => typeof key !== "string"))) {
    throw new Error("invalid_read_keys");
  }
  const maxTotalBytes = positive(input.max_total_bytes, 4_194_304, 16_777_216);
  let continuation = input.continuation_token ?? null;
  if (continuation !== null
      && (typeof continuation !== "string" || continuation.length === 0)) {
    throw new Error("invalid_continuation_token");
  }
  let objectOffset = input.object_offset ?? 0;
  if (!Number.isInteger(objectOffset) || objectOffset < 0 || objectOffset > 1_000) {
    throw new Error("invalid_object_offset");
  }
  if (maxTotalBytes < 256 || resultSize(
    0, 0, 0, 0, 0, 0, true,
    checkpoint(continuation, objectOffset, "checkpoint_limit"),
  ) > maxTotalBytes) throw new Error("max_total_bytes_too_small");
  const client = new S3Client({
    endpoint,
    region: input.region ?? "us-east-1",
    forcePathStyle: input.force_path_style !== false,
    maxAttempts: 2,
    credentials: {
      accessKeyId,
      secretAccessKey,
      // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
      sessionToken: context.secrets.get("S3_SESSION_TOKEN") ?? undefined,
    },
  });
  const maxObjects = positive(input.max_objects, 1_000, 10_000);
  const maxPages = positive(input.max_pages, 20, 200);
  const maxObjectBytes = positive(input.max_object_bytes, 1_048_576, 8_388_608);
  const requested = new Set(input.read_keys ?? []);
  const objects = []; const contents = [];
  let objectItemBytes = 0; let contentItemBytes = 0;
  let totalBytes = 0; let pages = 0; let partial = false;
  let outputCheckpoint = null; let stopped = false;
  try {
    while (pages < maxPages && !stopped) {
      const pageToken = continuation;
      const pageOffset = objectOffset;
      const response = await client.send(new ListObjectsV2Command({
        Bucket: input.bucket,
        Prefix: input.prefix ?? "",
        MaxKeys: Math.min(1000, maxObjects),
        ContinuationToken: pageToken ?? undefined,
      }));
      pages += 1;
      const pageItems = response.Contents ?? [];
      if (pageOffset > pageItems.length) throw new Error("invalid_checkpoint");
      for (let itemAt = pageOffset; itemAt < pageItems.length; itemAt += 1) {
        const item = pageItems[itemAt];
        const currentCheckpoint = checkpoint(pageToken, itemAt, "output_limit");
        const afterCheckpoint = checkpoint(pageToken, itemAt + 1, "checkpoint_limit");
        if (objects.length >= maxObjects) {
          partial = true;
          outputCheckpoint = checkpoint(pageToken, itemAt, "max_objects");
          stopped = true;
          break;
        }
        const key = item.Key ?? "";
        const size = Number(item.Size ?? 0);
        const metadata = {
          key,
          size,
          etag: (item.ETag ?? "").replaceAll('"', ""),
          lastModified: item.LastModified?.toISOString() ?? null,
        };
        const candidateObjectBytes = objectItemBytes + encodedSize(metadata);
        let candidateContent = null;
        if (size > maxObjectBytes || totalBytes + size > maxTotalBytes) {
          if (requested.has(key)) candidateContent = { key, status: "limit_exceeded", size };
        }
        if (candidateContent !== null) {
          const candidateContentBytes = contentItemBytes + encodedSize(candidateContent);
          if (resultSize(
            objects.length + 1, candidateObjectBytes,
            contents.length + 1, candidateContentBytes,
            totalBytes, pages, true, afterCheckpoint,
          ) > maxTotalBytes) {
            partial = true; outputCheckpoint = currentCheckpoint; stopped = true; break;
          }
          objects.push(metadata); contents.push(candidateContent);
          objectItemBytes = candidateObjectBytes;
          contentItemBytes = candidateContentBytes;
          partial = true;
          continue;
        }
        if (!requested.has(key)) {
          if (resultSize(
            objects.length + 1, candidateObjectBytes,
            contents.length, contentItemBytes,
            totalBytes, pages, true, afterCheckpoint,
          ) > maxTotalBytes) {
            partial = true; outputCheckpoint = currentCheckpoint; stopped = true; break;
          }
          objects.push(metadata); objectItemBytes = candidateObjectBytes;
          continue;
        }
        const readLimit = rawCapacity(
          key, maxObjectBytes, objects.length + 1, candidateObjectBytes,
          contents.length, contentItemBytes, totalBytes, pages,
          afterCheckpoint, maxTotalBytes,
        );
        if (size > readLimit) {
          partial = true; outputCheckpoint = currentCheckpoint; stopped = true; break;
        }
        let body = Buffer.alloc(0);
        if (readLimit > 0) {
          const value = await client.send(new GetObjectCommand({
            Bucket: input.bucket, Key: key, Range: `bytes=0-${readLimit - 1}`,
          }));
          body = await boundedBody(value.Body, readLimit);
          if (body === null) {
            partial = true; outputCheckpoint = currentCheckpoint; stopped = true; break;
          }
        }
        candidateContent = {
          key, status: "read", bytes: body.length, content_base64: body.toString("base64"),
        };
        const candidateContentBytes = contentItemBytes + encodedSize(candidateContent);
        if (resultSize(
          objects.length + 1, candidateObjectBytes,
          contents.length + 1, candidateContentBytes,
          totalBytes + body.length, pages, true, afterCheckpoint,
        ) > maxTotalBytes) {
          partial = true; outputCheckpoint = currentCheckpoint; stopped = true; break;
        }
        objects.push(metadata); contents.push(candidateContent);
        objectItemBytes = candidateObjectBytes;
        contentItemBytes = candidateContentBytes;
        totalBytes += body.length;
      }
      if (stopped) break;
      const truncated = response.IsTruncated === true;
      const nextContinuation = response.NextContinuationToken;
      if (!truncated) { continuation = null; objectOffset = 0; break; }
      if (typeof nextContinuation !== "string" || nextContinuation.length === 0) {
        partial = true;
        outputCheckpoint = checkpoint(pageToken, pageItems.length, "missing_token");
        break;
      }
      const nextCheckpoint = checkpoint(nextContinuation, 0);
      if (resultSize(
        objects.length, objectItemBytes, contents.length, contentItemBytes,
        totalBytes, pages, true, nextCheckpoint,
      ) > maxTotalBytes) {
        partial = true;
        outputCheckpoint = checkpoint(pageToken, pageItems.length, "output_limit");
        break;
      }
      continuation = nextContinuation;
      objectOffset = 0;
    }
    if (continuation !== null && outputCheckpoint === null) {
      partial = true;
      outputCheckpoint = checkpoint(continuation, objectOffset);
    }
    const result = resultValue(objects, contents, totalBytes, pages, partial, outputCheckpoint);
    if (encodedSize(result) > maxTotalBytes) throw new Error("max_total_bytes_too_small");
    return result;
  } finally {
    try { client.destroy(); } catch { /* cleanup must not replace the stable result */ }
  }
}

export async function handle(context, input) {
  if (input === undefined || input === null) input = {};
  if (typeof input !== "object" || Array.isArray(input)) throw new Error("输入必须是 JSON 对象");
  input = { ...CONFIG, ...input };
  try {
    return await run(context, input);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (STABLE_ERRORS.has(code)) throw new Error(code);
    throw new Error("s3_operation_failed");
  }
}
