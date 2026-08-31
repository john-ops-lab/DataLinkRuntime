import { createRef } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, setAuthToken } from "../api";
import { applyUiLocale } from "../i18n";
import type {
  Adapter,
  AdapterInputConfig,
  InputArtifactSummary,
  ManagedInputCapability,
  ManagedInputArtifact,
} from "../types";
import TaskRunSettingsPanel, { type TaskRunSettingsHandle } from "./TaskRunSettingsPanel";

function adapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 7,
    name: "d1-fixture",
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

function artifact(overrides: Partial<ManagedInputArtifact> = {}): ManagedInputArtifact {
  return {
    id: 90,
    original_filename: "report.csv",
    content_type: "text/csv",
    size_bytes: 4,
    sha256: "a".repeat(64),
    status: "STAGED",
    created_at: "2026-08-28T00:00:00Z",
    expires_at: "2026-08-28T01:00:00Z",
    ...overrides,
  };
}

function currentArtifact(overrides: Partial<InputArtifactSummary> = {}): InputArtifactSummary {
  return {
    ...artifact(),
    ordinal: 0,
    status: "READY",
    retention_mode: "system_default",
    ...overrides,
  };
}

function inputConfig(overrides: Partial<AdapterInputConfig> = {}): AdapterInputConfig {
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

function renderPanel(
  config: AdapterInputConfig,
  adapterOverrides: Partial<Adapter> = {},
  staged: ManagedInputArtifact[] = [],
  onError: ReturnType<typeof vi.fn> = vi.fn(),
  capabilityOverrides: Partial<ManagedInputCapability> = {},
) {
  vi.spyOn(api, "getInputConfig").mockResolvedValue(config);
  vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
    managed_files_enabled: true,
    ready: true,
    default_retention_seconds: 86_400,
    max_custom_retention_seconds: 2_592_000,
    allow_manual_delete: true,
    allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    ...capabilityOverrides,
  });
  vi.spyOn(api, "listInputArtifacts").mockResolvedValue(staged);
  render(
    <TaskRunSettingsPanel
      adapter={adapter(adapterOverrides)}
      workers={[{
        id: 3,
        name: "D1 worker",
        status: "online",
        last_heartbeat: "2026-08-28T00:00:00Z",
        capabilities: ["python"],
      }]}
      workersLoading={false}
      workersError={null}
      execution={null}
      dirty={false}
      onAdapterChange={vi.fn()}
      onExecutionStarted={vi.fn()}
      onRuntimeStateChange={vi.fn()}
      onError={onError}
    />,
  );
}

function fileInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>("input[type=file]");
  if (input === null) {
    throw new Error("managed input file picker not rendered");
  }
  return input;
}

afterEach(async () => {
  await applyUiLocale("zh-CN");
  setAuthToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Issue #127 D1 managed input editor", () => {
  it("opens the capability card, restores STAGED files, and sends a revisioned managed-files payload", async () => {
    const staged = artifact({ id: 91, original_filename: "恢复.csv" });
    vi.spyOn(api, "listInputArtifacts").mockResolvedValue([staged]);
    const put = vi.spyOn(api, "putInputConfig").mockResolvedValue(inputConfig({
      revision: 5,
      source_type: "managed_files",
      artifacts: [currentArtifact({ id: staged.id, original_filename: staged.original_filename })],
      valid_for_run: true,
      invalid_reason: null,
    }));

    renderPanel(inputConfig(), {}, [staged]);
    await waitFor(() => expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("false"));
    expect(api.listInputArtifacts).toHaveBeenCalledWith(7);
    expect(screen.getByTestId("managed-input-status-91").textContent).toContain("待保存");

    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(put).toHaveBeenCalledWith(7, {
      expected_revision: 4,
      source_type: "managed_files",
      artifact_ids: [91],
      retention: { mode: "system_default", seconds: null },
    }));
    // Revision remains an internal optimistic-concurrency value; it is not
    // exposed as user-facing status text.
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
  });

  it("explains all Managed Files formats on both upload entry points", async () => {
    renderPanel(inputConfig({
      source_type: "managed_files",
      artifacts: [currentArtifact({ id: 97, original_filename: "current.csv" })],
      valid_for_run: true,
    }));

    const upload = await screen.findByTestId("managed-input-upload");
    fireEvent.focus(upload);
    const uploadTooltip = await screen.findByRole("tooltip");
    expect(uploadTooltip.textContent).toContain(".xlsx");
    expect(uploadTooltip.textContent).toContain(".xls");
    expect(uploadTooltip.textContent).toContain(".csv");
    expect(uploadTooltip.textContent).toContain(".log");
    expect(uploadTooltip.textContent).toContain(".txt");
    expect(uploadTooltip.textContent).toContain(".json");
    expect(uploadTooltip.textContent).not.toContain("替换也支持");

    fireEvent.blur(upload);
    const replace = await screen.findByTestId("replace-managed-file-97");
    fireEvent.focus(replace);
    const replaceTooltip = await screen.findByRole("tooltip");
    expect(replaceTooltip.textContent).toBe(uploadTooltip.textContent);
  });

  it("blocks the ninth file before multipart transport and detects NFC/case-fold collisions", async () => {
    const eight = Array.from({ length: 8 }, (_, index) => currentArtifact({
      id: index + 1,
      ordinal: index,
      original_filename: `file-${index}.txt`,
    }));
    const onError = vi.fn();
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig({
      source_type: "managed_files",
      artifacts: eight,
      valid_for_run: true,
    }));
    vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({ managed_files_enabled: true, ready: true, default_retention_seconds: 86_400, max_custom_retention_seconds: 2_592_000, allow_manual_delete: true, allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"] });
    vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
    const upload = vi.spyOn(XMLHttpRequest.prototype, "send");
    render(
      <TaskRunSettingsPanel
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={onError}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("managed-input-count").textContent).toContain("8/8"));
    fireEvent.change(fileInput(), {
      target: { files: [new File(["ninth"], "ninth.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith("托管文件最多只能选择 8 个。"));
    expect(upload).not.toHaveBeenCalled();

  });

  it("detects NFC/case-folded filename collisions before uploading", async () => {
    const existing = currentArtifact({ original_filename: "Caf\u00e9.txt" });
    const onError = vi.fn();
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig({
      source_type: "managed_files",
      artifacts: [existing],
      valid_for_run: true,
    }));
    vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({ managed_files_enabled: true, ready: true, default_retention_seconds: 86_400, max_custom_retention_seconds: 2_592_000, allow_manual_delete: true, allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"] });
    vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
    const upload = vi.spyOn(XMLHttpRequest.prototype, "send");
    render(
      <TaskRunSettingsPanel
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={onError}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("managed-input-count").textContent).toContain("1/8"));
    fireEvent.change(fileInput(), {
      target: { files: [new File(["duplicate"], "cafe\u0301.TXT", { type: "text/plain" })] },
    });
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith("文件名经 NFC 与大小写折叠后重复，请修改名称。"));
    expect(upload).not.toHaveBeenCalled();
  });

  it("keeps a dirty managed-files draft after 409 and deletes STAGED without changing revision", async () => {
    const staged = artifact({ id: 92, original_filename: "draft.txt" });
    const put = vi.spyOn(api, "putInputConfig").mockRejectedValue(
      new ApiError(409, "input_config_revision_conflict", "stale fixture"),
    );
    const deleteArtifact = vi.spyOn(api, "deleteInputArtifact").mockResolvedValue();
    renderPanel(inputConfig({ source_type: "managed_files", valid_for_run: false, invalid_reason: "managed_files_empty" }), {}, [staged]);

    await waitFor(() => expect(screen.getByTestId("managed-input-status-92").textContent).toContain("待保存"));
    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));
    fireEvent.click(screen.getByTestId("save-task-input"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("task-input-state").textContent).toContain("草稿");
    expect(screen.getByTestId("managed-input-status-92").textContent).toContain("待保存");
    const configReadsBeforeDelete = vi.mocked(api.getInputConfig).mock.calls.length;

    fireEvent.click(screen.getByTestId("delete-managed-file-92"));
    await waitFor(() => expect(deleteArtifact).toHaveBeenCalledWith(7, 92));
    expect(screen.queryByTestId("managed-input-status-92")).toBeNull();
    expect(put).toHaveBeenCalledTimes(1);
    expect(api.getInputConfig).toHaveBeenCalledTimes(configReadsBeforeDelete);
  });

  it("keeps the managed draft when the server Runtime Lock wins a save race", async () => {
    const current = currentArtifact({ id: 98, original_filename: "current.csv" });
    const staged = artifact({ id: 99, original_filename: "new.csv" });
    const put = vi.spyOn(api, "putInputConfig").mockRejectedValue(
      new ApiError(409, "adapter_runtime_locked", "runtime lock fixture"),
    );
    const refresh = vi.spyOn(api, "getAdapter").mockResolvedValue(adapter({ runtime_locked: true }));
    const onError = vi.fn();
    renderPanel(
      inputConfig({ source_type: "managed_files", artifacts: [current], valid_for_run: true }),
      { runtime_locked: false },
      [staged],
      onError,
    );

    await waitFor(() => expect(screen.getByTestId("managed-input-status-99").textContent).toContain("待保存"));
    fireEvent.click(screen.getByTestId("save-task-input"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("task-input-state").textContent).toContain("草稿");
    expect(screen.getByTestId("managed-input-status-99").textContent).toContain("待保存");
    expect(screen.queryByTestId("task-input-revision")).toBeNull();
    expect(onError.mock.calls.at(-1)?.[0]).toContain("请停止适配器");
    await waitFor(() => expect(refresh).toHaveBeenCalledWith(7));
  });

  it("keeps upload available under Runtime Lock while configuration mutations stay disabled", async () => {
    renderPanel(
      inputConfig({
        source_type: "managed_files",
        artifacts: [currentArtifact({ id: 101, original_filename: "locked.csv" })],
        valid_for_run: true,
      }),
      { runtime_locked: true },
    );

    const upload = await screen.findByTestId("managed-input-upload");
    expect((upload as HTMLButtonElement).disabled).toBe(false);
    expect(fileInput().disabled).toBe(false);
    expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTestId("replace-managed-file-101")).toBeNull();
  });

  it("shows server expires_at and submits custom/manual retention without client-side expiry guesses", async () => {
    const staged = artifact({ id: 93, expires_at: "2026-08-28T03:04:05Z" });
    const put = vi.spyOn(api, "putInputConfig").mockResolvedValue(inputConfig({
      revision: 5,
      source_type: "managed_files",
      artifacts: [currentArtifact({ id: 93, expires_at: null, retention_mode: "manual_delete" })],
      retention: { mode: "manual_delete", seconds: null },
      valid_for_run: true,
      invalid_reason: null,
    }));
    renderPanel(inputConfig(), {}, [staged]);
    await waitFor(() => expect(screen.getByTestId("managed-input-expires-93").textContent).toContain("2026"));
    expect(screen.getByTestId("managed-input-created-93").textContent).toContain("2026");
    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));

    fireEvent.mouseDown(screen.getByTestId("managed-input-retention-mode").querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(screen.getByText("永久保留")).toBeTruthy());
    fireEvent.click(screen.getByText("永久保留"));
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(put).toHaveBeenCalledWith(7, {
      expected_revision: 4,
      source_type: "managed_files",
      artifact_ids: [93],
      retention: { mode: "manual_delete", seconds: null },
    }));
  });

  it("keeps the draft and localizes a server rejection when permanent retention is disabled", async () => {
    const put = vi.spyOn(api, "putInputConfig").mockRejectedValue(
      new ApiError(422, "input_invalid", "manual retention disabled", {
        reason: "manual_delete_not_allowed",
      }),
    );
    const onError = vi.fn();
    renderPanel(inputConfig(), {}, [], onError);
    await waitFor(() => expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("false"));
    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));
    fireEvent.mouseDown(screen.getByTestId("managed-input-retention-mode").querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(screen.getByText("永久保留")).toBeTruthy());
    fireEvent.click(screen.getByText("永久保留"));
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(onError.mock.calls.at(-1)?.[0]).toContain("管理员已禁用永久保留");
    expect(screen.getByTestId("task-input-state").textContent).toContain("草稿");
  });

  it("applies capability retention policy to permanent and custom controls", async () => {
    renderPanel(
      inputConfig({
        source_type: "managed_files",
        artifacts: [currentArtifact({ id: 102, original_filename: "policy.csv" })],
        retention: { mode: "custom", seconds: 7_200 },
        valid_for_run: true,
      }),
      {},
      [],
      vi.fn(),
      {
        default_retention_seconds: 7_200,
        max_custom_retention_seconds: 10_800,
        allow_manual_delete: false,
      },
    );
    await waitFor(() => expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("false"));
    fireEvent.mouseDown(screen.getByTestId("managed-input-retention-mode").querySelector(".ant-select-selector") as HTMLElement);

    const permanent = await screen.findByText("永久保留");
    expect(permanent.closest(".ant-select-item")?.className).toContain("ant-select-item-option-disabled");
    const customInput = await screen.findByTestId("managed-input-retention-seconds") as HTMLInputElement;
    fireEvent.change(customInput, { target: { value: "20000" } });
    fireEvent.blur(customInput);
    await waitFor(() => expect(customInput.value).toBe("10800"));
  });

  it("shows the server retention maximum in the localized rejection", async () => {
    vi.spyOn(api, "putInputConfig").mockRejectedValue(
      new ApiError(422, "input_invalid", "retention out of range", {
        reason: "retention_out_of_range",
        max_seconds: 10_800,
      }),
    );
    const onError = vi.fn();
    renderPanel(
      inputConfig({
        source_type: "managed_files",
        artifacts: [currentArtifact({ id: 104, original_filename: "retention.csv" })],
        retention: { mode: "custom", seconds: 7_200 },
        valid_for_run: true,
      }),
      {},
      [],
      onError,
    );

    await waitFor(() => expect(screen.getByTestId("managed-input-retention-seconds")).toBeTruthy());
    fireEvent.click(screen.getByTestId("save-task-input"));

    await waitFor(() => {
      expect(onError.mock.calls.at(-1)?.[0]).toContain("10800");
      expect(screen.getByTestId("managed-input-error").textContent).toContain("10800");
    });
  });

  it("fails retention closed when capability loading fails and offers a retry", async () => {
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig({
      source_type: "managed_files",
      artifacts: [currentArtifact({ id: 105, original_filename: "policy.csv" })],
      retention: { mode: "custom", seconds: 7_200 },
      valid_for_run: true,
    }));
    const capability = vi.spyOn(api, "getManagedInputCapability")
      .mockRejectedValueOnce(new Error("capability unavailable"))
      .mockResolvedValue({
        managed_files_enabled: true,
        ready: true,
        default_retention_seconds: 7_200,
        max_custom_retention_seconds: 10_800,
        allow_manual_delete: true,
        allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
      });
    vi.spyOn(api, "listInputArtifacts").mockResolvedValue([]);
    const put = vi.spyOn(api, "putInputConfig");
    render(
      <TaskRunSettingsPanel
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("managed-input-capability-error")).toBeTruthy();
    expect((screen.getByTestId("managed-input-retention-seconds") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByTestId("managed-input-retention-mode").className).toContain("ant-select-disabled");
    expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("save-task-input"));
    expect(put).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("managed-input-capability-retry"));
    await waitFor(() => expect(capability).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("managed-input-capability-error")).toBeNull());
    expect((screen.getByTestId("managed-input-retention-seconds") as HTMLInputElement).disabled).toBe(false);
  });

  it("keeps capability usable when only the staged list fails and retries only that list", async () => {
    const current = currentArtifact({ id: 106, original_filename: "current.csv" });
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig({
      source_type: "managed_files",
      artifacts: [current],
      valid_for_run: true,
    }));
    const capability = vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
      managed_files_enabled: true,
      ready: true,
      default_retention_seconds: 86_400,
      max_custom_retention_seconds: 2_592_000,
      allow_manual_delete: true,
      allowed_extensions: [".csv"],
    });
    const staged = artifact({ id: 107, original_filename: "recovered.csv" });
    const list = vi.spyOn(api, "listInputArtifacts")
      .mockRejectedValueOnce(new Error("staged list unavailable"))
      .mockResolvedValueOnce([staged]);

    render(
      <TaskRunSettingsPanel
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("managed-input-staged-list-error")).toBeTruthy();
    expect(screen.queryByTestId("managed-input-capability-error")).toBeNull();
    expect(screen.getByTestId("managed-input-retention-mode").className).not.toContain("ant-select-disabled");
    expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByTestId("managed-input-staged-list-retry"));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("managed-input-staged-list-error")).toBeNull());
    expect(await screen.findByTestId("managed-input-status-107")).toBeTruthy();
    expect(capability).toHaveBeenCalledTimes(1);
  });

  it("keeps none input saveable while Managed Input capability is unavailable", async () => {
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig({ source_type: "none" }));
    vi.spyOn(api, "getManagedInputCapability").mockRejectedValue(new Error("capability unavailable"));
    const put = vi.spyOn(api, "putInputConfig").mockResolvedValue(inputConfig({ revision: 5 }));

    render(
      <TaskRunSettingsPanel
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={vi.fn()}
      />,
    );

    await waitFor(() => expect(
      screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled"),
    ).toBe("true"));
    expect((screen.getByTestId("save-task-input") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("save-task-input"));
    await waitFor(() => expect(put).toHaveBeenCalledWith(7, {
      expected_revision: 4,
      source_type: "none",
    }));
  });

  it("derives file picker hints and prevalidation from capability extensions", async () => {
    const onError = vi.fn();
    const upload = vi.spyOn(XMLHttpRequest.prototype, "send");
    renderPanel(
      inputConfig({ source_type: "managed_files", valid_for_run: false, invalid_reason: "managed_files_empty" }),
      {},
      [],
      onError,
      { allowed_extensions: [".csv"] },
    );

    const uploadButton = await screen.findByTestId("managed-input-upload");
    expect(fileInput().accept).toBe(".csv");
    fireEvent.focus(uploadButton);
    expect((await screen.findByRole("tooltip")).textContent).toContain(".csv");
    expect(screen.getByRole("tooltip").textContent).not.toContain(".txt");

    fireEvent.change(fileInput(), {
      target: { files: [new File(["blocked"], "blocked.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(onError.mock.calls.at(-1)?.[0]).toContain("不允许上传此文件类型"));
    expect(upload).not.toHaveBeenCalled();
  });

  it("caps refresh recovery at eight while overflow STAGED files stay visible and deletable", async () => {
    const current = [
      currentArtifact({ id: 201, ordinal: 0, original_filename: "ready-0.txt" }),
      currentArtifact({ id: 202, ordinal: 1, original_filename: "ready-1.txt" }),
    ];
    const staged = Array.from({ length: 8 }, (_, index) => artifact({
      id: 210 + index,
      original_filename: `staged-${index}.txt`,
    }));
    const deleteArtifact = vi.spyOn(api, "deleteInputArtifact").mockResolvedValue();
    renderPanel(inputConfig({
      source_type: "managed_files",
      artifacts: current,
      valid_for_run: true,
    }), {}, staged);

    await waitFor(() => expect(screen.getByTestId("managed-input-count").textContent).toContain("8/8"));
    const overflow = screen.getByTestId("managed-input-overflow-staged");
    expect(overflow.textContent).toContain("staged-6.txt");
    expect(overflow.textContent).toContain("staged-7.txt");
    expect(screen.queryByTestId("managed-input-status-216")).toBeNull();

    fireEvent.click(screen.getByTestId("delete-overflow-staged-216"));
    await waitFor(() => expect(deleteArtifact).toHaveBeenCalledWith(7, 216));
    await waitFor(() => expect(screen.queryByTestId("delete-overflow-staged-216")).toBeNull());
    expect(screen.getByTestId("delete-overflow-staged-217")).toBeTruthy();
  });

  it("keeps an independent multipart upload visible while it reports progress", async () => {
    class PendingXmlHttpRequest {
      static last: PendingXmlHttpRequest | null = null;
      readonly upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
      withCredentials = false;
      status = 201;
      responseText = JSON.stringify(artifact({ id: 95, original_filename: "progress.csv" }));
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      method = "";
      url = "";
      headers: Record<string, string> = {};
      body: BodyInit | null = null;

      constructor() {
        PendingXmlHttpRequest.last = this;
      }

      open(method: string, url: string): void {
        this.method = method;
        this.url = url;
      }

      setRequestHeader(name: string, value: string): void {
        this.headers[name] = value;
      }

      send(body: BodyInit | null): void {
        this.body = body;
        this.upload.onprogress?.({ loaded: 4, total: 8 } as ProgressEvent);
      }

      complete(): void {
        this.onload?.();
      }

      abort(): void {
        this.onabort?.();
      }
    }

    setAuthToken("d1-auth");
    document.cookie = "dlr_account_csrf=d1-csrf; path=/";
    vi.stubGlobal("XMLHttpRequest", PendingXmlHttpRequest);
    renderPanel(inputConfig());
    await waitFor(() => expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("false"));
    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));
    fireEvent.change(fileInput(), {
      target: { files: [new File(["progress"], "progress.csv", { type: "text/csv" })] },
    });

    await waitFor(() => expect(screen.getByTestId("managed-input-upload-progress")).toBeTruthy());
    const request = PendingXmlHttpRequest.last;
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe("/api/adapters/7/input-artifacts");
    expect(request?.withCredentials).toBe(true);
    expect(request?.headers.Authorization).toBe("Bearer d1-auth");
    expect(request?.headers["X-CSRF-Token"]).toBe("d1-csrf");
    expect(request?.headers["Content-Type"]).toBeUndefined();
    expect(request?.body).toBeInstanceOf(FormData);

    request?.complete();
    await waitFor(() => expect(screen.getByTestId("managed-input-status-95").textContent).toContain("待保存"));
    expect(screen.queryByTestId("managed-input-upload-progress")).toBeNull();
  });

  it("surfaces the server low-watermark error without leaking transport details", async () => {
    class LowWatermarkXmlHttpRequest {
      readonly upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
      withCredentials = false;
      status = 409;
      responseText = JSON.stringify({
        detail: {
          code: "artifact_store_low_watermark",
          message: "internal storage path must not reach the browser",
        },
      });
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;

      open(): void {}

      setRequestHeader(): void {}

      send(): void {
        this.onload?.();
      }

      abort(): void {
        this.onabort?.();
      }
    }

    const onError = vi.fn();
    vi.stubGlobal("XMLHttpRequest", LowWatermarkXmlHttpRequest);
    renderPanel(inputConfig(), {}, [], onError);
    await waitFor(() => expect(screen.getByTestId("task-input-source-managed_files").getAttribute("aria-disabled")).toBe("false"));
    fireEvent.click(screen.getByTestId("task-input-source-managed_files"));
    fireEvent.change(fileInput(), {
      target: { files: [new File(["blocked"], "blocked.csv", { type: "text/csv" })] },
    });

    await waitFor(() => expect(onError.mock.calls.at(-1)?.[0]).toContain("可用存储空间不足"));
    expect(onError.mock.calls.at(-1)?.[0]).not.toContain("internal storage path");
  });

  it("warns before leaving while an unbound STAGED artifact remains", async () => {
    const staged = artifact({ id: 96, original_filename: "leave.csv" });
    renderPanel(inputConfig(), {}, [staged]);
    await waitFor(() => expect(screen.getByTestId("managed-input-status-96").textContent).toContain("待保存"));

    const event = new Event("beforeunload", { cancelable: true }) as BeforeUnloadEvent;
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("exposes the same staged warning to SPA navigation", async () => {
    const staged = artifact({ id: 103, original_filename: "spa-leave.csv" });
    const panelRef = createRef<TaskRunSettingsHandle>();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    vi.spyOn(api, "getInputConfig").mockResolvedValue(inputConfig());
    vi.spyOn(api, "getManagedInputCapability").mockResolvedValue({
      managed_files_enabled: true,
      ready: true,
      default_retention_seconds: 86_400,
      max_custom_retention_seconds: 2_592_000,
      allow_manual_delete: true,
      allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    });
    vi.spyOn(api, "listInputArtifacts").mockResolvedValue([staged]);
    render(
      <TaskRunSettingsPanel
        ref={panelRef}
        adapter={adapter()}
        workers={[]}
        workersLoading={false}
        workersError={null}
        execution={null}
        dirty={false}
        onAdapterChange={vi.fn()}
        onExecutionStarted={vi.fn()}
        onRuntimeStateChange={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("managed-input-status-103")).toBeTruthy());

    expect(panelRef.current?.confirmLeave()).toBe(false);
    expect(confirm).toHaveBeenCalledTimes(1);
  });
});
