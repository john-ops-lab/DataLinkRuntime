/** M5.7 Wave B3: browser-side attachment contract helpers.
 *
 * The server (M5.7 Wave B2) is the authoritative validator and parser. This
 * module mirrors the stable B2 limits/MIME table so the upload UI can reject
 * obviously invalid files up front with actionable localized messages — the
 * client checks never replace the server checks, and the server's stable
 * ``ai_attachment_*`` error codes remain the final authority.
 */

import type { AiAttachment, AiAttachmentLimits } from "./types";

/** Canonical fallback limits, identical to the B2 server constants. Used only
 * while the capability endpoint is unreachable; the server stays the source
 * of truth for every actual request. */
export const DEFAULT_ATTACHMENT_LIMITS: AiAttachmentLimits = {
  max_attachments: 8,
  max_file_bytes: 6 * 1024 * 1024,
  max_total_bytes: 12 * 1024 * 1024,
  max_parsed_chars_per_file: 64 * 1024,
  max_parsed_total_chars: 256 * 1024,
  parse_timeout_seconds: 30,
};

export type AttachmentCategory = "image" | "pdf" | "docx" | "text" | "code";

const TEXT_EXTENSIONS = new Set([
  "txt",
  "md",
  "markdown",
  "csv",
  "json",
  "yaml",
  "yml",
  "xml",
  "toml",
  "ini",
  "conf",
  "cfg",
  "properties",
  "env",
]);

const CODE_EXTENSIONS = new Set([
  "py",
  "js",
  "mjs",
  "cjs",
  "ts",
  "tsx",
  "jsx",
  "go",
  "rs",
  "java",
  "kt",
  "sh",
  "bash",
  "sql",
  "html",
  "css",
  "yaml",
  "yml",
  "json",
  "xml",
  "toml",
]);

/** Mirrors the B2 server MIME_EXTENSIONS table: the declared MIME and the
 * filename extension must both agree, otherwise the file is a fake/mistyped
 * type and gets rejected (client-side early, server-side authoritatively). */
export const MIME_EXTENSIONS: Record<string, ReadonlySet<string>> = {
  "image/png": new Set(["png"]),
  "image/jpeg": new Set(["jpg", "jpeg"]),
  "image/webp": new Set(["webp"]),
  "application/pdf": new Set(["pdf"]),
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": new Set(["docx"]),
  "text/plain": new Set([...TEXT_EXTENSIONS, ...CODE_EXTENSIONS]),
  "text/markdown": new Set(["md", "markdown"]),
  "text/csv": new Set(["csv"]),
  "application/json": new Set(["json"]),
  "text/x-yaml": new Set(["yaml", "yml"]),
  "application/x-yaml": new Set(["yaml", "yml"]),
  "text/xml": new Set(["xml"]),
  "application/xml": new Set(["xml"]),
  "text/javascript": new Set(["js", "mjs", "cjs"]),
  "application/javascript": new Set(["js", "mjs", "cjs"]),
  "application/octet-stream": new Set([...TEXT_EXTENSIONS, ...CODE_EXTENSIONS]),
};

export const DEFAULT_SUPPORTED_CONTENT_TYPES = Object.keys(MIME_EXTENSIONS);

const MIME_CATEGORY: Record<string, AttachmentCategory> = {
  "image/png": "image",
  "image/jpeg": "image",
  "image/webp": "image",
  "application/pdf": "pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
  "text/plain": "text",
  "text/markdown": "text",
  "text/csv": "text",
  "application/json": "code",
  "text/x-yaml": "code",
  "application/x-yaml": "code",
  "text/xml": "code",
  "application/xml": "code",
  "text/javascript": "code",
  "application/javascript": "code",
  "application/octet-stream": "text",
};

export type AttachmentClassification =
  | { ok: true; category: AttachmentCategory; contentType: string }
  | { ok: false; reason: "unsupported" | "filename_invalid" };

/** Reason of a client-side add rejection (mirrors the B2 bounds). */
export type AttachmentAddErrorReason =
  | "unsupported"
  | "filename_invalid"
  | "empty"
  | "too_large"
  | "count_exceeded"
  | "total_too_large";

/** Verdict of the client-side add validation (mirrors the B2 bounds). */
export type AttachmentAddVerdict =
  | { ok: true; category: AttachmentCategory; contentType: string }
  | { ok: false; reason: AttachmentAddErrorReason };

/** One shared validation used by both the composer toolbar (pre-validation
 * with panel errors) and the AttachmentAdapter (defense-in-depth error rows).
 * ``existing`` is the current composer attachment list (``file`` may be
 * absent on complete attachments). */
export function validateAttachmentAdd(
  file: File,
  limits: AiAttachmentLimits,
  existing: readonly { file?: File | undefined }[],
): AttachmentAddVerdict {
  const classification = classifyAttachment(file.name, file.type);
  if (!classification.ok) {
    return classification as AttachmentAddVerdict;
  }
  if (file.size === 0) {
    // The B2 server rejects empty bodies with ai_attachment_invalid; reject
    // them up front with actionable copy instead of failing at send time.
    return { ok: false, reason: "empty" };
  }
  if (file.size > limits.max_file_bytes) {
    return { ok: false, reason: "too_large" };
  }
  if (existing.length >= limits.max_attachments) {
    return { ok: false, reason: "count_exceeded" };
  }
  const totalBytes =
    existing.reduce((sum, attachment) => sum + (attachment.file?.size ?? 0), 0) + file.size;
  if (totalBytes > limits.max_total_bytes) {
    return { ok: false, reason: "total_too_large" };
  }
  return classification;
}

/** Mirror of the B2 server ``sanitize_filename`` + ``classify`` checks.
 * Returns the normalized category/content-type or a stable failure reason. */
export function classifyAttachment(
  filename: string,
  declaredContentType: string,
): AttachmentClassification {
  const stripped = filename.trim();
  const hasControlCharacter = [...stripped].some(
    (character) => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) === 0x7f,
  );
  if (
    stripped === "" ||
    stripped.length > 255 ||
    hasControlCharacter ||
    stripped.includes("/") ||
    stripped.includes("\\") ||
    stripped === "." ||
    stripped === ".." ||
    stripped.startsWith(".")
  ) {
    return { ok: false, reason: "filename_invalid" };
  }
  const normalized = declaredContentType.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  const allowed = MIME_EXTENSIONS[normalized];
  if (allowed === undefined) {
    return { ok: false, reason: "unsupported" };
  }
  const extension = stripped.includes(".")
    ? stripped.slice(stripped.lastIndexOf(".") + 1).toLowerCase()
    : "";
  if (extension === "" || !allowed.has(extension)) {
    return { ok: false, reason: "unsupported" };
  }
  return { ok: true, category: MIME_CATEGORY[normalized], contentType: normalized };
}

/** Decoded-byte estimate of a base64 body (base64 is 4 chars per 3 bytes). */
export function base64DecodedSize(dataBase64: string): number {
  return Math.floor((dataBase64.length * 3) / 4);
}

/** Read a File into a strict base64 body (bounded by the per-file limit; the
 * caller already validated the size before the read). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("File read failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("File read failed"));
        return;
      }
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.readAsDataURL(file);
  });
}

/** Human-readable size label, e.g. "6 MiB" / "512 KiB" / "12 MiB". */
export function formatAttachmentSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const mebibytes = bytes / (1024 * 1024);
    return `${Number.isInteger(mebibytes) ? mebibytes : mebibytes.toFixed(1)} MiB`;
  }
  if (bytes >= 1024) {
    const kibibytes = bytes / 1024;
    return `${Number.isInteger(kibibytes) ? kibibytes : kibibytes.toFixed(1)} KiB`;
  }
  return `${bytes} B`;
}

/** Build the accept attribute from the B2 capability table. */
export function acceptStringFor(contentTypes: readonly string[]): string {
  return contentTypes.join(",");
}

/** Assemble the wire attachment (B2 contract) for one file. The body is
 * strict base64; sizes were validated before this call. */
export async function buildWireAttachment(
  filename: string,
  contentType: string,
  file: File,
): Promise<AiAttachment> {
  return {
    filename,
    content_type: contentType,
    data_base64: await fileToBase64(file),
  };
}
