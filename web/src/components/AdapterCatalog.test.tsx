import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Adapter } from "../types";
import AdapterCatalog from "./AdapterCatalog";

function makeAdapter(id: number, name: string, overrides: Partial<Adapter> = {}): Adapter {
  return {
    id,
    name,
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: null,
    created_at: "2026-08-15T00:00:00Z",
    updated_at: "2026-08-15T00:00:00Z",
    ...overrides,
  };
}

function renderCatalog(
  adapters: Adapter[],
  onSelect = vi.fn(),
  onOpenSettings = vi.fn(),
  onClone = vi.fn(),
) {
  render(
    <AdapterCatalog
      adapters={adapters}
      selectedId={null}
      busy={false}
      onSelect={onSelect}
      onCreate={vi.fn(async () => false)}
      versionSeqById={new Map()}
      workers={[]}
      onOpenSettings={onOpenSettings}
      onClone={onClone}
    />,
  );
  return { onSelect, onOpenSettings, onClone };
}

it("exposes each Task and Webhook runtime status and type in the catalog item name", () => {
  const adapters = [
    makeAdapter(1, "task-idle"),
    makeAdapter(2, "task-scheduled", { runtime_locked: true }),
    makeAdapter(3, "task-running", { running_execution_id: 31 }),
    makeAdapter(4, "webhook-stopped", { adapter_type: "webhook" }),
    makeAdapter(5, "webhook-receiving", { adapter_type: "webhook", runtime_locked: true }),
    makeAdapter(6, "webhook-calling", {
      adapter_type: "webhook",
      running_execution_id: 61,
    }),
  ];

  renderCatalog(adapters);

  // M5.5.9：目录行直接展示 [任务]/[Webhook] 类型。
  expect(
    screen.getByRole("button", {
      name: "task-idle，[任务] Python · 手动运行 · 任务状态：空闲 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-scheduled，[任务] Python · 手动运行 · 任务状态：定时运行中 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-running，[任务] Python · 手动运行 · 任务状态：运行中 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-stopped，[Webhook] Python · Webhook 状态：已停止 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-receiving，[Webhook] Python · Webhook 状态：接收中 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-calling，[Webhook] Python · Webhook 状态：调用中 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
});

it("opens a three-dot menu with only 设置/复制", async () => {
  renderCatalog([makeAdapter(1, "alpha"), makeAdapter(2, "beta")]);

  fireEvent.click(screen.getAllByTestId("adapter-item-menu")[0]);

  const menu = await screen.findByRole("menu");
  expect(within(menu).getByRole("menuitem", { name: "设置" })).toBeTruthy();
  expect(within(menu).getByRole("menuitem", { name: "复制" })).toBeTruthy();
  expect(within(menu).queryByText("删除")).toBeNull();
  expect(within(menu).getAllByRole("menuitem")).toHaveLength(2);
});

it("routes 设置/复制 to the current Adapter from the three-dot menu", async () => {
  const onOpenSettings = vi.fn();
  const onClone = vi.fn();
  renderCatalog(
    [makeAdapter(1, "alpha"), makeAdapter(2, "beta")],
    vi.fn(),
    onOpenSettings,
    onClone,
  );

  const menus = screen.getAllByTestId("adapter-item-menu");
  fireEvent.click(menus[1]);
  fireEvent.click(await screen.findByRole("menuitem", { name: "设置" }));
  expect(onOpenSettings).toHaveBeenCalledWith(expect.objectContaining({ id: 2, name: "beta" }));

  fireEvent.click(menus[1]);
  fireEvent.click(await screen.findByRole("menuitem", { name: "复制" }));
  expect(onClone).toHaveBeenCalledWith(expect.objectContaining({ id: 2, name: "beta" }));
});

it("keeps the three-dot menu keyboard reachable and closable on outside click", async () => {
  renderCatalog([makeAdapter(1, "alpha")]);

  const menuButton = screen.getByTestId("adapter-item-menu") as HTMLButtonElement;
  expect(menuButton.getAttribute("aria-label")).toBe("alpha 更多操作");
  // 原生 button 可 Tab 聚焦；真实浏览器中 Enter/Space 会触发 click 打开菜单。
  menuButton.focus();
  expect(document.activeElement).toBe(menuButton);

  fireEvent.click(menuButton);
  const menu = await screen.findByRole("menu");
  expect(within(menu).getAllByRole("menuitem")).toHaveLength(2);

  // 再次点击触发按钮关闭。
  fireEvent.click(menuButton);
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

  // 点击空白处关闭。
  fireEvent.click(menuButton);
  await screen.findByRole("menu");
  fireEvent.mouseDown(document.body);
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
});

it("does not select the row when clicking the three-dot button", () => {
  const { onSelect } = renderCatalog([makeAdapter(1, "alpha")]);

  fireEvent.click(screen.getByTestId("adapter-item-menu"));
  expect(onSelect).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId("adapter-item"));
  expect(onSelect).toHaveBeenCalledTimes(1);
});
