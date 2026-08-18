/**
 * M5.7 Wave A: assistant-ui External Store Runtime primitives regressions.
 *
 * Focus: Markdown/GFM rendering, Code Block + Copy, keyboard contract
 * (Enter sends / Shift+Enter newline / Ctrl/Cmd+Enter sends), and scroll
 * follow (a new message never steals the viewport while the user is scrolled
 * up; returning to the bottom resumes following). Candidate/Diff/Apply,
 * stale, Secret binding and recent_messages regressions live in App.test.tsx
 * and keep running unchanged on the primitive-based conversation.
 */

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { Adapter, AiAssistResponse } from "./types";

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

function renderPanel() {
  const adapter = makeAdapter();
  return render(
    <AiAssistantPanel
      open
      adapter={adapter}
      selectedVersionId={10}
      selectedVersionSeq={1}
      workingCopy={workingCopy}
      contentReady
      busy={false}
      contextSnippets={[]}
      theme="vs-dark"
      onOpen={vi.fn()}
      onClose={vi.fn()}
      onApply={vi.fn()}
      onRemoveContextSnippet={vi.fn()}
      onClearContextSnippets={vi.fn()}
    />,
  );
}

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  return writeText;
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("renders Markdown/GFM (lists, table, inline code) and a code block with copy", async () => {
  const writeText = stubClipboard();
  const markdown = [
    "### 说明",
    "",
    "- 第一项",
    "- 第二项",
    "",
    "| A | B |",
    "| --- | --- |",
    "| 1 | 2 |",
    "",
    "```python",
    "def add(a, b):",
    "    return a + b",
    "```",
    "",
    "行内 `code` 与 **粗体**。",
  ].join("\n");
  vi.spyOn(api, "assistAdapter").mockResolvedValue({
    message: markdown,
    provider: "openai",
    model: "test-model",
    candidate: null,
  });

  renderPanel();
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "生成说明" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));

  await screen.findByRole("heading", { name: "说明" });
  expect(screen.getByText("第一项")).toBeTruthy();
  expect(screen.getByText("第二项")).toBeTruthy();
  const table = screen.getByRole("table");
  expect(within(table).getByText("A")).toBeTruthy();
  expect(within(table).getByText("2")).toBeTruthy();
  // 内联代码与粗体。
  const inlineCode = document.querySelector(".ai-markdown p code");
  expect(inlineCode?.textContent).toBe("code");
  expect(inlineCode?.classList.contains("ai-markdown-code-inline")).toBe(true);
  expect(screen.getByText("粗体").tagName).toBe("STRONG");

  // 代码块：语言标头 + 复制按钮；复制写入剪贴板并切换成功文案。
  const copyButton = screen.getByTestId("ai-code-copy");
  expect(copyButton.textContent).toBe("复制代码");
  fireEvent.click(copyButton);
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith("def add(a, b):\n    return a + b\n"),
  );
  await waitFor(() => expect(copyButton.textContent).toBe("已复制"));

  // 新增文案跟随即时语言切换（zh-CN/en parity）。
  await applySystemLocale("en");
  await waitFor(() => expect(copyButton.textContent).toBe("Copied"));
  expect(copyButton.getAttribute("aria-label")).toBe("Copy code");
});

it("sends with Enter, keeps Shift+Enter as a newline, and sends with Ctrl/Cmd+Enter", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce({ message: "回车已发送", provider: "openai", model: "m", candidate: null })
    .mockResolvedValueOnce({ message: "Ctrl 已发送", provider: "openai", model: "m", candidate: null })
    .mockResolvedValueOnce({ message: "Cmd 已发送", provider: "openai", model: "m", candidate: null });
  renderPanel();
  const input = screen.getByTestId("ai-message-input") as HTMLTextAreaElement;

  // Enter → 发送（payload 为去首尾空白的输入）。
  fireEvent.change(input, { target: { value: "回车发送" } });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(1));
  expect((assistAdapter.mock.calls[0][1] as { message: string }).message).toBe("回车发送");
  await screen.findByText("回车已发送");

  // Shift+Enter → 不发送（默认换行；输入保留）。
  fireEvent.change(input, { target: { value: "第一行" } });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(assistAdapter).toHaveBeenCalledTimes(1);
  expect(input.value).toBe("第一行");

  // Ctrl+Enter → 发送。
  fireEvent.change(input, { target: { value: "Ctrl 发送" } });
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(2));
  expect((assistAdapter.mock.calls[1][1] as { message: string }).message).toBe("Ctrl 发送");
  await screen.findByText("Ctrl 已发送");

  // Cmd(Meta)+Enter → 发送。
  fireEvent.change(input, { target: { value: "Cmd 发送" } });
  fireEvent.keyDown(input, { key: "Enter", metaKey: true });
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(3));
  expect((assistAdapter.mock.calls[2][1] as { message: string }).message).toBe("Cmd 发送");
  await screen.findByText("Cmd 已发送");
});

it("sends the click path synchronously into the preparing stage and clears the composer", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue({
    message: "回复完成",
    provider: "openai",
    model: "m",
    candidate: null,
  });
  renderPanel();
  const input = screen.getByTestId("ai-message-input") as HTMLTextAreaElement;

  fireEvent.change(input, { target: { value: "点击发送" } });
  fireEvent.click(screen.getByTestId("ai-send"));
  // 点击路径同步进入“准备”阶段（既有生命周期回归语义），输入框已清空。
  expect(screen.getByTestId("ai-progress-stage").textContent).toBe("正在准备当前代码上下文…");
  expect(input.value).toBe("");
  await screen.findByText("回复完成");
  expect(screen.queryByTestId("ai-progress-stage")).toBeNull();
  expect(screen.queryByTestId("ai-progress-done")).toBeNull();
});

it("does not steal the viewport while the user is scrolled up, and resumes following at the bottom", async () => {
  let resolveSecond: ((response: AiAssistResponse) => void) | undefined;
  const secondResponse = new Promise<AiAssistResponse>((resolve) => {
    resolveSecond = resolve;
  });
  let resolveThird: ((response: AiAssistResponse) => void) | undefined;
  const thirdResponse = new Promise<AiAssistResponse>((resolve) => {
    resolveThird = resolve;
  });
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce({ message: "回答 1", provider: "openai", model: "m", candidate: null })
    .mockImplementationOnce(() => secondResponse)
    .mockImplementationOnce(() => thirdResponse);
  renderPanel();
  const conversation = screen.getByTestId("ai-conversation") as HTMLDivElement;
  let scrollHeight = 300;
  Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(conversation, "scrollHeight", {
    configurable: true,
    get: () => scrollHeight,
  });

  // 第一轮：发送后跟随到底部。
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "问题 1" } });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByText("回答 1");
  expect(conversation.scrollTop).toBe(300);

  // 用户上翻（离开底部）：发送会回到当前交流（既有合同），随后再次上翻；
  // 挂起中的新回复到达时不得抢回滚动位置。
  conversation.scrollTop = 10;
  fireEvent.scroll(conversation);
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "问题 2" } });
  fireEvent.click(screen.getByTestId("ai-send"));
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(2));
  expect(conversation.scrollTop).toBe(300);
  conversation.scrollTop = 10;
  fireEvent.scroll(conversation);
  scrollHeight = 500;
  await act(async () => {
    resolveSecond?.({ message: "回答 2", provider: "openai", model: "m", candidate: null });
    await secondResponse;
  });
  await screen.findByText("回答 2");
  expect(conversation.scrollTop).toBe(10);

  // 回到底部（500-100-400=0 ≤ 32）：发送与回复都恢复跟随新底部。
  conversation.scrollTop = 400;
  fireEvent.scroll(conversation);
  scrollHeight = 900;
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "问题 3" } });
  fireEvent.click(screen.getByTestId("ai-send"));
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(3));
  expect(conversation.scrollTop).toBe(900);
  await act(async () => {
    resolveThird?.({ message: "回答 3", provider: "openai", model: "m", candidate: null });
    await thirdResponse;
  });
  await screen.findByText("回答 3");
  expect(conversation.scrollTop).toBe(900);
});
