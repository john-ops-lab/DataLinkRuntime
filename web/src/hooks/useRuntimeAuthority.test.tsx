import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api";
import type { Adapter, AdapterSchedule } from "../types";
import { useRuntimeAuthority } from "./useRuntimeAuthority";

function adapter(id: number, overrides: Partial<Adapter> = {}): Adapter {
  return {
    id,
    name: `adapter-${id}`,
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "schedule",
    timeout_seconds: 300,
    runtime_worker_id: 1,
    latest_version_id: 2,
    runtime_locked: false,
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function schedule(id: number, overrides: Partial<AdapterSchedule> = {}): AdapterSchedule {
  return {
    adapter_id: id,
    enabled: false,
    cron: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    input: null,
    next_run_at: null,
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("invalidates an older GET synchronously and keeps the mutation lock until the write follow-up pair is accepted", async () => {
  const oldAdapter = deferred<Adapter>();
  const oldSchedule = deferred<AdapterSchedule>();
  const acceptedAdapters: Adapter[] = [];
  const authoritativeAdapter = adapter(1, { runtime_locked: true });
  const authoritativeSchedule = schedule(1, { enabled: true });
  vi.spyOn(api, "getAdapter")
    .mockImplementationOnce(() => oldAdapter.promise)
    .mockResolvedValue(authoritativeAdapter);
  vi.spyOn(api, "getSchedule")
    .mockImplementationOnce(() => oldSchedule.promise)
    .mockResolvedValue(authoritativeSchedule);

  const { result } = renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted: (value) => acceptedAdapters.push(value),
    onExplicitError: vi.fn(),
    errorMessage: String,
  }));
  await waitFor(() => expect(api.getAdapter).toHaveBeenCalledTimes(1));

  const mutation = result.current.beginRuntimeMutation();
  expect(mutation).not.toBeNull();
  act(() => {
    mutation?.acceptAdapter(authoritativeAdapter);
    mutation?.acceptSchedule(authoritativeSchedule);
    mutation?.finish();
  });
  expect(result.current.synchronizingAfterMutation).toBe(true);

  await act(async () => {
    oldAdapter.resolve(adapter(1));
    oldSchedule.resolve(schedule(1));
    await oldAdapter.promise;
    await oldSchedule.promise;
  });
  await waitFor(() => expect(api.getAdapter).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.synchronizingAfterMutation).toBe(false));

  expect(acceptedAdapters).not.toContainEqual(adapter(1));
  expect(acceptedAdapters.at(-1)).toEqual(authoritativeAdapter);
  expect(result.current.triggerSnapshot).toMatchObject({
    adapterId: 1,
    loaded: true,
    value: authoritativeSchedule,
  });
});

it("rejects late A responses after A to B to A selection epochs", async () => {
  const firstAAdapter = deferred<Adapter>();
  const firstASchedule = deferred<AdapterSchedule>();
  const accepted: string[] = [];
  const getAdapter = vi.spyOn(api, "getAdapter").mockImplementation((id) => {
    if (id === 1 && getAdapter.mock.calls.filter(([value]) => value === 1).length === 1) {
      return firstAAdapter.promise;
    }
    return Promise.resolve(adapter(id, { name: id === 1 ? "new-a" : "b" }));
  });
  const getSchedule = vi.spyOn(api, "getSchedule").mockImplementation((id) => {
    if (id === 1 && getSchedule.mock.calls.filter(([value]) => value === 1).length === 1) {
      return firstASchedule.promise;
    }
    return Promise.resolve(schedule(id, { cron: id === 1 ? "1 * * * *" : "2 * * * *" }));
  });

  const { result, rerender } = renderHook(
    ({ id }: { id: number }) => useRuntimeAuthority({
      adapterId: id,
      adapterType: "task",
      onAdapterAccepted: (value) => accepted.push(value.name),
      onExplicitError: vi.fn(),
      errorMessage: String,
    }),
    { initialProps: { id: 1 } },
  );
  await waitFor(() => expect(api.getAdapter).toHaveBeenCalledTimes(1));
  rerender({ id: 2 });
  await waitFor(() => expect(accepted).toContain("b"));
  rerender({ id: 1 });
  await waitFor(() => expect(accepted).toContain("new-a"));

  await act(async () => {
    firstAAdapter.resolve(adapter(1, { name: "old-a" }));
    firstASchedule.resolve(schedule(1, { cron: "old" }));
    await firstAAdapter.promise;
    await firstASchedule.promise;
  });

  expect(accepted).not.toContain("old-a");
  expect(result.current.triggerSnapshot).toMatchObject({
    adapterId: 1,
    value: { cron: "1 * * * *" },
  });
});

it("polls an idle visible selection, pauses while hidden, and refreshes immediately when visible", async () => {
  vi.useFakeTimers();
  let visibility: DocumentVisibilityState = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  vi.spyOn(api, "getAdapter").mockResolvedValue(adapter(1));
  vi.spyOn(api, "getSchedule").mockResolvedValue(schedule(1));

  renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted: vi.fn(),
    onExplicitError: vi.fn(),
    errorMessage: String,
  }));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(api.getAdapter).toHaveBeenCalledTimes(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000);
  });
  expect(api.getAdapter).toHaveBeenCalledTimes(2);

  visibility = "hidden";
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6_000);
  });
  expect(api.getAdapter).toHaveBeenCalledTimes(2);

  visibility = "visible";
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();
  });
  expect(api.getAdapter).toHaveBeenCalledTimes(3);
});

it("keeps the last complete locked pair on read failure and accepts a later recovery", async () => {
  const lockedAdapter = adapter(1, { runtime_locked: true });
  const lockedSchedule = schedule(1, { enabled: true });
  const onAdapterAccepted = vi.fn();
  const onExplicitError = vi.fn();
  const getAdapter = vi.spyOn(api, "getAdapter").mockResolvedValueOnce(lockedAdapter);
  const getSchedule = vi.spyOn(api, "getSchedule").mockResolvedValueOnce(lockedSchedule);
  const { result } = renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted,
    onExplicitError,
    errorMessage: () => "refresh failed",
  }));
  await waitFor(() => expect(result.current.triggerSnapshot).toMatchObject({ value: lockedSchedule }));

  const failedAdapter = deferred<Adapter>();
  getAdapter.mockImplementationOnce(() => failedAdapter.promise);
  getSchedule.mockRejectedValueOnce(new Error("offline"));
  const failedRefresh = result.current.requestRuntimeRefresh("explicit");
  failedAdapter.resolve(adapter(1));
  await act(async () => failedRefresh);
  expect(result.current.triggerSnapshot).toMatchObject({ value: lockedSchedule });
  expect(onExplicitError).toHaveBeenLastCalledWith("refresh failed");

  const recoveredAdapter = adapter(1, { runtime_locked: false });
  const recoveredSchedule = schedule(1, { enabled: false, cron: "0 * * * *" });
  getAdapter.mockResolvedValueOnce(recoveredAdapter);
  getSchedule.mockResolvedValueOnce(recoveredSchedule);
  await act(async () => result.current.requestRuntimeRefresh("explicit"));
  expect(onAdapterAccepted).toHaveBeenLastCalledWith(recoveredAdapter);
  expect(result.current.triggerSnapshot).toMatchObject({ value: recoveredSchedule });
});

it("coalesces simultaneous refresh requests behind one in-flight Adapter and trigger pair", async () => {
  const nextAdapter = deferred<Adapter>();
  const nextSchedule = deferred<AdapterSchedule>();
  const getAdapter = vi.spyOn(api, "getAdapter").mockResolvedValueOnce(adapter(1));
  const getSchedule = vi.spyOn(api, "getSchedule").mockResolvedValueOnce(schedule(1));
  const { result } = renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted: vi.fn(),
    onExplicitError: vi.fn(),
    errorMessage: String,
  }));
  await waitFor(() => expect(result.current.triggerSnapshot).toMatchObject({ loaded: true }));
  getAdapter.mockImplementationOnce(() => nextAdapter.promise).mockResolvedValue(adapter(1));
  getSchedule.mockImplementationOnce(() => nextSchedule.promise).mockResolvedValue(schedule(1));

  const first = result.current.requestRuntimeRefresh("explicit");
  const second = result.current.requestRuntimeRefresh("focus");
  const third = result.current.requestRuntimeRefresh("timer");
  expect(getAdapter).toHaveBeenCalledTimes(2);
  expect(getSchedule).toHaveBeenCalledTimes(2);

  await act(async () => {
    nextAdapter.resolve(adapter(1));
    nextSchedule.resolve(schedule(1));
    await Promise.all([first, second, third]);
  });
});

it("retains the in-flight pair until both reads settle after one side rejects", async () => {
  const pendingAdapter = deferred<Adapter>();
  const errors = vi.fn();
  const recoveredAdapter = adapter(1, { runtime_locked: true });
  const recoveredSchedule = schedule(1, { enabled: true });
  const getAdapter = vi.spyOn(api, "getAdapter")
    .mockImplementationOnce(() => pendingAdapter.promise)
    .mockResolvedValue(recoveredAdapter);
  vi.spyOn(api, "getSchedule")
    .mockRejectedValueOnce(new Error("trigger failed quickly"))
    .mockResolvedValue(recoveredSchedule);
  const { result } = renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted: vi.fn(),
    onExplicitError: errors,
    errorMessage: String,
  }));

  await waitFor(() => expect(errors).toHaveBeenCalledTimes(1));
  act(() => {
    void result.current.requestRuntimeRefresh("focus");
    void result.current.requestRuntimeRefresh("timer");
  });
  expect(getAdapter).toHaveBeenCalledTimes(1);

  await act(async () => {
    pendingAdapter.resolve(adapter(1));
    await pendingAdapter.promise;
  });
  await waitFor(() => expect(getAdapter).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.triggerSnapshot).toMatchObject({
    value: recoveredSchedule,
  }));
});

it("also retains the pair when Adapter fails before the trigger read settles", async () => {
  const pendingSchedule = deferred<AdapterSchedule>();
  const errors = vi.fn();
  vi.spyOn(api, "getAdapter")
    .mockRejectedValueOnce(new Error("adapter failed quickly"))
    .mockResolvedValue(adapter(1));
  const getSchedule = vi.spyOn(api, "getSchedule")
    .mockImplementationOnce(() => pendingSchedule.promise)
    .mockResolvedValue(schedule(1));
  const { result } = renderHook(() => useRuntimeAuthority({
    adapterId: 1,
    adapterType: "task",
    onAdapterAccepted: vi.fn(),
    onExplicitError: errors,
    errorMessage: String,
  }));

  await waitFor(() => expect(errors).toHaveBeenCalledTimes(1));
  act(() => {
    void result.current.requestRuntimeRefresh("explicit");
    void result.current.requestRuntimeRefresh("focus");
  });
  expect(getSchedule).toHaveBeenCalledTimes(1);

  await act(async () => {
    pendingSchedule.resolve(schedule(1));
    await pendingSchedule.promise;
  });
  await waitFor(() => expect(getSchedule).toHaveBeenCalledTimes(2));
});
