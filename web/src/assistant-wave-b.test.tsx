/**
 * M5.8 Wave B: AI Assistant UX closeout regressions (Issue #89).
 *
 * These tests keep the four Wave B contracts deterministic:
 * - maximize/restore changes only the panel layout;
 * - Context Snippets are frozen before send and consumed only when that
 *   request enters the provider call;
 * - attachment limits and the privacy notice remain aligned with B2;
 * - Candidate Diff stays code-only, matching the Wave A Apply boundary.
 */

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { Adapter, AiAssistResponse, AiAttachmentCapabilities, AiCandidate } from "./types";

vi.mock("@monaco-editor/react", () => ({
  default: function Editor() {
    return <textarea data-testid="code-editor" readOnly />;
  },
  DiffEditor: function DiffEditor(props: { original?: string; modified?: string }) {
    return (
      <div
        data-testid="diff-editor"
        data-original={props.original ?? ""}
        data-modified={props.modified ?? ""}
      />
    );
  },
  loader: {
    init: () => Promise.resolve({ editor: { setTheme: () => undefined } }),
  },
}));

async function openDetails(view: "scope" | "help") {
  fireEvent.click(screen.getByTestId("ai-more"));
  fireEvent.click(await screen.findByRole("menuitem", { name: view === "scope" ? /查看发送内容|Review outgoing content/ : /使用说明|Usage guide/ }));
}
async function closeDetails() {
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /close|关闭/i }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
}
async function waitForCapabilities() {
  await act(async () => { await Promise.allSettled(vi.mocked(api.getAiAttachmentCapabilities).mock.results.map((result) => result.value)); });
}

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
  requirements: "requests==2.32.0",
  runtimeConfigText: '{"timeout": 30}',
};

const attachmentCapabilities: AiAttachmentCapabilities = {
  limits: {
    max_attachments: 8,
    max_file_bytes: 6 * 1024 * 1024,
    max_total_bytes: 12 * 1024 * 1024,
    max_parsed_chars_per_file: 64 * 1024,
    max_parsed_total_chars: 256 * 1024,
    parse_timeout_seconds: 30,
  },
  supported_content_types: ["text/plain"],
  providers: [],
};

function aiResponse(message: string, candidate: AiCandidate | null): AiAssistResponse {
  return { message, provider: "openai", model: "test-model", candidate };
}

const candidate: AiCandidate = {
  summary: "只修改代码",
  code: "def handle(context, input):\n    return 42\n",
  requirements: "provider-must-not-display-this",
  runtime_config: { provider_must_not_display: true },
  required_secret_keys: ["MANUAL_CREDENTIAL"],
};

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
  const view = render(<AiAssistantPanel {...base} />);
  return {
    view,
    rerender: (next: Partial<PanelProps>) =>
      view.rerender(<AiAssistantPanel {...base} {...next} />),
  };
}

function makeFile(name: string, content = "draft attachment") {
  return new File([content], name, { type: "text/plain" });
}

async function sendQuestion(text: string) {
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("ai-send"));
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
  vi.spyOn(api, "getAiAttachmentCapabilities").mockResolvedValue(attachmentCapabilities);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("M5.8-001 maximizes and restores in place without losing draft, snippets, attachments, request or Candidate", async () => {
  let resolveAssist: ((response: AiAssistResponse) => void) | undefined;
  const pendingAssist = new Promise<AiAssistResponse>((resolve) => {
    resolveAssist = resolve;
  });
  vi.spyOn(api, "assistAdapter").mockImplementation(async () => pendingAssist);

  renderPanel({
    contextSnippets: [
      { id: 7, source: "code", text: "return input", start_line: 2, end_line: 2 },
    ],
  });
  await waitForCapabilities();

  fireEvent.change(screen.getByTestId("ai-attachment-input"), {
    target: { files: [makeFile("notes.txt")] },
  });
  await screen.findByTestId("ai-attachment-item");
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "未发送草稿" },
  });

  const panelElement = screen.getByTestId("ai-assistant-panel");
  const maximize = screen.getByTestId("maximize-ai-assistant") as HTMLButtonElement;
  maximize.focus();
  expect(document.activeElement).toBe(maximize);
  expect(maximize.getAttribute("aria-label")).toBe("最大化 AI 助手");
  expect(maximize.tabIndex).toBe(0);

  fireEvent.click(maximize);
  expect(screen.getByTestId("ai-assistant-panel")).toBe(panelElement);
  expect(panelElement.getAttribute("data-layout")).toBe("maximized");
  expect(screen.getByTestId("restore-ai-assistant").getAttribute("aria-pressed")).toBe("true");
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("未发送草稿");
  expect(screen.getByTestId("ai-snippet-7")).toBeTruthy();
  expect(screen.getByTestId("ai-attachment-item")).toBeTruthy();
  expect(screen.getByTestId("ai-current-context").textContent).toContain("adapter-a");

  fireEvent.click(screen.getByTestId("restore-ai-assistant"));
  expect(screen.getByTestId("ai-assistant-panel")).toBe(panelElement);
  expect(panelElement.getAttribute("data-layout")).toBe("sidebar");
  expect(screen.getByTestId("maximize-ai-assistant").getAttribute("aria-pressed")).toBe("false");
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("未发送草稿");
  expect(screen.getByTestId("ai-attachment-item")).toBeTruthy();

  // The same mounted runtime keeps the request alive while the layout changes.
  fireEvent.click(screen.getByTestId("ai-send"));
  await waitFor(() => expect(api.assistAdapter).toHaveBeenCalledTimes(1));
  await screen.findByTestId("ai-loading");
  fireEvent.click(screen.getByTestId("maximize-ai-assistant"));
  expect(screen.getByTestId("ai-loading")).toBeTruthy();
  fireEvent.click(screen.getByTestId("restore-ai-assistant"));
  expect(screen.getByTestId("ai-loading")).toBeTruthy();

  resolveAssist?.(aiResponse("候选仍在", candidate));
  await screen.findByTestId("ai-candidate-summary");
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("只修改代码");
});

it("M5.8-002 freezes Context Snippets, consumes only sent entries, and preserves history", async () => {
  const removeSnippet = vi.fn();
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮", null))
    .mockResolvedValueOnce(aiResponse("第二轮", null));
  const panel = renderPanel({
    contextSnippets: [
      { id: 11, source: "code", text: "first", start_line: 1, end_line: 1 },
      { id: 12, source: "log", text: "second", start_line: 2, end_line: 2 },
    ],
    onRemoveContextSnippet: removeSnippet,
  });

  await sendQuestion("带上下文发送");
  await screen.findByText("第一轮");
  await waitFor(() => expect(removeSnippet.mock.calls).toEqual([[11], [12]]));

  const firstPayload = assistAdapter.mock.calls[0]?.[1] as {
    context_snippets?: unknown;
  };
  expect(firstPayload.context_snippets).toEqual([
    { source: "code", text: "first", start_line: 1, end_line: 1 },
    { source: "log", text: "second", start_line: 2, end_line: 2 },
  ]);

  // Model the parent applying the selective removals. The existing messages
  // stay in the mounted panel and remain the next request's history.
  panel.rerender({ contextSnippets: [] });
  await sendQuestion("不带旧片段");
  await screen.findByText("第二轮");

  const secondPayload = assistAdapter.mock.calls[1]?.[1] as {
    context_snippets?: unknown;
    recent_messages: unknown;
  };
  expect(secondPayload.context_snippets).toBeUndefined();
  expect(secondPayload.recent_messages).toEqual([
    { role: "user", content: "带上下文发送" },
    { role: "assistant", content: "第一轮" },
  ]);
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(2);
  expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(2);
});

it("M5.8-002 does not consume snippets when a reliable snapshot cannot be formed", async () => {
  const removeSnippet = vi.fn();
  const assistAdapter = vi.spyOn(api, "assistAdapter");
  renderPanel({
    workingCopy: { ...workingCopy, runtimeConfigText: "not-json" },
    contextSnippets: [{ id: 21, source: "code", text: "keep", start_line: 1, end_line: 1 }],
    onRemoveContextSnippet: removeSnippet,
  });

  await sendQuestion("失败前保留");
  await screen.findByTestId("ai-panel-error");
  expect(removeSnippet).not.toHaveBeenCalled();
  expect(assistAdapter).not.toHaveBeenCalled();
  expect(screen.getByTestId("ai-snippet-21")).toBeTruthy();
});

it.each(["zh-CN", "en"] as const)("M5.8-009 keeps the %s attachment hint and safety copy aligned", async (locale) => {
  await applySystemLocale(locale);
  renderPanel();
  await openDetails("help");
  const hint = await screen.findByTestId("ai-attachment-hint");
  const privacy = screen.getByTestId("ai-attachment-privacy");

  expect(hint.contains(privacy)).toBe(true);
  expect(hint.textContent).toContain("6MB");
  expect(privacy.textContent).not.toContain(locale === "zh-CN" ? "DLR" : "detect");
  expect(privacy.textContent).toBe(
    locale === "zh-CN"
      ? "请勿上传敏感信息。"
      : "Do not upload sensitive information.",
  );
});

it("M5.8-010 keeps Candidate Diff to code even when the Candidate contains non-code fields", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("候选说明", candidate));
  renderPanel();

  await sendQuestion("查看修改");
  await screen.findByTestId("ai-candidate-summary");
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  const diff = await screen.findByTestId("version-diff");

  expect(within(diff).getByText("代码")).toBeTruthy();
  expect(within(diff).queryByText("运行参数")).toBeNull();
  expect(within(diff).queryByText("依赖")).toBeNull();
  expect(within(diff).queryByText("凭据")).toBeNull();
  expect(screen.getAllByTestId("diff-editor")).toHaveLength(1);
  expect(screen.getByTestId("diff-editor").getAttribute("data-original")).toBe(workingCopy.code);
  expect(screen.getByTestId("diff-editor").getAttribute("data-modified")).toBe(candidate.code);
});

it("Issue 212 examples only fill the composer and the nearby scope follows the current draft", async () => {
  const assist = vi.spyOn(api, "assistAdapter");
  const panel = renderPanel();
  expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByTestId("ai-example-explain"));
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toContain("解释当前代码");
  expect(assist).not.toHaveBeenCalled();
  expect(screen.queryByTestId("ai-message-user")).toBeNull();
  await openDetails("help");
  expect(screen.getByTestId("ai-session-notice").textContent).toContain("收起助手不清空");
  await closeDetails();
  await openDetails("scope");
  expect(screen.getByTestId("ai-send-scope").textContent).toContain(workingCopy.code);
  panel.rerender({ workingCopy: { ...workingCopy, code: "print('changed draft')" } });
  expect(screen.getByTestId("ai-send-scope").textContent).toContain("print('changed draft')");
  expect(assist).not.toHaveBeenCalled();
});

it("Issue 212 attachment records keep this round’s frozen scope after later edits", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("Check the values", null));
  const panel = renderPanel();
  fireEvent.change(screen.getByTestId("ai-attachment-input"), { target: { files: [makeFile("round-one.txt")] } });
  await screen.findByTestId("ai-attachment-item");
  await sendQuestion("first question");
  await screen.findByTestId("ai-message-assistant");
  panel.rerender({ workingCopy: { ...workingCopy, code: "new code for later round" } });
  const record = screen.getByTestId("ai-message-attachments");
  expect(record.textContent).toContain("first question");
  expect(record.textContent).toContain("round-one.txt");
  expect(within(record).getByTestId("ai-round-scope").textContent).toContain(workingCopy.code);
  expect(within(record).getByTestId("ai-round-scope").textContent).not.toContain("new code for later round");
  expect(record.textContent).toContain("不代表模型已完整读取或理解");
});


it("Issue 215 keeps limits in the usage dialog and only two composer actions", async () => {
  renderPanel();
  expect(screen.queryByTestId("ai-attachment-limits")).toBeNull();
  expect(screen.queryByTestId("ai-send-scope")).toBeNull();
  expect(within(screen.getByTestId("ai-composer-actions")).getAllByRole("button")).toHaveLength(2);
  await openDetails("help");
  const help = await screen.findByTestId("ai-help");
  expect(within(help).getByTestId("ai-attachment-capability")).toBeTruthy();
  expect(within(help).getByTestId("ai-attachment-hint").textContent).toContain("6MB");
  expect(help.textContent).not.toContain("application/");
  await closeDetails();
  expect(document.activeElement).toBe(screen.getByTestId("ai-more"));
});

it.each(["zh-CN", "en"] as const)("Issue 215 previews the live %s draft and files without sending", async (locale) => {
  await applySystemLocale(locale);
  const assist = vi.spyOn(api, "assistAdapter");
  const panel = renderPanel();
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "unsent question" } });
  fireEvent.change(screen.getByTestId("ai-attachment-input"), { target: { files: [makeFile("draft.txt")] } });
  await screen.findByTestId("ai-attachment-item");
  await openDetails("scope");
  const scope = await screen.findByTestId("ai-send-scope");
  expect(scope.textContent).toContain("draft.txt");
  expect(scope.textContent).toContain("unsent question");
  panel.rerender({ workingCopy: { ...workingCopy, code: "updated code" } });
  expect(scope.textContent).toContain("updated code");
  await closeDetails();
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("unsent question");
  expect(screen.getByTestId("ai-attachment-name").textContent).toBe("draft.txt");
  expect(assist).not.toHaveBeenCalled();
});

it("Issue 215 closes help with Escape before restoring maximized layout", async () => {
  renderPanel();
  fireEvent.click(screen.getByTestId("maximize-ai-assistant"));
  fireEvent.keyDown(screen.getByTestId("ai-more"), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "使用说明" }));
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape", code: "Escape", keyCode: 27 });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByTestId("ai-assistant-panel").getAttribute("data-layout")).toBe("maximized");
  expect(document.activeElement).toBe(screen.getByTestId("ai-more"));
});

it("Issue 215 code preview is read-only and keeps Diff as the Apply surface", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("候选说明", candidate));
  const onApply = vi.fn();
  renderPanel({ onApply });
  await sendQuestion("修改代码");
  await screen.findByTestId("ai-candidate-summary");
  fireEvent.click(screen.getByTestId("ai-preview-code"));
  expect((await screen.findByTestId("ai-code-preview")).textContent).toBe(candidate.code);
  expect(within(screen.getByRole("dialog")).queryByText("应用")).toBeNull();
  expect(onApply).not.toHaveBeenCalled();
});
