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
  selectedId: number | null = null,
  onRefresh = vi.fn(async () => {}),
) {
  render(
    <AdapterCatalog
      adapters={adapters}
      selectedId={selectedId}
      busy={false}
      onSelect={onSelect}
      onCreate={vi.fn(async () => false)}
      versionSeqById={new Map()}
      workers={[]}
      onOpenSettings={onOpenSettings}
      onClone={onClone}
      onRefresh={onRefresh}
    />,
  );
  return { onSelect, onOpenSettings, onClone, onRefresh };
}

async function openSelect(testId: string): Promise<HTMLElement> {
  const select = screen.getByTestId(testId);
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);

  let dropdown: HTMLElement | undefined;
  await waitFor(() => {
    dropdown = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
    ).find(
      (candidate) =>
        !candidate.classList.contains("ant-select-dropdown-hidden") &&
        candidate.querySelector(".ant-select-item-option-content") !== null,
    );
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

function visibleNames(): string[] {
  return screen.getAllByTestId("adapter-item").map((item) =>
    item.querySelector(".catalog-item-name")?.textContent?.trim() ?? "",
  );
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
      name: "task-idle，[任务] Python · 手动运行 · 任务 状态：空闲 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-scheduled，[任务] Python · 手动运行 · 任务 状态：定时运行中 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-running，[任务] Python · 手动运行 · 任务 状态：运行中 · 运行节点未配置",
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

it("marks the three-dot menu button with the visual state of its card (M5.8-007)", () => {
  renderCatalog(
    [makeAdapter(1, "alpha"), makeAdapter(2, "beta")],
    vi.fn(),
    vi.fn(),
    vi.fn(),
    2,
  );

  const menus = screen.getAllByTestId("adapter-item-menu");
  expect(menus[0].classList.contains("catalog-item-menu-selected")).toBe(false);
  expect(menus[1].classList.contains("catalog-item-menu-selected")).toBe(true);
  // 可访问性不变：仍是原生 button 且 aria-label 保持原样。
  expect(menus[1].getAttribute("aria-label")).toBe("beta 更多操作");
});

it("labels owned and shared relationships and hides cloning for read-only shares", async () => {
  render(
    <AdapterCatalog
      adapters={[
        makeAdapter(1, "mine", { owner_user_id: 7, access_level: "owner", owner_username: "me" }),
        makeAdapter(2, "editable", { owner_user_id: 7, access_level: "edit", owner_username: "me" }),
        makeAdapter(3, "readonly", { owner_user_id: 7, access_level: "read", owner_username: "me" }),
      ]}
      selectedId={null}
      busy={false}
      onSelect={vi.fn()}
      onCreate={vi.fn(async () => false)}
      versionSeqById={new Map()}
      workers={[]}
      onOpenSettings={vi.fn()}
      onClone={vi.fn()}
      onRefresh={vi.fn(async () => {})}
      accountPrincipal={{ id: 7, username: "me", role: "user", enabled: true, must_change_password: false }}
    />,
  );

  expect(screen.getByText("我的")).toBeTruthy();
  expect(screen.getByText("共享 · 可编辑")).toBeTruthy();
  expect(screen.getByText("共享 · 只读")).toBeTruthy();

  fireEvent.click(screen.getAllByTestId("adapter-item-menu")[2]);
  const menu = await screen.findByRole("menu");
  expect(within(menu).getByRole("menuitem", { name: "设置" })).toBeTruthy();
  expect(within(menu).queryByRole("menuitem", { name: "复制" })).toBeNull();
});

it("uses the Scheme A catalog structure with left filters and right actions", async () => {
  const onRefresh = vi.fn(async () => {});
  renderCatalog([makeAdapter(1, "alpha")], vi.fn(), vi.fn(), vi.fn(), null, onRefresh);

  const catalogHeader = screen.getByTestId("adapter-catalog-header");
  expect(within(catalogHeader).getByRole("heading", { name: "适配器" })).toBeTruthy();
  expect(screen.getByTestId("adapter-catalog-description").textContent).toContain(
    "管理可执行的任务和 Webhook 适配器。",
  );

  const toolbar = screen.getByRole("toolbar", { name: "适配器工具栏" });
  const searchInput = screen.getByTestId("adapter-search");
  const typeSelect = screen.getByTestId("adapter-type-filter");
  const statusSelect = screen.getByTestId("adapter-status-filter");
  const actions = screen.getByTestId("adapter-catalog-actions");
  expect(toolbar.contains(searchInput)).toBe(true);
  expect(toolbar.contains(typeSelect)).toBe(true);
  expect(toolbar.contains(statusSelect)).toBe(true);
  expect(searchInput.compareDocumentPosition(typeSelect) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(typeSelect.compareDocumentPosition(statusSelect) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(actions).getByTestId("show-create-form")).toBeTruthy();
  expect(within(actions).getByTestId("refresh-adapters")).toBeTruthy();
  expect(within(actions).getByTestId("adapter-catalog-help")).toBeTruthy();
  expect(screen.getByTestId("adapter-catalog-summary").textContent).toContain("1");

  const list = screen.getByTestId("adapter-catalog-list");
  expect(list.parentElement).toBe(screen.getByTestId("adapter-catalog"));
  expect(list.classList.contains("catalog-list")).toBe(true);

  fireEvent.click(within(actions).getByTestId("refresh-adapters"));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
});

it("lays out [搜索][类型][状态] as one continuous row (M5.8-008)", () => {
  renderCatalog([makeAdapter(1, "alpha")]);

  const control = document.querySelector(".catalog-search-control");
  expect(control).not.toBeNull();
  const typeSelect = screen.getByTestId("adapter-type-filter");
  const statusSelect = screen.getByTestId("adapter-status-filter");
  const searchInput = screen.getByTestId("adapter-search");
  expect(control?.contains(typeSelect)).toBe(true);
  expect(control?.contains(statusSelect)).toBe(true);
  expect(control?.contains(searchInput)).toBe(true);
  // DOM 顺序：搜索在最左，类型和状态作为紧凑筛选项跟随其后。
  expect(
    searchInput.compareDocumentPosition(typeSelect) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    typeSelect.compareDocumentPosition(statusSelect) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  // 默认显示全部类型 / 全部状态。
  expect(typeSelect.textContent).toContain("全部类型");
  expect(statusSelect.textContent).toContain("全部状态");
});

it("stacks type, status and keyword filters (M5.8-008)", async () => {
  const adapters = [
    makeAdapter(1, "manual-sync", { description: "sync source" }),
    makeAdapter(2, "scheduled-sync", { run_mode: "schedule" }),
    makeAdapter(3, "incoming-sync", { adapter_type: "webhook" }),
    makeAdapter(4, "manual-live", { running_execution_id: 41 }),
  ];
  renderCatalog(adapters);
  expect(screen.getAllByTestId("adapter-item")).toHaveLength(4);

  // 类型筛选：只保留任务型（手动）。
  await chooseOption("adapter-type-filter", "任务型（手动）");
  expect(visibleNames()).toEqual(["manual-sync", "manual-live"]);

  // 状态筛选叠加：运行中只剩 manual-live。
  await chooseOption("adapter-status-filter", "运行中");
  expect(visibleNames()).toEqual(["manual-live"]);

  // 关键词继续叠加：不命中时显示无匹配。
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "sync" } });
  expect(screen.queryAllByTestId("adapter-item")).toHaveLength(0);
  expect(screen.getByText("没有匹配的适配器")).toBeTruthy();
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "live" } });
  expect(visibleNames()).toEqual(["manual-live"]);

  // 状态回到全部状态后，类型与关键词仍生效。
  await chooseOption("adapter-status-filter", "全部状态");
  expect(visibleNames()).toEqual(["manual-live"]);
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "" } });
  expect(visibleNames()).toEqual(["manual-sync", "manual-live"]);

  // 已停止状态：只过滤列表，三个停止的适配器都保留在结果中。
  await chooseOption("adapter-type-filter", "全部类型");
  await chooseOption("adapter-status-filter", "已停止");
  expect(visibleNames()).toEqual(["manual-sync", "scheduled-sync", "incoming-sync"]);
  // 状态筛选不改变真实运行状态：运行中的适配器仍显示运行态圆点。
  await chooseOption("adapter-status-filter", "全部状态");
  expect(
    screen.getAllByTestId("adapter-item")[3].querySelector(".catalog-status-running"),
  ).not.toBeNull();
});
