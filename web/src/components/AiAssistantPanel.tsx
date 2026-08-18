/** Browser-only AI conversation and Candidate review surface (M4).
 *
 * M5.7 Wave A: the conversation/composer layer is rebuilt on the official
 * assistant-ui headless primitives via the External Store Runtime. DLR keeps
 * the authoritative message/business state (VisibleMessage, Candidate,
 * Working Copy snapshot, Secret binding knowledge); the runtime only mirrors
 * the visible user/assistant text for Thread/Message/Composer/Markdown
 * primitives.
 *
 * M5.7 Wave B1: Regenerate via the External Store Runtime's onReload
 * contract. Each sent round freezes a complete AssistRoundSnapshot (user
 * message, the visible recent_messages boundary, working_copy/base version,
 * ordered context snippets, adapter and UI locale); regenerating reuses that
 * frozen snapshot and never reads the current editor/Adapter/config.
 *
 * M5.7 Wave B3: Composer attachments through the official assistant-ui
 * AttachmentAdapter / Composer attachment primitives. File selection and
 * drag & drop are validated against the Wave B2 server contract (type/ext
 * table, per-file/total/count bounds); the server stays the authoritative
 * validator and parser. Attachment bodies (strict base64, inline per the B2
 * API) are frozen into the round snapshot at send time — bounded by the B2
 * total size cap, never rendered or logged — so Regenerate and the failed-
 * round retry reuse the original files without reading the current Composer.
 * No Tool Call / MCP / Streaming / Reasoning UI / Thread persistence.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import {
  AssistantRuntimeProvider,
  AttachmentPrimitive,
  ComposerPrimitive,
  fromThreadMessageLike,
  MessagePrimitive,
  MessageProvider,
  ThreadPrimitive,
  useExternalStoreRuntime,
  useAui,
  useAuiState,
  type AppendMessage,
  type Attachment,
  type AttachmentAdapter,
  type CompleteAttachment,
  type PendingAttachment,
  type ThreadMessageLike,
} from "@assistant-ui/react";

import { api, ApiError } from "../api";
import {
  acceptStringFor,
  base64DecodedSize,
  buildWireAttachment,
  classifyAttachment,
  DEFAULT_ATTACHMENT_LIMITS,
  DEFAULT_SUPPORTED_CONTENT_TYPES,
  formatAttachmentSize,
  validateAttachmentAdd,
  type AttachmentAddErrorReason,
} from "../attachment-client";
import { dependencyUiFor, LANGUAGE_LABELS } from "../languages";
import type {
  Adapter,
  AiAttachment,
  AiAttachmentCapabilities,
  AiAttachmentLimits,
  AiCandidate,
  AiConversationMessage,
  AiContextSnippet,
} from "../types";
import { logSnippetTimeLabel } from "../unified-log";
import { userErrorMessage } from "../user-message";
import { i18n } from "../i18n";
import { AssistantMarkdownText } from "./ai-markdown";
import VersionDiffModal, { type DiffPane } from "./VersionDiffModal";

export interface AiWorkingCopy {
  code: string;
  requirements: string;
  runtimeConfigText: string;
}

/** M5.5.13: one confirmed context snippet with a client-side identity; the
 * wire shape stays the API's AiContextSnippet. */
export type AiContextSnippetEntry = AiContextSnippet & { id: number };

interface CandidateState {
  value: AiCandidate;
  baseSnapshot: AiWorkingCopy;
  applied: boolean;
}

/** M5.7 Wave B1: the complete request context of one sent round, frozen at
 * send time. Regenerate reuses this snapshot and never reads the current
 * editor / Adapter / config. The wire contract stays exactly Wave A: the
 * backend is the authority for provider/model/credential, so the browser
 * freezes everything it contributes (message, working_copy incl. parsed
 * runtime_config, base version, recent_messages boundary, ordered context
 * snippets) plus the round's adapter identity and UI locale (locale is not
 * part of the wire request; it documents the frozen round context).
 *
 * M5.7 Wave B3: ``attachments`` freezes the B2 wire shape (filename,
 * content_type, strict base64 body) of the round's files. Bodies are bounded
 * by the B2 total-size cap, held in memory only for the round's Regenerate/
 * retry lifetime and never rendered, logged or persisted. */
interface AssistRoundSnapshot {
  adapterId: number;
  message: string;
  baseSnapshot: AiWorkingCopy;
  runtimeConfig: Record<string, unknown>;
  baseVersionId: number | null;
  recentMessages: AiConversationMessage[];
  contextSnippets: AiContextSnippet[];
  attachments: AiAttachment[];
  locale: string;
}

interface VisibleMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  candidate: CandidateState | null;
  /** Wave B1: present only on user messages; the frozen request context of
   * that round, reused verbatim by Regenerate. */
  snapshot: AssistRoundSnapshot | null;
}

/** Candidate diff stores only data; pane labels are derived at render time
 * (like the Workbench diff) so an open modal switches locale immediately. */
interface CandidateDiffState {
  messageId: number;
  panes: Omit<DiffPane, "label">[];
}

interface AiAssistantPanelProps {
  open: boolean;
  adapter: Adapter | null;
  selectedVersionId: number | null;
  selectedVersionSeq: number | null;
  workingCopy: AiWorkingCopy;
  contentReady: boolean;
  busy: boolean;
  /** M5.5.13: confirmed multi-snippet context (code and/or masked log
   * selections), in the order the administrator added them. */
  contextSnippets: AiContextSnippetEntry[];
  /** M5.5.9: Monaco 主题透传，Diff 弹窗与主编辑器保持同一主题。 */
  theme: string;
  onOpen: () => void;
  onClose: () => void;
  onApply: (candidate: AiCandidate) => void;
  onRemoveContextSnippet: (id: number) => void;
  onClearContextSnippets: () => void;
}

/** M5.5.5: DLR-known request lifecycle stages. Reasoning/CoT is never
 * requested, parsed, or displayed — these stages only reflect what the
 * browser itself knows about the in-flight assist request. */
type ProgressStage = "preparing" | "requesting" | "validating" | "succeeded";

function hasOnlyFiniteJsonNumbers(value: unknown): boolean {
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (Array.isArray(value)) {
    return value.every(hasOnlyFiniteJsonNumbers);
  }
  if (typeof value === "object" && value !== null) {
    return Object.values(value).every(hasOnlyFiniteJsonNumbers);
  }
  return true;
}

function parseRuntimeConfig(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    return typeof value === "object" &&
      value !== null &&
      !Array.isArray(value) &&
      hasOnlyFiniteJsonNumbers(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

// M4 §4.1 Candidate 严格校验的浏览器侧防御：后端保证合法，但前端仍拒绝
// 任何形状不符的 Candidate，绝不渲染或允许 Apply。
function isValidAiCandidate(value: unknown): value is AiCandidate {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.summary === "string" &&
    typeof candidate.code === "string" &&
    candidate.code.trim() !== "" &&
    typeof candidate.requirements === "string" &&
    typeof candidate.runtime_config === "object" &&
    candidate.runtime_config !== null &&
    !Array.isArray(candidate.runtime_config) &&
    Array.isArray(candidate.required_secret_keys) &&
    candidate.required_secret_keys.every((key) => typeof key === "string")
  );
}

function snapshotsEqual(left: AiWorkingCopy, right: AiWorkingCopy): boolean {
  return (
    left.code === right.code &&
    left.requirements === right.requirements &&
    left.runtimeConfigText === right.runtimeConfigText
  );
}

/** M5.7 Wave A: recent_messages 仍只取浏览器可见 user/assistant 对话，
 * 最多 8 条；Candidate / reasoning 从不进入该列表。 */
function recentVisibleMessages(messages: VisibleMessage[]): AiConversationMessage[] {
  return messages.slice(-8).map(({ role, content }) => ({ role, content }));
}

/** M5.7 Wave A: DLR 消息 → assistant-ui External Store 消息。只携带可见
 * 文本；Candidate / Secret / Working Copy 等权威语义保持在 DLR 状态中，
 * 不进入第三方 runtime。id 统一为字符串以满足 runtime 合同。 */
function toThreadMessageLike(message: VisibleMessage): ThreadMessageLike {
  return {
    id: String(message.id),
    role: message.role,
    content: [{ type: "text", text: message.content }],
  };
}

/** Composer 提交的 AppendMessage → 纯文本（Wave A 只存在 text part）。 */
function appendMessageText(message: AppendMessage): string {
  if (typeof message.content === "string") {
    return message.content;
  }
  return message.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("");
}

// --- M5.7 Wave B3: attachment helpers ---------------------------------------

/** M5.7 Wave B3: the stable B2 attachment error codes localize through the
 * bundled common.errors table with zh-CN/en key parity (the code is appended
 * in the platform's established style). Every other error keeps the M5.6
 * userErrorMessage contract unchanged. The server never echoes file bodies,
 * filenames, base64 or Secrets into the detail, so nothing here can leak
 * them into the panel either. */
function attachmentServerErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code.startsWith("ai_attachment_")) {
    const key = `errors.${error.code}`;
    if (i18n.exists(key, { ns: "common" })) {
      const message = i18n.getFixedT(i18n.language, "common")(key);
      const locale = i18n.language === "en" ? "en" : "zh-CN";
      return locale === "en"
        ? `${message} (Error code: ${error.code})`
        : `${message}（错误码：${error.code}）`;
    }
  }
  return userErrorMessage(error, fallback);
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
function errorPendingAttachment(file: File, message: string): PendingAttachment {
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
async function completeAttachment(
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
function attachmentAddErrorMessage(
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

/** M5.7 Wave A: 发送按钮。点击路径由 DLR 同步启动 assist 流程（保持既有
 * “准备态先于请求可见”的生命周期回归语义），并阻止原语自带的异步 send()
 * 双路径；Enter / Ctrl/Cmd+Enter 键盘路径仍走表单提交 → External Store
 * Runtime onNew。两条路径汇聚到同一个 DLR runAssist。
 *
 * M5.7 Wave B3: the click also consumes the composer attachment rows (the
 * primitive default send handler is prevented, so the runtime never
 * double-sends); the captured File references are resolved into the frozen
 * round snapshot by DLR's own send path. */
function ComposerSubmitButton(props: {
  disabled: boolean;
  sending: boolean;
  onSend: (text: string, attachments: readonly Attachment[]) => void;
}) {
  const { t } = useTranslation(["ai", "common"]);
  const composer = useAui().composer;
  return (
    <ComposerPrimitive.Send
      className="ai-composer-send"
      data-testid="ai-send"
      disabled={props.disabled}
      onClick={(event) => {
        event.preventDefault();
        const state = composer.getState();
        const text = state.text;
        const attachments = state.attachments;
        if (text.trim() === "" && attachments.length === 0) {
          return;
        }
        composer.setText("");
        // The rows are consumed by DLR's send path; clear them synchronously
        // (adapter.remove is a no-op — no per-attachment browser resources).
        void composer.clearAttachments();
        props.onSend(text, attachments);
      }}
    >
      {props.sending && <Spin size="small" />}
      {t("assistant.send")}
    </ComposerPrimitive.Send>
  );
}

/** M5.7 Wave B3: keeps the AttachmentAdapter's composer view in sync (the
 * add-time count/total checks need the current composer attachments) and
 * exposes composer controls (clear on Adapter switch) to the panel. */
function ComposerAttachmentsBridge(props: {
  onAttachmentsChange: (attachments: readonly Attachment[]) => void;
  onControlsChange: (controls: { clearAttachments: () => Promise<void> } | null) => void;
}) {
  const aui = useAui();
  const attachments = useAuiState((state) => state.composer.attachments);
  useEffect(() => {
    props.onAttachmentsChange(attachments);
  }, [attachments, props]);
  useEffect(() => {
    props.onControlsChange({
      clearAttachments: () => aui.composer.clearAttachments(),
    });
    return () => props.onControlsChange(null);
  }, [aui, props]);
  return null;
}

/** M5.7 Wave B3: one composer attachment row. Accessible name (filename),
 * category/type label, size, ready/error status and a remove button; long
 * filenames ellipsize without pushing the layout (real-browser width gate). */
function AttachmentItem(props: { attachment: Attachment }) {
  const { t } = useTranslation(["ai"]);
  const { attachment } = props;
  const classification = classifyAttachment(
    attachment.name,
    attachment.contentType ?? attachment.file?.type ?? "",
  );
  const category = classification.ok
    ? t(`assistant.attachments.category.${classification.category}`)
    : "";
  const size = attachment.file !== undefined ? formatAttachmentSize(attachment.file.size) : "";
  const failed = attachment.status.type === "incomplete";
  const failedMessage =
    attachment.status.type === "incomplete" ? attachment.status.message : null;
  return (
    <div
      className={`ai-attachment${failed ? " ai-attachment-error" : ""}`}
      data-testid="ai-attachment-item"
    >
      <span className="ai-attachment-name" data-testid="ai-attachment-name" title={attachment.name}>
        {attachment.name}
      </span>
      <span className="ai-attachment-meta" data-testid="ai-attachment-meta">
        {[category, size].filter(Boolean).join(" · ")}
      </span>
      {failed ? (
        <span
          className="ai-attachment-error-message"
          role="alert"
          data-testid="ai-attachment-error-message"
        >
          {failedMessage}
        </span>
      ) : (
        <span className="ai-attachment-ready" data-testid="ai-attachment-ready">
          {t("assistant.attachments.ready")}
        </span>
      )}
      <AttachmentPrimitive.Remove
        className="ai-attachment-remove"
        data-testid="ai-attachment-remove"
        aria-label={t("assistant.attachments.remove", { name: attachment.name })}
        title={t("assistant.attachments.remove", { name: attachment.name })}
      >
        ×
      </AttachmentPrimitive.Remove>
    </div>
  );
}

/** M5.7 Wave B3: the composer attachment surface — drag & drop zone, file
 * picker, accessible list, bounds/privacy hints and pre-send validation.
 * All client checks mirror the B2 server limits; the server remains the
 * authoritative validator (stable ai_attachment_* codes). */
function ComposerAttachmentArea(props: {
  disabled: boolean;
  sending: boolean;
  limits: AiAttachmentLimits;
  supportedContentTypes: readonly string[];
  error: string | null;
  onErrorChange: (message: string | null) => void;
  onSend: (text: string, attachments: readonly Attachment[]) => void;
}) {
  const { t } = useTranslation(["ai"]);
  const aui = useAui();
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function addFiles(files: readonly File[]) {
    const composer = aui.composer;
    for (const file of files) {
      const verdict = validateAttachmentAdd(file, props.limits, composer.getState().attachments);
      if (!verdict.ok) {
        props.onErrorChange(
          attachmentAddErrorMessage(verdict.reason, props.limits, (key, options) =>
            t(key, options),
          ),
        );
        continue;
      }
      try {
        await composer.addAttachment(file);
        // A successful add clears the previous rejection message.
        props.onErrorChange(null);
      } catch {
        // The runtime accept-table check (raw English) is never surfaced:
        // DLR's own pre-validation already rejected every unsupported file.
        props.onErrorChange(t("assistant.attachments.error.unsupported"));
      }
    }
  }

  function handleDrop(event: React.DragEvent<HTMLDivElement>) {
    // Own the drop handling: the primitive's internal handler is composed
    // after ours and skips when the event is default-prevented.
    event.preventDefault();
    if (props.disabled) {
      return;
    }
    const files = Array.from(event.dataTransfer.files);
    if (files.length > 0) {
      void addFiles(files);
    }
  }

  function handleInputChange(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    // Reset so re-selecting the same file fires change again.
    event.target.value = "";
    if (files.length > 0) {
      void addFiles(files);
    }
  }

  return (
    <ComposerPrimitive.AttachmentDropzone
      className="ai-composer-dropzone"
      data-testid="ai-attachment-dropzone"
      disabled={props.disabled}
      onDropCapture={handleDrop}
    >
      <ComposerPrimitive.Attachments>
        {({ attachment }) => <AttachmentItem attachment={attachment} />}
      </ComposerPrimitive.Attachments>
      <ComposerPrimitive.Input
        data-testid="ai-message-input"
        autoFocus
        aria-label={t("assistant.commandLabel")}
        placeholder={t("assistant.commandPlaceholder")}
        minRows={3}
        maxRows={10}
        disabled={props.disabled}
      />
      {props.error !== null && (
        <p className="ai-attachment-panel-error" role="alert" data-testid="ai-attachment-error">
          {props.error}
        </p>
      )}
      <div className="ai-composer-actions">
        <span className="ai-attachment-hint" data-testid="ai-attachment-hint">
          {t("assistant.attachments.hint", {
            count: props.limits.max_attachments,
            size: formatAttachmentSize(props.limits.max_file_bytes),
            total: formatAttachmentSize(props.limits.max_total_bytes),
          })}
        </span>
        <span className="ai-attachment-privacy" data-testid="ai-attachment-privacy">
          {t("assistant.attachments.privacyNotice")}
        </span>
        <Button
          size="small"
          className="ai-attachment-add"
          data-testid="ai-attachment-add"
          disabled={props.disabled}
          aria-label={t("assistant.attachments.addAria")}
          title={t("assistant.attachments.addAria")}
          onClick={() => fileInputRef.current?.click()}
        >
          {t("assistant.attachments.add")}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          data-testid="ai-attachment-input"
          accept={acceptStringFor(props.supportedContentTypes)}
          aria-label={t("assistant.attachments.addAria")}
          onChange={handleInputChange}
        />
        <ComposerSubmitButton
          disabled={props.disabled}
          sending={props.sending}
          onSend={props.onSend}
        />
      </div>
    </ComposerPrimitive.AttachmentDropzone>
  );
}

/** M5.7 Wave B1: Regenerate entry of one assistant round. DLR renders the
 * button itself: the ActionBarPrimitive.Reload primitive is not wired to the
 * External Store Runtime under MessageProvider in assistant-ui 0.15.x (its
 * reload throws "Not supported in ThreadMessageProvider"), so the click goes
 * through the official runtime regenerate path `thread.startRun({parentId})`,
 * which the External Store Runtime forwards to the adapter's `onReload`.
 *
 * M5.7 Wave B3: a round whose send failed has no assistant reply; the same
 * entry renders on the failed user round as "Retry", reusing the frozen
 * snapshot (message, context and attachments) and appending a fresh reply. */
function RegenerateButton(props: {
  userMessageId: number;
  disabled: boolean;
  retry?: boolean;
}) {
  const { t } = useTranslation(["ai"]);
  const aui = useAui();
  const label = props.retry ? t("assistant.retry") : t("assistant.regenerate");
  const ariaLabel = props.retry
    ? t("assistant.retryAria")
    : t("assistant.regenerateAria");
  return (
    <Button
      size="small"
      type="text"
      className="ai-regenerate"
      data-testid={props.retry ? "ai-retry" : "ai-regenerate"}
      disabled={props.disabled}
      aria-label={ariaLabel}
      title={ariaLabel}
      onClick={() => {
        void aui.thread.startRun({ parentId: String(props.userMessageId) });
      }}
    >
      {label}
    </Button>
  );
}

/** M5.5.13: user-facing label of one context snippet ("代码 第 12–20 行",
 * "实时日志 10:21:03–10:21:08"). Log ranges are derived from the capture-time
 * prefixes of the browser-visible masked text only. */
function contextSnippetLabel(
  snippet: AiContextSnippetEntry,
  language: Adapter["language"],
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (snippet.source === "code") {
    const range =
      snippet.end_line > snippet.start_line
        ? translate("assistant.context.lineRange", { start: snippet.start_line, end: snippet.end_line })
        : translate("assistant.context.line", { line: snippet.start_line });
    return translate("assistant.context.codeRange", {
      range,
      language: LANGUAGE_LABELS[language] ?? language,
    });
  }
  const timeLabel = logSnippetTimeLabel(snippet.text);
  if (timeLabel !== null) {
    return translate("assistant.context.logTime", { time: timeLabel });
  }
  const range =
    snippet.end_line > snippet.start_line
      ? translate("assistant.context.lineRange", { start: snippet.start_line, end: snippet.end_line })
      : translate("assistant.context.line", { line: snippet.start_line });
  return translate("assistant.context.logRange", { range });
}

export default function AiAssistantPanel(props: AiAssistantPanelProps) {
  const { t, i18n } = useTranslation(["ai", "common"]);
  const [messages, setMessages] = useState<VisibleMessage[]>([]);
  const [sending, setSending] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [boundSecretKeys, setBoundSecretKeys] = useState<Set<string>>(new Set());
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingsVerified, setBindingsVerified] = useState(false);
  const [candidateDiff, setCandidateDiff] = useState<CandidateDiffState | null>(null);
  const [progressStage, setProgressStage] = useState<ProgressStage | null>(null);
  // M5.5.13: the floating entry is draggable within the viewport. The position
  // is deliberately NOT persisted anywhere (no localStorage/sessionStorage/
  // database): a refresh restores the product default (CSS right: 16px).
  const [entryOffset, setEntryOffset] = useState<{ x: number; y: number } | null>(null);
  const dragStateRef = useRef<{
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startOffset: { x: number; y: number };
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);
  const requestGeneration = useRef(0);
  const bindingsGeneration = useRef(0);
  const nextMessageId = useRef(1);
  const conversationRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const previousOpenRef = useRef(props.open);
  // M5.7 Wave B3: attachment state — the capability table (bounded limits and
  // supported MIME types) and the last client-side rejection message.
  const [attachmentCapabilities, setAttachmentCapabilities] =
    useState<AiAttachmentCapabilities | null>(null);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const attachmentLimits: AiAttachmentLimits =
    attachmentCapabilities?.limits ?? DEFAULT_ATTACHMENT_LIMITS;
  const supportedContentTypes: readonly string[] =
    attachmentCapabilities?.supported_content_types ?? DEFAULT_SUPPORTED_CONTENT_TYPES;
  // The AttachmentAdapter is created once; everything it needs at call time
  // (limits, current composer attachments, translations, wire-body cache and
  // the send entry) flows through refs below. The refs are updated in
  // effects (never during render); the adapter only reads them from event
  // handlers and async flows, so the one-render lag is irrelevant.
  const attachmentLimitsRef = useRef(attachmentLimits);
  const translateRef = useRef<(key: string, options?: Record<string, unknown>) => string>(t);
  const composerAttachmentsRef = useRef<readonly Attachment[]>([]);
  const wireCacheRef = useRef(new WeakMap<object, AiAttachment>());
  const composerControlsRef = useRef<{ clearAttachments: () => Promise<void> } | null>(null);
  const previousAdapterIdRef = useRef<number | null>(props.adapter?.id ?? null);
  useEffect(() => {
    attachmentLimitsRef.current = attachmentLimits;
  }, [attachmentLimits]);
  useEffect(() => {
    translateRef.current = t;
  }, [t]);

  useLayoutEffect(() => {
    if (props.open && !previousOpenRef.current) {
      // The conversation DOM is recreated on reopen, so an old scrolled-up
      // position cannot be restored meaningfully. Reopen at the latest item.
      followLatestRef.current = true;
    }
    previousOpenRef.current = props.open;
    const conversation = conversationRef.current;
    if (conversation !== null && followLatestRef.current) {
      conversation.scrollTop = conversation.scrollHeight;
    }
  }, [messages, props.open, sending]);

  useEffect(
    () => () => {
      requestGeneration.current += 1;
    },
    [],
  );

  const adapterId = props.adapter?.id ?? null;
  useEffect(() => {
    if (!props.open || adapterId === null) {
      return;
    }
    const generation = ++bindingsGeneration.current;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- opening the panel starts an intentional metadata load
    setBindingsLoading(true);
    setBindingsVerified(false);
    void api
      .listAdapterBindings(adapterId)
      .then((bindings) => {
        if (!cancelled && generation === bindingsGeneration.current) {
          setBoundSecretKeys(new Set(bindings.map((binding) => binding.env_key)));
          setBindingsVerified(true);
        }
      })
      .catch(() => {
        if (!cancelled && generation === bindingsGeneration.current) {
          setBoundSecretKeys(new Set());
          setBindingsVerified(false);
        }
      })
      .finally(() => {
        if (!cancelled && generation === bindingsGeneration.current) {
          setBindingsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [adapterId, props.open]);

  // M5.7 Wave B3: load the stable B2 capability table (limits + accepted MIME
  // types) while the panel is open. Fail-soft: the canonical B2 defaults keep
  // the upload UI bounded; the server remains the authoritative validator.
  useEffect(() => {
    if (!props.open || adapterId === null) {
      return;
    }
    let cancelled = false;
    void api
      .getAiAttachmentCapabilities()
      .then((capabilities) => {
        if (!cancelled) {
          setAttachmentCapabilities(capabilities);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAttachmentCapabilities(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [adapterId, props.open]);

  // M5.7 Wave B3: Adapter switch isolates every historical run state — the
  // composer's pending attachments are cleared (nothing composed for the old
  // Adapter can leak into the new one) and the frozen attachment bodies of
  // past rounds are released. Regenerate across Adapters is additionally
  // blocked by the round-snapshot adapter guard in runAssist.
  useEffect(() => {
    const nextAdapterId = props.adapter?.id ?? null;
    if (previousAdapterIdRef.current === nextAdapterId) {
      return;
    }
    previousAdapterIdRef.current = nextAdapterId;
    setMessages((current) =>
      current.map((message) =>
        message.snapshot !== null && message.snapshot.attachments.length > 0
          ? { ...message, snapshot: { ...message.snapshot, attachments: [] } }
          : message,
      ),
    );
    void composerControlsRef.current?.clearAttachments();
    setAttachmentError(null);
  }, [props.adapter?.id]);

  /** M5.7 Wave B3: type predicate — the runtime's Attachment union nests the
 * status discriminant, which TypeScript cannot narrow through assignments;
 * the predicate narrows the full union for the send/resolve path. */
function isCompleteAttachment(attachment: Attachment): attachment is CompleteAttachment {
  return attachment.status.type === "complete";
}

/** M5.7 Wave B3: official assistant-ui AttachmentAdapter helper — resolve
 * one composer attachment into its complete form (see completeAttachment). */
async function resolveComposerAttachment(
  attachment: Attachment,
  adapter: AttachmentAdapter,
): Promise<CompleteAttachment> {
  if (isCompleteAttachment(attachment)) {
    return attachment;
  }
  return adapter.send(attachment);
}

/** M5.7 Wave B3: the official assistant-ui AttachmentAdapter for this
   * External Store Runtime. ``accept`` mirrors the B2 supported MIME table;
   * ``add`` validates every file against the B2 bounds (returning a visible,
   * removable error row for rejections so picker errors are never swallowed);
   * ``send`` reads the strict base64 body once and caches it for the wire
   * payload; ``remove`` releases nothing because DLR holds no per-attachment
   * browser resources (no object URLs, no previews). */
  const attachmentAdapter = useMemo<AttachmentAdapter>(
    () => ({
      accept: acceptStringFor(DEFAULT_SUPPORTED_CONTENT_TYPES),
      async add({ file }) {
        const limits = attachmentLimitsRef.current;
        const verdict = validateAttachmentAdd(file, limits, composerAttachmentsRef.current);
        if (!verdict.ok) {
          return errorPendingAttachment(
            file,
            attachmentAddErrorMessage(verdict.reason, limits, translateRef.current),
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
        return completeAttachment(attachment, wireCacheRef.current);
      },
      async remove() {
        // Nothing to release: attachments are held as plain File references
        // (GC reclaims them once the composer drops the row); object URLs are
        // never created, so there is nothing to revoke.
      },
    }),
    [],
  );

  /** M5.7 Wave B3: turn the composer's attachment rows into the B2 wire shape
   * (filename, content_type, strict base64 body), re-verifying the total
   * size bound. The per-file read happens exactly once (cached by the
   * adapter); the result is frozen into the round snapshot. */
  async function resolveComposerAttachments(
    composerAttachments: readonly Attachment[],
  ): Promise<AiAttachment[]> {
    const wireAttachments: AiAttachment[] = [];
    let totalBytes = 0;
    for (const attachment of composerAttachments) {
      const complete = await resolveComposerAttachment(attachment, attachmentAdapter);
      const cached = wireCacheRef.current.get(complete);
      const file = complete.file;
      if (file === undefined) {
        // Every complete attachment originates from this adapter's send()
        // (cache hit above); a missing File reference is unreachable in
        // practice and cannot produce a body — skip defensively.
        continue;
      }
      const wire =
        cached ??
        (await buildWireAttachment(
          complete.name,
          complete.contentType ?? file.type,
          file,
        ));
      totalBytes += base64DecodedSize(wire.data_base64);
      if (totalBytes > attachmentLimitsRef.current.max_total_bytes) {
        throw new Error(
          attachmentAddErrorMessage(
            "total_too_large",
            attachmentLimitsRef.current,
            translateRef.current,
          ),
        );
      }
      wireAttachments.push(wire);
    }
    return wireAttachments;
  }

  /** M5.7 Wave A/B1: DLR-owned single-shot assist flow. Called by the External
   * Store runtime's onNew (with a freshly frozen round snapshot) and by its
   * onReload (with the frozen snapshot of the original round). Never throws:
   * all failures converge to the panel error contract, and a failed
   * Regenerate keeps the original assistant message intact.
   *
   * `replaceAssistantMessageId` is null on a normal send; on Regenerate it is
   * the id of the assistant message being regenerated — on success it is
   * replaced in place by the new result (no duplicate user message, no branch
   * tree; later rounds are history and stay untouched). */
  async function runAssist(
    snapshot: AssistRoundSnapshot,
    replaceAssistantMessageId: number | null,
  ) {
    const adapter = props.adapter;
    if (
      adapter === null ||
      !props.contentReady ||
      props.busy ||
      sending ||
      // Wave B1: a regenerated round is bound to the Adapter it was sent
      // against; it must never silently rerun against a switched Adapter.
      snapshot.adapterId !== adapter.id
    ) {
      return;
    }

    const generation = ++requestGeneration.current;
    const requestAdapterId = snapshot.adapterId;
    // Regenerating explicitly returns to the regenerated exchange. A later
    // manual upward scroll can still pause following before the reply arrives.
    followLatestRef.current = true;
    setPanelError(null);
    setSending(true);
    setProgressStage("preparing");
    try {
      // M5.5.5: stages only reflect DLR's own request lifecycle. "preparing"
      // is committed for at least one frame so the platform stage is visible
      // before the network request begins.
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      if (generation !== requestGeneration.current) {
        return;
      }
      setProgressStage("requesting");
      const response = await api.assistAdapter(requestAdapterId, {
        // Wave B1: the frozen round snapshot, never the current editor,
        // Adapter or config.
        message: snapshot.message,
        working_copy: {
          code: snapshot.baseSnapshot.code,
          requirements: snapshot.baseSnapshot.requirements,
          runtime_config: snapshot.runtimeConfig,
        },
        recent_messages: snapshot.recentMessages,
        base_version_id: snapshot.baseVersionId,
        // M5.5.13: all confirmed snippets in the order they were added; the
        // snapshots are frozen at click time, later cursor movement never
        // changes them. Omitted entirely when none were added.
        ...(snapshot.contextSnippets.length === 0
          ? {}
          : { context_snippets: snapshot.contextSnippets }),
        // M5.7 Wave B3: request-only attachments frozen at send time (strict
        // base64 per the B2 contract). Omitted entirely for attachment-free
        // rounds so those requests stay byte-compatible with Wave A/B1. The
        // bodies never enter recent_messages, the thread DOM or any log.
        ...(snapshot.attachments.length === 0
          ? {}
          : { attachments: snapshot.attachments }),
      });
      // The component is keyed by Adapter in App, and this explicit guard also
      // prevents a late response from committing across an Adapter switch.
      if (generation !== requestGeneration.current) {
        return;
      }
      setProgressStage("validating");
      // Bindings can change in the Workbench while this panel stays open.
      // Refresh after generation so the Candidate warning reflects the
      // current names, while a failed check is reported as unknown.
      const bindingsRequestGeneration = ++bindingsGeneration.current;
      setBindingsLoading(true);
      try {
        const bindings = await api.listAdapterBindings(requestAdapterId);
        if (generation !== requestGeneration.current) {
          return;
        }
        if (bindingsRequestGeneration === bindingsGeneration.current) {
          setBoundSecretKeys(new Set(bindings.map((binding) => binding.env_key)));
          setBindingsVerified(true);
        }
      } catch {
        if (generation !== requestGeneration.current) {
          return;
        }
        if (bindingsRequestGeneration === bindingsGeneration.current) {
          setBoundSecretKeys(new Set());
          setBindingsVerified(false);
        }
      } finally {
        if (bindingsRequestGeneration === bindingsGeneration.current) {
          setBindingsLoading(false);
        }
      }
      const assistantMessage: VisibleMessage = {
        id: nextMessageId.current++,
        role: "assistant",
        content: response.message,
        snapshot: null,
        // The regenerated Candidate stays anchored to the frozen base snapshot
        // of the original round, so the stale check keeps comparing against
        // the current editor honestly.
        candidate:
          response.candidate === null || !isValidAiCandidate(response.candidate)
            ? null
            : {
                value: response.candidate,
                baseSnapshot: snapshot.baseSnapshot,
                applied: false,
              },
      };
      setMessages((current) => {
        if (replaceAssistantMessageId === null) {
          return [...current, assistantMessage];
        }
        const targetIndex = current.findIndex(
          (message) => message.id === replaceAssistantMessageId,
        );
        if (targetIndex === -1) {
          // The target round vanished (e.g. the panel was remounted mid-run);
          // surface the result as a new reply rather than dropping it.
          return [...current, assistantMessage];
        }
        return current.map((message) =>
          message.id === replaceAssistantMessageId ? assistantMessage : message,
        );
      });
      // M5.5.5: the success stage claims "waiting to view the Diff" only when
      // a Candidate is actually rendered; a plain-text reply converges
      // silently to the assistant message itself.
      setProgressStage(assistantMessage.candidate === null ? null : "succeeded");
    } catch (error) {
      if (generation === requestGeneration.current) {
        setPanelError(
          attachmentServerErrorMessage(error, t("assistant.errors.requestFailed")),
        );
        // M5.5.5: failures converge to an explicit error state; no progress
        // line lingers or keeps claiming an unfinished stage.
        setProgressStage(null);
      }
    } finally {
      if (generation === requestGeneration.current) {
        setSending(false);
      }
    }
  }

  /** M5.7 Wave B1: freeze the complete request context of one round at send
   * time. Returns null (and reports the error) when the runtime parameters do
   * not parse; Regenerate never revalidates — it reuses the frozen value.
   * M5.7 Wave B3: the B2 wire attachment bodies join the frozen context. */
  function buildRoundSnapshot(
    adapter: Adapter,
    message: string,
    attachments: AiAttachment[],
  ): AssistRoundSnapshot | null {
    const runtimeConfig = parseRuntimeConfig(props.workingCopy.runtimeConfigText);
    if (runtimeConfig === null) {
      setPanelError(t("assistant.invalidRuntimeConfig"));
      return null;
    }
    return {
      adapterId: adapter.id,
      message: message.trim(),
      baseSnapshot: { ...props.workingCopy },
      runtimeConfig,
      baseVersionId: props.selectedVersionId,
      recentMessages: recentVisibleMessages(messages),
      contextSnippets: props.contextSnippets.map(
        ({ source, text, start_line, end_line }) => ({
          source,
          text,
          start_line,
          end_line,
        }),
      ),
      attachments,
      locale: i18n.language,
    };
  }

  /** M5.7 Wave B1/B3: shared send entry for the composer click path and the
   * External Store runtime's onNew. Resolves the composer attachment rows
   * into the frozen round snapshot, appends the user message, then runs the
   * assist flow. Text-only sends keep the Wave A synchronous
   * preparing-stage contract; attachment reads happen before freezing. */
  async function sendMessage(rawText: string, composerAttachments: readonly Attachment[]) {
    const text = rawText.trim();
    const adapter = props.adapter;
    if (
      adapter === null ||
      !props.contentReady ||
      props.busy ||
      sending ||
      (text === "" && composerAttachments.length === 0)
    ) {
      return;
    }
    let wireAttachments: AiAttachment[];
    try {
      wireAttachments =
        composerAttachments.length === 0
          ? []
          : await resolveComposerAttachments(composerAttachments);
    } catch (error) {
      // A file body could not be produced (defensive; sizes were validated
      // at add time): surface the actionable message and keep the composer.
      setAttachmentError(
        error instanceof Error ? error.message : t("assistant.attachments.error.readFailed"),
      );
      return;
    }
    const snapshot = buildRoundSnapshot(adapter, text, wireAttachments);
    if (snapshot === null) {
      return;
    }
    const userMessage: VisibleMessage = {
      id: nextMessageId.current++,
      role: "user",
      content: snapshot.message,
      candidate: null,
      snapshot,
    };
    setMessages((current) => [...current, userMessage]);
    setAttachmentError(null);
    await runAssist(snapshot, null);
  }

  /** M5.7 Wave A/B1: External Store Runtime — DLR 继续持有消息状态，assistant-ui
   * 只读取镜像消息并驱动 Composer 提交与 Regenerate 入口。 */
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: sending,
    isDisabled:
      props.adapter === null || !props.contentReady || props.busy || sending,
    convertMessage: toThreadMessageLike,
    adapters: { attachments: attachmentAdapter },
    onNew: async (message: AppendMessage) => {
      await sendMessage(appendMessageText(message), message.attachments ?? []);
    },
    // Wave B1: Regenerate — parentId is the id of the user message whose
    // assistant reply should be regenerated; the round's frozen snapshot is
    // reused verbatim. Wave B3: a round whose send failed has no assistant
    // reply — retrying reuses the same frozen snapshot (including the
    // attachments) and appends a fresh reply without duplicating the user
    // message.
    onReload: async (parentId: string | null) => {
      if (parentId === null) {
        return;
      }
      const userMessage = messages.find(
        (message) => message.role === "user" && String(message.id) === parentId,
      );
      if (userMessage === undefined) {
        return;
      }
      const snapshot = userMessage.snapshot;
      if (snapshot === null) {
        return;
      }
      const userIndex = messages.findIndex((message) => message.id === userMessage.id);
      const assistantMessage = messages[userIndex + 1];
      if (assistantMessage === undefined || assistantMessage.role !== "assistant") {
        await runAssist(snapshot, null);
        return;
      }
      await runAssist(snapshot, assistantMessage.id);
    },
  });

  function openCandidateDiff(messageId: number, candidateState: CandidateState) {
    const adapter = props.adapter;
    if (adapter === null) {
      return;
    }
    const candidate = candidateState.value;
    setCandidateDiff({
      messageId,
      panes: [
        {
          key: "code",
          language: adapter.language,
          original: props.workingCopy.code,
          modified: candidate.code,
        },
        {
          key: "requirements",
          language: "plaintext",
          original: props.workingCopy.requirements,
          modified: candidate.requirements,
        },
        {
          key: "runtime-config",
          language: "json",
          original: props.workingCopy.runtimeConfigText,
          modified: JSON.stringify(candidate.runtime_config, null, 2),
        },
      ],
    });
  }

  function applyCandidate(messageId: number, candidate: AiCandidate) {
    if (
      !props.contentReady ||
      props.busy ||
      props.adapter?.archived_at ||
      props.adapter?.runtime_locked === true
    ) {
      return;
    }
    props.onApply(candidate);
    setMessages((current) =>
      current.map((message) =>
        message.id === messageId && message.candidate !== null
          ? { ...message, candidate: { ...message.candidate, applied: true } }
          : message,
      ),
    );
    // M5.5.13: a successful Apply closes the Diff automatically and returns to
    // the Workbench. Failed/blocked applies never reach this point (the Apply
    // button stays disabled with its reason), so the Diff and its error/stale
    // information are preserved in every failure path.
    setCandidateDiff(null);
  }

  // M5.5.13: drag the floating entry within the visible work area without
  // triggering a click. Pointer events are used so the drag works for mouse
  // and touch; a small movement threshold separates "drag" from "click".
  const ENTRY_SIZE = 46;
  const ENTRY_MARGIN = 8;
  const DRAG_THRESHOLD_PX = 4;

  /** The positioned containing block of the floating entry (the nearest
   * positioned ancestor, e.g. .console-body), or the viewport when none
   * exists. The inline left/top and the clamp bounds live in this space. */
  function entryContainerRect(button: HTMLElement): { left: number; top: number; width: number; height: number } {
    const host = button.closest(".ai-assistant-collapsed") as HTMLElement | null;
    const container = host?.offsetParent ?? null;
    if (container instanceof Element) {
      return container.getBoundingClientRect();
    }
    return { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
  }

  function handleEntryPointerDown(event: React.PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 && event.pointerType === "mouse") {
      return;
    }
    // The drag must start from the button's actual rendered position (the CSS
    // default right:16px center, or a previous drag offset), never from
    // (0,0): otherwise the first drag would teleport the button to the
    // top-left corner instead of following the pointer. getBoundingClientRect
    // is viewport-relative, so it is converted into containing-block
    // coordinates, which is the space the inline left/top lives in.
    const containerRect = entryContainerRect(event.currentTarget);
    const rect = event.currentTarget.getBoundingClientRect();
    dragStateRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startOffset: {
        x: rect.left - containerRect.left,
        y: rect.top - containerRect.top,
      },
      moved: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function handleEntryPointerMove(event: React.PointerEvent<HTMLButtonElement>) {
    const dragState = dragStateRef.current;
    if (dragState === null || dragState.pointerId !== event.pointerId) {
      return;
    }
    const deltaX = event.clientX - dragState.startClientX;
    const deltaY = event.clientY - dragState.startClientY;
    if (!dragState.moved && Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD_PX) {
      return;
    }
    dragState.moved = true;
    // Clamp inside the visible containing block: the button never leaves the
    // work area (top/bottom edge included; no translate residue shifts it).
    const containerRect = entryContainerRect(event.currentTarget);
    const maxX = Math.max(ENTRY_MARGIN, containerRect.width - ENTRY_SIZE - ENTRY_MARGIN);
    const maxY = Math.max(ENTRY_MARGIN, containerRect.height - ENTRY_SIZE - ENTRY_MARGIN);
    const x = Math.min(maxX, Math.max(ENTRY_MARGIN, dragState.startOffset.x + deltaX));
    const y = Math.min(maxY, Math.max(ENTRY_MARGIN, dragState.startOffset.y + deltaY));
    setEntryOffset({ x, y });
  }

  function handleEntryPointerUp(event: React.PointerEvent<HTMLButtonElement>) {
    const dragState = dragStateRef.current;
    if (dragState === null || dragState.pointerId !== event.pointerId) {
      return;
    }
    if (dragState.moved) {
      // The pointer movement was a drag, not a click: swallow the synthetic
      // click that follows so the panel never toggles accidentally.
      suppressClickRef.current = true;
    }
    dragStateRef.current = null;
  }

  const collapsedEntry = (
    <aside
      className="ai-assistant ai-assistant-collapsed"
      // M5.5.13: after a drag, the inline left/top (on the positioned aside
      // itself) is the authoritative viewport position; transform: none drops
      // the default translateY(-50%) so the clamped coordinates match the
      // actual rendered position exactly (otherwise the button renders 23px
      // higher than clamped and could leave the viewport at the top edge).
      style={
        entryOffset === null
          ? undefined
          : { left: entryOffset.x, top: entryOffset.y, transform: "none" }
      }
    >
      <Button
        type="primary"
        className="ai-assistant-open"
        data-testid="open-ai-assistant"
            aria-label={t("assistant.open")}
        aria-expanded={false}
        onPointerDown={handleEntryPointerDown}
        onPointerMove={handleEntryPointerMove}
        onPointerUp={handleEntryPointerUp}
        onClick={() => {
          if (suppressClickRef.current) {
            suppressClickRef.current = false;
            return;
          }
          props.onOpen();
        }}
      >
        AI
      </Button>
    </aside>
  );

  const contextVersion =
    props.selectedVersionSeq === null
      ? t("assistant.context.unsavedVersion")
      : t("assistant.context.version", { seq: props.selectedVersionSeq });

  const composerDisabled =
    props.adapter === null || !props.contentReady || props.busy || sending;

  const expandedPanel = (
    <aside className="ai-assistant ai-assistant-expanded" data-testid="ai-assistant-panel">
      <AssistantRuntimeProvider runtime={runtime}>
        <ThreadPrimitive.Root className="ai-thread">
          <div className="ai-assistant-header">
            <div>
              <strong>{t("assistant.title")}</strong>
              <p>{t("assistant.notice")}</p>
            </div>
            <Button
              type="text"
              data-testid="close-ai-assistant"
              aria-label={t("assistant.close")}
              aria-expanded={true}
              onClick={props.onClose}
            >
              ×
            </Button>
          </div>

          <div className="ai-assistant-context" data-testid="ai-current-context">
            {props.adapter === null ? (
               <span>{t("assistant.noAdapter")}</span>
            ) : (
              <>
                <strong>{props.adapter.name}</strong>
                <span>{LANGUAGE_LABELS[props.adapter.language]} · {contextVersion}</span>
              </>
            )}
          </div>

          {props.adapter !== null && props.contextSnippets.length > 0 && (
            <div className="ai-snippets" data-testid="ai-context-snippets">
              <div className="ai-snippets-header">
                 <span>{t("assistant.contextSnippets")}</span>
                <Button
                  size="small"
                  type="text"
                  data-testid="ai-clear-all-snippets"
                   aria-label={t("assistant.clearSnippets")}
                  onClick={props.onClearContextSnippets}
                >
                  {t("actions.clearAll", { ns: "common" })}
                </Button>
              </div>
              {props.adapter !== null &&
                (() => {
                  const adapterLanguage = props.adapter.language;
                  return props.contextSnippets.map((snippet) => (
                    <div
                      key={snippet.id}
                      className="ai-snippet-item"
                      data-testid={`ai-snippet-${snippet.id}`}
                    >
                      <span className="ai-snippet-label" data-testid="ai-snippet-label">
                         {contextSnippetLabel(snippet, adapterLanguage, (key, options) => t(key, options))}
                      </span>
                      <Button
                        size="small"
                        type="text"
                        data-testid={`ai-remove-snippet-${snippet.id}`}
                         aria-label={t("assistant.removeSnippet")}
                        onClick={() => props.onRemoveContextSnippet(snippet.id)}
                      >
                         {t("assistant.remove")}
                      </Button>
                    </div>
                  ));
                })()}
            </div>
          )}

          <ThreadPrimitive.Viewport
            ref={conversationRef}
            className="ai-conversation"
            data-testid="ai-conversation"
            // M5.7 Wave A: DLR owns the scroll-follow semantics (32px
            // threshold, explicit follow/resume on reopen) on the primitive
            // viewport; assistant-ui's built-in autoscroll is disabled so the
            // two mechanisms never fight.
            autoScroll={false}
            scrollToBottomOnInitialize={false}
            scrollToBottomOnRunStart={false}
            scrollToBottomOnThreadSwitch={false}
            onScroll={(event) => {
              const target = event.currentTarget;
              followLatestRef.current =
                target.scrollHeight - target.clientHeight - target.scrollTop <= 32;
            }}
          >
            {messages.length === 0 ? (
              <p className="ai-conversation-empty" data-testid="ai-conversation-empty">
                 {t("assistant.empty")}
              </p>
            ) : (
              messages.map((message, index) => {
                const candidateState = message.candidate;
                const stale =
                  candidateState !== null &&
                  !candidateState.applied &&
                  !snapshotsEqual(props.workingCopy, candidateState.baseSnapshot);
                const missingKeys =
                  candidateState === null
                    ? []
                    : [...new Set(candidateState.value.required_secret_keys)].filter(
                        (key) => !boundSecretKeys.has(key),
                      );
                return (
                  <MessageProvider
                    key={message.id}
                    message={fromThreadMessageLike(
                      toThreadMessageLike(message),
                      String(message.id),
                      { type: "complete", reason: "stop" },
                    )}
                    index={index}
                    isLast={index === messages.length - 1}
                  >
                    <MessagePrimitive.Root
                      className={`ai-message ai-message-${message.role}`}
                      data-testid={`ai-message-${message.role}`}
                    >
                      <span className="ai-message-role">{message.role === "user" ? t("assistant.user") : "AI"}</span>
                      <MessagePrimitive.Content
                        components={{ Text: AssistantMarkdownText }}
                      />
                      {/* M5.7 Wave B1: Regenerate entry — one per assistant
                          round; the previous message is always the round's
                          user message (DLR appends strictly alternating
                          pairs). Disabled while a request is in flight or the
                          panel is otherwise gated; the runtime/onReload
                          guards still re-check every gate.
                          M5.7 Wave B3: a round whose send failed has no
                          assistant reply; the entry renders on the failed
                          user round as "Retry" and reuses the frozen snapshot
                          (message, context and attachments). */}
                      {(message.role === "assistant" && index > 0) ||
                        (message.role === "user" &&
                          message.snapshot !== null &&
                          index === messages.length - 1 &&
                          !sending) ? (
                        <div className="ai-message-actions">
                          <RegenerateButton
                            userMessageId={
                              message.role === "assistant"
                                ? messages[index - 1].id
                                : message.id
                            }
                            retry={message.role === "user"}
                            disabled={
                              sending ||
                              props.busy ||
                              !props.contentReady ||
                              props.adapter === null
                            }
                          />
                        </div>
                      ) : null}
                      {candidateState !== null && (
                        <div className="ai-candidate" data-testid="ai-candidate">
                          <p className="ai-candidate-ready" data-testid="ai-candidate-ready">
                             {t("assistant.candidateReady")}
                          </p>
                          <strong data-testid="ai-candidate-summary">
                            {candidateState.value.summary}
                          </strong>
                          {candidateState.value.required_secret_keys.length > 0 && (
                            <p className="ai-secret-suggestion" data-testid="ai-required-secret-keys">
                               {t("assistant.requiredSecrets", { keys: candidateState.value.required_secret_keys.join(", ") })}
                            </p>
                          )}
                          {!bindingsLoading && bindingsVerified && missingKeys.length > 0 && (
                            <p className="ai-secret-warning" role="alert" data-testid="ai-missing-secret-keys">
                               {t("assistant.missingSecrets", { keys: missingKeys.join(", ") })}
                            </p>
                          )}
                          {!bindingsLoading && !bindingsVerified && (
                            <p className="ai-secret-check-unavailable" role="alert">
                               {t("assistant.secretCheckUnavailable")}
                            </p>
                          )}
                          {stale && (
                            <div className="ai-stale-warning" role="alert" data-testid="ai-candidate-stale">
                               <strong>{t("assistant.staleTitle")}</strong>
                               <span>{t("assistant.staleDescription")}</span>
                            </div>
                          )}
                          {props.adapter?.archived_at && (
                            <p className="ai-secret-warning" role="alert" data-testid="ai-archived-apply-blocked">
                               {t("assistant.archivedApplyBlocked")}
                            </p>
                          )}
                          {candidateState.applied && (
                            <p className="ai-candidate-applied" role="status" data-testid="ai-candidate-applied">
                               {t("assistant.applied")}
                            </p>
                          )}
                          <div className="ai-candidate-actions">
                            <Button
                              size="small"
                              type="primary"
                              data-testid="ai-view-diff"
                              disabled={!props.contentReady || props.busy}
                              onClick={() => openCandidateDiff(message.id, candidateState)}
                            >
                              {stale ? t("actions.viewCurrentChanges", { ns: "common" }) : t("actions.viewChanges", { ns: "common" })}
                            </Button>
                          </div>
                        </div>
                      )}
                    </MessagePrimitive.Root>
                  </MessageProvider>
                );
              })
            )}
            {sending && progressStage !== null && (
              <div className="ai-loading" data-testid="ai-loading" role="status" aria-live="polite">
                <Spin size="small" />
                <span data-testid="ai-progress-stage">{t(`assistant.progress.${progressStage}`)}</span>
              </div>
            )}
            {!sending && progressStage === "succeeded" && (
              <div className="ai-progress-done" data-testid="ai-progress-done" role="status">
                {t("assistant.progress.succeeded")}
              </div>
            )}
          </ThreadPrimitive.Viewport>

          <ComposerPrimitive.Root className="ai-composer">
            {panelError !== null && (
              <p className="ai-panel-error" role="alert" data-testid="ai-panel-error">
                {panelError}
              </p>
            )}
            {/* M5.7 Wave B3: the composer attachment surface (dropzone, file
                picker, accessible list, hints and pre-send validation). */}
            <ComposerAttachmentsBridge
              onAttachmentsChange={(attachments) => {
                composerAttachmentsRef.current = attachments;
              }}
              onControlsChange={(controls) => {
                composerControlsRef.current = controls;
              }}
            />
            <ComposerAttachmentArea
              disabled={composerDisabled}
              sending={sending}
              limits={attachmentLimits}
              supportedContentTypes={supportedContentTypes}
              error={attachmentError}
              onErrorChange={(message) => setAttachmentError(message)}
              onSend={(text, attachments) => void sendMessage(text, attachments)}
            />
          </ComposerPrimitive.Root>
        </ThreadPrimitive.Root>
      </AssistantRuntimeProvider>
    </aside>
  );

  const candidateDiffState = candidateDiff;
  const diffApplyAction = (() => {
    if (candidateDiffState === null) {
      return null;
    }
    // The open Candidate is looked up live from the conversation so the
    // applied/stale state stays correct after Apply inside the modal.
    const candidateState =
      messages.find((message) => message.id === candidateDiffState.messageId)?.candidate ??
      null;
    if (candidateState === null) {
      return null;
    }
    const stale =
      !candidateState.applied &&
      !snapshotsEqual(props.workingCopy, candidateState.baseSnapshot);
    const applyBlockedReason = candidateState.applied
      ? t("assistant.diff.applyBlockedApplied")
      : props.adapter?.archived_at
        ? t("assistant.diff.applyBlockedArchived")
        : !props.contentReady
          ? t("assistant.diff.applyBlockedNotReady")
          : props.busy
            ? t("assistant.diff.applyBlockedBusy")
            : props.adapter?.runtime_locked === true
              ? t("assistant.diff.applyBlockedLocked")
              : null;
    return {
      label: stale ? t("assistant.diff.applyStale") : t("actions.apply", { ns: "common" }),
      reason: applyBlockedReason,
      applied: candidateState.applied,
      stale,
      onApply: () => applyCandidate(candidateDiffState.messageId, candidateState.value),
    };
  })();

  return (
    <>
      <VersionDiffModal
        open={candidateDiff !== null}
        title={t("assistant.diff.title")}
        originalTitle={t("assistant.diff.original")}
        modifiedTitle={t("assistant.diff.modified")}
        panes={(candidateDiff?.panes ?? []).map((pane) => ({
          ...pane,
          label:
            pane.key === "code"
              ? t("assistant.diff.code")
              : pane.key === "requirements"
                ? dependencyUiFor(props.adapter?.language ?? "python").label
                : t("assistant.diff.runtimeConfig"),
        }))}
        theme={props.theme}
        onClose={() => setCandidateDiff(null)}
        applyAction={diffApplyAction}
      />
      {props.open ? expandedPanel : collapsedEntry}
    </>
  );
}
