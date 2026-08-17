import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Execution } from "../types";
import LiveLogWorkspace from "./LiveLogWorkspace";

function makeExecution(overrides: Partial<Execution> = {}): Execution {
  return {
    id: 42,
    adapter_id: 7,
    version_id: 3,
    worker_id: 2,
    target_worker_id: 2,
    trigger: "manual",
    scheduled_for: null,
    status: "running",
    input: {},
    output: null,
    output_size: null,
    output_truncated: false,
    output_preview: null,
    stdout: "",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    created_at: "2026-08-15T00:00:00Z",
    started_at: "2026-08-15T00:00:01Z",
    ended_at: null,
    duration_ms: null,
    ...overrides,
  };
}

function renderWorkspace(overrides: Partial<Parameters<typeof LiveLogWorkspace>[0]> = {}) {
  const props: Parameters<typeof LiveLogWorkspace>[0] = {
    execution: makeExecution(),
    liveStdout: "[2026-08-17 10:30:00] 任务开始\n",
    liveStderr: "",
    fallbackExhausted: false,
    waitingForWebhook: false,
    ...overrides,
  };
  render(<LiveLogWorkspace {...props} />);
  return props;
}

it("renders the unified log tab without an internal Execution #N", () => {
  renderWorkspace();

  expect(screen.getByTestId("live-log-workspace").textContent).toContain("实时日志");
  expect(screen.getByTestId("live-log").textContent).toContain("任务开始");
  expect(screen.queryByText(/执行 #/)).toBeNull();
  expect(screen.queryByTestId("live-log-collapsed")).toBeNull();
  expect(screen.getByText("统一日志")).toBeTruthy();
});

it("merges legacy stderr into the unified view without separate stream tabs", () => {
  renderWorkspace({
    liveStdout: "[2026-08-17 10:30:00] ok\n",
    liveStderr: "[2026-08-17 10:30:01] [ERROR] boom\n",
  });

  const log = screen.getByTestId("live-log");
  expect(log.textContent).toContain("ok");
  expect(log.textContent).toContain("boom");
  expect(screen.queryByText("stderr")).toBeNull();
});

it("pauses and resumes following the log tail", () => {
  const { rerender } = render(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout="[2026-08-17 10:30:00] line-1\n"
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  expect(screen.getByTestId("live-log-pause")).toBeTruthy();

  // Pause explicitly: new content must not yank the view to the bottom.
  fireEvent.click(screen.getByTestId("live-log-pause"));
  const resume = screen.getByTestId("live-log-resume");
  expect(resume).toBeTruthy();
  rerender(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout="[2026-08-17 10:30:00] line-1\n[2026-08-17 10:30:01] line-2\n"
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  expect(screen.getByTestId("live-log-resume")).toBeTruthy();

  fireEvent.click(screen.getByTestId("live-log-resume"));
  expect(screen.getByTestId("live-log-pause")).toBeTruthy();
});

it("shows the Webhook waiting state without inventing an Execution", () => {
  renderWorkspace({ execution: null, liveStdout: "", waitingForWebhook: true });

  expect(screen.getByTestId("live-log-workspace").textContent).toContain("等待 Webhook 请求…");
  expect(screen.queryByText(/执行 #/)).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("收到真实请求并创建执行后");
});

it("shows an idle placeholder when nothing has run yet", () => {
  renderWorkspace({ execution: null, liveStdout: "", waitingForWebhook: false });
  expect(screen.getByTestId("live-log-workspace").textContent).toContain("暂无实时日志");
});

// --- M5.5.13：实时日志选区 → AI 上下文（只使用浏览器可见的已脱敏文本） ------

function mockInPaneSelection(pre: HTMLElement, startOffset: number, endOffset: number) {
  const textNode = pre.firstChild as Text;
  const range = document.createRange();
  range.setStart(textNode, startOffset);
  range.setEnd(textNode, endOffset);
  return vi.spyOn(window, "getSelection").mockReturnValue({
    isCollapsed: false,
    rangeCount: 1,
    getRangeAt: () => range,
    toString: () => range.toString(),
    anchorNode: textNode,
    focusNode: textNode,
  } as unknown as Selection);
}

it("offers 加入对话上下文 only for in-pane selections and reports masked text + line range", () => {
  const onAddContext = vi.fn();
  renderWorkspace({
    onAddContext,
    liveStdout: "[2026-08-17 10:30:00] line one\n[2026-08-17 10:30:01] line two\n",
  });
  const pre = screen.getByTestId("live-log");
  const addButton = screen.getByTestId("live-log-add-context") as HTMLButtonElement;
  expect(addButton.disabled).toBe(true);

  const selectionSpy = mockInPaneSelection(pre, 0, 40);
  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
  expect(addButton.disabled).toBe(false);

  fireEvent.click(addButton);
  expect(onAddContext).toHaveBeenCalledTimes(1);
  // 回调只携带渲染出的已脱敏可见文本与 1-based 行范围。
  expect(onAddContext).toHaveBeenCalledWith({
    source: "log",
    text: "[2026-08-17 10:30:00] line one\n[2026-08-",
    start_line: 1,
    end_line: 2,
  });
  selectionSpy.mockRestore();
});

it("keeps 加入对话上下文 disabled without an in-pane selection and ignores empty text", () => {
  const onAddContext = vi.fn();
  renderWorkspace({
    onAddContext,
    liveStdout: "[2026-08-17 10:30:00] line one\n",
  });
  const pre = screen.getByTestId("live-log");
  const addButton = screen.getByTestId("live-log-add-context") as HTMLButtonElement;
  expect(addButton.disabled).toBe(true);

  // 空选区：保持禁用。
  const emptySpy = vi.spyOn(window, "getSelection").mockReturnValue({
    isCollapsed: true,
    rangeCount: 0,
  } as unknown as Selection);
  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
  expect(addButton.disabled).toBe(true);
  fireEvent.click(addButton);
  expect(onAddContext).not.toHaveBeenCalled();

  // 纯空白选区文本：点击不产生片段。
  const whitespaceSpy = mockInPaneSelection(pre, 0, 0);
  whitespaceSpy.mockImplementation(() => ({
    isCollapsed: false,
    rangeCount: 1,
    getRangeAt: () => {
      const textNode = pre.firstChild as Text;
      const range = document.createRange();
      range.setStart(textNode, 0);
      range.setEnd(textNode, 0);
      return range;
    },
    toString: () => "   ",
    anchorNode: pre.firstChild,
    focusNode: pre.firstChild,
  } as unknown as Selection));
  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
  fireEvent.click(addButton);
  expect(onAddContext).not.toHaveBeenCalled();
  emptySpy.mockRestore();
  whitespaceSpy.mockRestore();
});
