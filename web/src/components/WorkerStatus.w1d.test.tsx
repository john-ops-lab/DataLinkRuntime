import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { applyUiLocale } from "../i18n";
import { isWorkerExecutionReady, workerFleetStatus, workerStatusLevel } from "../system-status";
import { parseWorkerMaintenanceReceipt, parseWorkerMaintenanceSnapshot } from "../worker-maintenance";
import { makeMaintenanceSnapshot, makeObservedWorker } from "../worker-maintenance.fixtures";
import WorkerStatus from "./WorkerStatus";

afterEach(async () => { await applyUiLocale("zh-CN"); });

describe("W1-D limited remote Worker observations", () => {
  it("does not interpret v4 observation as v3 execution authority or an incompatible protocol", () => {
    const worker = makeObservedWorker({ active_attempts: 1, unsettled_attempts: 1 });
    expect(isWorkerExecutionReady(worker)).toBe(false);
    expect(workerStatusLevel(worker)).toBe("normal");
    expect(workerFleetStatus([worker], false, null)).toBe("normal");
    render(<WorkerStatus workers={[worker]} loading={false} error={null} canManageCache />);
    expect(screen.getByText("状态观测正常")).toBeTruthy();
    expect(screen.getByText("远程固定 Python（受限）")).toBeTruthy();
    expect(screen.queryByText("执行接口不兼容")).toBeNull();
    expect(screen.queryByText("消息执行不可用")).toBeNull();
    expect(screen.queryByText("缓存管理")).toBeNull();
    expect(screen.getByText("未观察到历史阻塞")).toBeTruthy();
  });

  it("keeps maintenance and expired observations distinct from protocol failures", () => {
    const worker = makeObservedWorker({ scheduling_state: "draining" });
    expect(workerStatusLevel(worker)).toBe("warning");
    const view = render(<WorkerStatus workers={[worker]} loading={false} error={null} />);
    expect(screen.getByText("正在排空")).toBeTruthy();
    const stale = { ...worker, maintenance: { ...worker.maintenance!, fresh: false } };
    view.rerender(<WorkerStatus workers={[stale]} loading={false} error={null} />);
    expect(workerFleetStatus([stale], false, null)).toBe("checking");
    expect(screen.getByText("状态未知")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("历史记录");
    expect(screen.queryByText("状态观测正常")).toBeNull();
  });

  it.each(["zh-CN", "en"] as const)("preserves original unknown business outcome after activate (%s)", async (locale) => {
    await applyUiLocale(locale);
    const worker = makeObservedWorker({ last_recovery_disposition: {
      operation_id: "33333333-3333-4333-8333-333333333333",
      disposition_id: "44444444-4444-4444-8444-444444444444",
      kind: "platform_terminal", reason: "result_uncommitted_outcome_unknown",
      execution_id: 21, attempt_id: 22, attempt_status: "worker_lost", execution_status: "cancelled",
    } });
    render(<WorkerStatus workers={[worker]} loading={false} error={null} />);
    const text = screen.getByTestId("worker-recovery-disposition").textContent;
    expect(text).toContain(locale === "en" ? "Original business outcome unknown" : "原业务结果未知");
    expect(text).toContain("worker_lost");
    expect(text).toContain("cancelled");
    expect(text).toContain(locale === "en" ? "neither reruns" : "不重跑业务");
    expect(workerStatusLevel(worker)).toBe("normal");
  });

  it("retains the default v3 readiness and cache entry", () => {
    const snapshot = makeMaintenanceSnapshot();
    const worker = {
      id: 3, name: "local-v3", status: "online", last_heartbeat: snapshot.observed_at,
      capabilities: ["python"], protocol_version: 3, rabbitmq_execution_v3: true,
      isolation_preflight_status: "passed", isolation_capabilities: snapshot.isolation_capabilities,
    };
    expect(workerStatusLevel(worker)).toBe("normal");
    render(<WorkerStatus workers={[worker]} loading={false} error={null} canManageCache />);
    expect(screen.getByRole("button", { name: "缓存管理" })).toBeTruthy();
    expect(screen.getByText("可执行")).toBeTruthy();
  });

  it("keeps an old Control read-only and does not allow another actor to confirm a drain", () => {
    const worker = makeObservedWorker();
    const props = { loading: false, error: null, canManageCache: true,
      onRefresh: async () => undefined, onReadMaintenance: async () => makeMaintenanceSnapshot() };
    const view = render(<WorkerStatus {...props} workers={[{ ...worker,
      maintenance: { ...worker.maintenance!, snapshot: null, fresh: false, error: "unsupported" } }]} />);
    expect(screen.getByText("状态未知")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "申请排空" })).toBeNull();
    expect(screen.queryByRole("button", { name: "激活节点" })).toBeNull();
    view.rerender(<WorkerStatus {...props} workers={[makeObservedWorker({ scheduling_state: "draining",
      maintenance_version: 5, current_maintenance_action: "drain", current_drain_operation: {
        action: "drain", request_id: "33333333-3333-4333-8333-333333333333",
        expected_version: 4, result_version: 5, same_actor: false,
      } })]} />);
    expect((screen.getByRole("button", { name: "确认本次排空" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("当前排空由其他管理员发起；本页仅可观察，不能代用其请求。")).toBeTruthy();
  });

  it("rejects missing, unsafe or mixed observation fields without retaining extra data", () => {
    const snapshot = makeMaintenanceSnapshot();
    const parsed = parseWorkerMaintenanceSnapshot({ ...snapshot, host_evidence: "must-not-copy" }, 7);
    expect(parsed).toEqual(snapshot);
    expect(parsed).not.toHaveProperty("host_evidence");
    for (const changes of [
      { observation_schema: undefined }, { observation_schema: 2 }, { maintenance_version: true },
      { maintenance_version: Number.MAX_SAFE_INTEGER + 1 }, { observed_at: "1" },
      { current_drain_operation: undefined }, { unsettled_attempts: undefined },
      { worker_id: 8 }, { instance_generation: 0 }, { isolation_capabilities: {} },
      { worker_identity_id: "00000000-0000-0000-0000-000000000000" },
    ]) expect(parseWorkerMaintenanceSnapshot({ ...snapshot, ...changes }, 7)).toBeNull();
    // Retirement clears the live instance, preserving the generation counter.
    const retired = { ...snapshot, instance_id: null, instance_lease_until: null, instance_generation: 2, online: false };
    expect(parseWorkerMaintenanceSnapshot(retired, 7)).toEqual(retired);
  });

  it("separates historical operation commit from current scheduling state", () => {
    const requestId = "33333333-3333-4333-8333-333333333333";
    const receipt = {
      operation: { request_id: requestId, worker_id: 7, action: "activate", expected_version: 1,
        result_version: 2, same_actor: true, created_at: new Date().toISOString() },
      current: { worker_identity_id: makeMaintenanceSnapshot().worker_identity_id,
        maintenance_version: 3, scheduling_state: "draining", observed_at: new Date().toISOString() },
      applies_to_current_version: false,
    };
    expect(parseWorkerMaintenanceReceipt(receipt, 7, requestId)).toEqual(receipt);
    expect(parseWorkerMaintenanceReceipt({ ...receipt, applies_to_current_version: true }, 7, requestId)).toBeNull();
    expect(parseWorkerMaintenanceReceipt(receipt, 9, requestId)).toBeNull();
  });
});
