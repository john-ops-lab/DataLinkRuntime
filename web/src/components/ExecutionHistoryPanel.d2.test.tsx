import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { applyUiLocale } from "../i18n";
import type { Execution, ExecutionSummary, ReliableExecutionDetail } from "../types";
import ExecutionHistoryPanel from "./ExecutionHistoryPanel";

function summary(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
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
    id: 71,
    adapter_id: 41,
    version_id: 11,
    worker_id: 3,
    target_worker_id: 3,
    trigger: "manual",
    scheduled_for: null,
    status: "succeeded",
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

function renderHistory(detail: Execution, row = summary()) {
  const list = vi.spyOn(api, "listExecutions").mockResolvedValue({
    items: [row],
    next_before_id: null,
  });
  vi.spyOn(api, "getExecution").mockResolvedValue(detail);
  render(<ExecutionHistoryPanel adapterId={41} />);
  return list;
}

afterEach(async () => {
  await applyUiLocale("zh-CN");
  vi.restoreAllMocks();
});

describe("Issue #127 D2 execution history", () => {
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
      incidents: [{
        id: 901,
        kind: "dispatch_infrastructure_error",
        status: "open",
        attempts: 2,
        last_error: "dispatch_infrastructure_error",
        created_at: "2026-08-28T00:00:04Z",
        resolved_at: null,
      }],
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
});
