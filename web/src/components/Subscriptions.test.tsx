import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { applyUiLocale } from "../i18n";
import type {
  EventSubscription,
  SourceCheckpoint,
  SubscriptionEvent,
} from "../subscription-types";
import type { Adapter } from "../types";
import KafkaSubscriptionPanel from "./KafkaSubscriptionPanel";
import KafkaSubscriptionConfig from "./KafkaSubscriptionConfig";
import SubscriptionActionDialog from "./SubscriptionActionDialog";
import SubscriptionWorkspace from "./SubscriptionWorkspace";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import EventTriggerSettings from "./EventTriggerSettings";

vi.mock("./ExecutionHistoryPanel", () => ({
  default: () => <div>Execution trace</div>,
}));

const adapter: Adapter = {
  id: 9,
  name: "event-task",
  description: "",
  adapter_type: "task",
  language: "python",
  run_mode: "manual",
  access_level: "admin",
  latest_version_id: 7,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
};
const sub = (values: Partial<EventSubscription> = {}): EventSubscription => ({
  id: 4,
  adapter_id: 9,
  name: "Kafka orders",
  description: "",
  source_type: "kafka",
  desired_state: "disabled",
  connection_state: "stopped",
  delivery_state: "blocked",
  config_version: 1,
  source_epoch: "epoch-one",
  activation_id: 8,
  repair_revision_id: null,
  access_level: "owner",
  available: true,
  config: {
    source_type: "kafka",
    cluster_id: "fixture-cluster",
    bootstrap_servers: ["broker.example.test:9092"],
    topics: ["orders"],
    initial_position: "earliest",
    security_protocol: "SSL",
    mode: "per_event",
    ordering: "partition_serial",
    limits: {},
  },
  quota: { pending_count: 0, pending_bytes: 0 },
  limits: { pending_count: 1000 },
  global_limits: {},
  event_counts: { failed: 1 },
  oldest_pending_at: null,
  last_error_code: null,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
  ...values,
});
const event: SubscriptionEvent = {
  id: 12,
  activation_id: 8,
  source_event_id: "source-event-one",
  source_epoch: "epoch-one",
  state: "failed",
  shard: "orders:0",
  sequence: 1,
  envelope: { schema: "dlr.event.v1" },
  payload_expired: false,
  first_received_at: "2026-10-23T00:00:00Z",
  duplicate_count: 0,
  deliveries: [
    {
      id: 3,
      ordinal: 1,
      execution_id: 21,
      state: "failed",
      revision_override_id: null,
      activation_override_id: null,
      effective_activation_id: 8,
    },
  ],
};

beforeEach(async () => {
  await applyUiLocale("en");
  const original = {
    id: 8,
    config_version: 1,
    source_epoch: "epoch-one",
    revision_id: 7,
    worker_id: 1,
    source_credential_id: null,
    credential_refs: [],
  };
  vi.spyOn(api, "subscriptionActivation").mockResolvedValue(original);
  vi.spyOn(api, "getSubscriptionEvent").mockImplementation(
    async () => (await api.listSubscriptionEvents(4)).items[0],
  );
  vi.spyOn(api, "subscriptionReplayContext").mockResolvedValue({
    original,
    current: {
      ...original,
      revision_id: 10,
      worker_id: 2,
      source_credential_id: 6,
      credential_refs: [
        { env_key: "TARGET_TOKEN", credential_id: 5, field: "token" },
      ],
    },
    preview_token: "a".repeat(64),
  });
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("subscription disposition contract", () => {
  it("keeps request ID, reason, expected version and original configuration fixed after an unknown result", async () => {
    const replay = vi
      .spyOn(api, "replaySubscriptionEvent")
      .mockRejectedValueOnce(new ApiError(503, "temporary_failure", "fixture"))
      .mockResolvedValue({ delivery_id: 30 });
    const done = vi.fn().mockResolvedValue(undefined),
      close = vi.fn();
    const view = render(
      <SubscriptionActionDialog
        action="replay"
        sub={sub()}
        event={event}
        onDone={done}
        onClose={close}
      />,
    );
    fireEvent.change(screen.getByLabelText("Disposition reason"), {
      target: { value: "reviewed target outcome" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(replay).toHaveBeenCalledTimes(1));
    await screen.findByText(/temporary_failure/);
    const first = structuredClone(replay.mock.calls[0]);
    view.rerender(
      <SubscriptionActionDialog
        action="replay"
        sub={sub({ config_version: 99, repair_revision_id: 88 })}
        event={event}
        onDone={done}
        onClose={close}
      />,
    );
    expect(
      (screen.getByLabelText("Disposition reason") as HTMLTextAreaElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(replay).toHaveBeenCalledTimes(2));
    expect(replay.mock.calls[1]).toEqual(first);
    expect(first[2]).toMatchObject({
      expected_config_version: 1,
      reason: "reviewed target outcome",
    });
    expect(first[2].request_id).toBeTruthy();
    expect(first[2].revision_id).toBeUndefined();
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
  });

  it("shows original/current Revision, Worker and references and retains the exact preview token on retry", async () => {
    const replay = vi
      .spyOn(api, "replaySubscriptionEvent")
      .mockRejectedValueOnce(new ApiError(503, "temporary_failure", "fixture"))
      .mockResolvedValue({ delivery_id: 31 });
    const view = render(
      <SubscriptionActionDialog
        action="replay"
        sub={sub()}
        event={event}
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    await screen.findByText("#10");
    expect(screen.getByText("#7")).toBeTruthy();
    expect(screen.getByText("#1")).toBeTruthy();
    expect(screen.getByText("#2")).toBeTruthy();
    expect(screen.getByText(/TARGET_TOKEN/)).toBeTruthy();
    fireEvent.mouseDown(screen.getByLabelText("Revision"));
    fireEvent.click(
      screen.getAllByText("Explicitly use current configuration").at(-1)!,
    );
    fireEvent.change(screen.getByLabelText("Disposition reason"), {
      target: { value: "reviewed current references" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await screen.findByText(/temporary_failure/);
    const first = structuredClone(replay.mock.calls[0][2]);
    view.rerender(
      <SubscriptionActionDialog
        action="replay"
        sub={sub({ config_version: 2 })}
        event={event}
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(replay).toHaveBeenCalledTimes(2));
    expect(replay.mock.calls[1][2]).toEqual(first);
    expect(first).toMatchObject({
      use_current_configuration: true,
      expected_preview_token: "a".repeat(64),
      expected_config_version: 1,
    });
    expect(api.subscriptionReplayContext).toHaveBeenCalledTimes(1);
  });

  it("requires a reason before a destructive skip", async () => {
    const skip = vi
      .spyOn(api, "skipSubscriptionEvent")
      .mockResolvedValue({ disposition_id: 5 });
    render(
      <SubscriptionActionDialog
        action="skip"
        sub={sub()}
        event={event}
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Skip without processing",
      }),
    );
    await screen.findByText("Required");
    expect(skip).not.toHaveBeenCalled();
  });

  it("never sends an arbitrary offset when skipping an unaccepted source record", async () => {
    const skip = vi
      .spyOn(api, "skipUnacceptedSubscriptionEvent")
      .mockResolvedValue({
        disposition_id: 5,
        outcome: "skipped_without_processing",
      });
    const checkpoint: SourceCheckpoint = {
      id: 1,
      shard: "orders:0",
      source_epoch: "epoch-one",
      assignment_generation: 2,
      position: { offset: "8" },
      confirmed_position: {},
      blocked_receipt_id: null,
      blocked_unaccepted: {
        code: "event_payload_too_large",
        source_event_id: "unaccepted-record",
        position: { offset: "8" },
      },
      source_lag: null,
    };
    render(
      <SubscriptionActionDialog
        action="skipUnaccepted"
        sub={sub()}
        checkpoint={checkpoint}
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText("Disposition reason"), {
      target: { value: "explicitly discard oversized record" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Skip unaccepted source record",
      }),
    );
    await waitFor(() => expect(skip).toHaveBeenCalledOnce());
    expect(skip.mock.calls[0][1]).toMatchObject({
      shard: "orders:0",
      source_event_id: "unaccepted-record",
      expected_config_version: 1,
    });
    expect(Object.keys(skip.mock.calls[0][1]).sort()).toEqual([
      "expected_config_version",
      "reason",
      "request_id",
      "shard",
      "source_event_id",
    ]);
  });

  it("saves repair code/dependencies without ordinary runtime or credential changes", async () => {
    const save = vi
      .spyOn(api, "saveSubscriptionRepair")
      .mockResolvedValue({ revision_id: 40 });
    render(
      <SubscriptionActionDialog
        action="repairSave"
        sub={sub({ delivery_state: "repair" })}
        initialCode="def handle(context, input): return input"
        initialRequirements=""
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Save repair Revision" }),
    );
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(Object.keys(save.mock.calls[0][1]).sort()).toEqual([
      "code",
      "expected_config_version",
      "requirements",
    ]);
  });
});

it.each(["en", "zh-CN"] as const)(
  "keeps read-only detail actions hidden in %s",
  async (locale) => {
    await applyUiLocale(locale);
    vi.spyOn(api, "getSubscription").mockResolvedValue(
      sub({ access_level: "read" }),
    );
    vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
      items: [event],
      total: 1,
      next_before_id: null,
    });
    vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    render(
      <SubscriptionWorkspace
        id={4}
        adapter={adapter}
        capabilities={{ enabled: true, available_sources: ["kafka"] }}
        onDeleted={() => {}}
      />,
    );
    await screen.findByRole("heading", { name: "Kafka orders" });
    const scope = within(screen.getByTestId("subscription-workspace"));
    expect(
      scope.queryByRole("button", {
        name: locale === "en" ? "Edit source configuration" : "修改来源配置",
      }),
    ).toBeNull();
    fireEvent.click(scope.getByRole("button", { name: "source-event-one" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).queryByRole("button", {
        name: locale === "en" ? "Replay" : "补处理",
      }),
    ).toBeNull();
  },
);

it("does not offer Kafka or future sources when capabilities expose no delivered source", async () => {
  vi.spyOn(api, "listSubscriptions").mockResolvedValue({
    items: [],
    total: 0,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionCapabilities").mockResolvedValue({
    enabled: true,
    available_sources: [],
  });
  render(
    <KafkaSubscriptionPanel
      adapter={adapter}
      readOnly={false}
      canManage
      dirty={false}
      onBindingChange={() => {}}
    />,
  );
  await screen.findByText(
    "No source is currently available for enablement. Existing custody remains visible and drains.",
  );
  expect(
    screen.queryByRole("button", { name: "Bind Kafka subscription" }),
  ).toBeNull();
  expect(screen.queryByText("Kubernetes")).toBeNull();
});

describe("subscription source configuration and trace", () => {
  it("explicitly saves before a connection test and leaves business execution untouched", async () => {
    const saved = sub({ desired_state: "disabled", delivery_state: "idle", event_counts: {} });
    const create = vi.spyOn(api, "createSubscription").mockResolvedValue(saved);
    const test = vi.spyOn(api, "testSubscriptionConnection").mockResolvedValue({ source_type: "kafka", cluster_id: "fixture-cluster", topic_count: 1, partition_count: 1 });
    const execute = vi.spyOn(api, "createExecution");
    render(<EventTriggerSettings adapter={adapter} sub={null} capabilities={{ enabled: true, available_sources: ["kafka"] }}
      readOnly={false} canManage={false} runtimeDirty={false} initialConfigure onChanged={vi.fn()}
      onRecords={vi.fn()} onDraftStateChange={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Actual broker cluster ID"), { target: { value: "fixture-cluster" } });
    fireEvent.change(screen.getByLabelText("Bootstrap servers (host:port per line)"), { target: { value: "broker.example.test:9093" } });
    fireEvent.change(screen.getByLabelText("Exact topic allowlist (one per line)"), { target: { value: "orders" } });
    fireEvent.mouseDown(screen.getByLabelText("Security protocol"));
    fireEvent.click(await screen.findByText("SSL", { selector: ".ant-select-item-option-content" }));
    fireEvent.mouseDown(screen.getByLabelText("Initial position"));
    fireEvent.click(await screen.findByText("Earliest readable position"));
    fireEvent.click(screen.getByRole("button", { name: "Save and test connection" }));
    await waitFor(() => expect(test).toHaveBeenCalledWith(saved.id, saved.config_version));
    expect(create.mock.invocationCallOrder[0]).toBeLessThan(test.mock.invocationCallOrder[0]);
    expect(execute).not.toHaveBeenCalled();
    expect(await screen.findByText("Connection test passed. Only source connectivity was checked; processing code was not run.")).toBeTruthy();
  });
  it("opens invalid advanced quota fields instead of hiding a save failure", async () => {
    const save = vi.fn();
    render(<KafkaSubscriptionConfig subscription={sub({ delivery_state: "idle", event_counts: {} })}
      adapterId={9} canManage={false} onClose={vi.fn()} onSave={save} />);
    const quota = screen.getByLabelText("Tightened quotas (optional JSON object)");
    expect(quota.closest("details")?.open).toBe(false);
    fireEvent.change(quota, { target: { value: '{"pending_count":0}' } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(quota.closest("details")?.open).toBe(true));
    expect(save).not.toHaveBeenCalled();
  });
  it("confirms discarding a dirty source before changing the source type", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<SubscriptionConfigDialog adapterId={9} canManage={false} availableSources={["kafka", "kubernetes"]}
      onClose={vi.fn()} onSave={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Actual broker cluster ID"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("radio", { name: "Kubernetes resource changes" }));
    expect(screen.queryByLabelText("Logical Kubernetes cluster ID")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Kubernetes resource changes" }));
    expect(await screen.findByLabelText("Logical Kubernetes cluster ID")).toBeTruthy();
    expect(confirm).toHaveBeenCalledTimes(2);
  });
  it("requires an explicit first position and submits only TLS/SCRAM references and allowed source fields", async () => {
    vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([
      {
        id: 6,
        name: "Kafka reference",
        type: "password",
        created_at: "",
        updated_at: "",
      },
    ]);
    const globalCredentials = vi.spyOn(api, "listCredentials");
    const save = vi.fn().mockResolvedValue(undefined);
    render(
      <KafkaSubscriptionConfig
        adapterId={9}
        canManage
        onClose={() => {}}
        onSave={save}
      />,
    );
    fireEvent.change(screen.getByLabelText("Actual broker cluster ID"), {
      target: { value: "fixture-cluster" },
    });
    fireEvent.change(
      screen.getByLabelText("Bootstrap servers (host:port per line)"),
      { target: { value: "broker.example.test:9092" } },
    );
    fireEvent.change(
      screen.getByLabelText("Exact topic allowlist (one per line)"),
      { target: { value: "orders\nshipments" } },
    );
    fireEvent.mouseDown(screen.getByLabelText("Source credential reference"));
    fireEvent.click(await screen.findByText("Kafka reference (#6)"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Required");
    expect(save).not.toHaveBeenCalled();
    fireEvent.mouseDown(screen.getByLabelText("Initial position"));
    fireEvent.click(await screen.findByText("Earliest readable position"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].config).toEqual({
      source_type: "kafka",
      cluster_id: "fixture-cluster",
      bootstrap_servers: ["broker.example.test:9092"],
      topics: ["orders", "shipments"],
      group_id: null,
      initial_position: "earliest",
      security_protocol: "SASL_SSL",
      sasl_mechanism: "SCRAM-SHA-256",
      credential_id: 6,
      ca_pem: null,
      mode: "per_event",
      ordering: "partition_serial",
      limits: {},
    });
    expect(screen.queryByLabelText(/password/i)).toBeNull();
    expect(api.listAdapterCredentialOptions).toHaveBeenCalledWith(9);
    expect(globalCredentials).not.toHaveBeenCalled();
  });

  it("keeps the opened configuration version fixed after an authoritative refresh", async () => {
    const ready = sub({ event_counts: {}, delivery_state: "idle" });
    vi.spyOn(api, "getSubscription")
      .mockResolvedValueOnce(ready)
      .mockResolvedValue({ ...ready, config_version: 2 });
    vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([]);
    const save = vi.spyOn(api, "updateSubscription").mockResolvedValue(ready);
    const draft = vi.fn();
    render(
      <SubscriptionWorkspace
        id={4}
        adapter={adapter}
        capabilities={{ enabled: true, available_sources: ["kafka"] }}
        onDeleted={() => {}}
        onDraftStateChange={draft}
      />,
    );
    const scope = within(await screen.findByTestId("subscription-workspace"));
    fireEvent.click(
      scope.getByRole("button", { name: "Edit source configuration" }),
    );
    fireEvent.change(screen.getByLabelText("Bootstrap servers (host:port per line)"), {
      target: { value: "changed.example.test:9093" },
    });
    await waitFor(() =>
      expect(draft).toHaveBeenLastCalledWith({ dirty: true, busy: false }),
    );
    fireEvent.click(scope.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.getSubscription).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1]).toMatchObject({
      expected_config_version: 1,
      config: expect.objectContaining({ bootstrap_servers: ["changed.example.test:9093"] }),
    });
  });

  it("traces the effective activation of a current-configuration replay rather than the original receipt activation", async () => {
    vi.spyOn(api, "getSubscription").mockResolvedValue(
      sub({ access_level: "read" }),
    );
    vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
      items: [
        {
          ...event,
          deliveries: [
            ...event.deliveries,
            {
              id: 4,
              ordinal: 2,
              execution_id: 22,
              state: "succeeded",
              revision_override_id: null,
              activation_override_id: 19,
              effective_activation_id: 19,
            },
          ],
        },
      ],
      total: 1,
      next_before_id: null,
    });
    vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    const activation = vi
      .spyOn(api, "subscriptionActivation")
      .mockResolvedValue({
        id: 19,
        revision_id: 30,
        worker_id: 2,
        config_version: 2,
        source_epoch: "epoch-two",
        source_credential_id: 6,
        credential_refs: [],
      });
    render(
      <SubscriptionWorkspace
        id={4}
        adapter={adapter}
        capabilities={{ enabled: true, available_sources: ["kafka"] }}
        onDeleted={() => {}}
      />,
    );
    const scope = within(await screen.findByTestId("subscription-workspace"));
    fireEvent.click(scope.getByRole("button", { name: "source-event-one" }));
    const modal = await screen.findByRole("dialog");
    fireEvent.click(within(modal).getByRole("button", { name: "#19" }));
    await waitFor(() => expect(activation).toHaveBeenCalledWith(4, 19));
    await screen.findByText("#30");
  });

  it("does not reset an epoch before the operator chooses a new initial position", async () => {
    const reset = vi.spyOn(api, "resetSubscription").mockResolvedValue(sub());
    render(
      <SubscriptionActionDialog
        action="reset"
        sub={sub({ event_counts: {}, delivery_state: "idle" })}
        onDone={async () => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText("Disposition reason"), {
      target: { value: "reviewed new epoch" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Reset to a new epoch" }),
    );
    await screen.findByText("Required");
    expect(reset).not.toHaveBeenCalled();
  });
});

it("refreshes an open event detail to include its latest terminal state and replay chain", async () => {
  vi.spyOn(api, "getSubscription").mockResolvedValue(
    sub({ access_level: "read" }),
  );
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
    items: [event],
    total: 1,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
    items: [],
    total: 0,
    next_before_id: null,
  });
  const detail = vi.spyOn(api, "getSubscriptionEvent").mockResolvedValue(event);
  render(
    <SubscriptionWorkspace
      id={4}
      adapter={adapter}
      capabilities={{ enabled: true, available_sources: ["kafka"] }}
      onDeleted={() => {}}
    />,
  );
  const scope = within(await screen.findByTestId("subscription-workspace"));
  fireEvent.click(scope.getByRole("button", { name: "source-event-one" }));
  const modal = await screen.findByRole("dialog");
  await waitFor(() => expect(detail).toHaveBeenCalledOnce());
  detail.mockResolvedValue({
    ...event,
    state: "succeeded",
    deliveries: [
      ...event.deliveries,
      {
        id: 5,
        ordinal: 2,
        execution_id: 99,
        state: "succeeded",
        revision_override_id: null,
        activation_override_id: 19,
        effective_activation_id: 19,
      },
    ],
  });
  fireEvent.click(within(modal).getByRole("button", { name: "Refresh" }));
  await within(modal).findByRole("button", { name: "View execution #99" });
  expect(within(modal).getByText("Execution succeeded")).toBeTruthy();
});
