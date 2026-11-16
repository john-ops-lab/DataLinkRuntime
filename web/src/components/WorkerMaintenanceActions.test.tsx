import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api, ApiError } from "../api";
import { applyUiLocale } from "../i18n";
import type { WorkerMaintenanceReceipt } from "../types";
import {
  persistWorkerMaintenanceIntent, WORKER_MAINTENANCE_INTENT_KEY, type WorkerMaintenanceIntent,
} from "../worker-maintenance-intent";
import { makeMaintenanceSnapshot, makeObservedWorker } from "../worker-maintenance.fixtures";
import WorkerMaintenanceActions from "./WorkerMaintenanceActions";

const requestId = "33333333-3333-4333-8333-333333333333";
const base = makeMaintenanceSnapshot();
const intent: WorkerMaintenanceIntent = {
  schema: 1, origin: window.location.origin, scope: "token:deployment", worker_id: 7,
  worker_identity_id: base.worker_identity_id, action: "activate", request_id: requestId, expected_version: 4,
};
function receipt(action: "drain" | "activate", id: string, state: "draining" | "disabled" | "active", version = 5): WorkerMaintenanceReceipt {
  return {
    operation: { request_id: id, worker_id: 7, action, expected_version: 4, result_version: 5,
      same_actor: true, created_at: new Date().toISOString() },
    current: { worker_identity_id: base.worker_identity_id, maintenance_version: version,
      scheduling_state: state, observed_at: new Date().toISOString() },
    applies_to_current_version: version === 5,
  };
}
async function confirm(label: string) {
  fireEvent.click(await screen.findByRole("button", { name: label }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
}

beforeEach(async () => {
  sessionStorage.clear();
  await applyUiLocale("zh-CN");
});
afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); });

it("keeps drain pending until explicit same-tuple confirmation reads disabled", async () => {
  let current = makeMaintenanceSnapshot();
  const read = vi.fn(async () => current);
  const post = vi.spyOn(api, "maintainWorker").mockImplementation(async (_id, action, tuple) => {
    expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toContain(tuple.request_id);
    expect(action).toBe("drain");
    if (post.mock.calls.length === 1) current = makeMaintenanceSnapshot({
      scheduling_state: "draining", maintenance_version: 5, current_maintenance_action: "drain",
      current_drain_operation: { action: "drain", request_id: tuple.request_id,
        expected_version: 4, result_version: 5, same_actor: true },
    });
    else current = { ...current, scheduling_state: "disabled" };
    return {};
  });
  vi.spyOn(api, "getWorkerMaintenanceOperation").mockImplementation(async (_id, id) => receipt("drain", id, current.scheduling_state));
  const onRefresh = vi.fn(async () => undefined);
  const view = render(<WorkerMaintenanceActions worker={makeObservedWorker()} onRead={read} onRefresh={onRefresh} />);
  await confirm("申请排空");
  await screen.findByText("排空意图已提交；当前仍在排空，未确认停用。");
  expect(post).toHaveBeenCalledTimes(1);
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).not.toBeNull();
  view.rerender(<WorkerMaintenanceActions worker={makeObservedWorker(current)} onRead={read} onRefresh={onRefresh} />);
  await confirm("确认本次排空");
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(post).toHaveBeenCalledTimes(2);
  expect(post.mock.calls[1][2]).toEqual(post.mock.calls[0][2]);
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});

it("reads a committed activation after 503 and restores pending intent with GET only", async () => {
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  const read = vi.fn(async () => snapshot);
  const post = vi.spyOn(api, "maintainWorker").mockRejectedValue(new ApiError(503, "unavailable", "Unavailable"));
  const get = vi.spyOn(api, "getWorkerMaintenanceOperation").mockImplementation(async (_id, id) => receipt("activate", id, "active"));
  const props = { worker: makeObservedWorker(snapshot), onRead: read, onRefresh: async () => undefined };
  const view = render(<WorkerMaintenanceActions {...props} />);
  await confirm("激活节点");
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(post).toHaveBeenCalledTimes(1);
  expect(get).toHaveBeenCalledTimes(1);
  view.unmount();
  persistWorkerMaintenanceIntent(intent);
  render(<WorkerMaintenanceActions {...props} />);
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(get).toHaveBeenCalledTimes(2);
  expect(post).toHaveBeenCalledTimes(1);
});

it("keeps missing receipts unknown and only retries the saved tuple on explicit confirmation", async () => {
  persistWorkerMaintenanceIntent(intent);
  const get = vi.spyOn(api, "getWorkerMaintenanceOperation").mockRejectedValue(new ApiError(404, "not_found", "Not found"));
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={async () => snapshot} onRefresh={async () => undefined} />);
  await screen.findByText("提交结果尚未确认。先查询本次回执，不把断线当作失败或回滚。");
  expect(post).not.toHaveBeenCalled();
  get.mockResolvedValue(receipt("activate", requestId, "active"));
  await confirm("重试同一请求");
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(post.mock.calls[0][2]).toEqual({ request_id: requestId, expected_version: 4 });
});

it.each([403, 409])("handles a %s rejection without a new operation", async (status) => {
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  vi.spyOn(api, "maintainWorker").mockRejectedValue(new ApiError(status, "rejected", "Rejected"));
  vi.spyOn(api, "getWorkerMaintenanceOperation").mockRejectedValue(new ApiError(status === 403 ? 403 : 404,
    status === 403 ? "account_admin_required" : "not_found", "Rejected"));
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={async () => snapshot} onRefresh={async () => undefined} />);
  await confirm("激活节点");
  await screen.findByText(status === 403 ? "管理员权限已变化；请重新核对授权。"
    : "当前版本或操作资格冲突；本次写入未被接受。请核对最新状态后明确选择。");
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
  expect(api.maintainWorker).toHaveBeenCalledTimes(1);
});

it("keeps the original unknown intent after a retry 409 and missing receipt, then observes late commit", async () => {
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  const post = vi.spyOn(api, "maintainWorker")
    .mockRejectedValueOnce(new Error("reply timeout"))
    .mockRejectedValueOnce(new ApiError(409, "worker_activation_unavailable", "Temporarily offline"));
  const get = vi.spyOn(api, "getWorkerMaintenanceOperation").mockRejectedValue(new ApiError(404, "not_found", "Not committed yet"));
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={async () => snapshot} onRefresh={async () => undefined} />);
  const unknown = "提交结果尚未确认。先查询本次回执，不把断线当作失败或回滚。";
  await confirm("激活节点");
  await screen.findByText(unknown);
  const saved = sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY);
  const tuple = post.mock.calls[0][2];
  await confirm("重试同一请求");
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  await waitFor(() => expect((screen.getByRole("button", { name: "查询本次回执" }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByText(unknown)).toBeTruthy();
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBe(saved);
  expect((screen.getByRole("button", { name: "激活节点" }) as HTMLButtonElement).disabled).toBe(true);
  expect(post.mock.calls[1][2]).toEqual(tuple);
  // The first dispatch can still commit after the retry's negative snapshot.
  get.mockResolvedValue(receipt("activate", tuple.request_id, "active"));
  fireEvent.click(screen.getByRole("button", { name: "查询本次回执" }));
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(post).toHaveBeenCalledTimes(2);
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});

it("blocks writes when storage fails", async () => {
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  const read = vi.fn(async () => snapshot);
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={read} onRefresh={async () => undefined} />);
  const store = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  await confirm("激活节点");
  await screen.findByText("无法暂存或读取本页请求记录，未发送新的管理操作。请检查浏览器存储后刷新。");
  expect(post).not.toHaveBeenCalled();
  store.mockRestore();
});

it("requires a fresh unchanged version at confirmation, and expired views cannot start actions", async () => {
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled" });
  const read = vi.fn().mockResolvedValueOnce(snapshot).mockResolvedValueOnce({ ...snapshot, maintenance_version: 6 });
  const worker = makeObservedWorker(snapshot);
  const view = render(<WorkerMaintenanceActions worker={worker} onRead={read} onRefresh={async () => undefined} />);
  await confirm("激活节点");
  await screen.findByText("节点身份、代际或维护状态已变化，本次未发送。请核对后重新选择。");
  expect(post).not.toHaveBeenCalled();
  view.rerender(<WorkerMaintenanceActions worker={{ ...worker, maintenance: { ...worker.maintenance!, fresh: false } }}
    onRead={read} onRefresh={async () => undefined} />);
  await waitFor(() => expect((screen.getByRole("button", { name: "激活节点" }) as HTMLButtonElement).disabled).toBe(true));
});

it("never represents a superseded historical activation as current success", async () => {
  persistWorkerMaintenanceIntent(intent);
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  vi.spyOn(api, "getWorkerMaintenanceOperation").mockResolvedValue(receipt("activate", requestId, "draining", 6));
  render(<WorkerMaintenanceActions worker={makeObservedWorker()} onRead={async () => base} onRefresh={async () => undefined} />);
  await screen.findByText("原操作已提交，但当前已被后续维护替代。");
  expect(post).not.toHaveBeenCalled();
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});

it("replaces the previous success notice immediately while a new POST is pending", async () => {
  persistWorkerMaintenanceIntent(intent);
  const get = vi.spyOn(api, "getWorkerMaintenanceOperation").mockResolvedValue(receipt("activate", requestId, "active"));
  let finish!: () => void;
  const post = vi.spyOn(api, "maintainWorker").mockImplementation(() => new Promise((resolve) => {
    finish = () => resolve({});
  }));
  const snapshot = makeMaintenanceSnapshot({ maintenance_version: 5 });
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={async () => snapshot} onRefresh={async () => undefined} />);
  await screen.findByText("本次操作已在独立查询中确认提交。");
  await confirm("申请排空");
  await screen.findByText("提交结果尚未确认。先查询本次回执，不把断线当作失败或回滚。");
  expect(screen.queryByText("本次操作已在独立查询中确认提交。")).toBeNull();
  const id = post.mock.calls[0][2].request_id;
  const done = receipt("drain", id, "disabled", 6);
  get.mockResolvedValue({ ...done, operation: { ...done.operation, expected_version: 5, result_version: 6 }, applies_to_current_version: true });
  finish();
  await screen.findByText("本次操作已在独立查询中确认提交。");
});


it.each([["python"], ["python", "javascript"]].map((capabilities) => ({ capabilities })))("allows explicit activation for the closed set %s", async ({ capabilities }) => {
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled", capabilities });
  const read = vi.fn(async () => snapshot);
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  vi.spyOn(api, "getWorkerMaintenanceOperation").mockImplementation(async (_id, id) => receipt("activate", id, "active"));
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={read} onRefresh={async () => undefined} />);
  await confirm("激活节点");
  await screen.findByText("本次操作已在独立查询中确认提交。");
  expect(post).toHaveBeenCalledTimes(1);
});

it.each([["javascript"], ["javascript", "python"], ["python", "python"], ["python", "go"]].map((capabilities) => ({ capabilities })))("blocks activation for unknown language sets %s", async ({ capabilities }) => {
  const snapshot = makeMaintenanceSnapshot({ scheduling_state: "disabled", capabilities });
  const post = vi.spyOn(api, "maintainWorker").mockResolvedValue({});
  render(<WorkerMaintenanceActions worker={makeObservedWorker(snapshot)} onRead={async () => snapshot} onRefresh={async () => undefined} />);
  expect((screen.getByRole("button", { name: "激活节点" }) as HTMLButtonElement).disabled).toBe(true);
  expect(post).not.toHaveBeenCalled();
});
