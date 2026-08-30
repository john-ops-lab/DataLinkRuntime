/** M5.7 Wave B3: browser-side attachment contract helpers.
 *
 * The server (M5.7 Wave B2) is the authoritative validator and parser. This
 * module mirrors the stable B2 limits/MIME table so the upload UI can reject
 * obviously invalid files up front with actionable localized messages — the
 * client checks never replace the server checks, and the server's stable
 * ``ai_attachment_*`` error codes remain the final authority.
 */

import type {
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/react";
import type { AiAttachment, AiAttachmentLimits } from "./types";

/** Canonical fallback limits, identical to the B2 server constants. Used only
 * while the capability endpoint is unreachable; the server stays the source
 * of truth for every actual request. */
export const DEFAULT_ATTACHMENT_LIMITS: AiAttachmentLimits = {
  max_attachments: 8,
  max_file_bytes: 6 * 1024 * 1024,
  max_total_bytes: 8 * 6 * 1024 * 1024,
  max_parsed_chars_per_file: 64 * 1024,
  max_parsed_total_chars: 256 * 1024,
  parse_timeout_seconds: 30,
};

export type AttachmentCategory = "image" | "pdf" | "docx" | "xls" | "xlsx" | "text" | "code";

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
  "application/vnd.ms-excel": new Set(["xls"]),
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": new Set(["xlsx"]),
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
  "application/vnd.ms-excel": "xls",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
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

// --- M5.7 Wave B3: official AttachmentAdapter + send guards ------------------

/** One composer row in the minimal shape the send guard inspects. */
export interface AttachmentRowStatus {
  type: string;
  message?: string;
  reason?: string;
}

/** The localized message of a client-rejected row, or null when the row is
 * sendable. A row the UI marked as rejected (error row from adapter.add)
 * must never produce a wire body — this guard backs both the composer send
 * path and the adapter's own send(). */
export function rejectedRowMessage(
  status: AttachmentRowStatus,
  fallback: string,
): string | null {
  return status.type === "incomplete" ? (status.message ?? fallback) : null;
}

/** Stable attachment identity (jsdom/node provide crypto.randomUUID). */
function attachmentId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `attachment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** An attachment that failed client-side validation. It renders as a visible,
 * removable error row with the localized reason (the runtime skips throwing
 * so the picker path never swallows the message silently). */
export function errorPendingAttachment(file: File, message: string): PendingAttachment {
  return {
    id: attachmentId(),
    type: "document",
    name: file.name,
    contentType: file.type,
    file,
    status: { type: "incomplete", reason: "error", message },
  };
}

/** Resolve one pending attachment into its complete form. The strict base64
 * body is read once and cached (WeakMap keyed by the returned complete
 * attachment object) so the wire payload reuses the same string instead of
 * re-reading the file. The content part carries a transient data URL only
 * for contract shape; DLR never renders it (the thread renders text-only
 * converted messages), and it is GC'd with the transient AppendMessage. */
export async function completeAttachment(
  attachment: PendingAttachment,
  wireCache: WeakMap<object, AiAttachment>,
): Promise<CompleteAttachment> {
  const wire = await buildWireAttachment(
    attachment.name,
    attachment.contentType ?? attachment.file.type,
    attachment.file,
  );
  const dataUrl = `data:${wire.content_type};base64,${wire.data_base64}`;
  const content = attachment.type === "image"
    ? [{ type: "image" as const, image: dataUrl, filename: attachment.name }]
    : [{ type: "file" as const, filename: attachment.name, data: dataUrl, mimeType: wire.content_type }];
  const complete: CompleteAttachment = {
    id: attachment.id,
    type: attachment.type,
    name: attachment.name,
    contentType: wire.content_type,
    file: attachment.file,
    content,
    status: { type: "complete" },
  };
  wireCache.set(complete, wire);
  return complete;
}

/** Localized message for one client-side add rejection (pre-validation path).
 * Server rejections use the stable ``ai_attachment_*`` codes via
 * common.errors instead. */
export function attachmentAddErrorMessage(
  reason: AttachmentAddErrorReason,
  limits: AiAttachmentLimits,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  switch (reason) {
    case "filename_invalid":
      return translate("assistant.attachments.error.filenameInvalid");
    case "empty":
      return translate("assistant.attachments.error.empty");
    case "too_large":
      return translate("assistant.attachments.error.tooLarge", {
        size: formatAttachmentSize(limits.max_file_bytes),
      });
    case "count_exceeded":
      return translate("assistant.attachments.error.countExceeded", {
        count: limits.max_attachments,
      });
    case "total_too_large":
      return translate("assistant.attachments.error.totalTooLarge", {
        total: formatAttachmentSize(limits.max_total_bytes),
      });
    case "unsupported":
      return translate("assistant.attachments.error.unsupported");
  }
}

/** Everything the adapter needs at call time; the panel feeds live refs so
 * the adapter stays stable while limits / capabilities / locale change. */
export interface DlrAttachmentAdapterOptions {
  limits: () => AiAttachmentLimits;
  composerAttachments: () => readonly { file?: File | undefined }[];
  supportedContentTypes: () => readonly string[];
  translate: (key: string, options?: Record<string, unknown>) => string;
  wireCache: () => WeakMap<object, AiAttachment>;
}

/** M5.7 Wave B3: the official assistant-ui AttachmentAdapter for the
 * External Store Runtime. ``accept`` mirrors the *current* B2 capability
 * table (a getter, so a future server-side narrowing of supported types is
 * followed immediately). ``add`` validates every file against the B2 bounds
 * (returning a visible, removable error row for rejections so picker errors
 * are never swallowed). ``send`` refuses rejected rows and otherwise reads
 * the strict base64 body once, caching it for the wire payload. ``remove``
 * releases nothing because DLR holds no per-attachment browser resources
 * (no object URLs, no previews). */
export function createDlrAttachmentAdapter(
  options: DlrAttachmentAdapterOptions,
): AttachmentAdapter {
  return {
    get accept() {
      return acceptStringFor(options.supportedContentTypes());
    },
    async add({ file }) {
      const limits = options.limits();
      const verdict = validateAttachmentAdd(file, limits, options.composerAttachments());
      if (!verdict.ok) {
        return errorPendingAttachment(
          file,
          attachmentAddErrorMessage(verdict.reason, limits, options.translate),
        );
      }
      return {
        id: attachmentId(),
        type: verdict.category === "image" ? "image" : "document",
        name: file.name.trim(),
        contentType: verdict.contentType,
        file,
        status: { type: "requires-action", reason: "composer-send" },
      };
    },
    async send(attachment) {
      // A client-rejected row must never leave the browser: refuse to
      // produce a wire body for it. Defense in depth — the composer paths
      // that can create error rows are pre-validated and paste is disabled,
      // but the runtime's own send loop resolves every pending row, so the
      // adapter re-checks instead of trusting the caller.
      const rejected = rejectedRowMessage(
        attachment.status,
        options.translate("assistant.attachments.error.rejected"),
      );
      if (rejected !== null) {
        throw new Error(rejected);
      }
      return completeAttachment(attachment, options.wireCache());
    },
    async remove() {
      // Nothing to release: attachments are held as plain File references
      // (GC reclaims them once the composer drops the row); object URLs are
      // never created, so there is nothing to revoke.
    },
  };
}

/** The localized message of the first rejected row in a composer snapshot,
 * or null when the whole selection is sendable. */
export function firstRejectedRowMessage(
  attachments: readonly { status: AttachmentRowStatus }[],
  fallback: string,
): string | null {
  for (const attachment of attachments) {
    const rejected = rejectedRowMessage(attachment.status, fallback);
    if (rejected !== null) {
      return rejected;
    }
  }
  return null;
}
