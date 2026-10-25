import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import type { AdapterBatchItem, AdapterBatchOperation } from "../adapter-batch-types";
import { adapterBatchStorageKey, useAdapterBatchOperation } from "./useAdapterBatchOperation";

const id = "cc000000-0000-4000-8000-000000000001";
const targets = [{ adapter_id: 1, expected_token: "saved-v1" }];
function item(overrides: Partial<AdapterBatchItem> = {}): AdapterBatchItem {
  return { adapter_id: 1, adapter_name: "orders", mode: "manual", action: "run_once", status: "pending",
    error_code: null, retryable: false, execution_ids: [], executions: [], subscription_id: null, subscription_state: null, ...overrides };
}
function receipt(items: AdapterBatchItem[] = [item()]): AdapterBatchOperation {
  return { id, action: "start", created_at: "2026-10-25T00:00:00Z", items };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
beforeEach(() => { localStorage.clear(); vi.spyOn(crypto, "randomUUID").mockReturnValue(id); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); localStorage.clear(); });

it("batch recovery: a lookup during preparation cannot leave a successful plan missing", async () => {
  const preparation = deferred<AdapterBatchOperation>();
  vi.spyOn(api, "prepareAdapterBatch").mockReturnValue(preparation.promise);
  const get = vi.spyOn(api, "getAdapterBatch").mockRejectedValue(new ApiError(404, "batch_not_found", "not committed yet"));
  const execute = vi.spyOn(api, "executeAdapterBatch").mockResolvedValue(receipt([item({ status: "accepted" })]));
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  let preparing!: Promise<void>;
  act(() => { preparing = result.current.prepare("start", targets); });
  await act(async () => { await result.current.query(); });
  await act(async () => { preparation.resolve(receipt()); await preparing; });
  expect(result.current.operation?.id).toBe(id);
  expect(result.current.missing).toBe(false);
  expect(get).not.toHaveBeenCalled();
  await act(async () => { await result.current.execute(); });
  expect(execute).toHaveBeenCalledTimes(1);
});

it("batch recovery: progress queries remain available while execution is in flight", async () => {
  const pending = receipt([item(), item({ adapter_id: 2 })]);
  const progress = receipt([item({ status: "accepted", execution_ids: [81] }), item({ adapter_id: 2 })]);
  const completed = receipt([item({ status: "accepted", execution_ids: [81] }), item({ adapter_id: 2, status: "accepted", execution_ids: [82] })]);
  const execution = deferred<AdapterBatchOperation>();
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(pending);
  vi.spyOn(api, "executeAdapterBatch").mockReturnValue(execution.promise);
  const get = vi.spyOn(api, "getAdapterBatch").mockResolvedValue(progress);
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  let executing!: Promise<void>;
  act(() => { executing = result.current.execute(); });
  await act(async () => { await result.current.query(); });
  expect(get).toHaveBeenCalledTimes(1);
  expect(result.current.working).toBe("execute");
  expect(result.current.operation).toEqual(progress);
  await act(async () => { execution.resolve(completed); await executing; });
  expect(result.current.operation).toEqual(completed);
});

it("batch recovery: a stale progress response cannot clear an unknown execution result", async () => {
  const execution = deferred<AdapterBatchOperation>();
  const progress = deferred<AdapterBatchOperation>();
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(receipt());
  const execute = vi.spyOn(api, "executeAdapterBatch").mockReturnValue(execution.promise);
  const get = vi.spyOn(api, "getAdapterBatch").mockReturnValueOnce(progress.promise);
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  let executing!: Promise<void>;
  let querying!: Promise<void>;
  act(() => { executing = result.current.execute(); });
  act(() => { querying = result.current.query(); });
  await act(async () => { execution.reject(new ApiError(0, "network_error", "lost response")); await executing; });
  expect(result.current.unknown).toBe(true);
  await act(async () => { progress.resolve(receipt()); await querying; });
  expect(result.current.unknown).toBe(true);
  await act(async () => { await result.current.execute(); });
  expect(execute).toHaveBeenCalledTimes(1);
  const recovered = receipt([item({ status: "accepted", execution_ids: [91] })]);
  get.mockResolvedValue(recovered);
  await act(async () => { await result.current.query(); });
  expect(result.current.unknown).toBe(false);
  expect(result.current.operation).toEqual(recovered);
});

it("prepares a frozen plan once on double-click and requires explicit execution", async () => {
  const pending = deferred<AdapterBatchOperation>();
  const prepare = vi.spyOn(api, "prepareAdapterBatch").mockReturnValue(pending.promise);
  const execute = vi.spyOn(api, "executeAdapterBatch").mockResolvedValue(receipt([item({ status: "accepted", execution_ids: [41] })]));
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  const mutableTargets = targets.map(target => ({ ...target }));
  act(() => { void result.current.prepare("start", mutableTargets); void result.current.prepare("start", mutableTargets); });
  mutableTargets[0].expected_token = "different";
  expect(prepare).toHaveBeenCalledTimes(1);
  expect(prepare).toHaveBeenCalledWith({ id, action: "start", targets });
  expect(execute).not.toHaveBeenCalled();
  expect(localStorage.getItem(adapterBatchStorageKey("token:deployment"))).toBe(id);
  await act(async () => { pending.resolve(receipt()); await pending.promise; });
  await act(async () => { await Promise.all([result.current.execute(), result.current.execute()]); });
  expect(execute).toHaveBeenCalledTimes(1);
  expect(result.current.operation?.items[0].execution_ids).toEqual([41]);
});

it("requeries a lost execution response before allowing actions and never duplicates accepted items", async () => {
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(receipt());
  const execute = vi.spyOn(api, "executeAdapterBatch").mockRejectedValue(new ApiError(0, "network_error", "offline"));
  const completed = receipt([item({ status: "accepted", execution_ids: [49], executions: [{ id: 49, status: "queued", cancel_requested: false }] })]);
  const get = vi.spyOn(api, "getAdapterBatch").mockResolvedValue(completed);
  const retry = vi.spyOn(api, "retryAdapterBatch");
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  await act(async () => { await result.current.execute(); });
  expect(result.current.unknown).toBe(true);
  await act(async () => { await result.current.execute(); await result.current.retry(); await result.current.prepare("start", targets); });
  expect(execute).toHaveBeenCalledTimes(1);
  expect(retry).not.toHaveBeenCalled();
  expect(api.prepareAdapterBatch).toHaveBeenCalledTimes(1);
  await act(async () => { await result.current.query(); });
  expect(get).toHaveBeenCalledWith(id);
  expect(result.current.unknown).toBe(false);
  await act(async () => { await result.current.execute(); });
  expect(execute).toHaveBeenCalledTimes(1);
  expect(result.current.operation).toEqual(completed);
});

it("retries only definite eligible failures and keeps successful receipt identities", async () => {
  const mixed = receipt([
    item({ status: "accepted", execution_ids: [51] }),
    item({ adapter_id: 2, status: "failed", retryable: true, error_code: "adapter_runtime_locked" }),
    item({ adapter_id: 3, status: "failed", error_code: "batch_config_conflict" }),
    item({ adapter_id: 4, status: "unknown", retryable: true }),
  ]);
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(mixed);
  const retry = vi.spyOn(api, "retryAdapterBatch").mockResolvedValue(mixed);
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  await act(async () => { await result.current.retry(); });
  expect(retry).toHaveBeenCalledWith(id, [2]);
  expect(result.current.operation?.items[0].execution_ids).toEqual([51]);
});

it("restores only the scoped UUID after reload and GET never executes a pending plan", async () => {
  localStorage.setItem(adapterBatchStorageKey("account:7"), id);
  const get = vi.spyOn(api, "getAdapterBatch").mockResolvedValue(receipt());
  const execute = vi.spyOn(api, "executeAdapterBatch");
  const prepare = vi.spyOn(api, "prepareAdapterBatch");
  const { result, rerender } = renderHook(({ scope }) => useAdapterBatchOperation({ scope, onChanged: vi.fn(async () => {}) }), {
    initialProps: { scope: "account:7" },
  });
  await waitFor(() => expect(result.current.operation?.id).toBe(id));
  expect(get).toHaveBeenCalledTimes(1);
  expect(execute).not.toHaveBeenCalled();
  expect(prepare).not.toHaveBeenCalled();
  rerender({ scope: "account:8" });
  expect(result.current.operation).toBeNull();
  expect(result.current.operationId).toBeNull();
  expect(get).toHaveBeenCalledTimes(1);
});

it("discards late receipts when the account changes", async () => {
  const old = deferred<AdapterBatchOperation>();
  vi.spyOn(api, "prepareAdapterBatch").mockReturnValue(old.promise);
  const { result, rerender } = renderHook(({ scope }) => useAdapterBatchOperation({ scope, onChanged: vi.fn(async () => {}) }), {
    initialProps: { scope: "account:7" },
  });
  act(() => { void result.current.prepare("start", targets); });
  rerender({ scope: "account:8" });
  await act(async () => { old.resolve(receipt()); await old.promise; });
  expect(result.current.operation).toBeNull();
  expect(result.current.operationId).toBeNull();
  expect(result.current.open).toBe(false);
});

it("restores an uncreated preparation with the same UUID and snapshot only after querying", async () => {
  const prepare = vi.spyOn(api, "prepareAdapterBatch").mockRejectedValueOnce(new ApiError(0, "network_error", "offline"))
    .mockResolvedValue(receipt());
  vi.spyOn(api, "getAdapterBatch").mockRejectedValue(new ApiError(404, "batch_not_found", "missing"));
  const execute = vi.spyOn(api, "executeAdapterBatch");
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  expect(result.current.recoverPreparation).toBeNull();
  await act(async () => { await result.current.query(); });
  expect(result.current.missing).toBe(true);
  await act(async () => { await result.current.recoverPreparation?.(); });
  expect(prepare).toHaveBeenCalledTimes(2);
  expect(prepare.mock.calls[1][0]).toEqual(prepare.mock.calls[0][0]);
  expect(execute).not.toHaveBeenCalled();
});

it("treats a missing restored ID as an explicit new-operation choice, and keeps storage failures nonfatal", async () => {
  localStorage.setItem(adapterBatchStorageKey("token:deployment"), id);
  vi.spyOn(api, "getAdapterBatch").mockRejectedValue(new ApiError(404, "batch_not_found", "missing"));
  const prepare = vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(receipt());
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await waitFor(() => expect(result.current.missing).toBe(true));
  expect(result.current.recoverPreparation).toBeNull();
  expect(prepare).not.toHaveBeenCalled();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("unavailable"); });
  await act(async () => { await result.current.prepare("start", targets); });
  expect(result.current.operation?.id).toBe(id);
});

it("does not reclassify successful actions when the subsequent catalog refresh fails", async () => {
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(receipt());
  vi.spyOn(api, "executeAdapterBatch").mockResolvedValue(receipt([item({ status: "accepted", execution_ids: [71] })]));
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => { throw new Error("refresh failed"); }) }));
  await act(async () => { await result.current.prepare("start", targets); });
  await act(async () => { await result.current.execute(); });
  expect(result.current.unknown).toBe(false);
  expect(result.current.operation?.items[0].status).toBe("accepted");
});

it("rejects empty, duplicated, over-limit and missing-token targets before preparation", async () => {
  const prepare = vi.spyOn(api, "prepareAdapterBatch");
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => {
    await result.current.prepare("start", []);
    await result.current.prepare("start", [...targets, ...targets]);
    await result.current.prepare("start", [{ adapter_id: 1, expected_token: "" }]);
    await result.current.prepare("start", Array.from({ length: 101 }, (_, i) => ({ adapter_id: i + 1, expected_token: "v1" })));
  });
  expect(prepare).not.toHaveBeenCalled();
});

it("polls accepted source activation until the listener has actually started", async () => {
  vi.useFakeTimers();
  vi.spyOn(api, "prepareAdapterBatch").mockResolvedValue(receipt([item({ action: "enable_subscription", mode: "subscription" })]));
  vi.spyOn(api, "executeAdapterBatch").mockResolvedValue(receipt([item({ action: "enable_subscription", mode: "subscription", status: "accepted", subscription_state: "starting" })]));
  const get = vi.spyOn(api, "getAdapterBatch").mockResolvedValue(receipt([item({ action: "enable_subscription", mode: "subscription", status: "succeeded", subscription_state: "listening" })]));
  const { result } = renderHook(() => useAdapterBatchOperation({ scope: "token:deployment", onChanged: vi.fn(async () => {}) }));
  await act(async () => { await result.current.prepare("start", targets); });
  await act(async () => { await result.current.execute(); });
  expect(result.current.operation?.items[0].status).toBe("accepted");
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(get).toHaveBeenCalledTimes(1);
  expect(result.current.operation?.items[0].status).toBe("succeeded");
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(get).toHaveBeenCalledTimes(1);
});
