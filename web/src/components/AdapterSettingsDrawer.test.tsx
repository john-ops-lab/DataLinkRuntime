import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "../i18n";
import type { Adapter } from "../types";
import AdapterSettingsDrawer from "./AdapterSettingsDrawer";

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 7,
    name: "orders-task",
    description: "同步订单数据",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: 4,
    owner_user_id: 12,
    owner_username: "alice",
    access_level: "owner",
    created_at: "2026-08-15T00:00:00Z",
    updated_at: "2026-08-15T00:00:00Z",
    ...overrides,
  };
}

function renderSettings(overrides: Partial<Adapter> = {}) {
  const adapter = makeAdapter(overrides);
  const handlers = {
    onClose: vi.fn(),
    onUpdate: vi.fn(async () => true),
    onDelete: vi.fn(),
    onClone: vi.fn(),
  };
  render(
    <AdapterSettingsDrawer
      open
      adapter={adapter}
      name={adapter.name}
      description={adapter.description}
      busy={false}
      contentReady
      accessLevel="owner"
      {...handlers}
    />,
  );
  return { adapter, ...handlers };
}

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("keeps Adapter settings quiet and structured for scanning", () => {
  renderSettings();

  expect(screen.getByTestId("adapter-settings-title").textContent).toContain("适配器设置");
  expect(screen.getByTestId("adapter-settings-summary").textContent).toContain("orders-task");
  expect(screen.getByTestId("adapter-settings-summary").textContent).toContain("任务");
  expect(screen.getByTestId("adapter-settings-summary").textContent).toContain("Python");
  expect(screen.queryByText("已保存 Revision")).toBeNull();
  expect(screen.queryByText("#4")).toBeNull();

  const language = screen.getByTestId("adapter-language");
  expect(language.tagName).not.toBe("INPUT");
  expect(language.getAttribute("aria-readonly")).toBe("true");
  expect(language.textContent).toContain("Python");

  expect(screen.getByTestId("adapter-settings-section-basic")).toBeTruthy();
  expect(screen.getByTestId("adapter-settings-section-permissions")).toBeTruthy();
  expect(screen.getByTestId("adapter-settings-section-more")).toBeTruthy();
  expect(screen.getByTestId("adapter-danger-zone")).toBeTruthy();
  expect(screen.getByTestId("update-details").textContent).toContain("保存更改");
});

it("keeps the fixed action area effective-change and bilingual contracts", async () => {
  renderSettings();
  const save = screen.getByTestId("update-details") as HTMLButtonElement;
  expect(save.disabled).toBe(true);

  fireEvent.change(screen.getByTestId("adapter-description"), {
    target: { value: "更新后的说明" },
  });
  expect(save.disabled).toBe(false);
  expect(screen.getByRole("button", { name: /取\s*消/ })).toBeTruthy();

  cleanup();
  await applySystemLocale("en");
  renderSettings();
  expect(screen.getByTestId("adapter-settings-title").textContent).toContain("Adapter settings");
  expect(screen.getByTestId("adapter-settings-summary").textContent).toContain("Python");
  expect(screen.getByTestId("update-details").textContent).toContain("Save changes");
});
