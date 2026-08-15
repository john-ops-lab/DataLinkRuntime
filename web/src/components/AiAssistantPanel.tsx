/** Browser-only AI conversation and Candidate review surface (M4). */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Spin } from "antd";

import { ApiError, api } from "../api";
import { LANGUAGE_LABELS } from "../languages";
import type {
  Adapter,
  AiCandidate,
  AiConversationMessage,
} from "../types";
import ActionWithReason from "./ActionWithReason";

export interface AiWorkingCopy {
  code: string;
  requirements: string;
  runtimeConfigText: string;
}

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

interface AiAssistantPanelProps {
  open: boolean;
  adapter: Adapter | null;
  selectedVersionId: number | null;
  selectedVersionSeq: number | null;
  workingCopy: AiWorkingCopy;
  contentReady: boolean;
  busy: boolean;
  onOpen: () => void;
  onClose: () => void;
  onApply: (candidate: AiCandidate) => void;
  onOpenDiff: (candidate: AiCandidate) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "AI 请求失败";
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

export default function AiAssistantPanel(props: AiAssistantPanelProps) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<VisibleMessage[]>([]);
  const [sending, setSending] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [boundSecretKeys, setBoundSecretKeys] = useState<Set<string>>(new Set());
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingsVerified, setBindingsVerified] = useState(false);
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
    try {
      const response = await api.assistAdapter(requestAdapterId, {
        message,
        working_copy: {
          code: baseSnapshot.code,
          requirements: baseSnapshot.requirements,
          runtime_config: runtimeConfig,
        },
        recent_messages: recentMessages,
        base_version_id: props.selectedVersionId,
      });
      // The component is keyed by Adapter in App, and this explicit guard also
      // prevents a late response from committing across an Adapter switch.
      if (generation !== requestGeneration.current) {
        return;
      }
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
          response.candidate === null
            ? null
            : { value: response.candidate, baseSnapshot, applied: false },
      };
      setMessages((current) => [...current, assistantMessage]);
    } catch (error) {
      if (generation === requestGeneration.current) {
        setPanelError(errorMessage(error));
      }
    } finally {
      if (generation === requestGeneration.current) {
        setSending(false);
      }
    }
  }

  function discardCandidate(messageId: number) {
    setMessages((current) =>
      current.map((message) =>
        message.id === messageId ? { ...message, candidate: null } : message,
      ),
    );
  }

  function applyCandidate(messageId: number, candidate: AiCandidate) {
    if (!props.contentReady || props.busy || props.adapter?.archived_at) {
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
  }

  if (!props.open) {
    return (
      <aside className="ai-assistant ai-assistant-collapsed">
        <Button
          type="text"
          className="ai-assistant-open"
          data-testid="open-ai-assistant"
          aria-label="展开 AI Assistant"
          aria-expanded={false}
          onClick={props.onOpen}
        >
          AI
        </Button>
      </aside>
    );
  }

  const contextVersion =
    props.selectedVersionSeq === null ? "未保存 Working Copy" : `Working Copy v${props.selectedVersionSeq}`;

  return (
    <aside className="ai-assistant ai-assistant-expanded" data-testid="ai-assistant-panel">
      <div className="ai-assistant-header">
        <div>
          <strong>AI Assistant</strong>
          <p>Candidate 仅写入浏览器，不会自动保存、测试或发布。</p>
        </div>
        <Button
          type="text"
          data-testid="close-ai-assistant"
          aria-label="收起 AI Assistant"
          aria-expanded={true}
          onClick={props.onClose}
        >
          ×
        </Button>
      </div>

      <div className="ai-assistant-context" data-testid="ai-current-context">
        {props.adapter === null ? (
          <span>请先选择一个 Adapter。</span>
        ) : (
          <>
            <strong>{props.adapter.name}</strong>
            <span>{LANGUAGE_LABELS[props.adapter.language]} · {contextVersion}</span>
          </>
        )}
      </div>

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
          <p className="ai-conversation-empty">
            描述你希望解释或修改的内容。每次请求都以当前 Working Copy 为唯一代码快照。
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
            const applyBlockedReason = props.adapter?.archived_at
              ? "Adapter 已删除，Candidate 只能查看，不能应用"
              : !props.contentReady
                ? "Working Copy 尚未加载完成，请稍后重试"
                : props.busy
                  ? "其他操作正在进行，请等待完成"
                  : candidateState?.applied
                    ? "该 Candidate 已应用到当前 Working Copy"
                    : null;
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
                        <strong>⚠ AI 生成期间 Working Copy 已发生修改。</strong>
                        <span>该 Candidate 基于较早的编辑内容生成。</span>
                      </div>
                    )}
                    {props.adapter?.archived_at && (
                      <p className="ai-secret-warning" role="alert" data-testid="ai-archived-apply-blocked">
                        已删除 Adapter 为只读，不能应用 Candidate。
                      </p>
                    )}
                    {candidateState.applied && (
                      <p className="ai-candidate-applied" role="status" data-testid="ai-candidate-applied">
                        已应用到浏览器 Working Copy；请继续人工保存、测试与发布。
                      </p>
                    )}
                    <div className="ai-candidate-actions">
                      <Button
                        size="small"
                        data-testid="ai-view-diff"
                        disabled={!props.contentReady || props.busy}
                        onClick={() => props.onOpenDiff(candidateState.value)}
                      >
                        {stale ? "查看与当前 Working Copy 的修改" : "查看修改"}
                      </Button>
                      <ActionWithReason label="应用 AI Candidate" reason={applyBlockedReason}>
                        <Button
                          size="small"
                          type="primary"
                          data-testid="ai-apply-candidate"
                          disabled={applyBlockedReason !== null}
                          onClick={() => applyCandidate(message.id, candidateState.value)}
                        >
                          {candidateState.applied ? "已应用" : stale ? "仍然应用" : "应用"}
                        </Button>
                      </ActionWithReason>
                      <Button
                        size="small"
                        type="text"
                        data-testid="ai-discard-candidate"
                        onClick={() => discardCandidate(message.id)}
                      >
                        放弃
                      </Button>
                    </div>
                  </div>
                )}
              </article>
            );
          })
        )}
        {sending && (
          <div className="ai-loading" data-testid="ai-loading">
            <Spin size="small" /> 正在生成 Candidate…
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
}
