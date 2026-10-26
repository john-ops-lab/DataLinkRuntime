import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Form } from "antd";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { applyUiLocale } from "../i18n";
import type { Adapter } from "../types";
import type { EventSubscription, SubscriptionEvent, SubscriptionSourceType } from "../subscription-types";
import { eventSourceLabel, eventSummary, eventWaitingLabel, httpIngressPath } from "../event-trigger";
import { subscriptionErrorMessage } from "../subscription-message";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import SubscriptionActionDialog from "./SubscriptionActionDialog";
import EventTriggerSettings from "./EventTriggerSettings";
import SubscriptionWorkspace from "./SubscriptionWorkspace";

const adapter: Adapter = { id: 9, name: "Intake", description: "", language: "python",
  adapter_type: "task", run_mode: "manual", latest_version_id: 7, runtime_worker_id: 3,
  access_level: "owner", created_at: "2026-10-26T00:00:00Z", updated_at: "2026-10-26T00:00:00Z" };
const sub = (values: Partial<EventSubscription> = {}): EventSubscription => ({
  id: 4, adapter_id: 9, name: "Intake", description: "", source_type: "http",
  desired_state: "disabled", connection_state: "stopped", delivery_state: "idle",
  config_version: 1, source_epoch: "one", activation_id: null, repair_revision_id: null,
  last_error_code: null, config: { source_type: "http", credential_id: 44, mode: "per_event",
    ordering: "receipt_serial", limits: {} }, quota: {}, limits: {}, global_limits: {},
  oldest_pending_at: null, event_counts: {}, available: true, access_level: "owner",
  ingress_path: "/api/ingress/public-route-one", created_at: "2026-10-26T00:00:00Z",
  updated_at: "2026-10-26T00:00:00Z", ...values,
});
const base = { adapterId: 9, canManage: true, canTest: true, onClose: vi.fn(),
  availableSources: ["http"] as SubscriptionSourceType[] };
const settingsProps = { adapter, sub: sub(), capabilities: { enabled: true, available_sources: ["http"] },
  readOnly: false, canManage: true, runtimeDirty: false, onChanged: vi.fn().mockResolvedValue(undefined),
  onRecords: vi.fn(), onDraftStateChange: vi.fn() };

beforeEach(async () => {
  await applyUiLocale("en");
  vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([
    { id: 44, name: "Sender token", type: "token" },
    { id: 45, name: "Other password", type: "password" },
  ] as Awaited<ReturnType<typeof api.listAdapterCredentialOptions>>);
});
afterEach(() => vi.restoreAllMocks());

it("saves an HTTP source with only a token reference and never starts receiving", async () => {
  const create = vi.spyOn(api, "createSubscription").mockResolvedValue(sub());
  const toggle = vi.spyOn(api, "subscriptionAction");
  render(<EventTriggerSettings {...settingsProps} sub={null} initialConfigure />);
  fireEvent.mouseDown(screen.getByLabelText("Receiving token"));
  fireEvent.click(await screen.findByText("Sender token (#44)"));
  expect(screen.queryByText("Other password (#45)")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(create).toHaveBeenCalledExactlyOnceWith({ adapter_id: 9,
    name: "Intake", description: "", config: { source_type: "http", credential_id: 44,
      mode: "per_event", ordering: "receipt_serial", limits: {} } }));
  expect(toggle).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("does not offer an HTTP source when the backend capability is off", async () => {
  render(<EventTriggerSettings {...settingsProps} sub={null} initialConfigure
    capabilities={{ enabled: true, available_sources: ["kafka", "future"] }} />);
  expect(screen.queryByLabelText("HTTP data push")).toBeNull();
  expect(screen.getByLabelText("Which messages should be received first?")).toBeTruthy();
});

it("requires a token and guards dirty cancellation without saving", async () => {
  const save = vi.fn(), close = vi.fn();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<SubscriptionConfigDialog {...base} onClose={close} onSave={save} />);
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await screen.findByText("Required");
  expect(save).not.toHaveBeenCalled();
  fireEvent.mouseDown(screen.getByLabelText("Receiving token"));
  fireEvent.click(await screen.findByText("Sender token (#44)"));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(close).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(close).toHaveBeenCalledOnce();
  expect(save).not.toHaveBeenCalled();
});

it("cannot switch or close an HTTP form during save", async () => {
  let finish: () => void = () => {};
  const save = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
  const close = vi.fn();
  render(<SubscriptionConfigDialog {...base} subscription={sub()} onClose={close} onSave={save} />);
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("Receiving token") as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(close).not.toHaveBeenCalled();
  await act(async () => { finish(); });
  expect(close).toHaveBeenCalledOnce();
});

it.each([false, true])("keeps checking busy but clears only the persisted HTTP draft after check failure (existing=%s)", async existing => {
  const saved = sub({ config_version: existing ? 2 : 1, config: { source_type: "http", credential_id: 44,
    mode: "per_event", ordering: "receipt_serial", limits: { pending_count: 10, rate: 20 } } });
  vi.spyOn(api, "createSubscription").mockResolvedValue(saved);
  vi.spyOn(api, "updateSubscription").mockResolvedValue(saved);
  let failCheck: (reason: unknown) => void = () => {};
  const check = vi.spyOn(api, "testSubscriptionConnection").mockImplementation(() => new Promise((_resolve, reject) => {
    failCheck = reject;
  }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const draft = vi.fn();
  render(<EventTriggerSettings {...settingsProps} sub={existing ? sub() : null} initialConfigure={!existing}
    onDraftStateChange={draft} />);
  if (existing) fireEvent.click(screen.getByRole("button", { name: "Change source" }));
  else {
    fireEvent.mouseDown(screen.getByLabelText("Receiving token"));
    fireEvent.click(await screen.findByText("Sender token (#44)"));
  }
  fireEvent.change(screen.getByLabelText("Capacity"), { target: { value: ' { "rate": 20, "pending_count": 10 } ' } });
  fireEvent.click(screen.getByRole("button", { name: "Save and check configuration" }));
  await waitFor(() => expect(check).toHaveBeenCalledExactlyOnceWith(4, saved.config_version));
  expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: /Save and check configuration/ }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("Receiving token") as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(confirm).not.toHaveBeenCalled();
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: false, busy: true }));
  await act(async () => { failCheck(new ApiError(409, "source_not_available", "private upstream detail")); });
  await screen.findByText(subscriptionErrorMessage(new Error("check failed"), "http"));
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: false, busy: false }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(confirm).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("keeps the HTTP draft dirty when saving itself fails", async () => {
  const failure = new ApiError(409, "subscription_configuration_stale", "private upstream detail");
  vi.spyOn(api, "updateSubscription").mockRejectedValue(failure);
  const check = vi.spyOn(api, "testSubscriptionConnection");
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const draft = vi.fn();
  render(<EventTriggerSettings {...settingsProps} onDraftStateChange={draft} />);
  fireEvent.click(screen.getByRole("button", { name: "Change source" }));
  fireEvent.change(screen.getByLabelText("Capacity"), { target: { value: '{"rate":20}' } });
  fireEvent.click(screen.getByRole("button", { name: "Save and check configuration" }));
  await screen.findByText(subscriptionErrorMessage(failure, "http"));
  expect(check).not.toHaveBeenCalled();
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: true, busy: false }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(confirm).toHaveBeenCalledExactlyOnceWith("There are unsaved changes. Discard them?");
  expect(screen.getByRole("dialog")).toBeTruthy();
});

it.each([false, true])("does not clear a programmatically changed HTTP draft that differs from the saved snapshot (edited=%s)", async edited => {
  const originalUseForm = Form.useForm;
  let currentForm: ReturnType<typeof Form.useForm>[0] | undefined;
  vi.spyOn(Form, "useForm").mockImplementation(form => {
    const result = originalUseForm(form);
    currentForm = result[0];
    return result;
  });
  const savedLimits: Record<string, number> = edited ? { rate: 20 } : {};
  vi.spyOn(api, "updateSubscription").mockResolvedValue(sub({ config_version: 2,
    config: { source_type: "http", credential_id: 44, mode: "per_event", ordering: "receipt_serial", limits: savedLimits } }));
  const failure = new ApiError(409, "source_not_available", "private upstream detail");
  let failCheck: (reason: unknown) => void = () => {};
  const check = vi.spyOn(api, "testSubscriptionConnection").mockImplementation(() => new Promise((_resolve, reject) => {
    failCheck = reject;
  }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const draft = vi.fn();
  render(<EventTriggerSettings {...settingsProps} onDraftStateChange={draft} />);
  fireEvent.click(screen.getByRole("button", { name: "Change source" }));
  if (edited) fireEvent.change(screen.getByLabelText("Capacity"), { target: { value: '{"rate":20}' } });
  fireEvent.click(screen.getByRole("button", { name: "Save and check configuration" }));
  await waitFor(() => expect(check).toHaveBeenCalledOnce());
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: false, busy: true }));
  expect(currentForm).toBeDefined();
  await act(async () => { currentForm!.setFieldsValue({ mode: undefined }); });
  expect(draft).toHaveBeenLastCalledWith({ dirty: false, busy: true });
  await act(async () => { currentForm!.setFieldsValue({ limits: '{"rate":30}' }); });
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: true, busy: true }));
  await act(async () => { failCheck(failure); });
  await screen.findByText(subscriptionErrorMessage(new Error("check failed"), "http"));
  expect((screen.getByLabelText("Capacity") as HTMLTextAreaElement).value).toBe('{"rate":30}');
  await waitFor(() => expect(draft).toHaveBeenLastCalledWith({ dirty: true, busy: false }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(screen.getByRole("dialog")).toBeTruthy();
});

it("preserves the existing credential reference for read-only viewing", () => {
  const save = vi.fn();
  render(<SubscriptionConfigDialog {...base} subscription={sub()} readOnly canManage={false} onSave={save} />);
  expect((screen.getByLabelText("Receiving token") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "Save and check configuration" })).toBeNull();
  expect(api.listAdapterCredentialOptions).not.toHaveBeenCalled();
});

it.each(['{"rate":0}', '{"global":{"rate":999}}'])("rejects invalid entry limits %s", async limits => {
  const save = vi.fn();
  render(<SubscriptionConfigDialog {...base} subscription={sub()} onSave={save} />);
  fireEvent.change(screen.getByLabelText("Capacity"), { target: { value: limits } });
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await screen.findByText("Enter a JSON object of supported positive integer limits within the deployment limits.");
  expect(save).not.toHaveBeenCalled();
});

it("checks saved configuration without implying a real sender or business test", async () => {
  const check = vi.spyOn(api, "testSubscriptionConnection").mockResolvedValue({ source_type: "http",
    cluster_id: "http", identity_verified: true });
  render(<EventTriggerSettings {...settingsProps} />);
  fireEvent.click(screen.getByRole("button", { name: "Check receiving configuration" }));
  await screen.findByText("Receiving configuration and token reference checked. External delivery and business processing have not been tested.");
  expect(check).toHaveBeenCalledExactlyOnceWith(4, 1);
  expect(screen.queryByText(/topics.*partitions/)).toBeNull();
});

it("shows a public receiving path and oldest waiting count in the adapter settings", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-26T00:02:01Z"));
  render(<EventTriggerSettings {...settingsProps} sub={sub({ oldest_pending_at: "2026-10-26T00:00:00Z",
    quota: { pending_count: 12 }, event_counts: { processing: 1, failed: 0 } })} />);
  expect(screen.getByText("/api/ingress/public-route-one")).toBeTruthy();
  expect(screen.getByText("Oldest waiting: 2 min")).toBeTruthy();
  expect(screen.getByText("Processing 1")).toBeTruthy();
  expect(screen.getByText("Waiting: 12")).toBeTruthy();
});

it("keeps HTTP records in the existing workspace and technical details", async () => {
  vi.spyOn(api, "getSubscription").mockResolvedValue(sub());
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({ items: [], total: 0, next_before_id: null });
  render(<SubscriptionWorkspace id={4} adapter={adapter} capabilities={settingsProps.capabilities} onDeleted={vi.fn()} />);
  await screen.findByText("/api/ingress/public-route-one");
  expect(screen.getByText("Technical details")).toBeTruthy();
  expect(screen.getByText("Change receiving address")).toBeTruthy();
  expect(screen.getByText("Receiving limit (events/s)", { exact: false })).toBeTruthy();
});

it("requires explicit address-change confirmation and preserves the saved source config", async () => {
  const reset = vi.spyOn(api, "resetSubscription").mockResolvedValue(sub({ ingress_path: "/api/ingress/next" }));
  render(<SubscriptionActionDialog action="reset" sub={sub()} onClose={vi.fn()} onDone={vi.fn().mockResolvedValue(undefined)} />);
  expect(screen.getByText(/The old address only reads existing records/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Disposition reason"), { target: { value: "new sender deployment" } });
  fireEvent.click(screen.getByRole("button", { name: "Change receiving address" }));
  await screen.findByText("Required");
  expect(reset).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("I understand the address will change and will update the sender."));
  fireEvent.click(screen.getByRole("button", { name: "Change receiving address" }));
  await waitFor(() => expect(reset).toHaveBeenCalledOnce());
  expect(reset.mock.calls[0][1].config).toEqual(sub().config);
});

it("localizes HTTP conflicts without claiming that other data is blocked", async () => {
  const error = new ApiError(409, "event_identity_conflict", "upstream secret text");
  expect(subscriptionErrorMessage(error, "http")).toContain("Other data can still be received");
  expect(subscriptionErrorMessage(error, "http")).not.toContain("upstream secret text");
  await applyUiLocale("zh-CN");
  expect(subscriptionErrorMessage(error, "http")).toContain("其他数据仍可正常接收");
});

it("summarizes HTTP IDs and rejects a server-supplied absolute/private address", () => {
  expect(eventSourceLabel(sub())).toBe("HTTP");
  expect(httpIngressPath(sub({ ingress_path: "http://private.invalid:123/api/ingress/x" }))).toBeNull();
  expect(eventWaitingLabel(sub(), key => key)).toBe("ux.noWaiting");
  const event = { id: 1, source_event_id: "sender-id", envelope: { source: { type: "http" } } } as unknown as SubscriptionEvent;
  const translate = vi.fn(() => "summary");
  expect(eventSummary(event, translate)).toBe("summary");
  expect(translate).toHaveBeenCalledWith("http.eventSummary", { id: "sender-id" });
});
