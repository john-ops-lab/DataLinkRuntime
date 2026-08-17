/** Browser-only AI conversation and Candidate review surface (M4). */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Spin } from "antd";

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

interface CandidateDiffState {
  messageId: number;
  panes: DiffPane[];
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

const PROGRESS_TEXT: Record<ProgressStage, string> = {
  preparing: "正在准备当前代码上下文…",
  requesting: "正在请求 AI 模型…",
  validating: "正在校验返回结果…",
  succeeded: "已生成修改，等待查看 Diff",
};

function errorMessage(error: unknown): string {
  return userErrorMessage(error, "AI 请求失败");
}

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

function recentVisibleMessages(messages: VisibleMessage[]): AiConversationMessage[] {
  return messages.slice(-8).map(({ role, content }) => ({ role, content }));
}

/** M5.5.13: user-facing label of one context snippet ("代码 第 12–20 行",
 * "实时日志 10:21:03–10:21:08"). Log ranges are derived from the capture-time
 * prefixes of the browser-visible masked text only. */
function contextSnippetLabel(
  snippet: AiContextSnippetEntry,
  language: Adapter["language"],
): string {
  if (snippet.source === "code") {
    const range =
      snippet.end_line > snippet.start_line
        ? `第 ${snippet.start_line}–${snippet.end_line} 行`
        : `第 ${snippet.start_line} 行`;
    return `代码 ${range}（${LANGUAGE_LABELS[language] ?? language}）`;
  }
  const timeLabel = logSnippetTimeLabel(snippet.text);
  if (timeLabel !== null) {
    return `实时日志 ${timeLabel}`;
  }
  const range =
    snippet.end_line > snippet.start_line
      ? `第 ${snippet.start_line}–${snippet.end_line} 行`
      : `第 ${snippet.start_line} 行`;
  return `实时日志 ${range}`;
}

export default function AiAssistantPanel(props: AiAssistantPanelProps) {
  const [draft, setDraft] = useState("");
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
  const messageInputRef = useRef<HTMLTextAreaElement>(null);
  const followLatestRef = useRef(true);
  const previousOpenRef = useRef(props.open);

  // 展开时把焦点移入面板（键盘可达、焦点可见）；收起后由浏览器自然回到页面。
  useEffect(() => {
    if (props.open) {
      messageInputRef.current?.focus();
    }
  }, [props.open]);

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

  async function handleSend() {
    const adapter = props.adapter;
    const message = draft.trim();
    if (adapter === null || !props.contentReady || props.busy || sending || message === "") {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(props.workingCopy.runtimeConfigText);
    if (runtimeConfig === null) {
      setPanelError("发送前请先修正运行参数：必须是合法的 JSON 对象。");
      return;
    }

    const generation = ++requestGeneration.current;
    const requestAdapterId = adapter.id;
    const baseSnapshot = { ...props.workingCopy };
    const recentMessages = recentVisibleMessages(messages);
    const userMessage: VisibleMessage = {
      id: nextMessageId.current++,
      role: "user",
      content: message,
      candidate: null,
    };
    // Sending explicitly returns to the current exchange. A later manual
    // upward scroll can still pause following before the response arrives.
    followLatestRef.current = true;
    setMessages((current) => [...current, userMessage]);
    setDraft("");
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
        message,
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
        setPanelError(errorMessage(error));
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
          label: "代码",
          language: adapter.language,
          original: props.workingCopy.code,
          modified: candidate.code,
        },
        {
          key: "requirements",
          label: dependencyUiFor(adapter.language).label,
          language: "plaintext",
          original: props.workingCopy.requirements,
          modified: candidate.requirements,
        },
        {
          key: "runtime-config",
          label: "运行参数",
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
        aria-label="展开 AI 助手"
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
    props.selectedVersionSeq === null ? "未保存版本" : `版本 v${props.selectedVersionSeq}`;

  const expandedPanel = (
    <aside className="ai-assistant ai-assistant-expanded" data-testid="ai-assistant-panel">
      <div className="ai-assistant-header">
        <div>
          <strong>AI 助手</strong>
          <p>候选修改仅写入浏览器，不会自动保存、测试或运行。</p>
        </div>
        <Button
          type="text"
          data-testid="close-ai-assistant"
          aria-label="收起 AI 助手"
          aria-expanded={true}
          onClick={props.onClose}
        >
          ×
        </Button>
      </div>

      <div className="ai-assistant-context" data-testid="ai-current-context">
        {props.adapter === null ? (
          <span>请先选择一个适配器。</span>
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
            <span>已加入的上下文片段</span>
            <Button
              size="small"
              type="text"
              data-testid="ai-clear-all-snippets"
              aria-label="清空全部上下文片段"
              onClick={props.onClearContextSnippets}
            >
              清空全部
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
                    {contextSnippetLabel(snippet, adapterLanguage)}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    data-testid={`ai-remove-snippet-${snippet.id}`}
                    aria-label="删除该上下文片段"
                    onClick={() => props.onRemoveContextSnippet(snippet.id)}
                  >
                    删除
                  </Button>
                </div>
              ));
            })()}
        </div>
      )}

      <div
        ref={conversationRef}
        className="ai-conversation"
        data-testid="ai-conversation"
        onScroll={(event) => {
          const target = event.currentTarget;
          followLatestRef.current =
            target.scrollHeight - target.clientHeight - target.scrollTop <= 32;
        }}
      >
        {messages.length === 0 ? (
          <p className="ai-conversation-empty" data-testid="ai-conversation-empty">
            描述你的需求，可引用原代码片段。建议不要在代码中直接写入密码、Token、密钥等凭据，
            请使用「凭据绑定」功能，避免敏感凭据随代码发送给 AI。
          </p>
        ) : (
          messages.map((message) => {
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
              <article
                key={message.id}
                className={`ai-message ai-message-${message.role}`}
                data-testid={`ai-message-${message.role}`}
              >
                <span className="ai-message-role">{message.role === "user" ? "你" : "AI"}</span>
                <p>{message.content}</p>
                {candidateState !== null && (
                  <div className="ai-candidate" data-testid="ai-candidate">
                    <p className="ai-candidate-ready" data-testid="ai-candidate-ready">
                      代码已生成
                    </p>
                    <strong data-testid="ai-candidate-summary">
                      {candidateState.value.summary}
                    </strong>
                    {candidateState.value.required_secret_keys.length > 0 && (
                      <p className="ai-secret-suggestion" data-testid="ai-required-secret-keys">
                        AI 建议需要：{candidateState.value.required_secret_keys.join(", ")}
                      </p>
                    )}
                    {!bindingsLoading && bindingsVerified && missingKeys.length > 0 && (
                      <p className="ai-secret-warning" role="alert" data-testid="ai-missing-secret-keys">
                        ⚠ 缺少凭据绑定：{missingKeys.join(", ")}
                      </p>
                    )}
                    {!bindingsLoading && !bindingsVerified && (
                      <p className="ai-secret-check-unavailable" role="alert">
                        暂时无法核对当前凭据绑定。
                      </p>
                    )}
                    {stale && (
                      <div className="ai-stale-warning" role="alert" data-testid="ai-candidate-stale">
                        <strong>⚠ AI 生成期间当前代码已发生修改。</strong>
                        <span>该候选修改基于较早的编辑内容生成。</span>
                      </div>
                    )}
                    {props.adapter?.archived_at && (
                      <p className="ai-secret-warning" role="alert" data-testid="ai-archived-apply-blocked">
                        已删除适配器为只读，不能应用候选修改。
                      </p>
                    )}
                    {candidateState.applied && (
                      <p className="ai-candidate-applied" role="status" data-testid="ai-candidate-applied">
                        已应用到浏览器中的当前代码；请继续人工保存、测试与运行。
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
                        {stale ? "查看与当前代码的修改" : "查看修改"}
                      </Button>
                    </div>
                  </div>
                )}
              </article>
            );
          })
        )}
        {sending && progressStage !== null && (
          <div className="ai-loading" data-testid="ai-loading" role="status" aria-live="polite">
            <Spin size="small" />
            <span data-testid="ai-progress-stage">{PROGRESS_TEXT[progressStage]}</span>
          </div>
        )}
        {!sending && progressStage === "succeeded" && (
          <div className="ai-progress-done" data-testid="ai-progress-done" role="status">
            {PROGRESS_TEXT.succeeded}
          </div>
        )}
      </div>

      <div className="ai-composer">
        {panelError !== null && (
          <p className="ai-panel-error" role="alert" data-testid="ai-panel-error">
            {panelError}
          </p>
        )}
        <textarea
          ref={messageInputRef}
          rows={4}
          data-testid="ai-message-input"
          aria-label="AI 指令"
          placeholder="输入问题或修改要求…"
          value={draft}
          disabled={props.adapter === null || !props.contentReady || props.busy || sending}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void handleSend();
            }
          }}
        />
        <Button
          type="primary"
          data-testid="ai-send"
          loading={sending}
          disabled={
            props.adapter === null ||
            !props.contentReady ||
            props.busy ||
            sending ||
            draft.trim() === ""
          }
          onClick={() => void handleSend()}
        >
          发送
        </Button>
      </div>
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
      ? "该候选修改已应用到当前代码"
      : props.adapter?.archived_at
        ? "适配器已删除，候选修改只能查看，不能应用"
        : !props.contentReady
          ? "当前代码尚未加载完成，请稍后重试"
          : props.busy
            ? "其他操作正在进行，请等待完成"
            : props.adapter?.runtime_locked === true
              ? "适配器正在运行，不能应用候选修改"
              : null;
    return {
      label: stale ? "仍然应用" : "应用修改",
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
        title="AI 候选修改：与当前编辑内容对比"
        originalTitle="当前编辑内容"
        modifiedTitle="AI 候选修改"
        panes={candidateDiff?.panes ?? []}
        theme={props.theme}
        onClose={() => setCandidateDiff(null)}
        applyAction={diffApplyAction}
      />
      {props.open ? expandedPanel : collapsedEntry}
    </>
  );
}
