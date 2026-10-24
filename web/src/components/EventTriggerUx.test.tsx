import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Adapter } from "../types";
import type { EventSubscription } from "../subscription-types";
import { eventNeedsAttention, eventStatusKey } from "../event-trigger";
import { loadEventSubscriptionCatalog } from "../hooks/useEventSubscriptions";
import { EventReceivingButton } from "./EventTriggerSettings";
import LegacySubscriptionRoute from "./LegacySubscriptionRoute";
import AdapterCatalog from "./AdapterCatalog";

const adapter = { id: 41, name: "订单同步", description: "", language: "python", adapter_type: "task", run_mode: "manual", latest_version_id: 9, runtime_worker_id: 3 } as Adapter;
const sub = (values: Partial<EventSubscription> = {}): EventSubscription => ({
  id: 7, adapter_id: 41, name: "", description: "", source_type: "kafka", desired_state: "disabled", connection_state: "stopped", delivery_state: "idle", config_version: 1, source_epoch: "example", activation_id: 1, repair_revision_id: null, last_error_code: null,
  config: { source_type: "kafka", cluster_id: "example", bootstrap_servers: ["broker.example.test:9093"], topics: ["orders"], initial_position: "latest", security_protocol: "SSL", mode: "per_event", ordering: "partition_serial", limits: {} },
  quota: { pending_count: 0 }, limits: {}, global_limits: {}, oldest_pending_at: null, event_counts: {}, available: true, created_at: "", updated_at: "", ...values,
});
afterEach(() => vi.restoreAllMocks());

it("loads the next authorized page before publishing the adapter catalog", async () => {
  const list = vi.spyOn(api, "listSubscriptions").mockResolvedValueOnce({ items: [sub()], total: 2, next_before_id: 7 })
    .mockResolvedValueOnce({ items: [sub({ id: 6, adapter_id: 42 })], total: 2, next_before_id: null });
  expect([...(await loadEventSubscriptionCatalog()).keys()]).toEqual([41, 42]);
  expect(list).toHaveBeenLastCalledWith({ limit: 100, before_id: 7 });
});
it("rejects partial or repeated-cursor catalogs instead of reporting absence", async () => {
  vi.spyOn(api, "listSubscriptions").mockResolvedValue({ items: [sub()], total: 2, next_before_id: 7 });
  await expect(loadEventSubscriptionCatalog()).rejects.toThrow("pagination cursor");
});
it("keeps stopped reception and remaining work distinct, and ignores resolved historical errors", () => {
  const stopped = sub({ delivery_state: "draining", quota: { pending_count: 28 } });
  expect(eventStatusKey(stopped)).toBe("stoppedWithWork");
  expect(eventNeedsAttention(sub({ last_error_code: "old_error", event_counts: { succeeded: 4 } }))).toBe(false);
  expect(eventNeedsAttention(sub({ delivery_state: "blocked" }))).toBe(true);
});
it("does not allow a disabled source with old work to be enabled", () => {
  render(<EventReceivingButton adapter={adapter} sub={sub({ delivery_state: "draining", quota: { pending_count: 28 } })}
    capabilities={{ enabled: true, available_sources: ["kafka"] }} dirty={false} busy={false} readOnly={false}
    onChanged={vi.fn()} onDraftStateChange={vi.fn()} />);
  expect((screen.getByTestId("header-event-toggle") as HTMLButtonElement).disabled).toBe(true);
});
it("keeps stopping reception available when a catalog refresh failed", () => {
  render(<EventReceivingButton adapter={adapter} sub={sub({ desired_state: "enabled", connection_state: "listening" })}
    capabilities={{ enabled: false, available_sources: [] }} dirty statusPending busy={false} readOnly={false}
    onChanged={vi.fn()} onDraftStateChange={vi.fn()} />);
  expect((screen.getByTestId("header-event-toggle") as HTMLButtonElement).disabled).toBe(false);
});
it("filters current problems inside the original adapter catalog", () => {
  render(<AdapterCatalog adapters={[adapter, { ...adapter, id: 42, name: "历史已处理" }]} selectedId={null} busy={false}
    onSelect={vi.fn()} onCreate={vi.fn()} versionSeqById={new Map()} workers={[]} onOpenSettings={vi.fn()}
    onClone={vi.fn()} onRefresh={vi.fn()} eventSubscriptions={new Map([[41, sub({ delivery_state: "blocked" })], [42, sub({ adapter_id: 42, last_error_code: "old_error" })]])} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "只看需要处理" }));
  expect(screen.getAllByTestId("adapter-item")).toHaveLength(1);
  expect(screen.getByTestId("adapter-item").textContent).toContain("订单同步");
});
it("resolves old detail bookmarks only after authorized subscription and adapter reads", async () => {
  vi.spyOn(api, "getSubscription").mockRejectedValue(new Error("forbidden"));
  const getAdapter = vi.spyOn(api, "getAdapter");
  const onOpen = vi.fn();
  render(<LegacySubscriptionRoute id={7} onOpen={onOpen} onBack={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(getAdapter).not.toHaveBeenCalled();
  expect(onOpen).not.toHaveBeenCalled();
});
