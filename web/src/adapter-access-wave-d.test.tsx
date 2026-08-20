import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { applySystemLocale } from "./i18n";
import type { Adapter } from "./types";
import TaskWorkbenchHeader from "./components/TaskWorkbenchHeader";
import WebhookWorkbenchHeader from "./components/WebhookWorkbenchHeader";

const adapter = (adapterType: "task" | "webhook"): Adapter => ({
  id: 1,
  name: "shared-adapter",
  description: "",
  language: "python",
  adapter_type: adapterType,
  run_mode: "schedule",
  latest_version_id: 2,
  runtime_worker_id: 3,
  runtime_locked: false,
  running_execution_id: null,
  archived_at: null,
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-20T00:00:00Z",
});

beforeEach(async () => {
  await applySystemLocale("zh-CN");
});

it("removes task write, run, stop and schedule controls for read-only shares", () => {
  render(
    <TaskWorkbenchHeader
      adapter={adapter("task")}
      runtimeWorker={null}
      runtimeState={{
        scheduleEnabled: false,
        loading: false,
        activeExecution: false,
        canRun: false,
        scheduleEnableBlockedReason: null,
      }}
      dirty={false}
      busy={false}
      contentReady
      readOnly
      onSave={vi.fn()}
      onOpenSettings={vi.fn()}
      onRunOnce={vi.fn()}
      onStopExecution={vi.fn()}
      onToggleSchedule={vi.fn()}
    />,
  );

  expect(screen.getByTestId("adapter-read-only")).toBeTruthy();
  expect(screen.getByTestId("adapter-settings")).toBeTruthy();
  expect(screen.queryByTestId("save-version")).toBeNull();
  expect(screen.queryByTestId("header-task-run-once")).toBeNull();
  expect(screen.queryByTestId("header-task-stop")).toBeNull();
  expect(screen.queryByTestId("header-task-schedule-toggle")).toBeNull();
});

it("removes Webhook save and receiving controls for read-only shares", () => {
  render(
    <WebhookWorkbenchHeader
      adapter={adapter("webhook")}
      runtimeWorker={null}
      runtimeState={{
        loaded: true,
        enabled: false,
        runtimeLocked: false,
        changingState: false,
        startBlockedReason: null,
      }}
      busy={false}
      contentReady
      readOnly
      onSave={vi.fn()}
      onOpenSettings={vi.fn()}
      onToggleReceiving={vi.fn()}
    />,
  );

  expect(screen.getByTestId("adapter-read-only")).toBeTruthy();
  expect(screen.queryByTestId("save-version")).toBeNull();
  expect(screen.queryByTestId("header-webhook-toggle")).toBeNull();
});
