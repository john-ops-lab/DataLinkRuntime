import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { Adapter } from "../types";
import type { AdapterBatchAction, AdapterBatchTarget } from "../adapter-batch-types";
import AdapterCatalog from "./AdapterCatalog";

function adapter(id: number, overrides: Partial<Adapter> = {}): Adapter {
  return { id, name: `adapter-${id}`, description: "", language: "python", adapter_type: "task", run_mode: "manual",
    latest_version_id: 1, batch_token: `saved-${id}`, created_at: "", updated_at: "", ...overrides };
}
function props(adapters: Adapter[]) {
  return { adapters, selectedId: null, busy: false, onSelect: vi.fn(), onCreate: vi.fn(), versionSeqById: new Map<number, number>(),
    workers: [], onOpenSettings: vi.fn(), onClone: vi.fn(), onRefresh: vi.fn(async () => {}),
    onBatchPrepare: vi.fn<(action: AdapterBatchAction, targets: AdapterBatchTarget[]) => Promise<void>>(async () => {}) };
}
function enter() { fireEvent.click(screen.getByTestId("batch-select-toggle")); }

it("keeps checkbox selection separate from navigation and prepares only the selected saved targets", () => {
  const values = props([adapter(1), adapter(2)]);
  render(<AdapterCatalog {...values} />);
  expect(screen.queryByRole("checkbox", { name: "选择 adapter-1" })).toBeNull();
  enter();
  expect((screen.getByRole("button", { name: "批量启动" }) as HTMLButtonElement).disabled).toBe(true);
  const checkbox = screen.getByRole("checkbox", { name: "选择 adapter-1" });
  checkbox.focus();
  expect(document.activeElement).toBe(checkbox);
  fireEvent.click(checkbox);
  expect(values.onSelect).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "批量启动" }));
  expect(values.onBatchPrepare).toHaveBeenCalledWith("start", [{ adapter_id: 1, expected_token: "saved-1" }]);
  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  expect(values.onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 2 }));
  fireEvent.click(screen.getByRole("button", { name: "清空" }));
  expect(screen.getByText("已选 0 个")).toBeTruthy();
  expect((screen.getByRole("button", { name: "批量停止" }) as HTMLButtonElement).disabled).toBe(true);
});

it("selects only visible editable items and does not resurrect filtered-out selections", async () => {
  const values = props([adapter(1, { name: "orders-a" }), adapter(2, { name: "orders-b" }), adapter(3, { name: "other" }), adapter(4, { name: "orders-readonly", access_level: "read" })]);
  render(<AdapterCatalog {...values} />);
  enter();
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "orders" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "全选当前列表" }));
  expect(screen.getByText("已选 2 个")).toBeTruthy();
  expect((screen.getByRole("checkbox", { name: "选择 orders-readonly" }) as HTMLInputElement).disabled).toBe(true);
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "orders-a" } });
  await waitFor(() => expect(screen.getByText("已选 1 个")).toBeTruthy());
  fireEvent.change(screen.getByTestId("adapter-search"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "批量停止" }));
  expect(values.onBatchPrepare).toHaveBeenCalledWith("stop", [{ adapter_id: 1, expected_token: "saved-1" }]);
});

it("clears archived or permission-revoked targets and refuses production rows without a token", () => {
  const values = props([adapter(1), adapter(2, { batch_token: undefined })]);
  const { rerender } = render(<AdapterCatalog {...values} />);
  enter();
  fireEvent.click(screen.getByRole("checkbox", { name: "全选当前列表" }));
  expect(screen.getByText("已选 1 个")).toBeTruthy();
  expect((screen.getByRole("checkbox", { name: "选择 adapter-2" }) as HTMLInputElement).disabled).toBe(true);
  rerender(<AdapterCatalog {...values} adapters={[adapter(1, { access_level: "read" }), adapter(2, { batch_token: undefined })]} />);
  expect(screen.getByText("已选 0 个")).toBeTruthy();
  rerender(<AdapterCatalog {...values} adapters={[adapter(1, { archived_at: "2026-10-25" }), adapter(2, { batch_token: undefined })]} />);
  expect(screen.queryByRole("checkbox", { name: "选择 adapter-1" })).toBeNull();
});

it("rejects over-limit select-visible without silently truncating, then permits an exact 100", () => {
  const values = props(Array.from({ length: 101 }, (_, i) => adapter(i + 1)));
  const { rerender } = render(<AdapterCatalog {...values} />);
  enter();
  fireEvent.click(screen.getByRole("checkbox", { name: "全选当前列表" }));
  expect(screen.getByRole("alert").textContent).toContain("每批最多 100 个");
  expect(screen.getByText("已选 0 个")).toBeTruthy();
  expect(values.onBatchPrepare).not.toHaveBeenCalled();
  rerender(<AdapterCatalog {...values} adapters={values.adapters.slice(0, 100)} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "全选当前列表" }));
  fireEvent.click(screen.getByRole("button", { name: "批量启动" }));
  expect(values.onBatchPrepare.mock.calls[0][1]).toHaveLength(100);
});

it("does not reinterpret an unsaved event-creation intent as a manual run", () => {
  render(<AdapterCatalog {...props([adapter(1)])} eventIntentAdapterId={1} eventSubscriptions={new Map()} />);
  enter();
  expect((screen.getByRole("checkbox", { name: "选择 adapter-1" }) as HTMLInputElement).disabled).toBe(true);
});
