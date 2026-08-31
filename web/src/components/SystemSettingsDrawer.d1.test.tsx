import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { applySystemLocale } from "../i18n";
import type { Adapter, ManagedInputSettings } from "../types";
import SystemSettingsDrawer from "./SystemSettingsDrawer";

function adapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 7,
    name: "sales-input",
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

function managedSettings(overrides: Partial<ManagedInputSettings> = {}): ManagedInputSettings {
  return {
    id: 1,
    default_retention_seconds: 86_400,
    max_file_bytes: 104_857_600,
    platform_quota_bytes: 10 * 1024 * 1024 * 1024,
    adapter_quota_bytes: 1024 * 1024 * 1024,
    allow_manual_delete: true,
    max_custom_retention_seconds: 2_592_000,
    min_free_space_bytes: 1024 * 1024 * 1024,
    staged_ttl_seconds: 3_600,
    usage: {
      platform_actual_bytes: 2 * 1024 * 1024,
      platform_reserved_bytes: 512 * 1024,
      platform_total_bytes: 2.5 * 1024 * 1024,
      adapters: [
        {
          adapter_id: 7,
          actual_bytes: 2 * 1024 * 1024,
          reserved_bytes: 512 * 1024,
          total_bytes: 2.5 * 1024 * 1024,
          quota_bytes: 1024 * 1024,
          over_quota: true,
        },
      ],
    },
    over_quota: true,
    platform_over_quota: false,
    adapter_over_quota: [7],
    created_at: "2026-08-28T00:00:00Z",
    updated_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

async function openSelect(testId: string): Promise<HTMLElement> {
  const select = screen.getByTestId(testId);
  fireEvent.mouseDown(document.body);
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);

  let dropdown: HTMLElement | undefined;
  await waitFor(() => {
    dropdown = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
    ).find((candidate) => !candidate.classList.contains("ant-select-dropdown-hidden"));
    expect(dropdown).not.toBeUndefined();
  });
  return dropdown as HTMLElement;
}

async function chooseOption(testId: string, label: string): Promise<void> {
  const dropdown = await openSelect(testId);
  const content = Array.from(
    dropdown.querySelectorAll<HTMLElement>(".ant-select-item-option-content"),
  ).find((option) => option.textContent === label);
  if (content === undefined) {
    throw new Error(`Select option not found: ${label}`);
  }
  fireEvent.click(content.closest(".ant-select-item-option") ?? content);
}

afterEach(async () => {
  await applySystemLocale("zh-CN");
  vi.restoreAllMocks();
});

describe("Issue #127 D1 Managed Input system settings", () => {
  it("shows bounded policy, usage and over-quota facts without deployment details", async () => {
    const loaded = managedSettings();
    vi.spyOn(api, "getManagedInputSettings").mockResolvedValue(loaded);

    render(
      <SystemSettingsDrawer
        open
        category="managed-input"
        adapters={[adapter()]}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("managed-input-settings-save")).toBeTruthy());
    expect(screen.getByTestId("managed-input-over-quota")).toBeTruthy();
    expect(screen.getByTestId("managed-input-usage-summary").textContent).toContain("2.0 MB");
    expect(screen.getByTestId("managed-input-allow-manual-delete")).toBeTruthy();
    // The UI uses readable units while the draft sent to the API remains
    // integer seconds/bytes.
    expect(screen.getByTestId("managed-input-default-retention-unit").textContent).toContain("天");
    expect(screen.getByTestId("managed-input-default-retention").getAttribute("aria-valuemin")).toBe("0.04");
    expect(screen.getByTestId("managed-input-default-retention").getAttribute("aria-valuemax")).toBe("30");
    expect(screen.getByTestId("managed-input-staged-ttl-unit").textContent).toContain("小时");
    expect(screen.getByTestId("managed-input-staged-ttl").getAttribute("aria-valuemin")).toBe("0.08");
    expect(screen.getByTestId("managed-input-staged-ttl").getAttribute("aria-valuemax")).toBe("24");
    expect(screen.getByTestId("managed-input-adapter-7").textContent).toContain("sales-input");
    expect(document.body.textContent).not.toContain("artifact_store_root");
    expect(document.body.textContent).not.toContain("/var/lib");
    expect(document.body.textContent).not.toContain("token");
  });

  it("saves only the bounded policy payload and keeps the reduction warning visible", async () => {
    const loaded = managedSettings({ over_quota: false, adapter_over_quota: [] });
    const updated = managedSettings({
      ...loaded,
      default_retention_seconds: 172_800,
      over_quota: false,
      adapter_over_quota: [],
    });
    vi.spyOn(api, "getManagedInputSettings").mockResolvedValue(loaded);
    const save = vi.spyOn(api, "updateManagedInputSettings").mockResolvedValue(updated);

    render(
      <SystemSettingsDrawer
        open
        category="managed-input"
        onClose={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("managed-input-settings-save")).toBeTruthy());

    fireEvent.change(screen.getByTestId("managed-input-default-retention"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByTestId("managed-input-settings-save"));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith({
      default_retention_seconds: 172_800,
      max_file_bytes: 104_857_600,
      platform_quota_bytes: 10 * 1024 * 1024 * 1024,
      adapter_quota_bytes: 1024 * 1024 * 1024,
      allow_manual_delete: true,
      max_custom_retention_seconds: 2_592_000,
      min_free_space_bytes: 1024 * 1024 * 1024,
      staged_ttl_seconds: 3_600,
    });
    await screen.findByText("文件托管策略已保存");
    expect(screen.getByText(/后台清理/)).toBeTruthy();
  });

  it("keeps integer payloads when units change and converts later edits in the selected units", async () => {
    const loaded = managedSettings({ over_quota: false, adapter_over_quota: [] });
    vi.spyOn(api, "getManagedInputSettings").mockResolvedValue(loaded);
    const save = vi.spyOn(api, "updateManagedInputSettings").mockResolvedValue(loaded);

    render(
      <SystemSettingsDrawer
        open
        category="managed-input"
        onClose={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("managed-input-settings-save")).toBeTruthy());

    // Changing units only changes the display; the underlying integer values
    // remain unchanged when the policy is saved.
    await chooseOption("managed-input-default-retention-unit", "小时");
    await chooseOption("managed-input-platform-quota-unit", "MB");
    fireEvent.click(screen.getByTestId("managed-input-settings-save"));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      default_retention_seconds: 86_400,
      platform_quota_bytes: 10 * 1024 * 1024 * 1024,
    }));

    // Saving resets units to the server's preferred values. Select new units
    // again and verify subsequent edits are converted using those units.
    save.mockClear();
    await chooseOption("managed-input-default-retention-unit", "小时");
    await chooseOption("managed-input-platform-quota-unit", "MB");
    fireEvent.change(screen.getByTestId("managed-input-default-retention"), {
      target: { value: "48" },
    });
    fireEvent.change(screen.getByTestId("managed-input-platform-quota"), {
      target: { value: "20480" },
    });
    fireEvent.click(screen.getByTestId("managed-input-settings-save"));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      default_retention_seconds: 172_800,
      platform_quota_bytes: 20 * 1024 * 1024 * 1024,
    }));
  });

  it("uses the existing adapter catalog for usage navigation", async () => {
    const loaded = managedSettings({ over_quota: false, adapter_over_quota: [] });
    vi.spyOn(api, "getManagedInputSettings").mockResolvedValue(loaded);
    const onSelectAdapter = vi.fn(() => true);

    render(
      <SystemSettingsDrawer
        open
        category="managed-input"
        adapters={[adapter()]}
        onSelectAdapter={onSelectAdapter}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("managed-input-adapter-7")).toBeTruthy());
    fireEvent.click(screen.getByTestId("managed-input-adapter-7"));
    expect(onSelectAdapter).toHaveBeenCalledWith(7);
  });

  it("does not render system settings for a principal without managed-input administration", () => {
    const getSettings = vi.spyOn(api, "getManagedInputSettings");
    const { container } = render(
      <SystemSettingsDrawer
        open
        category="managed-input"
        canManageManagedInput={false}
        onClose={vi.fn()}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(getSettings).not.toHaveBeenCalled();
  });
});
