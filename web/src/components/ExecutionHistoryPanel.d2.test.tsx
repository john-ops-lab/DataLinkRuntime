import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api";
import { applyUiLocale } from "../i18n";
import { openExecutionEvents } from "../sse";
import type { ExecutionEventsHandlers } from "../sse";
import type {
  Execution,
  ExecutionSummary,
  IncidentDispositionResponse,
  ReliableExecutionDetail,
  ReliableExecutionIncident,
} from "../types";
import ExecutionHistoryPanel from "./ExecutionHistoryPanel";

vi.mock("../sse", () => ({
  openExecutionEvents: vi.fn(() => ({ close: vi.fn() })),
}));

function summary(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
    dispatch_backend: "rabbitmq",
    id: 71,
    adapter_id: 41,
    version_id: 11,
    version_seq: 2,
    worker_id: 3,
    worker_name: "d2-worker",
    trigger: "manual",
    scheduled_for: null,
    status: "succeeded",
    created_at: "2026-08-28T00:00:00Z",
    started_at: "2026-08-28T00:00:01Z",
    ended_at: "2026-08-28T00:00:02Z",
    duration_ms: 1000,
    ...overrides,
  };
}

function execution(overrides: Partial<Execution> = {}): Execution {
  return {
    dispatch_backend: "rabbitmq",
    id: 71,
    adapter_id: 41,
    version_id: 11,
    worker_id: 3,
    target_worker_id: 3,
    trigger: "manual",
    scheduled_for: null,
    status: "succeeded",
    dispatch_generation: 2,
    input: null,
    input_source_type: "none",
    input_config_revision: 4,
    input_snapshot: { source_type: "none", revision: 4 },
    output: { ok: true },
    output_size: null,
    output_truncated: false,
    output_preview: null,
    stdout: "done",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    error_code: null,
    locale: "zh-CN",
    created_at: "2026-08-28T00:00:00Z",
    started_at: "2026-08-28T00:00:01Z",
    ended_at: "2026-08-28T00:00:02Z",
    duration_ms: 1000,
    ...overrides,
  };
}

function incident(overrides: Partial<ReliableExecutionIncident> = {}): ReliableExecutionIncident {
  return {
    id: 901,
    execution_id: 71,
    dispatch_generation: 2,
    message_id: "dispatch-71-2",
    kind: "dispatch_infrastructure_error",
    status: "open",
    attempts: 2,
    observation_count: 3,
    disposition_count: 0,
    recovery_dispatch_count: 0,
    last_error: "dispatch_infrastructure_error",
    created_at: "2026-08-28T00:00:04Z",
    resolved_at: null,
    recent_disposition: null,
    dispositions_url: "/api/executions/71/incidents/901/dispositions",
    recover_available: true,
    recover_reason: null,
    terminate_available: true,
    terminate_reason: null,
    ...overrides,
  };
}

function reliableDetail(
  incidents: ReliableExecutionIncident[],
  overrides: Partial<ReliableExecutionDetail> = {},
): ReliableExecutionDetail {
  return {
    execution_id: 71,
    dispatch_backend: "rabbitmq",
    status: "queued",
    attempts: [],
    incidents,
    replay_available: false,
    replay_reason: null,
    ...overrides,
  };
}

function dispositionResponse(overrides: Partial<IncidentDispositionResponse> = {}): IncidentDispositionResponse {
  return {
    receipt: {
      id: "receipt-901",
      incident_id: 901,
      execution_id: 71,
      idempotency_key: "00000000-0000-4000-8000-000000000901",
      actor_kind: "account",
      user_id: 8,
      action: "recover",
      reason_code: "capacity_repaired",
      outcome: "recovery_dispatched",
      code: "recovery_dispatched",
      from_generation: 2,
      to_generation: 3,
      from_outbox_id: "outbox-2",
      to_outbox_id: "outbox-3",
      execution_status: "queued",
      created_at: "2026-08-28T00:01:00Z",
    },
    incident_status: "resolved",
    execution_status: "queued",
    retry_after_seconds: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function firstHistoryHandlers(): ExecutionEventsHandlers {
  const call = vi.mocked(openExecutionEvents).mock.calls[0];
  expect(call).toBeDefined();
  return call?.[1] as ExecutionEventsHandlers;
}

function mockTwoExecutionEpochs() {
  vi.spyOn(api, "listExecutions").mockResolvedValue({
    items: [summary(), summary({ id: 72 })],
    next_before_id: null,
  });
  vi.spyOn(api, "getExecution").mockImplementation(async (id) => execution({
    id,
    dispatch_generation: id === 71 ? 2 : 4,
  }));
  return vi.spyOn(api, "getReliableExecutionDetail").mockImplementation(async (id) => reliableDetail(
    [incident({
      id: id === 71 ? 901 : 902,
      execution_id: id,
      recover_available: false,
      recover_reason: "execution_terminal",
    })],
    { execution_id: id, status: "succeeded" },
  ));
}

function renderHistory(detail: Execution, row = summary(), canCancel = false) {
  const list = vi.spyOn(api, "listExecutions").mockResolvedValue({
    items: [row],
    next_before_id: null,
  });
  vi.spyOn(api, "getExecution").mockResolvedValue(detail);
  render(<ExecutionHistoryPanel adapterId={41} canCancel={canCancel} />);
  return list;
}

beforeEach(() => {
  vi.mocked(openExecutionEvents).mockClear();
  vi.mocked(openExecutionEvents).mockReturnValue({ close: vi.fn() });
  vi.spyOn(api, "getReliableExecutionDetail").mockImplementation(async (executionId) => ({
    execution_id: executionId,
    dispatch_backend: "rabbitmq",
    status: "succeeded",
    attempts: [],
    incidents: [],
    replay_available: false,
    replay_reason: null,
  }));
});

afterEach(async () => {
  await applyUiLocale("zh-CN");
  vi.restoreAllMocks();
});

describe("Issue #155A queued execution cancellation", () => {
  it("captures the selected ID and synchronously deduplicates a true double click", async () => {
    const pending = deferred<Execution>();
    const cancel = vi.spyOn(api, "cancelExecution").mockReturnValue(pending.promise);
    const list = vi.spyOn(api, "listExecutions").mockResolvedValue({
      items: [
        summary({ id: 71, status: "running" }),
        summary({ id: 72, status: "queued" }),
      ],
      next_before_id: null,
    });
    vi.spyOn(api, "getExecution").mockImplementation(async (id) => execution({
      id,
      status: id === 71 ? "running" : "queued",
    }));
    render(<ExecutionHistoryPanel adapterId={41} canCancel />);
    const rows = await screen.findAllByTestId("history-row");
    const queuedRow = rows[1];
    if (queuedRow === undefined) {
      throw new Error("Queued Execution B row not found");
    }
    fireEvent.click(queuedRow);
    const button = await screen.findByTestId("execution-cancel-queued");

    act(() => {
      button.click();
      button.click();
    });

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith(72);
    expect(button.className).toContain("ant-btn-loading");
    await act(async () => {
      pending.resolve(execution({ status: "cancelled", id: 72 }));
      await pending.promise;
    });
    await waitFor(() => expect(screen.queryByTestId("execution-cancel-queued")).toBeNull());
    expect(screen.getByTestId("execution-run-id").textContent).toContain("72");
    expect(document.body.textContent).toContain("已取消");
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it.each(["success", "403", "409", "network"])(
    "drops a late B %s while C has its own pending cancellation",
    async (outcome) => {
      const first = deferred<Execution>();
      const second = deferred<Execution>();
      vi.spyOn(api, "listExecutions").mockResolvedValue({
        items: [summary({ id: 71, status: "queued" }), summary({ id: 72, status: "queued" })],
        next_before_id: null,
      });
      const getExecution = vi.spyOn(api, "getExecution").mockImplementation(async (id) => execution({
        id,
        status: "queued",
      }));
      vi.spyOn(api, "cancelExecution")
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);
      const view = render(
        <ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} canCancel />,
      );
      fireEvent.click(await screen.findByTestId("execution-cancel-queued"));

      view.rerender(
        <ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={72} canCancel />,
      );
      await waitFor(() => expect(screen.getByTestId("execution-run-id").textContent).toContain("72"));
      fireEvent.click(await screen.findByTestId("execution-cancel-queued"));
      await waitFor(() => expect(api.cancelExecution).toHaveBeenCalledTimes(2));

      await act(async () => {
        if (outcome === "success") {
          first.resolve(execution({ id: 71, status: "cancelled" }));
          await first.promise;
        } else if (outcome === "403") {
          first.reject(new ApiError(403, "adapter_read_only", "old permission failure"));
          await first.promise.catch(() => undefined);
        } else if (outcome === "409") {
          first.reject(new ApiError(409, "incident_execution_active", "old claim conflict"));
          await first.promise.catch(() => undefined);
        } else {
          first.reject(new Error("old network failure"));
          await first.promise.catch(() => undefined);
        }
      });

      expect(screen.getByTestId("execution-run-id").textContent).toContain("72");
      expect(screen.getByTestId("execution-cancel-queued").className).toContain("ant-btn-loading");
      expect(screen.queryByTestId("execution-cancel-error")).toBeNull();
      expect(getExecution).toHaveBeenCalledTimes(2);
      await act(async () => {
        second.resolve(execution({ id: 72, status: "cancelled" }));
        await second.promise;
      });
    },
  );

  it("drops a late response after the detail drawer closes", async () => {
    const pending = deferred<Execution>();
    vi.spyOn(api, "cancelExecution").mockReturnValue(pending.promise);
    renderHistory(execution({ status: "queued" }), summary({ status: "queued" }), true);
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByTestId("execution-cancel-queued"));
    const close = document.querySelector(".ant-drawer-close");
    if (!(close instanceof HTMLButtonElement)) {
      throw new Error("Execution detail close button not found");
    }
    fireEvent.click(close);

    await act(async () => {
      pending.resolve(execution({ status: "cancelled" }));
      await pending.promise;
    });

    expect(screen.queryByTestId("execution-run-id")).toBeNull();
    expect(screen.queryByTestId("execution-cancel-error")).toBeNull();
  });

  it.each(["success", "error follow-up"] as const)(
    "does not restart watcher or list work after unmount on %s",
    async (path) => {
      const pendingCancel = deferred<Execution>();
      const pendingFollowUp = deferred<Execution>();
      const list = vi.spyOn(api, "listExecutions").mockResolvedValue({
        items: [summary({ status: "queued" })],
        next_before_id: null,
      });
      const getExecution = vi.spyOn(api, "getExecution");
      if (path === "success") {
        getExecution.mockResolvedValue(execution({ status: "queued" }));
        vi.spyOn(api, "cancelExecution").mockReturnValue(pendingCancel.promise);
      } else {
        getExecution
          .mockResolvedValueOnce(execution({ status: "queued" }))
          .mockReturnValueOnce(pendingFollowUp.promise);
        vi.spyOn(api, "cancelExecution").mockRejectedValue(
          new ApiError(409, "incident_execution_active", "claim conflict"),
        );
      }
      const panel = <ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} canCancel />;
      const view = render(path === "success" ? <StrictMode>{panel}</StrictMode> : panel);
      fireEvent.click(await screen.findByTestId("execution-cancel-queued"));
      if (path === "error follow-up") {
        await waitFor(() => expect(getExecution).toHaveBeenCalledTimes(2));
      }
      const streamCallsBeforeUnmount = vi.mocked(openExecutionEvents).mock.calls.length;
      const listCallsBeforeUnmount = list.mock.calls.length;
      view.unmount();

      await act(async () => {
        if (path === "success") {
          pendingCancel.resolve(execution({ status: "running", cancel_requested: true }));
          await pendingCancel.promise;
        } else {
          pendingFollowUp.resolve(execution({ status: "running", cancel_requested: true }));
          await pendingFollowUp.promise;
        }
      });

      expect(openExecutionEvents).toHaveBeenCalledTimes(streamCallsBeforeUnmount);
      expect(list).toHaveBeenCalledTimes(listCallsBeforeUnmount);
    },
  );

  it.each(["success", "error follow-up"] as const)(
    "keeps a newer terminal watcher result when an older %s result arrives",
    async (path) => {
      const pendingOperation = deferred<Execution>();
      const terminal = execution({
        status: "succeeded",
        stdout: "new final log",
        output: { result: "new final output" },
      });
      vi.spyOn(api, "listExecutions").mockResolvedValue({
        items: [summary({ status: "queued" })],
        next_before_id: null,
      });
      const getExecution = vi.spyOn(api, "getExecution")
        .mockResolvedValueOnce(execution({ status: "queued" }));
      if (path === "success") {
        getExecution.mockResolvedValueOnce(terminal);
        vi.spyOn(api, "cancelExecution").mockReturnValue(pendingOperation.promise);
      } else {
        getExecution
          .mockReturnValueOnce(pendingOperation.promise)
          .mockResolvedValueOnce(terminal);
        vi.spyOn(api, "cancelExecution").mockRejectedValue(
          new ApiError(409, "incident_execution_active", "claim conflict"),
        );
      }
      render(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} canCancel />);
      fireEvent.click(await screen.findByTestId("execution-cancel-queued"));
      if (path === "error follow-up") {
        await waitFor(() => expect(getExecution).toHaveBeenCalledTimes(2));
      }
      const handlers = firstHistoryHandlers();
      await act(async () => {
        handlers.onExecution?.(terminal);
      });
      await waitFor(() => expect(getExecution).toHaveBeenCalledTimes(path === "success" ? 2 : 3));
      expect(within(screen.getByRole("dialog")).getByText("成功", { exact: true })).toBeTruthy();

      await act(async () => {
        pendingOperation.resolve(execution({
          status: "running",
          cancel_requested: true,
          stdout: "old partial log",
          output: null,
        }));
        await pendingOperation.promise;
      });

      expect(within(screen.getByRole("dialog")).getByText("成功", { exact: true })).toBeTruthy();
      expect(screen.queryByTestId("execution-cancel-pending")).toBeNull();
      expect(openExecutionEvents).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    [403, "adapter_read_only", "permission revoked"],
    [409, "incident_execution_active", "claim conflict"],
    [0, "network_error", "network failed"],
  ] as const)("shows current cancellation error %s and re-reads the fixed target", async (status, code, message) => {
    vi.spyOn(api, "listExecutions").mockResolvedValue({
      items: [summary({ id: 71, status: "queued" })],
      next_before_id: null,
    });
    const getExecution = vi.spyOn(api, "getExecution")
      .mockResolvedValueOnce(execution({ status: "queued" }))
      .mockResolvedValueOnce(execution({ status: "running", cancel_requested: true }));
    vi.spyOn(api, "cancelExecution").mockRejectedValue(new ApiError(status, code, message));
    render(<ExecutionHistoryPanel adapterId={41} canCancel />);
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByTestId("execution-cancel-queued"));

    expect((await screen.findByTestId("execution-cancel-error")).textContent).toContain(code);
    expect(await screen.findByTestId("execution-cancel-pending")).toBeTruthy();
    expect(getExecution).toHaveBeenNthCalledWith(2, 71);
    expect(screen.queryByTestId("execution-cancel-queued")).toBeNull();
  });

  it("preserves the cancellation error when the authoritative follow-up read also fails", async () => {
    vi.spyOn(api, "listExecutions").mockResolvedValue({
      items: [summary({ id: 71, status: "queued" })],
      next_before_id: null,
    });
    vi.spyOn(api, "getExecution")
      .mockResolvedValueOnce(execution({ status: "queued" }))
      .mockRejectedValueOnce(new Error("refresh must not replace the operation error"));
    vi.spyOn(api, "cancelExecution").mockRejectedValue(
      new ApiError(409, "incident_execution_active", "原始取消冲突"),
    );
    render(<ExecutionHistoryPanel adapterId={41} canCancel />);
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByTestId("execution-cancel-queued"));

    const error = await screen.findByTestId("execution-cancel-error");
    expect(error.textContent).toContain("原始取消冲突");
    expect(error.textContent).not.toContain("refresh must not replace");
  });

  it.each([
    ["Claim race", execution({ status: "running", cancel_requested: true }), "execution-cancel-pending"],
    ["terminal race", execution({ status: "succeeded", cancel_requested: false }), null],
  ] as const)("keeps the server-authoritative status for %s", async (_, response, pendingTestId) => {
    vi.spyOn(api, "cancelExecution").mockResolvedValue(response);
    renderHistory(execution({ status: "queued" }), summary({ status: "queued" }), true);
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByTestId("execution-cancel-queued"));

    await waitFor(() => expect(screen.queryByTestId("execution-cancel-queued")).toBeNull());
    if (pendingTestId === null) {
      expect(screen.queryByTestId("execution-cancel-pending")).toBeNull();
      expect(document.body.textContent).toContain("成功");
    } else {
      expect(await screen.findByTestId(pendingTestId)).toBeTruthy();
      expect(document.body.textContent).toContain("运行中");
      expect(document.body.textContent).not.toContain("已取消");
    }
  });

  it("defaults to no cancel capability and follows permission changes in both locales", async () => {
    vi.spyOn(api, "listExecutions").mockResolvedValue({
      items: [summary({ status: "queued" })],
      next_before_id: null,
    });
    vi.spyOn(api, "getExecution").mockResolvedValue(execution({ status: "queued" }));
    const view = render(<ExecutionHistoryPanel adapterId={41} />);
    fireEvent.click(await screen.findByTestId("history-row"));
    expect(screen.queryByTestId("execution-cancel-queued")).toBeNull();

    view.rerender(<ExecutionHistoryPanel adapterId={41} canCancel />);
    expect((await screen.findByTestId("execution-cancel-queued")).textContent).toContain("取消此排队执行");
    await act(async () => {
      await applyUiLocale("en");
    });
    expect(screen.getByTestId("execution-cancel-queued").textContent).toContain(
      "Cancel this queued execution",
    );
    view.rerender(<ExecutionHistoryPanel adapterId={41} canCancel={false} />);
    expect(screen.queryByTestId("execution-cancel-queued")).toBeNull();
  });
});

describe("Issue #127 D2 execution history", () => {
  it.each([
    [
      "legal JSON null",
      execution({ output: null, output_size: 4, attempt_count: 1 }),
      summary(),
      "output-content",
      "null",
    ],
    [
      "never-started empty output",
      execution({
        status: "cancelled",
        output: null,
        output_size: null,
        attempt_count: 0,
        started_at: null,
      }),
      summary({ status: "cancelled", started_at: null }),
      "output-empty",
      "无 Output",
    ],
    [
      "incomplete historical output",
      execution({ output: null, output_size: null, attempt_count: 1 }),
      summary(),
      "output-unknown",
      "输出信息不足，无法确认",
    ],
  ] as const)("uses the shared output classification for %s", async (_, detail, row, testId, text) => {
    renderHistory(detail, row);
    fireEvent.click(await screen.findByTestId("history-row"));
    const drawer = document.querySelector(".ant-drawer-content");
    if (!(drawer instanceof HTMLElement)) {
      throw new Error("Execution detail drawer not found");
    }

    fireEvent.click(await within(drawer).findByRole("tab", { name: "输出" }));
    expect((await within(drawer).findByTestId(testId)).textContent).toContain(text);
  });

  it("renders RabbitMQ Attempt facts, infrastructure Incidents, and Replay", async () => {
    const rabbitExecution = execution({
      dispatch_backend: "rabbitmq",
      status: "dead_letter",
      replay_available: false,
      replay_unavailable_reason: null,
    });
    const runtimeDetail: ReliableExecutionDetail = {
      execution_id: rabbitExecution.id,
      dispatch_backend: "rabbitmq",
      status: "dead_letter",
      attempts: [{
        id: 801,
        execution_id: rabbitExecution.id,
        adapter_id: rabbitExecution.adapter_id,
        attempt_no: 1,
        worker_id: 3,
        fencing_token: 1,
        lease_expires_at: "2026-08-28T00:01:00Z",
        status: "failed",
        claimed_at: "2026-08-28T00:00:01Z",
        started_at: "2026-08-28T00:00:02Z",
        ended_at: "2026-08-28T00:00:03Z",
        error_code: "adapter_failed",
        resource_usage_json: null,
        output_summary: null,
        cleanup_summary: null,
      }],
      incidents: [incident()],
      replay_available: true,
      replay_reason: null,
    };
    const detailApi = vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(runtimeDetail);
    const replayApi = vi.spyOn(api, "replayExecution").mockResolvedValue({
      execution_id: 72,
      replay_of_execution_id: rabbitExecution.id,
    });
    renderHistory(rabbitExecution);
    fireEvent.click(await screen.findByTestId("history-row"));

    expect(await screen.findByTestId("execution-attempt-timeline")).toBeTruthy();
    expect(screen.getByTestId("execution-attempt-1").textContent).toContain("Attempt #1");
    expect(screen.getByTestId("execution-attempt-1").textContent).toContain("adapter_failed");
    expect(screen.getByTestId("execution-incidents").textContent).toContain("dispatch_infrastructure_error");
    expect(detailApi).toHaveBeenCalledWith(rabbitExecution.id);

    fireEvent.click(screen.getByTestId("execution-replay"));
    expect(await screen.findByTestId("execution-replay-success")).toBeTruthy();
    expect(screen.getByTestId("execution-replay-success").textContent).toContain("72");
    expect(replayApi).toHaveBeenCalledWith(rabbitExecution.id);
  });

  it("renders none as a read-only summary and keeps the history request lightweight", async () => {
    const list = renderHistory(execution({ input: { storage_key: "must-not-render" } }));
    const row = await screen.findByTestId("history-row");
    fireEvent.click(row);

    expect(await screen.findByTestId("detail-input-none")).toBeTruthy();
    expect(screen.getByTestId("detail-input").textContent).toContain("无输入");
    expect(screen.getByTestId("detail-input").textContent).not.toContain("storage_key");
    expect(api.getReliableExecutionDetail).toHaveBeenCalledWith(71);
    expect(screen.queryByText("派发后端")).toBeNull();
    expect(screen.queryByText("Legacy")).toBeNull();
    expect(list).toHaveBeenCalledWith(41, { limit: 50 });
    expect(list.mock.calls[0]?.[1]).not.toHaveProperty("input");
  });

  it("renders the JSON value read-only without adding run or persistence actions", async () => {
    renderHistory(execution({
      input: { region: "cn-east" },
      input_source_type: "json",
      input_snapshot: { source_type: "json", revision: 9 },
    }));
    fireEvent.click(await screen.findByTestId("history-row"));

    const detailInput = await screen.findByTestId("detail-input-json");
    expect(detailInput.textContent).toContain('"region": "cn-east"');
    expect(screen.getByTestId("detail-input-revision").textContent).toContain("9");
    expect(screen.queryByRole("button", { name: /再次运行|rerun/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /复用|reuse/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /恢复配置|restore configuration/i })).toBeNull();
  });

  it("renders only managed-file snapshot facts, keeps log download, and has no private-file reuse affordance", async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    if (typeof URL.createObjectURL !== "function") {
      URL.createObjectURL = () => "blob:execution-log";
    }
    if (typeof URL.revokeObjectURL !== "function") {
      URL.revokeObjectURL = () => undefined;
    }
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:execution-log");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const clickedAnchors: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function recordDownload(
      this: HTMLAnchorElement,
    ) {
      clickedAnchors.push(this);
    });
    renderHistory(execution({
      // Deliberately unsafe legacy input is ignored for managed_files.
      input: {
        artifact_id: 9001,
        storage_key: "private-storage-key",
        path: "/private/control/path",
        token: "private-token",
      },
      input_source_type: "managed_files",
      input_snapshot: {
        source_type: "managed_files",
        revision: 12,
        artifacts: [{
          ordinal: 0,
          original_filename: "审计.csv",
          content_type: "text/csv",
          size_bytes: 128,
          sha256: "b".repeat(64),
        }],
      },
    }));
    fireEvent.click(await screen.findByTestId("history-row"));

    const drawer = document.querySelector(".ant-drawer-content");
    if (!(drawer instanceof HTMLElement)) {
      throw new Error("Execution detail drawer not found");
    }
    const file = await within(drawer).findByTestId("detail-input-file");
    expect(file.textContent).toContain("审计.csv");
    expect(file.textContent).toContain("text/csv");
    expect(file.textContent).toContain("128");
    expect(file.textContent).toContain("b".repeat(64));
    expect(within(drawer).getByText("托管文件快照（只读）")).toBeTruthy();
    expect(drawer.textContent).not.toContain("9001");
    expect(drawer.textContent).not.toContain("private-storage-key");
    expect(drawer.textContent).not.toContain("/private/control/path");
    expect(drawer.textContent).not.toContain("private-token");
    fireEvent.click(within(drawer).getByRole("tab", { name: "执行日志" }));
    fireEvent.click(within(drawer).getByTestId("detail-log-download"));
    expect(clickedAnchors).toHaveLength(1);
    expect(clickedAnchors[0]?.download).toBe("execution-71.log");
    expect(within(drawer).queryByRole("button", { name: /再次运行|rerun|复用|reuse/i })).toBeNull();
    if (originalCreateObjectURL === undefined) {
      delete (URL as { createObjectURL?: typeof URL.createObjectURL }).createObjectURL;
    }
    if (originalRevokeObjectURL === undefined) {
      delete (URL as { revokeObjectURL?: typeof URL.revokeObjectURL }).revokeObjectURL;
    }
  });

  it("keeps the managed-file snapshot labels bilingual", async () => {
    await applyUiLocale("en");
    renderHistory(execution({
      input_source_type: "managed_files",
      input_snapshot: {
        source_type: "managed_files",
        revision: 3,
        artifacts: [],
      },
    }));
    fireEvent.click(await screen.findByTestId("history-row"));
    await waitFor(() => expect(screen.getByText("Managed files snapshot (read-only)")).toBeTruthy());
    expect(screen.getByText("No file facts were retained in this snapshot.")).toBeTruthy();
  });

  it("shows terminal verification, counts, eligibility reasons, and a result receipt", async () => {
    const terminalIncident = incident({
      recover_available: false,
      recover_reason: "execution_terminal",
      terminate_available: true,
    });
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail(
      [terminalIncident],
      { status: "cancelled" },
    ));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockResolvedValue(
      dispositionResponse({
        receipt: {
          ...dispositionResponse().receipt,
          action: "terminate",
          reason_code: "verified_terminal",
          outcome: "execution_terminal",
          code: "execution_terminal",
          from_generation: 2,
          to_generation: null,
        },
      }),
    );
    renderHistory(execution({ status: "cancelled" }));
    fireEvent.click(await screen.findByTestId("history-row"));

    const incidentAlert = await screen.findByTestId("execution-incident-901");
    expect(incidentAlert.textContent).toContain("观测 3 次");
    expect(incidentAlert.textContent).toContain("人工处置 0 次");
    expect(incidentAlert.textContent).toContain("执行已是终态");
    const verifyButton = within(incidentAlert).getByRole("button", { name: "核实并关闭事件" });
    verifyButton.focus();
    expect(document.activeElement).toBe(verifyButton);
    fireEvent.click(verifyButton);
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));
    expect(dispose.mock.calls[0]?.[2]).toEqual({
      action: "terminate",
      expected_generation: 2,
      reason_code: "verified_terminal",
    });
    expect((await screen.findByTestId("incident-result-901")).textContent).toContain("已核实终态并关闭事件");
  });

  it("reuses the same idempotency key when an explicit action is retried before refresh", async () => {
    const mutableIncident = incident();
    const mutableExecution = execution({ status: "queued", dispatch_generation: 2 });
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([mutableIncident]));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident")
      .mockRejectedValueOnce(new ApiError(503, "outbox_backlog_full", "Outbox capacity is full"))
      .mockResolvedValueOnce(dispositionResponse());
    renderHistory(mutableExecution);
    fireEvent.click(await screen.findByTestId("history-row"));
    const recoverButton = await screen.findByRole("button", { name: "恢复此执行" });

    fireEvent.click(recoverButton);
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    expect(await screen.findByTestId("incident-disposition-error")).toBeTruthy();
    mutableExecution.dispatch_generation = 9;
    mutableIncident.kind = "rejected";
    fireEvent.click(recoverButton);
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(2));
    expect(dispose.mock.calls[0]?.[3]).toBe(dispose.mock.calls[1]?.[3]);
    expect(dispose.mock.calls[0]?.[3]).toMatch(/^[0-9a-f-]{36}$/i);
    expect(dispose.mock.calls[0]?.[2]).toEqual(dispose.mock.calls[1]?.[2]);
    expect(dispose.mock.calls[1]?.[2]).toEqual({
      action: "recover",
      expected_generation: 2,
      reason_code: "capacity_repaired",
    });
  });

  it("refreshes the selected execution after a 409 without navigating away", async () => {
    const initial = reliableDetail([incident()]);
    const refreshed = reliableDetail([incident({
      status: "resolved",
      recover_available: false,
      recover_reason: "incident_closed",
      terminate_available: false,
      terminate_reason: "incident_closed",
    })]);
    const detailApi = vi.spyOn(api, "getReliableExecutionDetail")
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(refreshed);
    vi.spyOn(api, "disposeInfrastructureIncident").mockRejectedValue(
      new ApiError(409, "incident_generation_conflict", "Incident generation changed"),
    );
    renderHistory(execution({ status: "queued" }));
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByRole("button", { name: "恢复此执行" }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => expect(detailApi).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("execution-run-id").textContent).toContain("71");
    expect(screen.getByRole("button", { name: "恢复此执行" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByTestId("execution-incident-901").textContent).toContain("Incident 已关闭");
  });

  it("explains active termination as cooperative cancellation in both locales", async () => {
    const activeIncident = incident({
      recover_available: false,
      recover_reason: "incident_execution_active",
      terminate_available: true,
    });
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail(
      [activeIncident],
      { status: "running" },
    ));
    renderHistory(execution({ status: "running" }));
    fireEvent.click(await screen.findByTestId("history-row"));
    expect((await screen.findByRole("button", { name: "请求取消" })).hasAttribute("disabled")).toBe(false);
    expect(screen.getByTestId("incident-cooperative-cancellation").textContent).toContain("协作式取消");

    await applyUiLocale("en");
    expect((await screen.findByRole("button", { name: "Request cancellation" })).hasAttribute("disabled")).toBe(false);
    expect(screen.getByTestId("incident-cooperative-cancellation").textContent).toContain("cooperative cancellation");
  });

  it("keeps both actions disabled when the server reports read-only access", async () => {
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([incident({
      recover_available: false,
      recover_reason: "adapter_read_only",
      terminate_available: false,
      terminate_reason: "adapter_read_only",
    })]));
    renderHistory(execution({ status: "queued" }));
    fireEvent.click(await screen.findByTestId("history-row"));

    const incidentAlert = await screen.findByTestId("execution-incident-901");
    expect(within(incidentAlert).getByRole("button", { name: "恢复此执行" }).hasAttribute("disabled")).toBe(true);
    expect(within(incidentAlert).getByRole("button", { name: "终结此执行" }).hasAttribute("disabled")).toBe(true);
    expect(incidentAlert.textContent).toContain("当前账号只有只读权限");
  });

  it("uses the current Execution generation and closes an older Incident without cancelling the current dispatch", async () => {
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([incident({
      dispatch_generation: 1,
      recover_available: false,
      recover_reason: "incident_stale_generation",
      terminate_available: true,
    })]));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockResolvedValue(dispositionResponse());
    renderHistory(execution({ status: "queued", dispatch_generation: 4 }));
    fireEvent.click(await screen.findByTestId("history-row"));

    fireEvent.click(await screen.findByRole("button", { name: "关闭旧代事件" }));
    expect(await screen.findByText("只忽略并关闭旧代 Incident，不会取消、重派或修改当前派发代次。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));
    expect(dispose.mock.calls[0]?.[2]).toEqual({
      action: "terminate",
      expected_generation: 4,
      reason_code: "operator_cancel",
    });
  });

  it("uses the current Execution generation when a terminal Incident has no message generation", async () => {
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([incident({
      dispatch_generation: null,
      recover_available: false,
      recover_reason: "execution_terminal",
      terminate_available: true,
    })], { status: "cancelled" }));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockResolvedValue(dispositionResponse());
    renderHistory(execution({ status: "cancelled", dispatch_generation: 4 }));
    fireEvent.click(await screen.findByTestId("history-row"));
    fireEvent.click(await screen.findByRole("button", { name: "核实并关闭事件" }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));
    expect(dispose.mock.calls[0]?.[2].expected_generation).toBe(4);
  });

  it("drops every late disposition state update after switching to another Execution", async () => {
    let resolveFirst!: (result: IncidentDispositionResponse) => void;
    const firstDisposition = new Promise<IncidentDispositionResponse>((resolve) => {
      resolveFirst = resolve;
    });
    const firstIncident = incident({
      recover_available: false,
      recover_reason: "execution_terminal",
    });
    vi.spyOn(api, "listExecutions").mockResolvedValue({
      items: [summary(), summary({ id: 72 })],
      next_before_id: null,
    });
    vi.spyOn(api, "getExecution").mockImplementation(async (id) => execution({
      id,
      dispatch_generation: id === 71 ? 2 : 8,
    }));
    const details = vi.spyOn(api, "getReliableExecutionDetail").mockImplementation(async (id) => reliableDetail(
      id === 71
        ? [firstIncident]
        : [incident({
            id: 902,
            execution_id: 72,
            recover_available: false,
            recover_reason: "execution_terminal",
          })],
      { execution_id: id, status: "succeeded" },
    ));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockReturnValue(firstDisposition);
    const view = render(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} />);
    fireEvent.click(await screen.findByRole("button", { name: "核实并关闭事件" }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));

    view.rerender(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={72} />);
    expect(await screen.findByTestId("execution-incident-902")).toBeTruthy();
    const secondButton = screen.getByRole("button", { name: "核实并关闭事件" });
    expect(secondButton.className).not.toContain("ant-btn-loading");
    resolveFirst(dispositionResponse());
    await Promise.resolve();
    await Promise.resolve();

    expect(details).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("execution-run-id").textContent).toContain("72");
    expect(screen.queryByTestId("execution-incident-901")).toBeNull();
    expect(screen.getByTestId("execution-incident-902")).toBeTruthy();
    expect(screen.queryByTestId("incident-result-901")).toBeNull();
    expect(screen.queryByTestId("incident-disposition-error")).toBeNull();
  });

  it.each(["success", "409", "network"])(
    "keeps Execution B's unsubmitted confirmation open after late Execution A %s",
    async (outcome) => {
      const first = deferred<IncidentDispositionResponse>();
      const details = mockTwoExecutionEpochs();
      const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockReturnValue(first.promise);
      const view = render(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} />);
      fireEvent.click(await screen.findByRole("button", { name: "核实并关闭事件" }));
      fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
      await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));

      view.rerender(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={72} />);
      await screen.findByTestId("execution-incident-902");
      fireEvent.click(screen.getByRole("button", { name: "核实并关闭事件" }));
      expect(await screen.findByRole("button", { name: /确\s*认/ })).toBeTruthy();
      await act(async () => {
        if (outcome === "success") {
          first.resolve(dispositionResponse());
        } else if (outcome === "409") {
          first.reject(new ApiError(409, "incident_generation_conflict", "old conflict"));
        } else {
          first.reject(new Error("old network failure"));
        }
        await first.promise.catch(() => undefined);
      });

      expect(details).toHaveBeenCalledTimes(2);
      expect(screen.queryByTestId("incident-disposition-error")).toBeNull();
      expect(screen.getByTestId("execution-incident-902")).toBeTruthy();
      expect(screen.getByRole("button", { name: /确\s*认/ })).toBeTruthy();
    },
  );

  it("keeps Execution B's pending confirmation and loading after late Execution A completion", async () => {
    const first = deferred<IncidentDispositionResponse>();
    const second = deferred<IncidentDispositionResponse>();
    const details = mockTwoExecutionEpochs();
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const view = render(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={71} />);
    fireEvent.click(await screen.findByRole("button", { name: "核实并关闭事件" }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));

    view.rerender(<ExecutionHistoryPanel adapterId={41} autoOpenExecutionId={72} />);
    await screen.findByTestId("execution-incident-902");
    fireEvent.click(screen.getByRole("button", { name: "核实并关闭事件" }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(2));
    await act(async () => {
      first.resolve(dispositionResponse());
      await first.promise;
    });

    expect(details).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /核实并关闭事件/ }).className).toContain("ant-btn-loading");
    expect(screen.getByRole("button", { name: /确\s*认/ })).toBeTruthy();
    await act(async () => {
      second.resolve(dispositionResponse());
      await second.promise;
    });
  });

  it("keeps another Incident's unsubmitted confirmation after a same-Execution request completes", async () => {
    const first = deferred<IncidentDispositionResponse>();
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([
      incident({ id: 901, recover_available: false, recover_reason: "execution_terminal" }),
      incident({ id: 902, recover_available: false, recover_reason: "execution_terminal" }),
    ], { status: "succeeded" }));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident").mockReturnValue(first.promise);
    renderHistory(execution({ dispatch_generation: 2 }));
    fireEvent.click(await screen.findByTestId("history-row"));
    const firstAlert = await screen.findByTestId("execution-incident-901");
    fireEvent.click(within(firstAlert).getByRole("button", {
      name: "核实并关闭事件",
    }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));

    fireEvent.click(within(screen.getByTestId("execution-incident-902")).getByRole("button", {
      name: "核实并关闭事件",
    }));
    expect(await screen.findByRole("button", { name: /确\s*认/ })).toBeTruthy();
    await act(async () => {
      first.resolve(dispositionResponse());
      await first.promise;
    });

    expect(screen.getByRole("button", { name: /确\s*认/ })).toBeTruthy();
    expect(screen.getByTestId("execution-incident-902")).toBeTruthy();
  });

  it("keeps another Incident's pending confirmation and loading after a same-Execution request completes", async () => {
    const first = deferred<IncidentDispositionResponse>();
    const second = deferred<IncidentDispositionResponse>();
    vi.spyOn(api, "getReliableExecutionDetail").mockResolvedValue(reliableDetail([
      incident({ id: 901, recover_available: false, recover_reason: "execution_terminal" }),
      incident({ id: 902, recover_available: false, recover_reason: "execution_terminal" }),
    ], { status: "succeeded" }));
    const dispose = vi.spyOn(api, "disposeInfrastructureIncident")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    renderHistory(execution({ dispatch_generation: 2 }));
    fireEvent.click(await screen.findByTestId("history-row"));
    const firstAlert = await screen.findByTestId("execution-incident-901");
    fireEvent.click(within(firstAlert).getByRole("button", {
      name: "核实并关闭事件",
    }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(1));

    fireEvent.click(within(screen.getByTestId("execution-incident-902")).getByRole("button", {
      name: "核实并关闭事件",
    }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
    await waitFor(() => expect(dispose).toHaveBeenCalledTimes(2));
    await act(async () => {
      first.resolve(dispositionResponse());
      await first.promise;
    });

    const secondButton = within(screen.getByTestId("execution-incident-902")).getByRole("button", {
      name: /核实并关闭事件/,
    });
    expect(secondButton.className).toContain("ant-btn-loading");
    expect(screen.getByRole("button", { name: /确\s*认/ })).toBeTruthy();
    await act(async () => {
      second.resolve(dispositionResponse({
        receipt: {
          ...dispositionResponse().receipt,
          incident_id: 902,
        },
      }));
      await second.promise;
    });
  });
});
