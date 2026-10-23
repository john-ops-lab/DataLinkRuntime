import { createRef, useState, type RefObject } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ApiError, api } from "../api";
import { applyUiLocale } from "../i18n";
import type { Adapter, Execution } from "../types";
import { userErrorMessage } from "../user-message";
import TaskRunSettingsPanel, { type TaskRunSettingsHandle, type TaskRuntimeState } from "./TaskRunSettingsPanel";
import TaskWorkbenchHeader from "./TaskWorkbenchHeader";

// The shared runtime/toolbar contract does not depend on rendering the source workspace.
vi.mock("./KafkaSubscriptionPanel", () => ({ default: () => null }));

const adapter = (values: Partial<Adapter> = {}): Adapter => ({
  id: 41,
  name: "subscription-task",
  description: "",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  runtime_worker_id: 3,
  latest_version_id: 11,
  runtime_locked: false,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
  ...values,
});
const readyRuntime: TaskRuntimeState = {
  scheduleEnabled: false,
  loading: false,
  activeExecution: false,
  canRun: true,
  scheduleEnableBlockedReason: null,
  mutationInFlight: false,
};
const refreshRuntime = async () => {};
const headerActions = {
  runtimeWorker: null,
  dirty: false,
  busy: false,
  contentReady: true,
  onSave: vi.fn(),
  onOpenSettings: vi.fn(),
  onStopExecution: vi.fn(),
  onToggleSchedule: vi.fn(),
};

function SharedTask({ value, runtimeRef, onError }: {
  value: Adapter;
  runtimeRef: RefObject<TaskRunSettingsHandle | null>;
  onError: (message: string | null) => void;
}) {
  const [state, setState] = useState(readyRuntime);
  return <>
    <TaskWorkbenchHeader {...headerActions} adapter={value} runtimeState={state}
      onRunOnce={() => runtimeRef.current?.runOnce()} />
    <TaskRunSettingsPanel ref={runtimeRef} adapter={value} workers={[]} workersLoading={false}
      workersError={null} execution={null} dirty={false} onExecutionStarted={vi.fn()}
      onRuntimeStateChange={setState} onError={onError} requestRuntimeRefresh={refreshRuntime} />
  </>;
}

beforeEach(async () => {
  await applyUiLocale("en");
  vi.spyOn(api, "getInputConfig").mockResolvedValue({
    adapter_id: 41, revision: 5, source_type: "json", json_value: { saved: true },
    retention: { mode: "system_default", seconds: null }, artifacts: [],
    valid_for_run: true, invalid_reason: null,
  });
  vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
    managed_files_enabled: false, ready: true, default_retention_seconds: 86400,
    max_custom_retention_seconds: 2592000, allow_manual_delete: false, allowed_extensions: [],
  });
  vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
});
afterEach(async () => {
  vi.restoreAllMocks();
  await applyUiLocale("zh-CN");
});

it.each([
  ["en", "Subscription locked", "Disable the event subscription"],
  ["zh-CN", "订阅锁定", "请先停用事件订阅"],
] as const)("blocks stale toolbar run eligibility and explains the subscription lock in %s", async (locale, label, reason) => {
  await applyUiLocale(locale);
  const onRunOnce = vi.fn();
  render(<TaskWorkbenchHeader {...headerActions} adapter={adapter({ runtime_locked: true })}
    runtimeState={readyRuntime} onRunOnce={onRunOnce} />);
  const run = screen.getByTestId("header-task-run-once") as HTMLButtonElement;
  expect(run.disabled).toBe(true);
  expect(run.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(reason);
  expect(within(screen.getByTestId("workbench-meta")).getByText(label)).toBeTruthy();
  fireEvent.click(run);
  expect(onRunOnce).not.toHaveBeenCalled();
  const message = userErrorMessage(new ApiError(409, "subscription_draining", "ignored"));
  expect(message).toContain(locale === "en" ? "Disable it" : "请先停用订阅");
});

it("keeps the actual shared toolbar and imperative run blocked until the subscription lock is cleared", async () => {
  const create = vi.spyOn(api, "createExecution").mockResolvedValue({ id: 91 } as Execution);
  const runtimeRef = createRef<TaskRunSettingsHandle>();
  const onError = vi.fn();
  const view = render(<SharedTask value={adapter()} runtimeRef={runtimeRef} onError={onError} />);
  await waitFor(() => expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false));

  // The backend retains this same lock for enabled reception and disabled backlog/repair.
  view.rerender(<SharedTask value={adapter({ runtime_locked: true })} runtimeRef={runtimeRef} onError={onError} />);
  const run = screen.getByTestId("header-task-run-once") as HTMLButtonElement;
  expect(run.disabled).toBe(true);
  expect(screen.getByTestId("workbench-meta").textContent).toContain("Subscription locked");
  fireEvent.click(run);
  act(() => runtimeRef.current?.runOnce());
  expect(onError).toHaveBeenLastCalledWith(expect.stringContaining("Disable the event subscription"));
  expect(create).not.toHaveBeenCalled();

  // A disabled, drained subscription releases runtime_locked; ordinary manual runs use Saved Input.
  view.rerender(<SharedTask value={adapter()} runtimeRef={runtimeRef} onError={onError} />);
  await waitFor(() => expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByTestId("header-task-run-once"));
  await waitFor(() => expect(create).toHaveBeenCalledExactlyOnceWith(41));
});

it("preserves the existing run-once action for an enabled schedule", async () => {
  vi.spyOn(api, "getSchedule").mockResolvedValue({
    adapter_id: 41, enabled: true, cron: "*/5 * * * *", timezone: "UTC", input: null,
    next_run_at: null, updated_at: "2026-10-23T00:00:00Z",
  });
  const create = vi.spyOn(api, "createExecution").mockResolvedValue({ id: 92 } as Execution);
  render(<SharedTask value={adapter({ run_mode: "schedule", runtime_locked: true })}
    runtimeRef={createRef<TaskRunSettingsHandle>()} onError={vi.fn()} />);
  await waitFor(() => expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByTestId("workbench-meta").textContent).toContain("Schedule enabled");
  fireEvent.click(screen.getByTestId("header-task-run-once"));
  await waitFor(() => expect(create).toHaveBeenCalledExactlyOnceWith(41));
});
