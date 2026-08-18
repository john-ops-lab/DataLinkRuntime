/**
 * M5.7 Wave B1: Regenerate / onReload regressions (Issue #80).
 *
 * The External Store Runtime contract: a Regenerate click goes through
 * `thread.startRun({ parentId })`, which the runtime forwards to the adapter
 * option `onReload(parentId)`; DLR reuses the frozen AssistRoundSnapshot of
 * the original round (user message, visible recent_messages boundary,
 * working_copy/base version, ordered context snippets, adapter identity) and
 * never reads the current editor / Adapter / config. The target assistant
 * round is replaced in place; no duplicate user message; failures keep the
 * original message and draft.
 *
 * Candidate / Diff / Apply / stale / Secret-binding / running / late-response
 * / adapter-isolation / a11y / i18n-parity regressions live here and keep the
 * Wave A contracts unchanged.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { Adapter, AiAssistResponse, AiCandidate, AiContextSnippet } from "./types";

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
  requirements: "依赖：无",
  runtimeConfigText: "{}",
};

function aiResponse(message: string, candidate: AiCandidate | null): AiAssistResponse {
  return { message, provider: "openai", model: "test-model", candidate };
}

function makeCandidate(overrides: Partial<AiCandidate> = {}): AiCandidate {
  return {
    summary: "方案一",
    code: "def handle(context, input):\n    return \"new\"\n",
    requirements: "依赖：无",
    runtime_config: {},
    required_secret_keys: [],
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
  const view = render(<AiAssistantPanel {...base} />);
  return {
    view,
    rerender: (next: Partial<PanelProps>) =>
      view.rerender(<AiAssistantPanel {...base} {...next} />),
  };
}

async function sendQuestion(text: string, expectedReply: string) {
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: text },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByText(expectedReply);
}

type AssistPayload = {
  message: string;
  working_copy: { code: string; requirements: string; runtime_config: Record<string, unknown> };
  recent_messages: { role: "user" | "assistant"; content: string }[];
  base_version_id: number | null;
  context_snippets?: AiContextSnippet[];
};

function payloadOf(
  mock: { mock: { calls: unknown[][] } },
  index: number,
): AssistPayload {
  return mock.mock.calls[index][1] as AssistPayload;
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("regenerate reuses the frozen round snapshot, never the current editor/version/snippets, and adds no duplicate user message", async () => {
  const snippets: PanelProps["contextSnippets"] = [
    { id: 1, source: "code", text: "第一段", start_line: 1, end_line: 2 },
    { id: 2, source: "log", text: "10:21:03 已启动", start_line: 1, end_line: 1 },
  ];
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", null))
    .mockResolvedValueOnce(aiResponse("重新生成的回复", null));
  const panel = renderPanel({ contextSnippets: snippets });
  await sendQuestion("请修改", "第一轮回复");

  const first = payloadOf(assistAdapter, 0);
  expect(first.message).toBe("请修改");
  expect(first.working_copy.code).toBe(workingCopy.code);
  expect(first.base_version_id).toBe(10);
  expect(first.context_snippets).toEqual([
    { source: "code", text: "第一段", start_line: 1, end_line: 2 },
    { source: "log", text: "10:21:03 已启动", start_line: 1, end_line: 1 },
  ]);

  // 发送后编辑器 / 版本 / 上下文片段全部变化：Regenerate 仍必须使用原值。
  panel.rerender({
    workingCopy: {
      code: "def handle(context, input):\n    return \"current-editor\"\n",
      requirements: "依赖：新",
      runtimeConfigText: '{"timeout": 5}',
    },
    selectedVersionId: 99,
    selectedVersionSeq: 7,
    contextSnippets: [{ id: 3, source: "code", text: "后来的片段", start_line: 9, end_line: 9 }],
  });

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("重新生成的回复");

  const second = payloadOf(assistAdapter, 1);
  expect(second.message).toBe("请修改");
  expect(second.working_copy.code).toBe(workingCopy.code);
  expect(second.working_copy.requirements).toBe(workingCopy.requirements);
  expect(second.working_copy.runtime_config).toEqual({});
  expect(second.base_version_id).toBe(10);
  expect(second.context_snippets).toEqual([
    { source: "code", text: "第一段", start_line: 1, end_line: 2 },
    { source: "log", text: "10:21:03 已启动", start_line: 1, end_line: 1 },
  ]);

  // 无重复 user message：替换目标 assistant 轮，旧回复消失、新回复在位。
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
  expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(1);
  expect(screen.queryByText("第一轮回复")).toBeNull();
  expect(screen.getByText("重新生成的回复")).toBeTruthy();
});

it("regenerate reuses the frozen recent_messages boundary of the original round", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("回答一", null))
    .mockResolvedValueOnce(aiResponse("回答二", null))
    .mockResolvedValueOnce(aiResponse("回答一重生成", null));
  renderPanel();

  await sendQuestion("问题一", "回答一");
  const first = payloadOf(assistAdapter, 0);
  expect(first.recent_messages).toEqual([]);

  await sendQuestion("问题二", "回答二");
  const second = payloadOf(assistAdapter, 1);
  expect(second.recent_messages).toEqual([
    { role: "user", content: "问题一" },
    { role: "assistant", content: "回答一" },
  ]);

  // 重新生成第一轮：recent_messages 必须是当时的空边界，而不是当前尾部
  // （当前尾部已包含 问题一/回答一/问题二/回答二）。
  fireEvent.click(screen.getAllByTestId("ai-regenerate")[0]);
  await screen.findByText("回答一重生成");

  const third = payloadOf(assistAdapter, 2);
  expect(third.message).toBe("问题一");
  expect(third.recent_messages).toEqual([]);

  // 目标轮被替换；后续轮次作为历史保留，user 消息不重复。
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(2);
  expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(2);
  expect(screen.queryByText("回答一")).toBeNull();
  expect(screen.getByText("回答一重生成")).toBeTruthy();
  expect(screen.getByText("回答二")).toBeTruthy();
});

it("regenerates a plain-text round with candidate=null and never invents a candidate", async () => {
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("普通回复", null))
    .mockResolvedValueOnce(aiResponse("再次普通回复", null));
  renderPanel();
  await sendQuestion("解释一下", "普通回复");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("再次普通回复");

  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.queryByTestId("ai-view-diff")).toBeNull();
  expect(payloadOf(assistAdapter, 1).message).toBe("解释一下");
});

it("regenerate replaces the Candidate and the new Candidate keeps Candidate→Diff→Apply (browser Working Copy only)", async () => {
  const onApply = vi.fn();
  const candidateOne = makeCandidate({ summary: "方案一" });
  const candidateTwo = makeCandidate({ summary: "方案二", code: "def handle():\n    return 2\n" });
  vi.spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", candidateOne))
    .mockResolvedValueOnce(aiResponse("重新生成的回复", candidateTwo));
  renderPanel({ onApply });
  await sendQuestion("改代码", "第一轮回复");
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案一");

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("重新生成的回复");
  // 旧 Candidate 被替换，不保留多答案树。
  expect(screen.queryByText("方案一")).toBeNull();
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案二");
  expect(screen.getByTestId("ai-candidate-ready")).toBeTruthy();

  // Diff 原始侧是浏览器当前 Working Copy；Apply 只回写浏览器 Working Copy。
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-editor").getAttribute("data-original")).toBe(workingCopy.code);
  expect(screen.getByTestId("diff-editor").getAttribute("data-modified")).toBe(candidateTwo.code);

  fireEvent.click(screen.getByTestId("diff-apply-candidate"));
  expect(onApply).toHaveBeenCalledTimes(1);
  expect((onApply.mock.calls[0][0] as AiCandidate).summary).toBe("方案二");
  await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
  await screen.findByTestId("ai-candidate-applied");
});

it("keeps the regenerated Candidate anchored to the frozen base snapshot, so it turns stale when the editor changed afterwards", async () => {
  const candidateOne = makeCandidate();
  const candidateTwo = makeCandidate({ summary: "方案二" });
  vi.spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", candidateOne))
    .mockResolvedValueOnce(aiResponse("重新生成的回复", candidateTwo));
  const panel = renderPanel();
  await sendQuestion("改代码", "第一轮回复");
  expect(screen.queryByTestId("ai-candidate-stale")).toBeNull();

  // 发送后编辑器变化：重新生成仍基于冻结快照 → 新 Candidate 相对当前编辑器是 stale。
  panel.rerender({ workingCopy: { ...workingCopy, code: "changed-after-send\n" } });
  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("重新生成的回复");

  await screen.findByTestId("ai-candidate-stale");
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案二");
  // stale 的 Diff 仍可查看，Apply 标签为“仍然应用”。
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-apply-candidate").textContent).toContain("仍然应用");
});

it("regenerate refreshes the Secret binding check against the new Candidate", async () => {
  const bindings = vi
    .spyOn(api, "listAdapterBindings")
    .mockResolvedValue([{ env_key: "DB_PASS", credential_id: 1, field: "token" }]);
  const candidateOne = makeCandidate({ required_secret_keys: ["DB_PASS"] });
  const candidateTwo = makeCandidate({
    summary: "方案二",
    required_secret_keys: ["DB_PASS", "API_TOKEN"],
  });
  vi.spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", candidateOne))
    .mockResolvedValueOnce(aiResponse("重新生成的回复", candidateTwo));
  renderPanel();
  await sendQuestion("改代码", "第一轮回复");
  expect(bindings).toHaveBeenCalledTimes(2); // 打开面板 + 首轮生成后
  expect(screen.queryByTestId("ai-missing-secret-keys")).toBeNull();

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("重新生成的回复");
  expect(bindings).toHaveBeenCalledTimes(3); // Regenerate 后重新核对
  await screen.findByTestId("ai-missing-secret-keys");
  expect(screen.getByTestId("ai-missing-secret-keys").textContent).toBe("缺少凭据绑定：API_TOKEN");
});

it("a failed regenerate keeps the original message and Candidate and shows the panel error", async () => {
  const candidateOne = makeCandidate();
  vi.spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", candidateOne))
    .mockRejectedValueOnce(new Error("provider down"));
  renderPanel();
  await sendQuestion("改代码", "第一轮回复");
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案一");

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByTestId("ai-panel-error");
  expect(screen.getByTestId("ai-panel-error").textContent).toContain("AI 请求失败");

  // 失败不丢失原消息与 Candidate；面板恢复可继续操作（输入后发送可用）。
  expect(screen.getByText("第一轮回复")).toBeTruthy();
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("方案一");
  expect(screen.queryByTestId("ai-loading")).toBeNull();
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "新的问题" },
  });
  await waitFor(() =>
    expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(false),
  );
  // 旧的 Candidate 仍可审阅。
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
});

it("disables conflicting actions while a regenerate is running and serializes the requests", async () => {
  let resolveRegen: ((response: AiAssistResponse) => void) | undefined;
  const pendingRegen = new Promise<AiAssistResponse>((resolve) => {
    resolveRegen = resolve;
  });
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", null))
    .mockImplementationOnce(() => pendingRegen);
  renderPanel();
  await sendQuestion("问题一", "第一轮回复");
  expect(assistAdapter).toHaveBeenCalledTimes(1);

  // 输入框留有草稿，用于区分“Composer 空文本禁用”与“运行中禁用”。
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "问题二" },
  });
  expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(false);

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByTestId("ai-loading");
  expect(assistAdapter).toHaveBeenCalledTimes(2);

  // 运行中：发送按钮、输入框、Regenerate、Composer 均禁用。
  expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("ai-regenerate") as HTMLButtonElement).disabled).toBe(true);

  // 原回复在重新生成期间保持可见（成功后才替换）。
  expect(screen.getByText("第一轮回复")).toBeTruthy();

  await act(async () => {
    resolveRegen?.({ message: "重新生成的回复", provider: "openai", model: "m", candidate: null });
    await pendingRegen;
  });
  await screen.findByText("重新生成的回复");
  expect(screen.queryByText("第一轮回复")).toBeNull();
  // 请求结束：输入框恢复、草稿保留、发送与 Regenerate 重新可用。
  await waitFor(() =>
    expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).disabled).toBe(false),
  );
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("问题二");
  await waitFor(() =>
    expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(false),
  );
  expect((screen.getByTestId("ai-regenerate") as HTMLButtonElement).disabled).toBe(false);
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
});

it("drops a regenerate response that resolves after the panel unmounted (generation isolation)", async () => {
  let resolveRegen: ((response: AiAssistResponse) => void) | undefined;
  const pendingRegen = new Promise<AiAssistResponse>((resolve) => {
    resolveRegen = resolve;
  });
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValueOnce(aiResponse("第一轮回复", null))
    .mockImplementationOnce(() => pendingRegen);
  const { view } = renderPanel();
  await sendQuestion("问题一", "第一轮回复");

  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(2));
  view.unmount();

  // 卸载后到达的响应不得提交任何状态（generation 在卸载时已递增）。
  await act(async () => {
    resolveRegen?.({ message: "迟到的回复", provider: "openai", model: "m", candidate: null });
    await pendingRegen;
  });
  expect(screen.queryByText("迟到的回复")).toBeNull();
});

it("never regenerates across an Adapter switch (round snapshot adapter identity)", async () => {
  const adapterA = makeAdapter({ id: 1, name: "adapter-a" });
  const adapterB = makeAdapter({ id: 2, name: "adapter-b" });
  const assistAdapter = vi
    .spyOn(api, "assistAdapter")
    .mockResolvedValue(aiResponse("A 的回复", null));
  const panel = renderPanel({ adapter: adapterA });
  await sendQuestion("A 的问题", "A 的回复");
  expect(assistAdapter).toHaveBeenCalledTimes(1);

  // 面板直接切换 Adapter（真实 App 中会 remount，这里验证显式守卫）。
  panel.rerender({ adapter: adapterB });
  const regenerate = screen.getByTestId("ai-regenerate") as HTMLButtonElement;
  expect(regenerate.disabled).toBe(false);
  fireEvent.click(regenerate);

  // 旧轮次 Regenerate 不得跨 Adapter 使用：不发新请求。
  await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(1));
});

it("shows Regenerate only on assistant rounds with a11y labels and live zh-CN/en parity", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
  renderPanel();
  await sendQuestion("问题", "回复");

  const regenerate = screen.getByTestId("ai-regenerate") as HTMLButtonElement;
  expect(screen.getAllByTestId("ai-regenerate")).toHaveLength(1);
  expect(regenerate.tagName).toBe("BUTTON");
  expect(regenerate.textContent).toBe("重新生成");
  expect(regenerate.getAttribute("aria-label")).toBe(
    "重新生成该回复（复用发送时的原始代码与上下文）",
  );
  expect(regenerate.disabled).toBe(false);

  // 键盘/无障碍：原生 button 可聚焦，aria-label 完整（真实浏览器中 Enter /
  // Space 由原生 button 激活语义触发，jsdom 不合成该激活）。
  regenerate.focus();
  expect(document.activeElement).toBe(regenerate);
  expect(regenerate.tabIndex).toBe(0);

  // 新增文案 zh-CN/en key parity 且即时切换。
  await applySystemLocale("en");
  await waitFor(() => expect(screen.getByTestId("ai-regenerate").textContent).toBe("Regenerate"));
  expect(screen.getByTestId("ai-regenerate").getAttribute("aria-label")).toBe(
    "Regenerate this reply (reusing the original code and context from when it was sent)",
  );
});

it("renders the Regenerate round UI at every tracked width (1280/1440/1680/1920)", async () => {
  // Same functional rendering smoke as the M5.6 Wave 2 B width gate: jsdom
  // has no layout engine, so this pins that each tracked width renders the
  // assistant round with Regenerate visible, without crashes or missing keys.
  vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
  for (const width of [1280, 1440, 1680, 1920]) {
    Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    const { view } = renderPanel();
    await sendQuestion(`问题-${width}`, "回复");
    const regenerate = screen.getByTestId("ai-regenerate") as HTMLButtonElement;
    expect(regenerate.disabled).toBe(false);
    expect(screen.getByTestId("ai-message-user")).toBeTruthy();
    expect(document.body.textContent).not.toContain("assistant.");
    view.unmount();
  }
  Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
});
