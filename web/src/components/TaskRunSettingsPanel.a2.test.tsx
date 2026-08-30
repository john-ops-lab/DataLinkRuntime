import { createRef } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, setAuthToken } from "../api";
import { applyUiLocale } from "../i18n";
import type {
  Adapter,
  AdapterInputConfig,
  AdapterSchedule,
  Execution,
  TaskRunMode,
} from "../types";
import TaskRunSettingsPanel, { type TaskRunSettingsHandle } from "./TaskRunSettingsPanel";

function makeAdapter(runMode: TaskRunMode = "manual"): Adapter {
  return {
    id: 7,
    name: "fixture-adapter",
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: runMode,
    timeout_seconds: 300,
    runtime_worker_id: 3,
    latest_version_id: 11,
    created_at: "2026-08-26T00:00:00Z",
    updated_at: "2026-08-26T00:00:00Z",
  };
}

function makeInputConfig(overrides: Partial<AdapterInputConfig> = {}): AdapterInputConfig {
  return {
    adapter_id: 7,
    revision: 4,
    source_type: "none",
    json_value: null,
    retention: { mode: "system_default", seconds: null },
    artifacts: [],
    valid_for_run: true,
    invalid_reason: null,
    ...overrides,
  };
}

function makeSchedule(): AdapterSchedule {
  return {
    adapter_id: 7,
    enabled: false,
    cron: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    input: null,
    next_run_at: null,
    updated_at: "2026-08-26T00:00:00Z",
  };
}

function makeExecution(): Execution {
  return {
    id: 31,
    adapter_id: 7,
    version_id: 11,
    worker_id: 3,
    target_worker_id: 3,
    trigger: "manual",
    scheduled_for: null,
    status: "pending",
    input: null,
    output: null,
    output_size: null,
    output_truncated: false,
    output_preview: null,
    stdout: "",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    created_at: "2026-08-26T00:00:00Z",
    started_at: null,
    ended_at: null,
    duration_ms: null,
  };
}

function renderPanel(
  inputConfig: AdapterInputConfig,
  options: {
    runMode?: TaskRunMode;
    onRuntimeStateChange?: ReturnType<typeof vi.fn>;
    onError?: ReturnType<typeof vi.fn>;
    inputLoadError?: unknown;
    managedFilesEnabled?: boolean;
  } = {},
) {
  const getInputConfig = vi.spyOn(api, "getInputConfig");
  if (options.inputLoadError !== undefined) {
    getInputConfig.mockRejectedValue(options.inputLoadError);
  } else {
    getInputConfig.mockResolvedValue(inputConfig);
  }
  const managedFilesEnabled = options.managedFilesEnabled === true;
  vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
    managed_files_enabled: managedFilesEnabled,
    ready: managedFilesEnabled,
    default_retention_seconds: 86_400,
    max_custom_retention_seconds: 2_592_000,
    allow_manual_delete: true,
    allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
  });
  vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
  const runtimeRef = createRef<TaskRunSettingsHandle>();
  const onError = options.onError ?? vi.fn();
  render(
    <TaskRunSettingsPanel
      ref={runtimeRef}
      adapter={makeAdapter(options.runMode)}
      workers={[
        {
          id: 3,
          name: "fixture-worker",
          status: "online",
          last_heartbeat: "2026-08-26T00:00:00Z",
          capabilities: ["python"],
        },
      ]}
      workersLoading={false}
      workersError={null}
      execution={null}
      dirty={false}
      onAdapterChange={vi.fn()}
      onExecutionStarted={vi.fn()}
      onRuntimeStateChange={options.onRuntimeStateChange ?? vi.fn()}
      onError={onError}
    />,
  );
  return runtimeRef;
}

afterEach(async () => {
  await applyUiLocale("zh-CN");
  setAuthToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Task Input Object A2", () => {
  it("uses one draft across manual and schedule modes and exposes no legacy JSON paths", async () => {
    vi.spyOn(api, "getSchedule").mockResolvedValue(makeSchedule());
    renderPanel(
      makeInputConfig({
        source_type: "json",
        json_value: { saved: true },
      }),
    );

    const input = await screen.findByTestId("task-input-json") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '["draft"]' } });
    fireEvent.click(screen.getByLabelText("定时运行"));

    await screen.findByTestId("task-schedule-cron");
    expect((screen.getByTestId("task-input-json") as HTMLTextAreaElement).value).toBe('["draft"]');
    fireEvent.click(screen.getByLabelText("手动运行"));
    expect((await screen.findByTestId("task-input-json") as HTMLTextAreaElement).value).toBe('["draft"]');
    expect(screen.queryByTestId("task-manual-input")).toBeNull();
    expect(screen.queryByTestId("task-schedule-input")).toBeNull();
  });

  it.each([
    ["object", { nested: true }],
    ["array", [1, "two"]],
    ["string", "text fixture"],
    ["number", 42],
    ["boolean", false],
    ["null", null],
  ] as const)("saves a JSON %s top-level value without changing the draft contract", async (_label, value) => {
    const put = vi.spyOn(api, "putInputConfig").mockImplementation(async (_adapterId, payload) =>
      makeInputConfig({
        revision: 5,
        source_type: "json",
        json_value: payload.source_type === "json" ? payload.json_value : null,
      }),
    );
    renderPanel(makeInputConfig());

    await screen.findByTestId("task-input-source-json");
    await waitFor(() => expect(screen.queryByTestId("task-input-revision")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("task-input-source-json").getAttribute("aria-disabled")).toBe("false"));
    fireEvent.click(screen.getByTestId("task-input-source-json"));
    fireEvent.change(await screen.findByTestId("task-input-json"), {
      target: { value: JSON.stringify(value) },
    });
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]?.[1]).toEqual({
      expected_revision: 4,
      source_type: "json",
      json_value: value,
    });
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
  });

  it("saves none without a JSON field and keeps all four source cards focusable", async () => {
    const put = vi.spyOn(api, "putInputConfig").mockResolvedValue(
      makeInputConfig({ revision: 5, source_type: "none" }),
    );
    renderPanel(makeInputConfig());

    const inputConfigSection = screen.getByTestId("task-input-config");
    const cards = within(inputConfigSection).getAllByRole("radio");
    await waitFor(() => expect(screen.queryByTestId("task-input-revision")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("task-input-source-none").getAttribute("aria-disabled")).toBe("false"));
    expect(cards).toHaveLength(4);
    expect(cards.every((card) => card.getAttribute("tabindex") === "0")).toBe(true);
    expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByTestId("task-input-source-remote_files").getAttribute("aria-disabled")).toBe("true");
    expect(screen.queryByTestId("task-input-json")).toBeNull();

    fireEvent.keyDown(screen.getByTestId("task-input-source-managed_files"), { key: "Enter" });
    expect(screen.getByTestId("task-input-source-none").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByTestId("save-task-input"));
    await waitFor(() => expect(put).toHaveBeenCalledWith(7, {
      expected_revision: 4,
      source_type: "none",
    }));
  });

  it("keeps the last valid revision and draft after a revision conflict", async () => {
    vi.spyOn(api, "putInputConfig").mockRejectedValue(
      new ApiError(409, "input_config_revision_conflict", "stale fixture"),
    );
    renderPanel(makeInputConfig({ source_type: "json", json_value: { saved: true } }));

    const input = await screen.findByTestId("task-input-json") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '{"draft":true}' } });
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(screen.getByTestId("task-input-state").textContent).toContain("草稿"));
    expect((screen.getByTestId("task-input-json") as HTMLTextAreaElement).value).toBe('{"draft":true}');
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
    expect(document.body.textContent).toContain("输入对象已被其他页面更新");
  });

  it("blocks save and imperative run when InputConfig loading fails", async () => {
    const put = vi.spyOn(api, "putInputConfig");
    const createExecution = vi.spyOn(api, "createExecution");
    const onError = vi.fn();
    const runtimeRef = renderPanel(makeInputConfig(), {
      inputLoadError: new ApiError(503, "input_config_not_initialized", "fixture load failure"),
      onError,
    });

    await waitFor(() => expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(true));
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
    runtimeRef.current?.runOnce();

    await waitFor(() => expect(onError).toHaveBeenLastCalledWith("输入对象加载失败，请刷新后重试。"));
    expect(put).not.toHaveBeenCalled();
    expect(createExecution).not.toHaveBeenCalled();
  });

  it.each([
    ["managed_files_empty", "托管文件为空，请先准备文件后再运行。", "none", null, "托管文件为空"],
    ["input_source_not_available", "该输入对象来源尚未启用。", "json", { recovered: true }, "来源尚未启用"],
  ] as const)("recovers invalid managed_files (%s) through %s and only runs after save", async (invalidReason, blockedReason, sourceType, jsonValue, invalidMessage) => {
    const put = vi.spyOn(api, "putInputConfig").mockImplementation(async (_adapterId, payload) =>
      makeInputConfig({
        revision: 5,
        source_type: payload.source_type,
        json_value: payload.source_type === "json" ? payload.json_value : null,
        valid_for_run: true,
        invalid_reason: null,
      }),
    );
    const createExecution = vi.spyOn(api, "createExecution");
    const onError = vi.fn();
    const runtimeRef = renderPanel(makeInputConfig({
      source_type: "managed_files",
      valid_for_run: false,
      invalid_reason: invalidReason,
    }), { onError, managedFilesEnabled: true });

    await waitFor(() => expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByTestId("task-input-invalid").textContent).toContain(invalidMessage);
    runtimeRef.current?.runOnce();
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith(blockedReason));
    expect(createExecution).not.toHaveBeenCalled();

    const sourceCard = screen.getByTestId(`task-input-source-${sourceType}`);
    expect(sourceCard.getAttribute("aria-disabled")).toBe("false");
    fireEvent.click(sourceCard);
    if (sourceType === "json") {
      fireEvent.change(await screen.findByTestId("task-input-json"), {
        target: { value: JSON.stringify(jsonValue) },
      });
    }
    expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(put).toHaveBeenCalledWith(7, {
      expected_revision: 4,
      source_type: sourceType,
      ...(sourceType === "json" ? { json_value: jsonValue } : {}),
    }));
    runtimeRef.current?.runOnce();
    await waitFor(() => expect(createExecution).toHaveBeenCalledTimes(1));
  });

  it("blocks an invalid JSON draft before the save request", async () => {
    const put = vi.spyOn(api, "putInputConfig");
    renderPanel(makeInputConfig({ source_type: "json", json_value: { saved: true } }));

    const input = await screen.findByTestId("task-input-json") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '{"broken":' } });
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(screen.getByTestId("task-input-json").getAttribute("aria-invalid")).toBe("true"));
    expect(put).not.toHaveBeenCalled();
    expect((screen.getByTestId("task-input-json") as HTMLTextAreaElement).value).toBe('{"broken":');
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
  });

  it("runs through the saved Input Object and never passes a per-run input override", async () => {
    const createExecution = vi.spyOn(api, "createExecution").mockResolvedValue(makeExecution());
    vi.spyOn(api, "getAdapter").mockResolvedValue(makeAdapter());
    const runtimeRef = renderPanel(makeInputConfig({ source_type: "json", json_value: { saved: true } }));

    await screen.findByTestId("task-input-json");
    runtimeRef.current?.runOnce();
    await waitFor(() => expect(createExecution).toHaveBeenCalledTimes(1));
    expect(createExecution).toHaveBeenCalledWith(7);
  });

  it("serializes the new Execution request with an empty JSON body", async () => {
    const execution = makeExecution();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.body).toBe("{}");
      expect(String(init?.body)).not.toContain("input");
      return { ok: true, status: 200, json: async () => execution };
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.createExecution(7);
    expect(fetchMock).toHaveBeenCalledWith("/api/adapters/7/executions", expect.objectContaining({
      method: "POST",
      body: "{}",
    }));
  });
});
