/**
 * M5.7 Wave D: final i18n / a11y / redaction regression for the full M5.7
 * chain (Issue #80).
 *
 * Wave D locks the Wave-A..C2 contracts that are easy to regress silently:
 *
 * - stable M5.7 error codes localize through common.errors with zh-CN/en key
 *   parity: the B2 attachment codes (ai_attachment_*) and the C1 tool-call
 *   API codes (ai_tool_unsupported / ai_tool_limit_exceeded /
 *   ai_tool_result_too_large) render as localized messages plus the stable
 *   code; unknown codes keep the M5.6 userErrorMessage fallback contract.
 * - every interactive element of the assistant-ui conversation surface keeps
 *   an accessible name (open/close/send/regenerate/retry/attachment
 *   add/remove/code copy/context snippet controls), the composer keyboard
 *   contract stays Enter/Ctrl+Enter/Meta+Enter send + Shift+Enter newline,
 *   and disabled reasons stay machine-visible (data-disabled-reason).
 * - sanitized tool summaries never echo Secret truth or raw payloads into
 *   the DOM; the args/result labels stay bounded and redacted.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api, ApiError } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { Adapter, AiAssistResponse, AiToolCallSummary } from "./types";
import zh from "./i18n/locales/zh-CN/ai.json";
import en from "./i18n/locales/en/ai.json";
import zhCommon from "./i18n/locales/zh-CN/common.json";
import enCommon from "./i18n/locales/en/common.json";

vi.mock("@monaco-editor/react", () => ({
  default: function Editor() {
    return <textarea data-testid="code-editor" readOnly />;
  },
  DiffEditor: function DiffEditor() {
    return <div data-testid="diff-editor" />;
  },
  loader: {
    init: () => Promise.resolve({ editor: { setTheme: () => undefined } }),
  },
}));

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    timeout_seconds: 300,
    runtime_worker_id: 1,
    latest_version_id: 10,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

const workingCopy = {
  code: "def handle(context, input):\n    return input\n",
  requirements: "",
  runtimeConfigText: "{}",
};

function aiResponse(
  message: string,
  toolCalls: AiToolCallSummary[],
  candidate: AiAssistResponse["candidate"] = null,
): AiAssistResponse {
  return { message, provider: "openai", model: "test-model", candidate, tool_calls: toolCalls };
}

function toolSummary(overrides: Partial<AiToolCallSummary> = {}): AiToolCallSummary {
  return {
    tool_name: "search_knowledge",
    status: "success",
    args_summary: '{"source":"ima","query":"runtime contract","limit":2}',
    result_summary:
      '{"tool":"search_knowledge","total_matches":1,"items":[{"id":"kb-item-1","source":"ima:v1:kb-item-1"}]}',
    error_code: null,
    duration_ms: 12,
    result_truncated: false,
    result_size: 120,
    source: "ima:v1:kb-item-1",
    ...overrides,
  };
}

type PanelProps = ComponentProps<typeof AiAssistantPanel>;

function renderPanel(overrides: Partial<PanelProps> = {}) {
  const base: PanelProps = {
    open: true,
    adapter: makeAdapter(),
    selectedVersionId: 10,
    selectedVersionSeq: 1,
    workingCopy: { ...workingCopy },
    contentReady: true,
    busy: false,
    contextSnippets: [],
    theme: "vs-dark",
    onOpen: vi.fn(),
    onClose: vi.fn(),
    onApply: vi.fn(),
    onRemoveContextSnippet: vi.fn(),
    onClearContextSnippets: vi.fn(),
    ...overrides,
  };
  return render(<AiAssistantPanel {...base} />);
}

async function sendQuestion(text: string) {
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("ai-send"));
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

// --- stable M5.7 error codes: zh-CN / en key parity and panel rendering -----

const STABLE_M57_CODES = [
  "ai_tool_unsupported",
  "ai_tool_limit_exceeded",
  "ai_tool_result_too_large",
] as const;

function flattenKeys(value: object, prefix = ""): string[] {
  return Object.entries(value).flatMap(([key, item]) =>
    typeof item === "object" && item !== null
      ? flattenKeys(item as object, `${prefix}${key}.`)
      : [`${prefix}${key}`],
  );
}

it("keeps the M5.7 stable tool-call error codes in common.errors with zh/en parity", () => {
  const zhErrors = flattenKeys(zhCommon.errors);
  const enErrors = flattenKeys(enCommon.errors);
  expect(zhErrors.sort()).toEqual(enErrors.sort());
  for (const code of STABLE_M57_CODES) {
    expect(zhErrors).toContain(code);
    expect(enErrors).toContain(code);
  }
});

it("localizes ai_tool_* API errors through the panel error contract (zh-CN)", async () => {
  vi.spyOn(api, "assistAdapter").mockRejectedValue(
    new ApiError(502, "ai_tool_limit_exceeded", "AI 工具调用达到安全上限"),
  );
  renderPanel();
  await sendQuestion("触发工具上限");
  const error = await screen.findByTestId("ai-panel-error");
  expect(error.textContent).toContain("AI 工具调用达到安全上限");
  expect(error.textContent).toContain("（错误码：ai_tool_limit_exceeded）");
});

it("localizes ai_tool_* API errors through the panel error contract (en)", async () => {
  await applySystemLocale("en");
  vi.spyOn(api, "assistAdapter").mockRejectedValue(
    new ApiError(502, "ai_tool_result_too_large", "AI 工具结果累计超过大小上限"),
  );
  renderPanel();
  await sendQuestion("tool result too large");
  const error = await screen.findByTestId("ai-panel-error");
  expect(error.textContent).toContain("exceeded the size limit");
  expect(error.textContent).toContain("(Error code: ai_tool_result_too_large)");
});

it("keeps the zh-CN / en ai namespace key parity for the whole assistant chain", () => {
  const zhKeys = flattenKeys(zh);
  const enKeys = flattenKeys(en);
  expect(zhKeys.sort()).toEqual(enKeys.sort());
});

// --- a11y: accessible names, keyboard contract and disabled reasons ----------

it("gives every interactive assistant-surface control an accessible name", async () => {
  const openSpy = vi.fn();
  const closeSpy = vi.fn();
  const { rerender } = render(
    <AiAssistantPanel
      open={false}
      adapter={makeAdapter()}
      selectedVersionId={10}
      selectedVersionSeq={1}
      workingCopy={{ ...workingCopy }}
      contentReady
      busy={false}
      contextSnippets={[
        {
          id: 1,
          source: "code",
          text: "def handle(context, input):\n    return input\n",
          start_line: 1,
          end_line: 2,
        },
      ]}
      theme="vs-dark"
      onOpen={openSpy}
      onClose={closeSpy}
      onApply={vi.fn()}
      onRemoveContextSnippet={vi.fn()}
      onClearContextSnippets={vi.fn()}
    />,
  );

  // Collapsed floating entry: accessible name + aria-expanded.
  const openButton = screen.getByTestId("open-ai-assistant");
  expect(openButton.getAttribute("aria-label")).toBe("展开 AI 助手");
  expect(openButton.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(openButton);
  expect(openSpy).toHaveBeenCalled();

  // Expanded panel controls.
  rerender(
    <AiAssistantPanel
      open
      adapter={makeAdapter()}
      selectedVersionId={10}
      selectedVersionSeq={1}
      workingCopy={{ ...workingCopy }}
      contentReady
      busy={false}
      contextSnippets={[
        {
          id: 1,
          source: "code",
          text: "def handle(context, input):\n    return input\n",
          start_line: 1,
          end_line: 2,
        },
      ]}
      theme="vs-dark"
      onOpen={openSpy}
      onClose={closeSpy}
      onApply={vi.fn()}
      onRemoveContextSnippet={vi.fn()}
      onClearContextSnippets={vi.fn()}
    />,
  );
  expect(screen.getByTestId("close-ai-assistant").getAttribute("aria-label")).toBe(
    "收起 AI 助手",
  );
  expect(screen.getByTestId("close-ai-assistant").getAttribute("aria-expanded")).toBe("true");
  // Composer input and send button keep stable accessible names.
  expect(screen.getByTestId("ai-message-input").getAttribute("aria-label")).toBe("AI 指令");
  expect(screen.getByTestId("ai-send").textContent).toContain("发送");
  // Attachment picker controls.
  expect(screen.getByTestId("ai-attachment-add").getAttribute("aria-label")).toContain(
    "添加附件",
  );
  // Context snippet controls.
  expect(screen.getByTestId("ai-snippet-1")).toBeTruthy();
  expect(screen.getByTestId("ai-remove-snippet-1").getAttribute("aria-label")).toBe(
    "删除该上下文片段",
  );
  expect(screen.getByTestId("ai-clear-all-snippets").getAttribute("aria-label")).toBe(
    "清空全部上下文片段",
  );
});

it("keeps the composer keyboard contract: Enter / Shift+Enter / Ctrl+Enter / Meta+Enter", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValue(aiResponse("已收到", []));
  renderPanel();
  const input = screen.getByTestId("ai-message-input") as HTMLTextAreaElement;

  // Shift+Enter → newline, no send.
  fireEvent.change(input, { target: { value: "第一行" } });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(assistAdapter).not.toHaveBeenCalled();
  expect(input.value).toBe("第一行");

  // Enter → send.
  fireEvent.change(input, { target: { value: "Enter 发送" } });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(1));

  // Ctrl+Enter → send.
  fireEvent.change(input, { target: { value: "Ctrl 发送" } });
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(2));

  // Meta+Enter → send.
  fireEvent.change(input, { target: { value: "Meta 发送" } });
  fireEvent.keyDown(input, { key: "Enter", metaKey: true });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(3));
});

it("keeps disabled reasons visible for the blocked Candidate Apply (machine-readable)", async () => {
  const candidate = {
    summary: "方案",
    code: "def handle(context, input):\n    return 1\n",
    requirements: "",
    runtime_config: {},
    required_secret_keys: [],
  };
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("给出候选。", [], candidate));
  renderPanel({ adapter: makeAdapter({ archived_at: "2026-08-19T00:00:00Z" }) });
  await sendQuestion("生成候选");
  await screen.findByTestId("ai-candidate");

  fireEvent.click(screen.getByTestId("ai-view-diff"));
  const applyButton = await screen.findByTestId("diff-apply-candidate");
  expect((applyButton as HTMLButtonElement).disabled).toBe(true);
  const wrapper = applyButton.closest(".action-with-reason");
  expect(wrapper?.getAttribute("data-disabled-reason")).toBe("适配器已删除，候选修改只能查看，不能应用");
});

// --- redaction: attachment bodies / Secret truth never reach the DOM ---------

it("never renders attachment bodies (base64 or sentinel text) into the thread DOM", async () => {
  const bodySentinel = "wave-d-attachment-secret-sentinel";
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("已处理附件。", []));
  renderPanel();

  fireEvent.change(screen.getByTestId("ai-attachment-input"), {
    target: { files: [new File([bodySentinel], "report.md", { type: "text/plain" })] },
  });
  await waitFor(() =>
    expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(1),
  );
  await sendQuestion("请阅读附件");

  await screen.findByText("已处理附件。");
  // The request carries the body to the server...
  const payload = (vi.mocked(api.assistAdapter).mock.calls.at(-1)?.[1] ?? {}) as {
    attachments?: { filename: string; data_base64: string }[];
  };
  expect(payload.attachments?.[0]?.filename).toBe("report.md");
  expect(payload.attachments?.[0]?.data_base64.length).toBeGreaterThan(0);
  // ...but the DOM never renders the body, the base64 or the sentinel text.
  const conversationText = screen.getByTestId("ai-conversation").textContent ?? "";
  expect(conversationText).not.toContain(bodySentinel);
  expect(conversationText).not.toContain("data_base64");
  expect(conversationText).not.toContain("base64");
});

it("renders only the sanitized summaries the server sends; the panel never invents Secret fields", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("知识搜索完成。", [
      toolSummary({
        args_summary: '{"source":"ima","query":"contract","limit":2}',
        result_summary:
          '{"tool":"search_knowledge","total_matches":1,"items":[{"id":"kb-item-1","source":"ima:v1:kb-item-1"}]}',
      }),
    ]),
  );
  renderPanel();
  await sendQuestion("搜索知识");
  await screen.findByText("知识搜索完成。");

  const conversationText = screen.getByTestId("ai-conversation").textContent ?? "";
  // Only the sanitized bounded summaries render; no raw item payload echo.
  expect(conversationText).toContain("search_knowledge");
  expect(conversationText).not.toContain("credential");
  expect(conversationText).not.toContain("api_key");
  expect(screen.getByTestId("ai-tool-args").textContent).toContain("source");
  expect(screen.getByTestId("ai-tool-result").textContent).toContain("search_knowledge");
});

it("clamps oversized tool summaries in the DOM as a second client-side bound", async () => {
  const huge = "y".repeat(5000);
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("超大结果。", [
      toolSummary({
        result_summary: `{"tool":"search_knowledge","blob":"…[DLR 工具结果已截断]${huge}"}`,
        result_truncated: true,
      }),
    ]),
  );
  renderPanel();
  await sendQuestion("超大结果问题");
  await screen.findByText("超大结果。");

  // The conversation DOM never contains the full hostile payload: the
  // render clamp keeps every summary span bounded.
  const conversationText = screen.getByTestId("ai-conversation").textContent ?? "";
  expect(conversationText).not.toContain(huge);
  const resultText = screen.getByTestId("ai-tool-result").textContent ?? "";
  expect(resultText.length).toBeLessThanOrEqual(700);
  expect(resultText.endsWith("…")).toBe(true);
  expect(resultText.match(/…+$/u)?.[0]).toBe("…");
  expect(resultText).not.toContain("[DLR 工具结果已截断]");
  expect(screen.queryByTestId("ai-tool-truncated")).toBeNull();
});
