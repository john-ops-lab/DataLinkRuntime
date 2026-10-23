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
import { createSubscriptionApi } from "../subscription-api";
import { subscriptionErrorMessage } from "../subscription-message";
import {
  canAbandonCompletion,
  completionReplayBlocked,
  subscriptionDrained,
} from "../subscription-types";
import type {
  EventSubscription,
  KubernetesSourceConfig,
  SubscriptionEvent,
  SyncBatchDetail,
} from "../subscription-types";
import type { Adapter } from "../types";
import KubernetesSubscriptionConfig from "./KubernetesSubscriptionConfig";
import KafkaSubscriptionPanel from "./KafkaSubscriptionPanel";
import SubscriptionAbandonDialog from "./SubscriptionAbandonDialog";
import SubscriptionActionDialog from "./SubscriptionActionDialog";
import SubscriptionBatchPanel from "./SubscriptionBatchPanel";
import SubscriptionWorkspace from "./SubscriptionWorkspace";

vi.mock("./ExecutionHistoryPanel", () => ({
  default: () => <div>Execution trace</div>,
}));
const adapter: Adapter = {
  id: 9,
  name: "sync-task",
  description: "",
  adapter_type: "task",
  language: "python",
  run_mode: "manual",
  access_level: "owner",
  latest_version_id: 7,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
};
const config: KubernetesSourceConfig = {
  source_type: "kubernetes",
  cluster_id: "logical-example",
  server: "https://api.example.test",
  group: "",
  version: "v1",
  resource: "pods",
  namespace: "team",
  label_selector: "app=owned",
  field_selector: "",
  credential_id: null,
  ca_pem: null,
  mode: "per_event",
  limits: {},
};
function sub(overrides: Partial<EventSubscription> = {}): EventSubscription {
  return {
    id: 4,
    adapter_id: 9,
    name: "Pod sync",
    description: "",
    source_type: "kubernetes",
    config,
    desired_state: "disabled",
    connection_state: "stopped",
    delivery_state: "idle",
    config_version: 2,
    source_epoch: "epoch-two",
    activation_id: 8,
    repair_revision_id: null,
    last_error_code: null,
    quota: {},
    limits: {},
    global_limits: {},
    oldest_pending_at: null,
    event_counts: {},
    available: true,
    access_level: "owner",
    created_at: "2026-10-23T00:00:00Z",
    updated_at: "2026-10-23T00:00:00Z",
    ...overrides,
  };
}
function batch(overrides: Partial<SyncBatchDetail> = {}): SyncBatchDetail {
  return {
    id: 22,
    subscription_id: 4,
    source_epoch: "epoch-two",
    activation_id: 8,
    config_version: 2,
    scope_fingerprint: "a".repeat(64),
    state: "processing",
    read_complete: true,
    completion_committed: true,
    completion_abandoned: false,
    closed: false,
    drain_open: false,
    control_pending: [],
    remaining_reserve: { count: 1, bytes: 8192 },
    pending_gap: null,
    failure_code: null,
    created_at: "2026-10-23T00:00:00Z",
    finished_at: null,
    resource_version: "opaque-rv",
    item_count: 1,
    page_count: 1,
    total_bytes: 2000,
    controls: [
      {
        kind: "sync.completed",
        receipt_id: 12,
        state: "pending",
        latest_delivery_id: 33,
        latest_delivery_ordinal: 2,
        execution_id: null,
        effective_activation_id: 8,
        revision_override_id: null,
      },
    ],
    abandonment: null,
    ...overrides,
  };
}
function event(overrides: Partial<SubscriptionEvent> = {}): SubscriptionEvent {
  return {
    id: 12,
    activation_id: 8,
    source_event_id: "completed-event",
    source_epoch: "epoch-two",
    state: "failed",
    shard: "kubernetes",
    sequence: 3,
    envelope: { schema: "dlr.event.v1" },
    payload_expired: false,
    first_received_at: "2026-10-23T00:00:00Z",
    duplicate_count: 0,
    batch_id: 22,
    source_phase: "sync.completed",
    coalesced_count: 0,
    deliveries: [
      {
        id: 33,
        ordinal: 2,
        execution_id: null,
        state: "pending",
        revision_override_id: null,
        activation_override_id: null,
        effective_activation_id: 8,
      },
    ],
    ...overrides,
  };
}
beforeEach(async () => {
  await applyUiLocale("en");
  vi.spyOn(api, "subscriptionActivation").mockResolvedValue({
    id: 8,
    revision_id: 7,
    worker_id: 5,
    config_version: 2,
    source_epoch: "epoch-two",
    source_credential_id: null,
    credential_refs: [],
  });
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("Kubernetes responsibility boundaries", () => {
  it.each(["en", "zh-CN"] as const)(
    "keeps Kubernetes token errors source-specific and hides native details in %s",
    async (locale) => {
      await applyUiLocale(locale);
      const text = subscriptionErrorMessage(
        new ApiError(422, "source_credential_invalid", "private-native-token"),
        "kubernetes",
      );
      expect(text).toContain("token");
      expect(text).not.toContain("private-native-token");
      expect(text).not.toContain("username/password");
    },
  );

  it("keeps an empty unclosed batch locked and permits a closed next-gap skeleton to drain", () => {
    expect(
      subscriptionDrained(sub({ latest_batch: batch({ item_count: 0 }) })),
    ).toBe(false);
    expect(
      subscriptionDrained(
        sub({
          latest_batch: batch({
            closed: true,
            remaining_reserve: { count: 0, bytes: 0 },
            pending_gap: {
              code: "source_history_gap",
              detected_at: "now",
              after_completion_committed: true,
              relist_required: true,
            },
          }),
        }),
      ),
    ).toBe(true);
    expect(canAbandonCompletion(sub({ available: false }), batch())).toBe(true);
    expect(canAbandonCompletion(sub(), batch({ closed: true }))).toBe(false);
    expect(completionReplayBlocked(event(), null)).toBe(true);
    expect(completionReplayBlocked(event(), batch({ closed: true }))).toBe(
      true,
    );
    expect(
      completionReplayBlocked(event(), batch({ completion_abandoned: true })),
    ).toBe(true);
    expect(
      completionReplayBlocked(
        event({ source_phase: "watch" }),
        batch({ closed: true }),
      ),
    ).toBe(false);
  });

  it("serializes the required explicitly null Execution instead of omitting it", async () => {
    const calls: { path: string; init?: RequestInit }[] = [];
    const client = createSubscriptionApi(
      async <T,>(path: string, init?: RequestInit): Promise<T> => {
        calls.push({ path, init });
        return {} as T;
      },
    );
    const body = {
      expected_config_version: 2,
      source_epoch: "epoch-two",
      receipt_id: 12,
      expected_latest_delivery_id: 33,
      expected_delivery_ordinal: 2,
      expected_execution_id: null,
      request_id: "fixed-id",
      reason: "reviewed",
      confirm_impact: true as const,
    };
    await client.abandonSubscriptionCompletion(4, 22, body);
    expect(calls[0].path).toBe(
      "/api/event-subscriptions/4/sync-batches/22/abandon-completion",
    );
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(body);
  });
});

describe("Kubernetes source form", () => {
  it("keeps Kubernetes hidden while only Kafka is available", async () => {
    vi.spyOn(api, "listSubscriptions").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    vi.spyOn(api, "subscriptionCapabilities").mockResolvedValue({
      enabled: true,
      available_sources: ["kafka"],
    });
    vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([]);
    render(
      <KafkaSubscriptionPanel
        adapter={adapter}
        canManage
        readOnly={false}
        dirty={false}
        onBindingChange={() => {}}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Bind event subscription" }),
    );
    expect(
      await screen.findByLabelText("Actual broker cluster ID"),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Logical Kubernetes cluster ID")).toBeNull();
    expect(screen.queryByText("Kubernetes")).toBeNull();
  });

  it("uses owner-scoped token references and submits the closed Deployment/state_sync configuration", async () => {
    const options = vi
      .spyOn(api, "listAdapterCredentialOptions")
      .mockResolvedValue([]);
    const global = vi.spyOn(api, "listCredentials").mockResolvedValue([]);
    const save = vi.fn().mockResolvedValue(undefined);
    render(
      <KubernetesSubscriptionConfig
        adapterId={9}
        canManage
        onClose={() => {}}
        onSave={save}
      />,
    );
    await waitFor(() => expect(options).toHaveBeenCalledWith(9));
    expect(global).not.toHaveBeenCalled();
    expect(
      screen.getByLabelText("Delivery mode").closest(".ant-select")
        ?.textContent,
    ).toContain("per_event");
    fireEvent.change(screen.getByLabelText("Logical Kubernetes cluster ID"), {
      target: { value: "logical-example" },
    });
    fireEvent.change(screen.getByLabelText("API server HTTPS origin"), {
      target: { value: "https://api.example.test" },
    });
    fireEvent.change(screen.getByLabelText("Namespace"), {
      target: { value: "team" },
    });
    fireEvent.mouseDown(
      screen.getByLabelText("Resource / API group and version"),
    );
    fireEvent.click(await screen.findByText("Deployment · apps/v1"));
    fireEvent.mouseDown(screen.getByLabelText("Delivery mode"));
    fireEvent.click(
      await screen.findByText(
        "state_sync · explicitly coalesce eligible pending state",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].config).toEqual({
      ...config,
      group: "apps",
      resource: "deployments",
      mode: "state_sync",
      label_selector: "",
    });
    expect(screen.queryByLabelText(/password|token value/i)).toBeNull();
  });

  it("offers a source only from the intersection of capabilities and implemented forms", async () => {
    vi.spyOn(api, "listSubscriptions").mockResolvedValue({
      items: [],
      total: 0,
      next_before_id: null,
    });
    vi.spyOn(api, "subscriptionCapabilities").mockResolvedValue({
      enabled: true,
      available_sources: ["kafka", "kubernetes", "future"],
    });
    vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([]);
    render(
      <KafkaSubscriptionPanel
        adapter={adapter}
        canManage
        readOnly={false}
        dirty={false}
        onBindingChange={() => {}}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Bind event subscription" }),
    );
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "Source" }));
    expect(screen.queryByText("future")).toBeNull();
    fireEvent.click(await screen.findByText("Kubernetes"));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(
      await screen.findByLabelText("Logical Kubernetes cluster ID"),
    ).toBeTruthy();
  });
});

describe("completion abandonment intent", () => {
  it.each(["pending", "failed"])(
    "freezes a %s responsibility including null Execution across unknown result, feature-off and live version changes",
    async (state) => {
      const call = vi
        .spyOn(api, "abandonSubscriptionCompletion")
        .mockRejectedValue(
          new ApiError(503, "temporary_failure", "not a source detail"),
        );
      const original = batch({ controls: [{ ...batch().controls[0], state }] });
      const view = render(
        <SubscriptionAbandonDialog
          sub={sub()}
          batch={original}
          onClose={() => {}}
          onDone={async () => {}}
        />,
      );
      fireEvent.change(screen.getByLabelText("Disposition reason"), {
        target: { value: "reviewed target uncertainty" },
      });
      fireEvent.click(
        screen.getByRole("button", { name: "Abandon completion confirmation" }),
      );
      expect(
        await screen.findByText("Confirm the impact before submitting."),
      ).toBeTruthy();
      expect(call).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("checkbox"));
      fireEvent.click(
        screen.getByRole("button", { name: "Abandon completion confirmation" }),
      );
      await waitFor(() => expect(call).toHaveBeenCalledOnce());
      const body = structuredClone(call.mock.calls[0][2]);
      expect(body.expected_execution_id).toBeNull();
      expect(Object.hasOwn(body, "expected_execution_id")).toBe(true);
      expect(body).toMatchObject({
        expected_config_version: 2,
        source_epoch: "epoch-two",
        receipt_id: 12,
        expected_latest_delivery_id: 33,
        expected_delivery_ordinal: 2,
        reason: "reviewed target uncertainty",
        confirm_impact: true,
      });
      view.rerender(
        <SubscriptionAbandonDialog
          sub={sub({
            config_version: 9,
            source_epoch: "later",
            available: false,
          })}
          batch={batch({ closed: true, controls: [] })}
          onClose={() => {}}
          onDone={async () => {}}
        />,
      );
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Retry the original abandonment request",
        }),
      );
      await waitFor(() => expect(call).toHaveBeenCalledTimes(2));
      expect(call.mock.calls[1][2]).toEqual(body);
    },
  );

  it("rejects a reason over 4096 UTF-8 bytes without truncation or an API call", async () => {
    const call = vi.spyOn(api, "abandonSubscriptionCompletion");
    render(
      <SubscriptionAbandonDialog
        sub={sub()}
        batch={batch()}
        onClose={() => {}}
        onDone={async () => {}}
      />,
    );
    const value = "界".repeat(1366);
    const input = screen.getByLabelText(
      "Disposition reason",
    ) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(
      screen.getByRole("button", { name: "Abandon completion confirmation" }),
    );
    expect(
      await screen.findByText("The reason must not exceed 4096 UTF-8 bytes."),
    ).toBeTruthy();
    expect(input.value).toBe(value);
    expect(call).not.toHaveBeenCalled();
  });
});

it("shows a bounded historical batch and opaque long RV to read-only users without offering abandon", async () => {
  const rv = "opaque-" + "r".repeat(4089);
  const historical = batch({
    id: 21,
    activation_id: null,
    config_version: null,
    source_epoch: null,
    resource_version: rv,
  });
  vi.spyOn(api, "subscriptionBatches").mockResolvedValue({
    items: [historical],
    total: 1,
    next_before_id: null,
  });
  const get = vi.spyOn(api, "subscriptionBatch").mockResolvedValue(historical);
  render(
    <SubscriptionBatchPanel
      sub={sub({ latest_batch: batch({ id: 99 }) })}
      canEdit={false}
      selectedId={21}
      onSelect={() => {}}
      onEvent={() => {}}
      onExecution={() => {}}
      onChanged={async () => {}}
    />,
  );
  expect(await screen.findByText(rv)).toBeTruthy();
  expect(get).toHaveBeenCalledWith(4, 21);
  expect(screen.getAllByText("Historical reference unavailable").length).toBe(
    3,
  );
  expect(
    screen.queryByRole("button", { name: "Abandon completion confirmation" }),
  ).toBeNull();
});

it("uses the event's old batch for completion actions, never the latest batch, and hides Kafka skip-unaccepted", async () => {
  vi.spyOn(api, "getSubscription").mockResolvedValue(
    sub({ latest_batch: batch({ id: 99 }) }),
  );
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
    items: [event()],
    total: 1,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
    items: [
      {
        id: 1,
        shard: "kubernetes",
        source_epoch: "epoch-two",
        position: {},
        confirmed_position: {},
        assignment_generation: 1,
        blocked_receipt_id: null,
        blocked_unaccepted: {
          code: "source_object_invalid",
          source_event_id: "poison",
        },
        source_lag: null,
      },
    ],
    total: 1,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionBatches").mockResolvedValue({
    items: [],
    total: 0,
    next_before_id: null,
  });
  const get = vi
    .spyOn(api, "subscriptionBatch")
    .mockResolvedValue(batch({ closed: true, state: "completed" }));
  render(
    <SubscriptionWorkspace
      id={4}
      adapter={adapter}
      capabilities={{
        enabled: true,
        available_sources: ["kafka", "kubernetes"],
      }}
      onDeleted={() => {}}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "completed-event" }),
  );
  const modal = await screen.findByRole("dialog");
  await waitFor(() => expect(get).toHaveBeenCalledWith(4, 22));
  expect(
    (within(modal).getByRole("button", { name: "Replay" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (
      within(modal).getByRole("button", {
        name: "Skip without processing",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    screen.queryByRole("button", { name: "Skip unaccepted source record" }),
  ).toBeNull();
});

it("resets Kubernetes with an explicit full-LIST confirmation and no Kafka initial position", async () => {
  const call = vi.spyOn(api, "resetSubscription").mockResolvedValue(sub());
  render(
    <SubscriptionActionDialog
      action="reset"
      sub={sub()}
      onClose={() => {}}
      onDone={async () => {}}
    />,
  );
  expect(screen.queryByLabelText("Initial position")).toBeNull();
  fireEvent.change(screen.getByLabelText("Disposition reason"), {
    target: { value: "new full reconciliation" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Reset to a new epoch" }));
  await waitFor(() => expect(call).toHaveBeenCalledOnce());
  expect(call.mock.calls[0][1].config).toEqual(config);
  expect(Object.hasOwn(call.mock.calls[0][1].config, "initial_position")).toBe(
    false,
  );
});

it("opens a historical Kubernetes event's own batch and coalescing chain after switching the subscription to Kafka", async () => {
  const historical = batch({
    id: 22,
    source_epoch: "old-kube-epoch",
    closed: true,
    state: "completed",
    remaining_reserve: { count: 0, bytes: 0 },
    controls: [
      { ...batch().controls[0], state: "succeeded", execution_id: 21 },
    ],
  });
  const latestHistorical = { ...historical, id: 99 };
  const current = sub({
    source_type: "kafka",
    config_version: 3,
    source_epoch: "new-kafka-epoch",
    config: {
      source_type: "kafka",
      cluster_id: "broker-cluster",
      bootstrap_servers: ["broker.example.test:9092"],
      topics: ["orders"],
      initial_position: "earliest",
      security_protocol: "SSL",
      mode: "per_event",
      ordering: "partition_serial",
      limits: {},
    },
    latest_batch: latestHistorical,
  });
  vi.spyOn(api, "getSubscription").mockResolvedValue(current);
  vi.spyOn(api, "listSubscriptionEvents").mockResolvedValue({
    items: [
      event({
        source_phase: "watch",
        state: "superseded",
        source_epoch: "old-kube-epoch",
        coalesced_count: 2,
        first_source_event_id: "original-kube-identity",
        superseded_by_id: 14,
        deliveries: [
          { ...event().deliveries[0], state: "superseded", execution_id: null },
        ],
      }),
    ],
    total: 1,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionCheckpoints").mockResolvedValue({
    items: [],
    total: 0,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionBatches").mockResolvedValue({
    items: [latestHistorical],
    total: 1,
    next_before_id: null,
  });
  const get = vi.spyOn(api, "subscriptionBatch").mockResolvedValue(historical);
  render(
    <SubscriptionWorkspace
      id={4}
      adapter={adapter}
      capabilities={{
        enabled: true,
        available_sources: ["kafka", "kubernetes"],
      }}
      onDeleted={() => {}}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "completed-event" }),
  );
  const eventDialog = await screen.findByRole("dialog");
  expect(
    screen.getByRole("button", { name: "#22 · watch", hidden: true }),
  ).toBeTruthy();
  expect(within(eventDialog).getByText("original-kube-identity")).toBeTruthy();
  expect(
    within(eventDialog).getByRole("button", {
      name: "Superseded by Receipt #14",
    }),
  ).toBeTruthy();
  fireEvent.click(
    within(eventDialog).getByRole("button", { name: "Batch #22 · watch" }),
  );
  expect(await screen.findByRole("dialog", { name: "Batch #22" })).toBeTruthy();
  await waitFor(() => expect(get).toHaveBeenCalledWith(4, 22));
  expect(
    screen.queryByRole("button", { name: "Abandon completion confirmation" }),
  ).toBeNull();
});

it("keeps an original completed replay retry fixed after a live close and feature-off", async () => {
  vi.spyOn(api, "subscriptionActivation").mockResolvedValue({
    id: 8,
    revision_id: 7,
    worker_id: 5,
    config_version: 2,
    source_epoch: "epoch-two",
    source_credential_id: null,
    credential_refs: [],
  });
  const call = vi
    .spyOn(api, "replaySubscriptionEvent")
    .mockRejectedValue(new ApiError(503, "temporary_failure", "unknown"));
  const view = render(
    <SubscriptionActionDialog
      action="replay"
      sub={sub({
        latest_batch: batch(),
        delivery_state: "blocked",
        event_counts: { failed: 1 },
      })}
      event={event()}
      batch={batch()}
      onClose={() => {}}
      onDone={async () => {}}
    />,
  );
  fireEvent.change(screen.getByLabelText("Disposition reason"), {
    target: { value: "retry original completed intent" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Replay" }));
  await screen.findByText(/temporary_failure/);
  expect(call).toHaveBeenCalledOnce();
  const original = structuredClone(call.mock.calls[0][2]);
  view.rerender(
    <SubscriptionActionDialog
      action="replay"
      sub={sub({
        config_version: 9,
        available: false,
        latest_batch: batch({ closed: true }),
      })}
      event={event({ state: "succeeded" })}
      batch={batch({ closed: true })}
      onClose={() => {}}
      onDone={async () => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Replay" }));
  await waitFor(() => expect(call).toHaveBeenCalledTimes(2));
  expect(call.mock.calls[1][2]).toEqual(original);
});

it("keeps full-quota abandonment available after feature-off without calling ordinary drained guards", async () => {
  vi.spyOn(api, "subscriptionBatches").mockResolvedValue({
    items: [batch()],
    total: 1,
    next_before_id: null,
  });
  vi.spyOn(api, "subscriptionBatch").mockResolvedValue(batch());
  render(
    <SubscriptionBatchPanel
      sub={sub({
        available: false,
        delivery_state: "blocked",
        event_counts: { failed: 1 },
        quota: { history_count: 4, dedupe_count: 4 },
        limits: { history_count: 4, dedupe_count: 4 },
      })}
      canEdit
      selectedId={22}
      onSelect={() => {}}
      onEvent={() => {}}
      onExecution={() => {}}
      onChanged={async () => {}}
    />,
  );
  const button = await screen.findByRole("button", {
    name: "Abandon completion confirmation",
  });
  expect((button as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(button);
  expect(await screen.findByRole("checkbox")).toBeTruthy();
  expect(
    screen.getAllByText("Business result unconfirmed").length,
  ).toBeGreaterThan(0);
});

it("disables ordinary configuration and enable for an empty but unclosed batch", async () => {
  vi.spyOn(api, "getSubscription").mockResolvedValue(
    sub({ latest_batch: batch({ item_count: 0 }) }),
  );
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
  vi.spyOn(api, "subscriptionBatches").mockResolvedValue({
    items: [],
    total: 0,
    next_before_id: null,
  });
  render(
    <SubscriptionWorkspace
      id={4}
      adapter={adapter}
      capabilities={{ enabled: true, available_sources: ["kubernetes"] }}
      onDeleted={() => {}}
    />,
  );
  await screen.findByRole("heading", { name: "Pod sync" });
  expect(
    (
      screen.getByRole("button", {
        name: "Edit source configuration",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "Enable reception",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});
