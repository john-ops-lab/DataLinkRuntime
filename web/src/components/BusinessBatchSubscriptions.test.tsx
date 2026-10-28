import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { applyUiLocale } from "../i18n";
import { subscriptionErrorMessage } from "../subscription-message";
import type { Adapter } from "../types";
import type { EventSubscription, SubscriptionEvent, SubscriptionSourceType } from "../subscription-types";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import SubscriptionWorkspace, { EventProcessingState } from "./SubscriptionWorkspace";
import ExecutionBatchNotice from "./ExecutionBatchNotice";

vi.mock("./ExecutionHistoryPanel", () => ({ default: () => <div>Execution details</div> }));

const adapter = { id: 9, name: "Batch task", language: "python", adapter_type: "task", run_mode: "manual",
  access_level: "owner", latest_version_id: 7 } as Adapter;
function subscription(source: "http" | "rabbitmq" | "rocketmq" = "http"): EventSubscription {
  const common = { mode: "per_event" as const, ordering: "receipt_serial" as const, limits: {}, credential_id: 44 };
  return { id: 4, adapter_id: 9, name: "Batch task", description: "", source_type: source,
    config: source === "http" ? { ...common, source_type: "http" }
      : source === "rabbitmq" ? { ...common, source_type: "rabbitmq", cluster_id: "fixture", server: "amqp://broker.example:5672", vhost: "/", queue: "events" }
      : { ...common, source_type: "rocketmq", cluster_id: "fixture", server: "grpc://proxy.example:8081", topic: "events", group_id: "consumer" },
    desired_state: "disabled", connection_state: "stopped", delivery_state: "idle", config_version: 1,
    source_epoch: "epoch", activation_id: 8, repair_revision_id: null, last_error_code: null,
    quota: {}, limits: {}, global_limits: {}, event_counts: {}, oldest_pending_at: null,
    available: true, access_level: "owner", created_at: "2026-10-26T00:00:00Z", updated_at: "2026-10-26T00:00:00Z" };
}
const event = (outcome: "unknown" | "not_run"): SubscriptionEvent => ({ id: 12, activation_id: 8,
  source_event_id: "source-12", source_epoch: "epoch", state: "failed", shard: "http", sequence: 12,
  envelope: null, payload_expired: false, first_received_at: "2026-10-26T00:00:00Z", duplicate_count: 0,
  processing_mode: "business_batch", batch_execution_id: 71, batch_member_count: 8,
  batch_member_index: 2, batch_member_outcome: outcome, batch_member_code: outcome === "unknown" ? "target_commit_unknown" : "predecessor_failed",
  deliveries: [{ id: 30, ordinal: 1, execution_id: 71, state: "failed", revision_override_id: null,
    activation_override_id: null, effective_activation_id: 8, processing_mode: "business_batch",
    batch_execution_id: 71, batch_member_index: 2, batch_member_count: 8, batch_member_outcome: outcome }],
});

beforeEach(async () => {
  await applyUiLocale("en");
  vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([
    { id: 44, name: "Source credential", type: "token" },
  ] as Awaited<ReturnType<typeof api.listAdapterCredentialOptions>>);
});
afterEach(() => vi.restoreAllMocks());

it.each([
  ["en", "http"], ["en", "rabbitmq"], ["zh-CN", "http"], ["zh-CN", "rabbitmq"],
] as const)("requires an explicit advanced selection for %s %s and does not save code", async (locale, source) => {
  await applyUiLocale(locale);
  const save = vi.fn().mockResolvedValue(undefined);
  const saveCode = vi.spyOn(api, "saveVersion");
  render(<SubscriptionConfigDialog subscription={subscription(source)} availableSources={[source]}
    adapterId={9} canManage canTest onClose={vi.fn()} onSave={save} />);
  const notice = locale === "en" ? "Save code that supports multiple inputs before starting reception." : "请先保存能够处理多条输入的代码，再开启接收。";
  expect(screen.queryByText(notice)).toBeNull();
  fireEvent.mouseDown(screen.getByLabelText(locale === "en" ? "Processing mode" : "处理方式"));
  fireEvent.click(await screen.findByText(locale === "en" ? "Several events per run (code support required)" : "一次处理多条（代码需支持）"));
  await screen.findByText(notice);
  expect(screen.getByText('{"event_batch_schema":"dlr.event.batch.v1"}')).toBeTruthy();
  expect(screen.getByText(locale === "en" ? /Unclassified exceptions leave the result unconfirmed/ : /无法确认的异常按结果未确认处理/)).toBeTruthy();
  const example = screen.getByText(/async def process_one/).textContent;
  expect(example).toContain('status="unknown", error_code="result_unavailable"');
  expect(example).toContain('status="not_run", error_code="predecessor_failed"');
  expect(example).not.toContain('status="failed"');
  expect(example).toContain("except Exception:");
  expect(example).not.toContain("except BaseException:");
  fireEvent.click(screen.getByRole("button", { name: locale === "en" ? /^Save$/ : /^保\s*存$/ }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0].config).toMatchObject({ source_type: source, mode: "business_batch" });
  expect(saveCode).not.toHaveBeenCalled();
});

it("keeps per-event defaults and does not expose business batches for RocketMQ", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<SubscriptionConfigDialog subscription={subscription("rocketmq")} availableSources={["rocketmq"]}
    adapterId={9} canManage onClose={vi.fn()} onSave={save} />);
  expect(screen.queryByLabelText("Processing mode")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0].config.mode).toBe("per_event");
});

it("keeps an existing batch mode visible but read-only", () => {
  const sub = subscription(); sub.config.mode = "business_batch";
  const save = vi.fn();
  render(<SubscriptionConfigDialog subscription={sub} availableSources={["http"] as SubscriptionSourceType[]}
    adapterId={9} canManage={false} readOnly onClose={vi.fn()} onSave={save} />);
  expect((screen.getByLabelText("Processing mode") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement).disabled).toBe(true);
});

it.each([
  ["en", "unknown", "Result unconfirmed"], ["en", "not_run", "Not processed"],
  ["zh-CN", "unknown", "结果未确认"], ["zh-CN", "not_run", "尚未处理"],
] as const)("distinguishes %s member %s without claiming executed failure", async (locale, outcome, label) => {
  await applyUiLocale(locale);
  render(<EventProcessingState event={event(outcome)} />);
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.queryByText(locale === "en" ? "Processing failed" : "处理失败")).toBeNull();
});

it.each([
  ["en", "failed", "Processing failed", "Skipped"],
  ["en", "unknown", "Result unconfirmed", "Skipped"],
  ["en", "not_run", "Not processed", "Skipped"],
  ["zh-CN", "failed", "处理失败", "已跳过"],
  ["zh-CN", "unknown", "结果未确认", "已跳过"],
  ["zh-CN", "not_run", "尚未处理", "已跳过"],
] as const)("keeps skipped %s member %s visible alongside its original result", async (locale, outcome, resultLabel, skippedLabel) => {
  await applyUiLocale(locale);
  const view = render(<EventProcessingState event={{ state: "failed", batch_member_outcome: outcome }} />);
  expect(screen.getByText(resultLabel)).toBeTruthy();
  expect(screen.queryByText(skippedLabel)).toBeNull();
  view.rerender(<EventProcessingState event={{ state: "skipped", batch_member_outcome: outcome }} />);
  expect(screen.getByText(skippedLabel)).toBeTruthy();
  expect(screen.getByText(resultLabel)).toBeTruthy();
  expect(screen.queryByText(locale === "en" ? "Skipped unprocessed" : "未处理跳过")).toBeNull();
});

it.each(["en", "zh-CN"] as const)("shows skipped state and original result in the %s event list, detail and delivery history", async locale => {
  await applyUiLocale(locale);
  const original = event("unknown");
  const skipped: SubscriptionEvent = { ...original, state: "skipped",
    deliveries: original.deliveries.map(delivery => ({ ...delivery, state: "skipped" })) };
  vi.spyOn(api, "getSubscription").mockResolvedValue(subscription());
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({ items: [skipped], total: 1, next_before_id: null });
  vi.spyOn(api, "getSubscriptionEvent").mockResolvedValue(skipped);
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  render(<SubscriptionWorkspace id={4} adapter={adapter} capabilities={{ enabled: true, available_sources: ["http"] }} onDeleted={vi.fn()} />);
  const skippedLabel = locale === "en" ? "Skipped" : "已跳过";
  const resultLabel = locale === "en" ? "Result unconfirmed" : "结果未确认";
  await screen.findByText(skippedLabel);
  expect(screen.getByText(resultLabel)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: locale === "en" ? "View event" : "查看事件" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getAllByText(skippedLabel)).toHaveLength(2);
  expect(within(dialog).getAllByText(resultLabel)).toHaveLength(2);
  expect(within(dialog).getByText(locale === "en"
    ? /The result is unconfirmed and the target may already have been written/
    : /本条的处理结果尚未确认，可能已经写入目标/)).toBeTruthy();
});

it("shows member outcome and shared cancellation scope in received records", async () => {
  vi.spyOn(api, "getSubscription").mockResolvedValue(subscription());
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({ items: [event("unknown")], total: 1, next_before_id: null });
  vi.spyOn(api, "getSubscriptionEvent").mockResolvedValue(event("unknown"));
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  render(<SubscriptionWorkspace id={4} adapter={adapter} capabilities={{ enabled: true, available_sources: ["http"] }} onDeleted={vi.fn()} />);
  await screen.findByText("This run processes 8 events");
  expect(screen.getByText("Result unconfirmed")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "View event" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/The result is unconfirmed and the target may already have been written/)).toBeTruthy();
  expect(within(dialog).getByText(/Cancelling it affects every unfinished event/)).toBeTruthy();
  expect(within(dialog).getByText("Event 3 in this run")).toBeTruthy();
});

it("shows cancellation scope from an immutable batch input, never arbitrary output", () => {
  const view = render(<ExecutionBatchNotice execution={{ trigger: "subscription", input: { schema: "dlr.event.batch.v1", members: [{}, {}, {}] } }} />);
  expect(screen.getByText("This run processes 3 events")).toBeTruthy();
  expect(screen.getByText(/Cancelling it affects every unfinished event/)).toBeTruthy();
  view.rerender(<ExecutionBatchNotice execution={{ trigger: "manual", input: { schema: "dlr.event.batch.v1", members: [{}, {}, {}] } }} />);
  expect(screen.queryByTestId("execution-business-batch-notice")).toBeNull();
  view.rerender(<ExecutionBatchNotice execution={{ trigger: "subscription", input: null }} />);
  expect(screen.queryByTestId("execution-business-batch-notice")).toBeNull();
});

it.each(["en", "zh-CN"] as const)("explains code support and cross-mode replay errors in %s", async locale => {
  await applyUiLocale(locale);
  const support = subscriptionErrorMessage(new ApiError(409, "subscription_batch_code_required", "unsafe detail"));
  const mode = subscriptionErrorMessage(new ApiError(409, "subscription_replay_mode_conflict", "unsafe detail"));
  expect(support).toContain(locale === "en" ? "supports multi-event input" : "支持多条输入");
  expect(mode).toContain(locale === "en" ? "original runtime settings" : "原运行配置");
  expect(support + mode).not.toContain("unsafe detail");
});
