import { fireEvent, render, screen } from "@testing-library/react";
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
    liveStdout: "任务开始\n",
    liveStderr: "",
    fallbackExhausted: false,
    waitingForWebhook: false,
    open: true,
    fullscreen: false,
    onOpen: vi.fn(),
    onClose: vi.fn(),
    onEnterFullscreen: vi.fn(),
    onRestoreBottom: vi.fn(),
    ...overrides,
  };
  render(<LiveLogWorkspace {...props} />);
  return props;
}

it("shows one shared stdout/stderr/输出 workspace and exposes fullscreen controls", () => {
  const props = renderWorkspace();

  expect(screen.getByTestId("live-log-workspace").textContent).toContain("执行 #42");
  expect(screen.getByTestId("live-log-stdout").textContent).toContain("任务开始");
  expect(screen.getByText("stderr")).toBeTruthy();
  expect(screen.getByText("输出")).toBeTruthy();

  fireEvent.click(screen.getByTestId("live-log-fullscreen"));
  expect(props.onEnterFullscreen).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByTestId("live-log-close"));
  expect(props.onClose).toHaveBeenCalledOnce();
});

it("shows the Webhook waiting state without inventing an Execution", () => {
  renderWorkspace({ execution: null, liveStdout: "", waitingForWebhook: true });

  expect(screen.getByTestId("live-log-workspace").textContent).toContain("等待 Webhook 请求…");
  expect(screen.queryByText(/执行 #/)).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("收到真实请求并创建执行后");
});

it("collapses without losing the active Execution context", () => {
  const props = renderWorkspace({ open: false });

  expect(screen.queryByTestId("live-log-workspace")).toBeNull();
  expect(screen.getByTestId("live-log-collapsed").textContent).toContain("执行 #42");
  fireEvent.click(screen.getByText(/打开实时日志/));
  expect(props.onOpen).toHaveBeenCalledOnce();
});
