/**
 * M5.7 Wave C2: KnowledgeSource Tool UI regressions (Issue #80).
 *
 * The C2 knowledge tools (list_knowledge_bases / search_knowledge /
 * read_knowledge) flow through the exact same sanitized AiToolCallSummary ->
 * assistant-ui tool-call part contract as the C1 docs tools. Covered here:
 *
 * - localized display names for the knowledge tools (zh-CN / en, instant
 *   switching) with the raw registered name as fallback for other tools
 * - accessibility: the tool card stays one role="status" region whose
 *   aria-label carries the localized name and status
 * - knowledge tool rounds keep the Wave A/B contracts: candidate=null,
 *   attachments, Regenerate with frozen snapshot, plain-text recent_messages
 * - sanitized summaries only: no raw item payload ever reaches the DOM
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { DlrToolCallUI } from "./components/ai-tool-call";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import type { Adapter, AiAssistResponse, AiToolCallSummary } from "./types";
import zh from "./i18n/locales/zh-CN/ai.json";
import en from "./i18n/locales/en/ai.json";

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

function knowledgeSummaries(): AiToolCallSummary[] {
  return [
    {
      tool_name: "list_knowledge_bases",
      status: "success",
      args_summary: '{"source":"ima"}',
      result_summary:
        '{"tool":"list_knowledge_bases","total":2,"items":[{"id":"team-knowledge","source":"ima:v1:team-knowledge"}]}',
      error_code: null,
      duration_ms: 30,
      result_truncated: false,
      result_size: 300,
      source: "ima:v1:team-knowledge",
    },
    {
      tool_name: "search_knowledge",
      status: "success",
      args_summary: '{"source":"ima","query":"contract","limit":2}',
      result_summary:
        '{"tool":"search_knowledge","total_matches":1,"items":[{"id":"kb-item-1","source":"ima:v1:kb-item-1"}]}',
      error_code: null,
      duration_ms: 22,
      result_truncated: false,
      result_size: 180,
      source: "ima:v1:kb-item-1",
    },
    {
      tool_name: "read_knowledge",
      status: "success",
      args_summary: '{"source":"ima","item_id":"kb-item-1"}',
      result_summary:
        '{"tool":"read_knowledge","item":{"id":"kb-item-1","source":"ima:v1:kb-item-1"}}',
      error_code: null,
      duration_ms: 18,
      result_truncated: false,
      result_size: 120,
      source: "ima:v1:kb-item-1",
    },
  ];
}

function aiResponse(
  message: string,
  toolCalls: AiToolCallSummary[],
  candidate: AiAssistResponse["candidate"] = null,
): AiAssistResponse {
  return { message, provider: "openai", model: "test-model", candidate, tool_calls: toolCalls };
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

function partProps(
  overrides: Partial<ToolCallMessagePartProps> = {},
): ToolCallMessagePartProps {
  return {
    type: "tool-call",
    toolCallId: "dlr-tool-k-0",
    toolName: "list_knowledge_bases",
    args: {},
    argsText: '{"source":"ima"}',
    result: '{"tool":"list_knowledge_bases","total":2}',
    isError: false,
    status: { type: "complete" },
    ...overrides,
  } as ToolCallMessagePartProps;
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

// --- localized knowledge tool display names -----------------------------------

it("renders knowledge tool calls with the zh-CN display names", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("已检索 ima 知识库。", knowledgeSummaries()),
  );
  renderPanel();
  await sendQuestion("查一下 ima 知识库");
  await screen.findByText("已检索 ima 知识库。");

  const cards = screen.getAllByTestId("ai-tool-call");
  expect(cards).toHaveLength(3);
  expect(screen.getAllByTestId("ai-tool-name").map((node) => node.textContent)).toEqual([
    "列出知识库",
    "搜索知识",
    "读取知识条目",
  ]);
  // a11y: each card is one role="status" region whose aria-label carries the
  // localized display name and the localized status.
  const first = cards[0];
  expect(first.getAttribute("role")).toBe("status");
  expect(first.getAttribute("aria-label")).toContain("列出知识库");
  expect(first.getAttribute("aria-label")).toContain("成功");
});

it("switches knowledge tool display names to en instantly", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("done.", knowledgeSummaries()),
  );
  renderPanel();
  await sendQuestion("ima knowledge");
  await screen.findByText("done.");

  await applySystemLocale("en");
  await waitFor(() =>
    expect(screen.getAllByTestId("ai-tool-status")[0].textContent).toBe("Success"),
  );
  expect(screen.getAllByTestId("ai-tool-name").map((node) => node.textContent)).toEqual([
    "List knowledge bases",
    "Search knowledge",
    "Read knowledge item",
  ]);
});

it("keeps the raw registered name for tools without a display mapping", () => {
  const { rerender } = render(
    <div>
      <DlrToolCallUI
        {...partProps({ toolName: "dlr_docs_list", result: '{"tool":"dlr_docs_list"}' })}
      />
    </div>,
  );
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("dlr_docs_list");
  rerender(
    <div>
      <DlrToolCallUI
        {...partProps({ toolName: "unknown_tool", result: '{"ok":false}' })}
      />
    </div>,
  );
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("unknown_tool");
});

it("keeps zh-CN / en display-name key parity", () => {
  const zhNames = (zh as { assistant: { tools: { names: object } } }).assistant.tools.names;
  const enNames = (en as { assistant: { tools: { names: object } } }).assistant.tools.names;
  expect(Object.keys(zhNames).sort()).toEqual(Object.keys(enNames).sort());
});

it("keeps knowledge search disabled while the feature is in development", async () => {
  const capabilitySpy = vi.spyOn(api, "getAiKnowledgeCapability");
  const assistSpy = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValue(aiResponse("普通回答。", []));
  renderPanel();

  const toggle = screen.getByRole("switch", { name: "知识库检索" });
  expect((toggle as HTMLButtonElement).disabled).toBe(true);
  expect(toggle.getAttribute("aria-checked")).toBe("false");
  expect(capabilitySpy).not.toHaveBeenCalled();

  const control = screen.getByText("知识库检索").closest(".ai-knowledge-search-control");
  expect(control).not.toBeNull();
  fireEvent.mouseOver(control as HTMLElement);
  expect((await screen.findByText("开发中")).textContent).toBe("开发中");

  await sendQuestion("普通问题");
  await screen.findByText("普通回答。");
  const payload = assistSpy.mock.calls[0][1];
  expect(payload.knowledge_search_enabled).toBeUndefined();
});

// --- knowledge round keeps the Wave A/B contracts -----------------------------

it("renders a knowledge round with candidate=null and no raw payload echo", async () => {
  const summaries = knowledgeSummaries();
  // Simulate an upstream that echoes the credential truth into a read result:
  // the backend redacts it, so it must never reach the DOM.
  summaries[2].result_summary =
    '{"tool":"read_knowledge","item":{"id":"kb-item-1","content":"[REDACTED]","source":"ima:v1:kb-item-1"}}';
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("无需修改。", summaries));
  renderPanel();
  await sendQuestion("读一下条目");
  await screen.findByText("无需修改。");

  const resultNodes = screen.getAllByTestId("ai-tool-result");
  const resultText = resultNodes[resultNodes.length - 1].textContent ?? "";
  expect(resultText).toContain("[REDACTED]");
  expect(resultText).not.toContain("ima-secret-token");
  // No raw item payload ever reaches the DOM: only the sanitized summaries.
  expect(screen.queryByText("kb-item-1")).toBeNull();
  // candidate=null stays the Wave A contract: no Candidate card is shown.
  expect(screen.queryByText("代码已生成")).toBeNull();
});

it("regenerate reuses the frozen snapshot and replaces the knowledge round", async () => {
  const apiSpy = vi.spyOn(api, "assistAdapter");
  apiSpy.mockResolvedValueOnce(
    aiResponse("第一次结果。", knowledgeSummaries(), {
      summary: "Candidate A",
      code: "def handle(context, input):\n    return 1\n",
      requirements: "",
      runtime_config: {},
      required_secret_keys: [],
    }),
  );
  apiSpy.mockResolvedValueOnce(
    aiResponse("重新生成结果。", knowledgeSummaries(), null),
  );
  renderPanel();
  await sendQuestion("用知识库生成候选");
  await screen.findByText("第一次结果。");

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("重新生成结果。");

  // The whole round was replaced: one knowledge round, newest result only.
  expect(screen.getAllByTestId("ai-tool-call")).toHaveLength(3);
  expect(screen.queryByText("第一次结果。")).toBeNull();
  // recent_messages stays plain text: the next payload carries no tool data
  // (the backend contract; the panel never writes tool parts into history).
  expect(apiSpy).toHaveBeenCalledTimes(2);
  const regeneratePayload = apiSpy.mock.calls[1][1] as {
    message: string;
    recent_messages: unknown[];
  };
  expect(regeneratePayload.message).toBe("用知识库生成候选");
  expect(regeneratePayload.recent_messages).toEqual([]);
});
