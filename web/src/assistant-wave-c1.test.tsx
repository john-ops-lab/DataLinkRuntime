/**
 * M5.7 Wave C1: controlled read-only Tool Call UI regressions (Issue #80).
 *
 * The backend executes DLR's whitelisted, bounded, read-only tools and
 * returns sanitized AiToolCallSummary entries; this panel converts them into
 * official assistant-ui tool-call parts rendered by the Tool Call UI
 * primitives (tool name, calling/success/error state, sanitized args/result
 * summaries, stable error code). Covered here:
 *
 * - success / error / calling states of the Tool UI (zh-CN / en parity)
 * - accessibility (role="status" region, aria-label, screen-reader text)
 * - tool data never pollutes recent_messages (plain text history only)
 * - Regenerate replaces the whole round (text + tools + Candidate) and
 *   reuses the frozen snapshot; no duplicate user message
 * - long/oversized summaries stay bounded in the DOM (no raw payload echo)
 * - candidate=null / candidate round with tools keep the Wave A/B contracts
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { DlrToolCallUI } from "./components/ai-tool-call";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import type {
  Adapter,
  AiAssistResponse,
  AiToolCallSummary,
} from "./types";
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

function successSummary(overrides: Partial<AiToolCallSummary> = {}): AiToolCallSummary {
  return {
    tool_name: "dlr_docs_list",
    status: "success",
    args_summary: "{}",
    result_summary:
      '{"tool":"dlr_docs_list","total":3,"items":[{"id":"runtime-contract-python","source":"dlr-docs:v1:runtime-contract-python"}]}',
    error_code: null,
    duration_ms: 12,
    result_truncated: false,
    result_size: 900,
    source: "dlr-docs:v1:runtime-contract-python",
    ...overrides,
  };
}

function errorSummary(overrides: Partial<AiToolCallSummary> = {}): AiToolCallSummary {
  return {
    tool_name: "not_registered_tool",
    status: "error",
    args_summary: '{"mode":"read"}',
    result_summary: "",
    error_code: "ai_tool_unknown",
    duration_ms: 1,
    result_truncated: false,
    result_size: 0,
    source: null,
    ...overrides,
  };
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
    toolCallId: "dlr-tool-1-0",
    toolName: "dlr_docs_list",
    args: {},
    argsText: "{}",
    result: '{"tool":"dlr_docs_list","total":3}',
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

// --- official Tool Call UI states --------------------------------------------

it("renders a successful tool call with name, status, args and result summary", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("已查阅平台文档。", [successSummary()]),
  );
  renderPanel();
  await sendQuestion("查一下平台文档");
  await screen.findByText("已查阅平台文档。");

  const card = screen.getByTestId("ai-tool-call");
  expect(card.getAttribute("role")).toBe("status");
  expect(card.getAttribute("aria-label")).toContain("dlr_docs_list");
  expect(card.getAttribute("aria-label")).toContain("成功");
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("dlr_docs_list");
  expect(screen.getByTestId("ai-tool-status").textContent).toBe("成功");
  expect(screen.getByTestId("ai-tool-args").textContent).toContain("参数");
  expect(screen.getByTestId("ai-tool-result").textContent).toContain("dlr-docs:v1");
  expect(screen.queryByTestId("ai-tool-error-code")).toBeNull();
  expect(screen.queryByTestId("ai-tool-truncated")).toBeNull();
});

it("renders a failed tool call with the stable error code and no raw content", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("无法使用该工具。", [errorSummary()]),
  );
  renderPanel();
  await sendQuestion("调用未知工具");
  await screen.findByText("无法使用该工具。");

  const card = screen.getByTestId("ai-tool-call");
  expect(card.classList.contains("ai-tool-call-error")).toBe(true);
  expect(card.getAttribute("aria-label")).toContain("失败");
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("not_registered_tool");
  expect(screen.getByTestId("ai-tool-status").textContent).toBe("失败");
  expect(screen.getByTestId("ai-tool-error-code").textContent).toContain("ai_tool_unknown");
  // The failed result shows only the stable rejection label — the sanitized
  // args summary stays visible (that is its contract), never raw content.
  expect(screen.getByTestId("ai-tool-result").textContent).toContain("已被拒绝或失败");
  expect(screen.getByTestId("ai-tool-result").textContent).not.toContain("mode");
});

it("renders the calling state through the official running part status", () => {
  const { rerender } = render(
    <div>
      <DlrToolCallUI {...partProps({ status: { type: "running" }, result: undefined })} />
    </div>,
  );
  expect(screen.getByTestId("ai-tool-status").textContent).toBe("调用中");
  expect(screen.getByTestId("ai-tool-call").classList.contains("ai-tool-call-calling")).toBe(
    true,
  );
  expect(screen.queryByTestId("ai-tool-result")).toBeNull();
  rerender(
    <div>
      <DlrToolCallUI
        {...partProps({ status: { type: "complete" }, result: '{"ok":true}' })}
      />
    </div>,
  );
  expect(screen.getByTestId("ai-tool-status").textContent).toBe("成功");
});

it("localizes the Tool UI zh-CN / en with instant switching", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("done.", [successSummary(), errorSummary()]),
  );
  renderPanel();
  await sendQuestion("工具测试");
  await screen.findByText("done.");

  await applySystemLocale("en");
  await waitFor(() => expect(screen.getAllByTestId("ai-tool-status")[0].textContent).toBe("Success"));
  const successCard = screen.getAllByTestId("ai-tool-call").find((card) =>
    card.classList.contains("ai-tool-call-success"),
  );
  expect(
    successCard?.querySelector('[data-testid="ai-tool-status"]')?.textContent,
  ).toBe("Success");
  expect(successCard?.querySelector('[data-testid="ai-tool-name"]')?.textContent).toBe(
    "dlr_docs_list",
  );
  const errorCard = screen
    .getAllByTestId("ai-tool-call")
    .find((card) => card.classList.contains("ai-tool-call-error"));
  expect(errorCard?.querySelector('[data-testid="ai-tool-status"]')?.textContent).toBe(
    "Failed",
  );
  expect(
    errorCard?.querySelector('[data-testid="ai-tool-error-code"]')?.textContent,
  ).toContain("Error code: ai_tool_unknown");
});

it("keeps zh-CN / en Tool UI i18n key parity", () => {
  const zhTools = (zh as { assistant: { tools: object } }).assistant.tools;
  const enTools = (en as { assistant: { tools: object } }).assistant.tools;
  const flatten = (value: object, prefix = ""): string[] =>
    Object.entries(value).flatMap(([key, item]) =>
      typeof item === "object" && item !== null
        ? flatten(item as object, `${prefix}${key}.`)
        : [`${prefix}${key}`],
    );
  expect(flatten(zhTools).sort()).toEqual(flatten(enTools).sort());
});

// --- recent_messages isolation -----------------------------------------------

it("never puts tool data into recent_messages; the next round's payload stays plain text", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮（带工具）。", [successSummary()]))
    .mockResolvedValueOnce(aiResponse("第二轮（无工具）。", []));
  renderPanel();
  await sendQuestion("第一轮问题");
  await screen.findByText("第一轮（带工具）。");

  await sendQuestion("第二轮问题");
  await screen.findByText("第二轮（无工具）。");

  const payload = assistAdapter.mock.calls[1][1] as {
    recent_messages: unknown[];
  };
  expect(payload.recent_messages).toEqual([
    { role: "user", content: "第一轮问题" },
    { role: "assistant", content: "第一轮（带工具）。" },
  ]);
  expect(JSON.stringify(payload.recent_messages)).not.toContain("tool");
  expect(JSON.stringify(payload.recent_messages)).not.toContain("dlr-docs");
  expect(JSON.stringify(payload.recent_messages)).not.toContain("tool_calls");
});

// --- Regenerate with tools ---------------------------------------------------

it("regenerate replaces the whole round (tools + text + candidate) and reuses the frozen snapshot", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(
      aiResponse("旧回复：查了文档。", [successSummary({ tool_name: "dlr_docs_search" })]),
    )
    .mockResolvedValueOnce(
      aiResponse("新回复：重新查了文档。", [
        successSummary({ tool_name: "dlr_docs_list", source: "dlr-docs:v1:tool-call-contract" }),
      ]),
    );
  renderPanel();
  await sendQuestion("原问题");
  await screen.findByText("旧回复：查了文档。");
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("dlr_docs_search");

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("新回复：重新查了文档。");

  // The regenerated round reuses the original frozen request verbatim.
  const regeneratePayload = assistAdapter.mock.calls[1][1] as {
    message: string;
    recent_messages: unknown[];
  };
  expect(regeneratePayload.message).toBe("原问题");
  expect(regeneratePayload.recent_messages).toEqual([]);
  // The old tool card was replaced by the new one (no duplicate, no branch).
  const toolCards = screen.getAllByTestId("ai-tool-call");
  expect(toolCards).toHaveLength(1);
  expect(screen.getByTestId("ai-tool-name").textContent).toBe("dlr_docs_list");
  expect(screen.getByText("新回复：重新查了文档。")).toBeTruthy();
  expect(screen.queryByText("旧回复：查了文档。")).toBeNull();
});

it("a failed round retry reuses the frozen snapshot without duplicating the user message", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(aiResponse("重试成功（带工具）。", [successSummary()]));
  renderPanel();
  await sendQuestion("会失败的问题");
  await screen.findByTestId("ai-retry");
  fireEvent.click(screen.getByTestId("ai-retry"));
  await screen.findByText("重试成功（带工具）。");
  expect(screen.getByTestId("ai-tool-call")).toBeTruthy();
  const retryPayload = assistAdapter.mock.calls[1][1] as { message: string };
  expect(retryPayload.message).toBe("会失败的问题");
});

// --- bounds / contract regressions -------------------------------------------

it("keeps long sanitized summaries bounded in the DOM (no layout blow-up, no raw echo)", async () => {
  const longResult = "x".repeat(5000);
  const longArgs = "y".repeat(3000);
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("长摘要回复。", [
      successSummary({ args_summary: longArgs, result_summary: longResult }),
    ]),
  );
  renderPanel();
  await sendQuestion("长摘要");
  await screen.findByText("长摘要回复。");
  const argsText = screen.getByTestId("ai-tool-args").textContent ?? "";
  const resultText = screen.getByTestId("ai-tool-result").textContent ?? "";
  // Server-capped summaries are already bounded; the client additionally
  // clamps to DISPLAY_MAX_CHARS, so the DOM never carries the full raw
  // strings and cannot blow up the layout.
  expect(argsText.length).toBeLessThanOrEqual(600 + "参数: ".length);
  expect(resultText.length).toBeLessThanOrEqual(600 + "结果: ".length);
  expect(argsText).not.toContain(longArgs);
  expect(resultText).not.toContain(longResult);
});

it("renders the truncated marker when the server marked the result truncated", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(
    aiResponse("截断回复。", [
      successSummary({
        result_truncated: true,
        result_summary: '{"value":"…[DLR 工具结果已截断]","truncated":true}',
      }),
    ]),
  );
  renderPanel();
  await sendQuestion("截断");
  await screen.findByText("截断回复。");
  expect(screen.getByTestId("ai-tool-truncated").textContent).toContain("已截断");
});

it("keeps candidate=null and candidate rounds working with tools", async () => {
  const candidate = {
    summary: "方案",
    code: "def handle(context, input):\n    return 1\n",
    requirements: "",
    runtime_config: {},
    required_secret_keys: [],
  };
  vi.spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("仅回答。", [successSummary()]))
    .mockResolvedValueOnce(aiResponse("给出候选。", [successSummary()], candidate));
  renderPanel();
  await sendQuestion("只要回答");
  await screen.findByText("仅回答。");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();

  await sendQuestion("给出候选");
  await screen.findByText("给出候选。");
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案");
  // Tool cards render for both rounds without breaking the Candidate card.
  expect(screen.getAllByTestId("ai-tool-call")).toHaveLength(2);
});

it("hides tool data from the DOM when the round used no tools (pre-C1 compat)", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("普通回答。", []));
  renderPanel();
  await sendQuestion("普通问题");
  await screen.findByText("普通回答。");
  expect(screen.queryByTestId("ai-tool-call")).toBeNull();
});

it("ignores a missing tool_calls field (backward-compatible response shape)", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue({
    message: "旧形状回答。",
    provider: "openai",
    model: "m",
    candidate: null,
  });
  renderPanel();
  await sendQuestion("旧形状");
  await screen.findByText("旧形状回答。");
  expect(screen.queryByTestId("ai-tool-call")).toBeNull();
});
