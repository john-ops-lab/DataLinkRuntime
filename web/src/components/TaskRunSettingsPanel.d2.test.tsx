import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setAuthToken } from "../api";
import { applyUiLocale } from "../i18n";
import type { Adapter, AdapterInputConfig, InputArtifactSummary } from "../types";
import TaskRunSettingsPanel from "./TaskRunSettingsPanel";

function adapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 41,
    name: "d2-fixture",
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    timeout_seconds: 300,
    runtime_worker_id: 3,
    latest_version_id: 11,
    runtime_locked: false,
    created_at: "2026-08-28T00:00:00Z",
    updated_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

function inputArtifact(overrides: Partial<InputArtifactSummary> = {}): InputArtifactSummary {
  return {
    id: 501,
    ordinal: 0,
    original_filename: "records.txt",
    content_type: "text/plain",
    size_bytes: 7,
    sha256: "a".repeat(64),
    status: "READY",
    retention_mode: "system_default",
    expires_at: "2026-08-29T00:00:00Z",
    ...overrides,
  };
}

function inputConfig(overrides: Partial<AdapterInputConfig> = {}): AdapterInputConfig {
  return {
    adapter_id: 41,
    revision: 5,
    source_type: "managed_files",
    json_value: null,
    retention: { mode: "system_default", seconds: null },
    artifacts: [inputArtifact()],
    valid_for_run: true,
    invalid_reason: null,
    ...overrides,
  };
}

function renderPanel(
  config: AdapterInputConfig,
  adapterOverrides: Partial<Adapter> = {},
  onError: ReturnType<typeof vi.fn> = vi.fn(),
  onRuntimeStateChange: ReturnType<typeof vi.fn> = vi.fn(),
) {
  vi.spyOn(api, "getInputConfig").mockResolvedValue(config);
  vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
    managed_files_enabled: true,
    ready: true,
    default_retention_seconds: 86_400,
    max_custom_retention_seconds: 2_592_000,
    allow_manual_delete: true,
    allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
  });
  vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
  if (adapterOverrides.run_mode === "schedule") {
    vi.spyOn(api, "getSchedule").mockResolvedValue({
      adapter_id: 41,
      enabled: false,
      cron: "*/5 * * * *",
      timezone: "Asia/Shanghai",
      input: null,
      next_run_at: null,
      updated_at: "2026-08-28T00:00:00Z",
    });
  }
  render(
    <TaskRunSettingsPanel
      adapter={adapter(adapterOverrides)}
      workers={[{
        id: 3,
        name: "D2 worker",
        status: "online",
        last_heartbeat: "2026-08-28T00:00:00Z",
        capabilities: [adapter(adapterOverrides).language],
      }]}
      workersLoading={false}
      workersError={null}
      execution={null}
      dirty={false}
      onAdapterChange={vi.fn()}
      onExecutionStarted={vi.fn()}
      onRuntimeStateChange={onRuntimeStateChange}
      onError={onError}
    />,
  );
}

afterEach(async () => {
  await applyUiLocale("zh-CN");
  setAuthToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Issue #127 D2 managed input examples", () => {
  it.each([
    ["python", "context.input_files", "original_name"],
    ["javascript", "context.inputFiles", "originalName"],
    ["java", "context.inputFiles", "InputFile"],
  ] as const)("generates a read-only %s Context example and copies it explicitly", async (language, collection, field) => {
    const clipboard = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboard },
    });
    const languageAdapter = adapter({ language });
    const save = vi.spyOn(api, "putInputConfig");
    const saveVersion = vi.spyOn(api, "saveVersion");
    renderPanel(inputConfig(), languageAdapter);

    const code = await waitFor(() => {
      const element = screen.getByTestId("managed-input-example-code");
      expect(element.textContent).toContain(collection);
      expect(element.textContent).toContain(field);
      return element.textContent ?? "";
    });
    expect(code).not.toContain("/api/");
    expect(code).not.toContain("monaco");
    expect(code).not.toContain("AdapterVersion");

    fireEvent.click(screen.getByTestId("managed-input-example-copy"));
    await waitFor(() => expect(clipboard).toHaveBeenCalledWith(code));
    expect(screen.getByTestId("managed-input-example-copy-status").textContent).toContain("已复制");
    expect(save).not.toHaveBeenCalled();
    expect(saveVersion).not.toHaveBeenCalled();
  });

  it("keeps a cloned empty managed-files Input Object read-only, announces re-upload, and exposes a keyboard reason", async () => {
    const onRuntimeStateChange = vi.fn();
    renderPanel(
      inputConfig({ artifacts: [], valid_for_run: false, invalid_reason: "managed_files_empty" }),
      { run_mode: "schedule" },
      vi.fn(),
      onRuntimeStateChange,
    );

    expect(await screen.findByTestId("managed-input-clone-notice")).toBeTruthy();
    expect(api.listInputArtifacts).toHaveBeenCalledWith(41);
    expect(screen.getByTestId("managed-input-clone-notice").textContent).toContain(
      "复制 Adapter 不会复制原 Adapter 的输入文件，请重新上传。",
    );
    const copy = screen.getByTestId("managed-input-example-copy") as HTMLButtonElement;
    expect(copy.disabled).toBe(true);
    expect(screen.getByTestId("managed-input-example-copy-wrapper").getAttribute("tabindex")).toBe("0");
    await waitFor(() => {
      expect(onRuntimeStateChange.mock.calls.at(-1)?.[0]?.scheduleEnableBlockedReason).toContain(
        "托管文件为空",
      );
    });

    await applyUiLocale("en");
    await waitFor(() => {
      expect(screen.getByTestId("managed-input-clone-notice").textContent).toContain(
        "Cloning an Adapter does not copy its input files",
      );
    });
  });
});
