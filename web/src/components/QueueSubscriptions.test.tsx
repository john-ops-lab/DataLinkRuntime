import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { subscriptionErrorMessage } from "../subscription-message";
import { applyUiLocale } from "../i18n";
import type { SubscriptionSourceType } from "../subscription-types";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import { eventSourceLabel, eventSummary } from "../event-trigger";
import type { EventSubscription, SubscriptionEvent } from "../subscription-types";

beforeEach(async () => {
  await applyUiLocale("en");
  vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([
    { id: 31, name: "Rabbit source account", type: "password" },
    { id: 32, name: "Rocket source key", type: "access_key" },
  ] as Awaited<ReturnType<typeof api.listAdapterCredentialOptions>>);
});
afterEach(() => vi.restoreAllMocks());

const base = { adapterId: 9, canManage: true, canTest: true, onClose: vi.fn(),
  availableSources: ["rabbitmq", "rocketmq"] as SubscriptionSourceType[] };
function fillCommon(server: string) {
  fireEvent.change(screen.getByLabelText("Source label"), { target: { value: "prod-orders" } });
  fireEvent.change(screen.getByLabelText("Connection address"), { target: { value: server } });
}
it("saves a RabbitMQ queue using a credential reference with no seek option", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<SubscriptionConfigDialog {...base} onSave={save} />);
  fillCommon("amqps://broker.example:5671");
  fireEvent.change(screen.getByLabelText("Receive queue"), { target: { value: "orders" } });
  fireEvent.mouseDown(screen.getByLabelText("Account credential"));
  fireEvent.click(await screen.findByText("Rabbit source account (#31)"));
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0].config).toEqual({ source_type: "rabbitmq", cluster_id: "prod-orders",
    server: "amqps://broker.example:5671", vhost: "/", queue: "orders", credential_id: 31,
    ca_pem: null, mode: "per_event", ordering: "receipt_serial", limits: {} });
  expect(screen.queryByText("Which messages should be received first?")).toBeNull();
});
it("requires RocketMQ group and saves no initial-position field", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<SubscriptionConfigDialog {...base} onSave={save} />);
  fireEvent.click(screen.getByLabelText("RocketMQ messages"));
  fillCommon("grpc://proxy.example:8081");
  fireEvent.change(screen.getByLabelText("Message topic"), { target: { value: "orders" } });
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await screen.findByText("Required");
  expect(save).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Receive group"), { target: { value: "dlr-orders" } });
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0].config).toMatchObject({ source_type: "rocketmq", topic: "orders",
    group_id: "dlr-orders", credential_id: null });
  expect(save.mock.calls[0][0].config).not.toHaveProperty("initial_position");
});
it("guards a dirty source switch and discards the old form only on confirmation", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<SubscriptionConfigDialog {...base} onSave={vi.fn()} />);
  fillCommon("amqp://broker.example:5672");
  fireEvent.click(screen.getByLabelText("RocketMQ messages"));
  expect(confirm).toHaveBeenCalled();
  expect(screen.getByLabelText("Receive queue")).toBeTruthy();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByLabelText("RocketMQ messages"));
  await screen.findByLabelText("Message topic");
  expect((screen.getByLabelText("Source label") as HTMLInputElement).value).toBe("");
});
it("rejects credentials in addresses and prevents read-only saves", async () => {
  const save = vi.fn();
  const result = render(<SubscriptionConfigDialog {...base} onSave={save} />);
  fillCommon("amqp://user:password@broker.example:5672");
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await screen.findByText("Include a port and select credentials separately. Do not include credentials in the address.");
  expect(save).not.toHaveBeenCalled();
  result.unmount();
  render(<SubscriptionConfigDialog {...base} canManage={false} readOnly onSave={save} />);
  expect((screen.getByLabelText("Source label") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement).disabled).toBe(true);
});
it("does not render a Kubernetes fallback for an unknown source", () => {
  render(<SubscriptionConfigDialog {...base} availableSources={["unknown" as SubscriptionSourceType]} onSave={vi.fn()} />);
  expect(screen.queryByRole("dialog")).toBeNull();
});
it("summarizes queue scopes and IDs without Kafka partitions", () => {
  const sub = { config: { source_type: "rabbitmq", queue: "orders" } } as EventSubscription;
  expect(eventSourceLabel(sub)).toBe("RabbitMQ · orders");
  const event = { id: 1, envelope: { source: { type: "rocketmq", position: { message_id: "one" },
    attributes: { topic: "orders" } } } } as unknown as SubscriptionEvent;
  const translate = vi.fn((_key, value) => `${value.scope}:${value.id}`);
  expect(eventSummary(event, translate)).toBe("orders:one");
  expect(translate).toHaveBeenCalledWith("queues.messageSummary", { scope: "orders", id: "one" });
});

it("explains a changed queue scope in plain language in both locales", async () => {
  const error = new ApiError(409, "source_epoch_required", "internal detail");
  expect(subscriptionErrorMessage(error, "rocketmq")).toContain("The connection scope changed");
  await applyUiLocale("zh-CN");
  expect(subscriptionErrorMessage(error, "rabbitmq")).toContain("连接范围已改变");
  expect(subscriptionErrorMessage(error, "rabbitmq")).not.toContain("新建 epoch");
});
