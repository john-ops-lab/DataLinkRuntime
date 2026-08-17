import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

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
