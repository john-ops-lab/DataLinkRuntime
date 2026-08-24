import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import LiveLogWorkspace from "../components/LiveLogWorkspace";
import { openExecutionEvents } from "../sse";
import type { ExecutionEventsHandlers } from "../sse";
import type { Execution } from "../types";
import { useExecutionWatcher } from "./useExecutionWatcher";

vi.mock("../sse", () => ({
  openExecutionEvents: vi.fn(),
}));

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

function WatcherHarness(props: { initial: Execution; next?: Execution }) {
  const watcher = useExecutionWatcher(() => undefined);
  const nextExecution = props.next;
  return (
    <>
      <button type="button" data-testid="watch" onClick={() => watcher.watch(props.initial)}>
        Watch
      </button>
      {nextExecution !== undefined && (
        <button type="button" data-testid="watch-next" onClick={() => watcher.watch(nextExecution)}>
          Watch next
        </button>
      )}
      <output data-testid="execution-status">{watcher.execution?.status ?? ""}</output>
      <output data-testid="server-line-count">{watcher.serverLogLineCount}</output>
      <LiveLogWorkspace
        execution={watcher.execution}
        liveStdout={watcher.liveStdout}
        liveStderr={watcher.liveStderr}
        serverLogLineCount={watcher.serverLogLineCount}
        fallbackExhausted={watcher.fallbackExhausted}
        waitingForWebhook={false}
      />
    </>
  );
}

function latestHandlers(): ExecutionEventsHandlers {
  const calls = vi.mocked(openExecutionEvents).mock.calls;
  const call = calls[calls.length - 1];
  expect(call).toBeDefined();
  return call?.[1] as ExecutionEventsHandlers;
}

describe("useExecutionWatcher live-log boundaries", () => {
  const getExecution = vi.spyOn(api, "getExecution");

  beforeEach(() => {
    vi.mocked(openExecutionEvents).mockReset();
    vi.mocked(openExecutionEvents).mockReturnValue({ close: vi.fn() });
    getExecution.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("keeps the SSE handle open while paused and fills the terminal snapshot on resume", async () => {
    const initial = makeExecution({ stdout: "start\n" });
    const terminal = makeExecution({
      status: "succeeded",
      stdout: "start\nduring-pause\nterminal-final\n",
      ended_at: "2026-08-15T00:00:02Z",
      duration_ms: 1000,
    });
    getExecution.mockResolvedValue(terminal);

    render(<WatcherHarness initial={initial} />);
    fireEvent.click(screen.getByTestId("watch"));
    await waitFor(() => expect(screen.getByTestId("live-log").textContent).toContain("start"));

    const handlers = latestHandlers();
    const streamHandle = vi.mocked(openExecutionEvents).mock.results[0]?.value as {
      close: ReturnType<typeof vi.fn>;
    };
    fireEvent.click(screen.getByTestId("live-log-pause"));

    act(() => {
      handlers.onLog?.({ stream: "stdout", chunk: "during-pause\n" });
    });
    expect(screen.getByTestId("live-log").textContent).not.toContain("during-pause");
    expect(streamHandle.close).not.toHaveBeenCalled();

    act(() => {
      handlers.onExecution?.(terminal);
    });
    await waitFor(() => expect(getExecution).toHaveBeenCalledWith(initial.id));
    expect(streamHandle.close).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("execution-status").textContent).toBe("succeeded");
    expect(screen.getByTestId("live-log").textContent).not.toContain("terminal-final");

    fireEvent.click(screen.getByTestId("live-log-resume"));
    const resumed = screen.getByTestId("live-log").textContent ?? "";
    expect(resumed).toContain("during-pause");
    expect(resumed).toContain("terminal-final");
    expect(resumed.match(/during-pause/g)).toHaveLength(1);
    expect(resumed.match(/terminal-final/g)).toHaveLength(1);
  });

  it("reconciles an unexpected SSE close from the authoritative snapshot without duplicates", async () => {
    const initial = makeExecution({ stdout: "start\n" });
    const authoritative = makeExecution({
      status: "succeeded",
      stdout: "start\nduring-recovery\nfinal\n",
      ended_at: "2026-08-15T00:00:02Z",
      duration_ms: 1000,
    });
    getExecution.mockResolvedValue(authoritative);

    render(<WatcherHarness initial={initial} />);
    fireEvent.click(screen.getByTestId("watch"));
    await waitFor(() => expect(screen.getByTestId("live-log").textContent).toContain("start"));

    const handlers = latestHandlers();
    const streamHandle = vi.mocked(openExecutionEvents).mock.results[0]?.value as {
      close: ReturnType<typeof vi.fn>;
    };
    fireEvent.click(screen.getByTestId("live-log-pause"));
    act(() => {
      handlers.onLog?.({ stream: "stdout", chunk: "during-recovery\n" });
      handlers.onUnexpectedClose?.();
    });

    await waitFor(() => expect(getExecution).toHaveBeenCalledWith(initial.id));
    expect(streamHandle.close).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("execution-status").textContent).toBe("succeeded");
    expect(screen.getByTestId("live-log").textContent).not.toContain("during-recovery");

    fireEvent.click(screen.getByTestId("live-log-resume"));
    const resumed = screen.getByTestId("live-log").textContent ?? "";
    expect(resumed).toBe("start\nduring-recovery\nfinal\n");
    expect(resumed.match(/during-recovery/g)).toHaveLength(1);

    act(() => {
      handlers.onLog?.({ stream: "stdout", chunk: "after-resume\n" });
    });
    expect(screen.getByTestId("live-log").textContent).toBe(
      "start\nduring-recovery\nfinal\nafter-resume\n",
    );
  });

  it("keeps a newer log delta when a reconnect delivers an older execution snapshot", () => {
    const initial = makeExecution({ stdout: "start\n" });
    render(<WatcherHarness initial={initial} />);
    fireEvent.click(screen.getByTestId("watch"));
    const handlers = latestHandlers();

    act(() => {
      handlers.onLog?.({ stream: "stdout", chunk: "first\n" });
      // A reconnect/status race can deliver a snapshot from before the
      // already-applied delta. It must not roll the visible tail backward.
      handlers.onExecution?.({ ...initial, stdout: "start\n" });
      handlers.onLog?.({ stream: "stdout", chunk: "second\n" });
    });

    expect(screen.getByTestId("live-log").textContent).toBe("start\nfirst\nsecond\n");
  });

  it("resets the frozen view when the watcher switches to another execution", async () => {
    const first = makeExecution({ id: 42, stdout: "execution-a\n" });
    const second = makeExecution({ id: 43, stdout: "execution-b\n" });
    render(<WatcherHarness initial={first} next={second} />);
    fireEvent.click(screen.getByTestId("watch"));
    await waitFor(() => expect(screen.getByTestId("live-log").textContent).toContain("execution-a"));

    fireEvent.click(screen.getByTestId("live-log-pause"));
    expect(screen.getByTestId("live-log-resume")).toBeTruthy();

    fireEvent.click(screen.getByTestId("watch-next"));
    await waitFor(() => expect(screen.getByTestId("live-log").textContent).toBe("execution-b\n"));
    expect(screen.getByTestId("live-log").textContent).not.toContain("execution-a");
    expect(screen.getByTestId("live-log-pause")).toBeTruthy();
    expect(screen.queryByTestId("live-log-resume")).toBeNull();
  });

  it("keeps the browser window bounded while the watcher retains the server line count", async () => {
    const initial = makeExecution({ stdout: "line-0\nline-1\n" });
    const lines = Array.from({ length: 2003 }, (_, index) => `line-${index + 2}`).join("\n");
    render(<WatcherHarness initial={initial} />);
    fireEvent.click(screen.getByTestId("watch"));
    await waitFor(() => expect(screen.getByTestId("live-log").textContent).toBe("line-0\nline-1\n"));
    const handlers = latestHandlers();

    fireEvent.click(screen.getByTestId("live-log-pause"));
    const pausedText = screen.getByTestId("live-log").textContent;
    act(() => {
      handlers.onLog?.({ stream: "stdout", chunk: lines });
    });

    expect(screen.getByTestId("live-log").textContent).toBe(pausedText);
    expect(screen.getByTestId("live-log").textContent).toContain("line-0");
    expect(screen.getByTestId("live-log").textContent).not.toContain("line-2004");
    fireEvent.click(screen.getByTestId("live-log-resume"));
    const displayedLines = (screen.getByTestId("live-log").textContent ?? "").split("\n");
    expect(displayedLines).toHaveLength(2000);
    expect(displayedLines[0]).toBe("line-5");
    expect(displayedLines.at(-1)).toBe("line-2004");
    expect(screen.getByTestId("server-line-count").textContent).toBe("2005");
    expect(screen.getByTestId("live-log-browser-window").textContent).toContain("2000");
  });

  it("uses the authoritative log snapshot when server truncation breaks the delta prefix", () => {
    const snapshot = "head\n...[truncated 64 bytes]...\ntail\n";
    render(<WatcherHarness initial={makeExecution()} />);
    fireEvent.click(screen.getByTestId("watch"));
    const handlers = latestHandlers();

    act(() => {
      handlers.onLogSnapshot?.({ stream: "stdout", content: snapshot, truncated: true });
    });

    expect(screen.getByTestId("live-log").textContent).toBe(snapshot);
    expect(screen.getByTestId("live-log-server-truncated")).toBeTruthy();
  });
});
