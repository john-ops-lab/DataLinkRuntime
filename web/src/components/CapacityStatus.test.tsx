import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { eventCapacityPaused, eventNeedsAttention, eventStatusKey } from "../event-trigger";
import { applyUiLocale } from "../i18n";
import type { Adapter } from "../types";
import type { EventSubscription } from "../subscription-types";
import SubscriptionWorkspace from "./SubscriptionWorkspace";

vi.mock("./ExecutionHistoryPanel", () => ({ default: () => <div>Execution details</div> }));

const adapter = { id: 9, name: "Orders", language: "python", adapter_type: "task", run_mode: "manual",
  access_level: "owner", latest_version_id: 7 } as Adapter;
const subscription = (values: Partial<EventSubscription> = {}): EventSubscription => ({
  id: 4, adapter_id: 9, name: "Orders", description: "", source_type: "http",
  config: { source_type: "http", mode: "per_event", ordering: "receipt_serial", limits: {}, credential_id: 44 },
  desired_state: "enabled", connection_state: "listening", delivery_state: "idle", config_version: 1,
  source_epoch: "epoch", activation_id: 8, repair_revision_id: null, last_error_code: null,
  quota: { history_count: 6 }, limits: { history_count: 6 }, global_limits: {}, event_counts: {}, oldest_pending_at: null,
  available: true, access_level: "owner", created_at: "2026-10-26T00:00:00Z", updated_at: "2026-10-26T00:00:00Z",
  ...values,
});

beforeEach(async () => { await applyUiLocale("en"); });
afterEach(() => vi.restoreAllMocks());

it("uses the optional projection for attention and keeps old backend responses compatible", () => {
  expect(eventStatusKey(subscription())).toBe("receiving");
  expect(eventNeedsAttention(subscription())).toBe(false);
  const paused = subscription({ capacity_paused: true });
  expect(eventCapacityPaused(paused)).toBe(true);
  expect(eventNeedsAttention(paused)).toBe(true);
  expect(eventStatusKey(paused)).toBe("capacityPaused");
});

it.each([
  [{ desired_state: "disabled", connection_state: "stopped" }, "stopped"],
  [{ desired_state: "disabled", connection_state: "stopping" }, "stopping"],
  [{ delivery_state: "repair" }, "repairing"],
  [{ delivery_state: "blocked" }, "needsAttention"],
  [{ connection_state: "blocked" }, "connectionProblem"],
  [{ connection_state: "reconnecting" }, "reconnecting"],
] as const)("preserves lifecycle and failure precedence: %s", (values, expected) => {
  const sub = subscription({ capacity_paused: true, ...values });
  expect(eventCapacityPaused(sub)).toBe(false);
  expect(eventStatusKey(sub)).toBe(expected);
});

function renderWorkspace(sub: EventSubscription) {
  vi.spyOn(api, "getSubscription").mockResolvedValue(sub);
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  return render(<SubscriptionWorkspace id={4} adapter={adapter}
    capabilities={{ enabled: true, available_sources: ["http"] }} onDeleted={vi.fn()} />);
}

it.each([
  ["en", "Waiting for capacity", "View capacity details", "Fix code"],
  ["zh-CN", "额度不足，暂缓接收", "查看容量详情", "修复代码"],
] as const)("gives a capacity action without code repair in %s", async (locale, status, action, fix) => {
  await applyUiLocale(locale);
  const view = renderWorkspace(subscription({ capacity_paused: true }));
  await screen.findByRole("button", { name: action });
  expect(screen.getAllByText(status).length).toBeGreaterThan(0);
  expect(screen.getByText(locale === "en"
    ? /Received messages continue processing and existing records are retained/
    : /已接收的消息继续处理，既有记录保持保留/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: fix })).toBeNull();
  const details = view.container.querySelector("details.event-technical-details") as HTMLDetailsElement;
  expect(details.open).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: action }));
  expect(details.open).toBe(true);
});

it("keeps genuine repair guidance when capacity is also exhausted", async () => {
  renderWorkspace(subscription({ capacity_paused: true, delivery_state: "repair" }));
  await screen.findByRole("button", { name: "Fix code" });
  expect(screen.queryByRole("button", { name: "View capacity details" })).toBeNull();
  expect(screen.getAllByText("Repair in progress").length).toBeGreaterThan(0);
});
