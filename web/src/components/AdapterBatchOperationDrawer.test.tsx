import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { applyUiLocale } from "../i18n";
import type { AdapterBatchItem } from "../adapter-batch-types";
import type { AdapterBatchController } from "../hooks/useAdapterBatchOperation";
import AdapterBatchOperationDrawer from "./AdapterBatchOperationDrawer";

function item(overrides: Partial<AdapterBatchItem> = {}): AdapterBatchItem {
  return { adapter_id: 1, adapter_name: "orders", mode: "manual", action: "run_once", status: "pending", error_code: null,
    retryable: false, execution_ids: [], executions: [], subscription_id: null, subscription_state: null, ...overrides };
}
function controller(items: AdapterBatchItem[], overrides: Partial<AdapterBatchController> = {}): AdapterBatchController {
  return { operationId: "cc000000-0000-4000-8000-000000000001", operation: { id: "cc000000-0000-4000-8000-000000000001", action: "start", created_at: "", items },
    open: true, working: null, querying: false, unknown: false, missing: false, error: null, canPrepare: true,
    prepare: vi.fn(), execute: vi.fn(), retry: vi.fn(), query: vi.fn(), recoverPreparation: null, show: vi.fn(), close: vi.fn(), ...overrides };
}
afterEach(async () => { await applyUiLocale("zh-CN"); });

it("previews concrete mixed actions and only executes after confirmation", () => {
  const controls = controller([
    item(), item({ adapter_id: 2, adapter_name: "scheduled", mode: "schedule", action: "enable_schedule" }),
    item({ adapter_id: 3, adapter_name: "hook", mode: "webhook", action: "enable_webhook" }),
    item({ adapter_id: 4, adapter_name: "events", mode: "subscription", action: "enable_subscription" }),
  ]);
  render(<AdapterBatchOperationDrawer controller={controls} onInspect={vi.fn()} />);
  expect(screen.getByText("使用已保存内容运行一次")).toBeTruthy();
  expect(screen.getByText("启用已有定时，不额外立即运行")).toBeTruthy();
  expect(screen.getByText("开始接收 Webhook 请求")).toBeTruthy();
  expect(screen.getByText("开始接收来源事件")).toBeTruthy();
  expect(controls.execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认执行（4 项）" }));
  expect(controls.execute).toHaveBeenCalledTimes(1);
});

it("separates accepted execution, real cancellation, natural completion and listener shutdown", () => {
  const controls = controller([
    item({ status: "accepted", executions: [{ id: 11, status: "queued", cancel_requested: false }] }),
    item({ adapter_id: 2, action: "cancel_executions", status: "succeeded", executions: [{ id: 12, status: "cancelled", cancel_requested: true }] }),
    item({ adapter_id: 3, action: "cancel_executions", status: "succeeded", executions: [{ id: 13, status: "succeeded", cancel_requested: false }] }),
    item({ adapter_id: 4, action: "disable_subscription", mode: "subscription", status: "stopping", subscription_state: "stopping" }),
  ]);
  const { rerender } = render(<AdapterBatchOperationDrawer controller={controls} onInspect={vi.fn()} />);
  expect(within(screen.getByTestId("batch-item-1")).getByText("已提交执行")).toBeTruthy();
  expect(within(screen.getByTestId("batch-item-1")).queryByText("动作已完成")).toBeNull();
  expect(within(screen.getByTestId("batch-item-2")).getByText("选定执行已取消")).toBeTruthy();
  expect(within(screen.getByTestId("batch-item-3")).getByText("选定执行已结束")).toBeTruthy();
  expect(within(screen.getByTestId("batch-item-4")).getByText("正在停止")).toBeTruthy();
  rerender(<AdapterBatchOperationDrawer controller={controller([item({ adapter_id: 4, action: "disable_subscription", mode: "subscription", status: "succeeded", subscription_state: "stopped" })])} onInspect={vi.fn()} />);
  expect(within(screen.getByTestId("batch-item-4")).getByText("已停止接收")).toBeTruthy();
});

it("blocks all retry and execute controls when results are unknown, but permits readback", () => {
  const controls = controller([item(), item({ adapter_id: 2, status: "failed", retryable: true })], { unknown: true });
  render(<AdapterBatchOperationDrawer controller={controls} onInspect={vi.fn()} />);
  expect((screen.getByRole("button", { name: "确认执行（1 项）" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "重试失败项（1 项）" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "重新查询" }));
  expect(controls.query).toHaveBeenCalledTimes(1);
});

it("counts an unconfirmed native close and gives its localized reason without claiming success", () => {
  render(<AdapterBatchOperationDrawer controller={controller([item({ action: "disable_subscription", mode: "subscription",
    status: "unknown", error_code: "source_close_unconfirmed" })])} onInspect={vi.fn()} />);
  expect(screen.getByRole("status").textContent).toContain("待确认 1");
  expect(screen.getByText("无法确认监听是否已关闭。请到适配器的运行设置检查，确认前不要重新开启接收。")).toBeTruthy();
  expect(screen.queryByText("已停止接收")).toBeNull();
});

it("renders safe permission failures without details and readable English actions and errors", async () => {
  await applyUiLocale("en");
  const inspect = vi.fn();
  render(<AdapterBatchOperationDrawer controller={controller([
    item({ adapter_name: null, mode: null, action: null, status: "failed", error_code: "batch_result_unavailable" }),
    item({ adapter_id: 2, adapter_name: "orders", mode: "schedule", action: "disable_schedule", status: "failed", error_code: "batch_config_conflict" }),
  ])} onInspect={inspect} />);
  const hidden = within(screen.getByTestId("batch-item-1"));
  expect(hidden.getByText("Adapter no longer accessible")).toBeTruthy();
  expect(hidden.queryByRole("button")).toBeNull();
  expect(screen.getByText("Disable scheduling; existing executions continue")).toBeTruthy();
  expect(screen.getByText("Saved configuration changed. Refresh the list, review it, and create a new operation.")).toBeTruthy();
  expect(screen.queryByText("batch_config_conflict")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "View adapter" }));
  expect(inspect).toHaveBeenCalledWith(2);
});
