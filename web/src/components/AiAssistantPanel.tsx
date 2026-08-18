/** Browser-only AI conversation and Candidate review surface (M4).
 *
 * M5.7 Wave A: the conversation/composer layer is rebuilt on the official
 * assistant-ui headless primitives via the External Store Runtime. DLR keeps
 * the authoritative message/business state (VisibleMessage, Candidate,
 * Working Copy snapshot, Secret binding knowledge); the runtime only mirrors
 * the visible user/assistant text for Thread/Message/Composer/Markdown
 * primitives. No Regenerate / Attachments / Tool Call / Streaming in Wave A.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  fromThreadMessageLike,
  MessagePrimitive,
  MessageProvider,
  ThreadPrimitive,
  useExternalStoreRuntime,
  useAui,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";

import { api } from "../api";
import { dependencyUiFor, LANGUAGE_LABELS } from "../languages";
import type {
  Adapter,
  AiCandidate,
  AiConversationMessage,
  AiContextSnippet,
} from "../types";
import { logSnippetTimeLabel } from "../unified-log";
import { userErrorMessage } from "../user-message";
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

interface VisibleMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  candidate: CandidateState | null;
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

/** M5.7 Wave A: 发送按钮。点击路径由 DLR 同步启动 assist 流程（保持既有
 * “准备态先于请求可见”的生命周期回归语义），并阻止原语自带的异步 send()
 * 双路径；Enter / Ctrl/Cmd+Enter 键盘路径仍走表单提交 → External Store
 * Runtime onNew。两条路径汇聚到同一个 DLR runAssist。 */
function ComposerSubmitButton(props: {
  disabled: boolean;
  sending: boolean;
  onSend: (text: string) => void;
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
        const text = composer.getState().text;
        if (text.trim() === "") {
          return;
        }
        composer.setText("");
        props.onSend(text);
      }}
    >
      {props.sending && <Spin size="small" />}
      {t("assistant.send")}
    </ComposerPrimitive.Send>
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
  const { t } = useTranslation(["ai", "common"]);
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

  /** M5.7 Wave A: DLR-owned single-shot assist flow. Called by the External
   * Store runtime's onNew with the composed text; never throws (all failures
   * converge to the panel error contract). */
  async function runAssist(message: string) {
    const adapter = props.adapter;
    if (adapter === null || !props.contentReady || props.busy || sending || message.trim() === "") {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(props.workingCopy.runtimeConfigText);
    if (runtimeConfig === null) {
      setPanelError(t("assistant.invalidRuntimeConfig"));
      return;
    }

    const generation = ++requestGeneration.current;
    const requestAdapterId = adapter.id;
    const baseSnapshot = { ...props.workingCopy };
    const recentMessages = recentVisibleMessages(messages);
    const userMessage: VisibleMessage = {
      id: nextMessageId.current++,
      role: "user",
      content: message.trim(),
      candidate: null,
    };
    // Sending explicitly returns to the current exchange. A later manual
    // upward scroll can still pause following before the response arrives.
    followLatestRef.current = true;
    setMessages((current) => [...current, userMessage]);
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
        message: message.trim(),
        working_copy: {
          code: baseSnapshot.code,
          requirements: baseSnapshot.requirements,
          runtime_config: runtimeConfig,
        },
        recent_messages: recentMessages,
        base_version_id: props.selectedVersionId,
        // M5.5.13: all confirmed snippets in the order they were added; the
        // snapshots are frozen at click time, later cursor movement never
        // changes them. Omitted entirely when none were added.
        ...(props.contextSnippets.length === 0
          ? {}
          : {
              context_snippets: props.contextSnippets.map(
                ({ source, text, start_line, end_line }) => ({
                  source,
                  text,
                  start_line,
                  end_line,
                }),
              ),
            }),
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
        candidate:
          response.candidate === null || !isValidAiCandidate(response.candidate)
            ? null
            : { value: response.candidate, baseSnapshot, applied: false },
      };
      setMessages((current) => [...current, assistantMessage]);
      // M5.5.5: the success stage claims "waiting to view the Diff" only when
      // a Candidate is actually rendered; a plain-text reply converges
      // silently to the assistant message itself.
      setProgressStage(assistantMessage.candidate === null ? null : "succeeded");
    } catch (error) {
      if (generation === requestGeneration.current) {
        setPanelError(userErrorMessage(error, t("assistant.errors.requestFailed")));
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

  /** M5.7 Wave A: External Store Runtime — DLR 继续持有消息状态，assistant-ui
   * 只读取镜像消息并驱动 Composer 提交。 */
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: sending,
    isDisabled:
      props.adapter === null || !props.contentReady || props.busy || sending,
    convertMessage: toThreadMessageLike,
    onNew: async (message: AppendMessage) => {
      await runAssist(appendMessageText(message));
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
            <ComposerPrimitive.Input
              data-testid="ai-message-input"
              autoFocus
              aria-label={t("assistant.commandLabel")}
              placeholder={t("assistant.commandPlaceholder")}
              minRows={3}
              maxRows={10}
              disabled={composerDisabled}
            />
            <ComposerSubmitButton
              disabled={composerDisabled}
              sending={sending}
              onSend={(text) => void runAssist(text)}
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
