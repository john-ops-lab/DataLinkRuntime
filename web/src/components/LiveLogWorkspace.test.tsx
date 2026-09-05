import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "../i18n";
import type { Execution } from "../types";
import LiveLogWorkspace from "./LiveLogWorkspace";
import { LogView } from "./OutputView";

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
});

function setScrollMetrics(element: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    value: scrollHeight,
  });
  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    value: clientHeight,
  });
}

function makeExecution(overrides: Partial<Execution> = {}): Execution {
  return {
    dispatch_backend: "rabbitmq",
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

function contrastRatio(foreground: string, background: string): number {
  function luminance(hex: string): number {
    const channels = hex
      .match(/[0-9a-f]{2}/gi)
      ?.map((channel) => Number.parseInt(channel, 16) / 255)
      .map((channel) =>
        channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
      );
    if (channels === undefined || channels.length !== 3) {
      throw new Error(`invalid color: ${hex}`);
    }
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (
    (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
    (Math.min(foregroundLuminance, backgroundLuminance) + 0.05)
  );
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

it("shows a queued Execution as waiting for its fixed Worker rather than failed", () => {
  renderWorkspace({
    execution: makeExecution({
      status: "queued",
      worker_id: null,
      target_worker_id: 2,
      dispatch_backend: "rabbitmq",
    }),
  });

  expect(screen.getByTestId("live-queued-notice").textContent).toContain("等待目标 Worker");
  expect(screen.getByTestId("live-queued-notice").textContent).toContain("暂时离线不会被视为失败");
});

it("scopes the shared toolbar contrast contract to history and live LogView controls", async () => {
  render(
    <>
      <LogView
        testId="history-contrast"
        content="saved line\n"
        truncated={false}
        mode="history"
        followControls={false}
      />
      <LogView
        testId="live-contrast"
        content="live line\n"
        truncated={false}
        mode="live"
        onAddContext={vi.fn()}
      />
    </>,
  );

  const historyRegion = screen.getByTestId("history-contrast").closest("[role='region']");
  const liveRegion = screen.getByTestId("live-contrast").closest("[role='region']");
  expect(historyRegion?.classList.contains("log-pane")).toBe(true);
  expect(historyRegion?.classList.contains("history-log-pane")).toBe(true);
  expect(liveRegion?.classList.contains("log-pane")).toBe(true);
  expect(liveRegion?.classList.contains("live-log-pane")).toBe(true);
  expect(screen.getByTestId("history-contrast-toolbar").getAttribute("aria-label")).toBe(
    "历史日志工具",
  );
  expect(screen.getByTestId("live-contrast-toolbar").getAttribute("aria-label")).toBe(
    "执行日志工具栏",
  );
  expect(screen.getByTestId("history-contrast-search").getAttribute("aria-label")).toBe(
    "搜索历史日志",
  );
  expect(screen.getByTestId("history-contrast-copy").getAttribute("aria-label")).toBe(
    "复制已保存日志",
  );
  expect(screen.getByTestId("history-contrast-download").getAttribute("aria-label")).toBe(
    "下载已保存日志",
  );
  expect(screen.getByTestId("history-contrast-maximize").getAttribute("aria-label")).toBe(
    "最大化日志",
  );
  expect(screen.getByTestId("live-contrast-pause").getAttribute("aria-label")).toBe(
    "暂停跟随",
  );
  expect(screen.getByTestId("live-contrast-maximize").getAttribute("aria-label")).toBe(
    "最大化日志",
  );
  expect((screen.getByTestId("live-contrast-add-context") as HTMLButtonElement).disabled).toBe(
    true,
  );

  await act(async () => {
    await applySystemLocale("en");
  });
  expect(screen.getByTestId("history-contrast-search").getAttribute("aria-label")).toBe(
    "Search history logs",
  );
  expect(screen.getByTestId("history-contrast-copy").getAttribute("aria-label")).toBe(
    "Copy saved logs",
  );
  expect(screen.getByTestId("history-contrast-download").getAttribute("aria-label")).toBe(
    "Download saved logs",
  );
  expect(screen.getByTestId("live-contrast-pause").getAttribute("aria-label")).toBe(
    "Pause following",
  );

  const styles = readFileSync(join(process.cwd(), "src/index.css"), "utf8");
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-btn\s*\{/);
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-btn:hover:not\(:disabled\)/);
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-btn:focus-visible/);
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-btn:disabled/);
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-input-affix-wrapper\s*\{/);
  expect(styles).toMatch(/\.log-pane \.log-toolbar \.ant-input-prefix/);
  expect(styles).not.toContain(".live-log-workspace .log-toolbar .ant-btn");
  expect(styles).not.toContain(".log-pane-maximized .log-toolbar .ant-btn");

  expect(contrastRatio("#c9d1d9", "#21262d")).toBeGreaterThanOrEqual(4.5);
  expect(contrastRatio("#ffffff", "#30363d")).toBeGreaterThanOrEqual(4.5);
  expect(contrastRatio("#6e7681", "#161b22")).toBeGreaterThanOrEqual(3);
  expect(contrastRatio("#24292f", "#ffffff")).toBeGreaterThanOrEqual(4.5);
  expect(contrastRatio("#57606a", "#ffffff")).toBeGreaterThanOrEqual(4.5);
  expect(contrastRatio("#8c959f", "#ffffff")).toBeGreaterThanOrEqual(3);
  expect(contrastRatio("#58a6ff", "#161b22")).toBeGreaterThanOrEqual(3);
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

it("keeps receiving appended logs at the paused scroll position", () => {
  const { rerender } = render(
    <LogView
      testId="paused-log"
      content={"line-1\nline-2\n"}
      truncated={false}
      mode="live"
    />,
  );
  const pre = screen.getByTestId("paused-log");
  setScrollMetrics(pre, 400, 100);
  pre.scrollTop = 70;
  fireEvent.click(screen.getByTestId("paused-log-pause"));

  setScrollMetrics(pre, 700, 100);
  rerender(
    <LogView
      testId="paused-log"
      content={"line-1\nline-2\nline-3\n"}
      truncated={false}
      mode="live"
    />,
  );
  expect(pre.textContent).toBe("line-1\nline-2\n");
  expect(pre.scrollTop).toBe(70);
  expect(screen.getByTestId("paused-log-resume")).toBeTruthy();

  fireEvent.click(screen.getByTestId("paused-log-resume"));
  expect(pre.textContent).toBe("line-1\nline-2\nline-3\n");
  expect(pre.scrollTop).toBe(700);
  expect(screen.getByTestId("paused-log-pause")).toBeTruthy();
});

it("manual history inspection stays put when the terminal detail refreshes", () => {
  const { rerender } = render(
    <LogView
      testId="history-log"
      content="old-1\nold-2\n"
      truncated={false}
      mode="history"
      followControls={false}
    />,
  );
  const pre = screen.getByTestId("history-log");
  setScrollMetrics(pre, 500, 100);
  pre.scrollTop = 125;
  fireEvent.scroll(pre);

  setScrollMetrics(pre, 800, 100);
  rerender(
    <LogView
      testId="history-log"
      content="old-1\nold-2\nterminal-refresh\n"
      truncated={false}
      mode="history"
      followControls={false}
    />,
  );
  expect(pre.scrollTop).toBe(125);
  expect(screen.queryByTestId("history-log-pause")).toBeNull();
  expect(screen.queryByTestId("history-log-resume")).toBeNull();
});

it("keeps the same follow semantics after an Execution reaches a terminal state", () => {
  const { rerender } = render(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout={"line-1\n"}
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  const pre = screen.getByTestId("live-log");
  setScrollMetrics(pre, 400, 100);
  pre.scrollTop = 80;
  fireEvent.click(screen.getByTestId("live-log-pause"));

  setScrollMetrics(pre, 700, 100);
  rerender(
    <LiveLogWorkspace
      execution={makeExecution({ status: "succeeded", ended_at: "2026-08-15T00:00:02Z" })}
      liveStdout="line-1\nterminal\n"
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  expect(pre.scrollTop).toBe(80);
  expect(screen.getByTestId("live-log-resume")).toBeTruthy();
});

it("localizes follow state in both Chinese and English", async () => {
  renderWorkspace();
  expect(screen.getByTestId("live-log-pause").textContent).toBe("暂停跟随");

  await act(async () => {
    await applySystemLocale("en");
  });
  expect(screen.getByTestId("live-log-pause").textContent).toBe("Pause following");
  fireEvent.click(screen.getByTestId("live-log-pause"));
  expect(screen.getByTestId("live-log-resume").textContent).toBe("Resume following");
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
  expect(addButton.disabled).toBe(true);
  fireEvent.click(addButton);
  expect(onAddContext).not.toHaveBeenCalled();
  emptySpy.mockRestore();
  whitespaceSpy.mockRestore();
});

it("keeps 加入对话上下文 disabled when a selection crosses outside the log pane", () => {
  const onAddContext = vi.fn();
  renderWorkspace({ onAddContext });
  const pre = screen.getByTestId("live-log");
  const textNode = pre.firstChild as Text;
  const outside = document.createTextNode("outside pane");
  document.body.append(outside);
  const range = document.createRange();
  range.setStart(textNode, 0);
  range.setEnd(outside, outside.length);
  const selectionSpy = vi.spyOn(window, "getSelection").mockReturnValue({
    isCollapsed: false,
    rangeCount: 1,
    getRangeAt: () => range,
    toString: () => range.toString(),
    anchorNode: textNode,
    focusNode: outside,
  } as unknown as Selection);

  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
  expect((screen.getByTestId("live-log-add-context") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByTestId("live-log-add-context"));
  expect(onAddContext).not.toHaveBeenCalled();

  selectionSpy.mockRestore();
  outside.remove();
});

it("keeps only the newest 2000 live-log lines", () => {
  const lines = Array.from({ length: 2005 }, (_, index) => `line-${index}`).join("\n");
  renderWorkspace({ liveStdout: lines });

  const content = screen.getByTestId("live-log").textContent ?? "";
  const displayedLines = content.split("\n");
  expect(displayedLines).not.toContain("line-0");
  expect(displayedLines).not.toContain("line-4");
  expect(displayedLines).toContain("line-5");
  expect(displayedLines).toContain("line-2004");
  expect(displayedLines).toHaveLength(2000);
});

it("distinguishes the 2000-line browser window and offers server history", () => {
  const lines = Array.from({ length: 2005 }, (_, index) => `line-${index}`).join("\n");
  const onViewServerLog = vi.fn();
  renderWorkspace({
    execution: makeExecution({ stdout: lines }),
    liveStdout: lines,
    onViewServerLog,
  });

  expect(screen.getByTestId("live-log-browser-window").textContent).toContain("2000");
  fireEvent.click(screen.getByTestId("live-log-view-server"));
  expect(onViewServerLog).toHaveBeenCalledTimes(1);
});

it("shows the browser-window notice while SSE is still appending saved lines", () => {
  renderWorkspace({
    execution: makeExecution({ stdout: "line-2200\n" }),
    liveStdout: "line-2200\n",
    serverLogLineCount: 2201,
  });

  expect(screen.getByTestId("live-log-browser-window").textContent).toContain("2000");
});

it("shows the number of lines appended while live reading is paused", () => {
  const { rerender } = render(
    <LogView testId="counted-log" content={"line-1\n"} truncated={false} mode="live" />,
  );
  fireEvent.click(screen.getByTestId("counted-log-pause"));
  rerender(
    <LogView
      testId="counted-log"
      content={"line-1\nline-2\nline-3\n"}
      truncated={false}
      mode="live"
    />,
  );
  expect(screen.getByTestId("counted-log-new-count").textContent).toContain("2");
});

it("keeps history search, copy and download intact across maximize and restore", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = () => "blob:history-log";
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = () => undefined;
  }
  const createObjectURL = vi
    .spyOn(URL, "createObjectURL")
    .mockReturnValue("blob:history-log");
  const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL");
  const clickedAnchors: HTMLAnchorElement[] = [];
  const anchorClick = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function recordDownload(this: HTMLAnchorElement) {
      clickedAnchors.push(this);
    });
  const saved = "before needle\nother line\nafter needle\n";
  try {
    render(
      <LogView
        testId="history-tools"
        content={saved}
        truncated={false}
        mode="history"
        followControls={false}
        downloadFileName="execution-42"
      />,
    );

    const historyContent = screen.getByTestId("history-tools");
    const historyRegion = historyContent.closest("[role='region']");
    const search = screen.getByTestId("history-tools-search") as HTMLInputElement;
    fireEvent.change(search, { target: { value: "needle" } });
    expect(historyContent.textContent).toContain("before needle");
    expect(historyContent.textContent).not.toContain("other line");
    expect(screen.getByText("匹配 2 行")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByTestId("history-tools-copy"));
    });
    expect(writeText).toHaveBeenCalledWith(saved);
    expect(screen.getByRole("status").textContent).toContain("已复制");

    fireEvent.click(screen.getByTestId("history-tools-download"));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(createObjectURL.mock.calls[0]?.[0]).toBeInstanceOf(Blob);
    expect((createObjectURL.mock.calls[0]?.[0] as Blob).size).toBe(new Blob([saved]).size);
    expect(clickedAnchors).toHaveLength(1);
    expect(clickedAnchors[0]?.download).toBe("execution-42.log");
    expect(clickedAnchors[0]?.href).toBe("blob:history-log");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:history-log");

    fireEvent.click(screen.getByTestId("history-tools-maximize"));
    expect(historyRegion?.classList.contains("log-pane-maximized")).toBe(true);
    expect(screen.getByTestId("history-tools-restore").getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement).toBe(screen.getByTestId("history-tools-restore"));
    expect(search.value).toBe("needle");
    expect(historyContent.textContent).toBe("before needle\nafter needle\n");

    fireEvent.click(screen.getByTestId("history-tools-restore"));
    expect(historyRegion?.classList.contains("log-pane-maximized")).toBe(false);
    expect(document.activeElement).toBe(screen.getByTestId("history-tools-maximize"));
    expect(search.value).toBe("needle");
    expect(historyContent.textContent).toBe("before needle\nafter needle\n");
  } finally {
    anchorClick.mockRestore();
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
    if (originalCreateObjectURL === undefined) {
      Reflect.deleteProperty(URL, "createObjectURL");
    } else {
      URL.createObjectURL = originalCreateObjectURL;
    }
    if (originalRevokeObjectURL === undefined) {
      Reflect.deleteProperty(URL, "revokeObjectURL");
    } else {
      URL.revokeObjectURL = originalRevokeObjectURL;
    }
  }
});

it("preserves live bottom-follow, paused content and dark terminal state across maximize and restore", () => {
  const { rerender } = render(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout="line-1\n"
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  const log = screen.getByTestId("live-log");
  const liveRegion = log.closest("[role='region']");
  setScrollMetrics(log, 400, 100);
  rerender(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout={"line-1\nline-2\n"}
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  expect(log.scrollTop).toBe(400);
  expect(screen.getByTestId("live-log-pause")).toBeTruthy();
  expect(log.textContent).toBe("line-1\nline-2\n");

  log.scrollTop = 70;
  fireEvent.click(screen.getByTestId("live-log-pause"));
  setScrollMetrics(log, 700, 100);
  rerender(
    <LiveLogWorkspace
      execution={makeExecution()}
      liveStdout={"line-1\nline-2\nline-3\n"}
      liveStderr=""
      fallbackExhausted={false}
      waitingForWebhook={false}
    />,
  );
  expect(screen.getByTestId("live-log-resume")).toBeTruthy();
  expect(log.textContent).toBe("line-1\nline-2\n");
  expect(log.scrollTop).toBe(70);

  fireEvent.click(screen.getByTestId("live-log-maximize"));
  expect(screen.getByTestId("live-log-restore")).toBeTruthy();
  expect(liveRegion?.classList.contains("log-pane-maximized")).toBe(true);
  expect(screen.getByTestId("live-log-resume")).toBeTruthy();
  expect(log.textContent).toBe("line-1\nline-2\n");
  expect(log.classList.contains("terminal-view")).toBe(true);
  expect(document.activeElement).toBe(screen.getByTestId("live-log-restore"));

  fireEvent.click(screen.getByTestId("live-log-restore"));
  expect(screen.getByTestId("live-log-maximize")).toBeTruthy();
  expect(liveRegion?.classList.contains("log-pane-maximized")).toBe(false);
  expect(screen.getByTestId("live-log-resume")).toBeTruthy();
  expect(log.textContent).toBe("line-1\nline-2\n");
  expect(log.scrollTop).toBe(70);
  expect(document.activeElement).toBe(screen.getByTestId("live-log-maximize"));

  fireEvent.click(screen.getByTestId("live-log-resume"));
  expect(screen.getByTestId("live-log-pause")).toBeTruthy();
  expect(log.textContent).toBe("line-1\nline-2\nline-3\n");
  expect(log.scrollTop).toBe(700);

  const styles = readFileSync(join(process.cwd(), "src/index.css"), "utf8");
  expect(styles).toMatch(
    /\.terminal-view\s*\{[^}]*background\s*:\s*#111827[^}]*color\s*:\s*#d1d5db/s,
  );
});
