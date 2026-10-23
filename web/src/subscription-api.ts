import type {
  CheckpointPage,
  EventSubscription,
  KafkaSourceConfig,
  SourceConnectionResult,
  SubscriptionActivation,
  SubscriptionCapabilities,
  SubscriptionDisposition,
  SubscriptionEventPage,
  SubscriptionEvent,
  SubscriptionPage,
  SubscriptionReplayContext,
} from "./subscription-types";

type Request = <T>(path: string, init?: RequestInit) => Promise<T>;

/** Reuses the authenticated Control client; all source secrets remain references. */
export function createSubscriptionApi(request: Request) {
  return {
    subscriptionCheckpoints: (
      id: number,
      before_id?: number,
    ): Promise<CheckpointPage> =>
      request(
        `/api/event-subscriptions/${id}/checkpoints?limit=20${before_id === undefined ? "" : `&before_id=${before_id}`}`,
      ),
    subscriptionActivation: (
      id: number,
      activationId: number,
    ): Promise<SubscriptionActivation> =>
      request(`/api/event-subscriptions/${id}/activations/${activationId}`),
    skipUnacceptedSubscriptionEvent: (
      id: number,
      payload: SubscriptionDisposition & {
        shard: string;
        source_event_id: string;
      },
    ): Promise<{ disposition_id: number; outcome: string }> =>
      request(`/api/event-subscriptions/${id}/skip-unaccepted`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    subscriptionCapabilities: (): Promise<SubscriptionCapabilities> =>
      request("/api/event-subscriptions/capabilities"),
    listSubscriptions: (
      query: {
        adapter_id?: number;
        source_type?: string;
        connection_state?: string;
        delivery_state?: string;
        before_id?: number;
        limit?: number;
      } = {},
    ): Promise<SubscriptionPage> => {
      const params = new URLSearchParams();
      Object.entries(query).forEach(([key, value]) => {
        if (value !== undefined && value !== "") params.set(key, String(value));
      });
      return request(`/api/event-subscriptions?${params}`);
    },
    getSubscription: (id: number): Promise<EventSubscription> =>
      request(`/api/event-subscriptions/${id}`),
    createSubscription: (payload: {
      adapter_id: number;
      name: string;
      description: string;
      config: KafkaSourceConfig;
    }): Promise<EventSubscription> =>
      request("/api/event-subscriptions", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    updateSubscription: (
      id: number,
      payload: {
        expected_config_version?: number;
        name?: string;
        description?: string;
        config?: KafkaSourceConfig;
        new_epoch?: boolean;
      },
    ): Promise<EventSubscription> =>
      request(`/api/event-subscriptions/${id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    subscriptionAction: (
      id: number,
      action: "enable" | "disable" | "resume" | "repair/enter" | "repair/exit",
      expected_config_version: number,
    ): Promise<EventSubscription> =>
      request(`/api/event-subscriptions/${id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ expected_config_version }),
      }),
    testSubscriptionConnection: (
      id: number,
      expected_config_version: number,
    ): Promise<SourceConnectionResult> =>
      request(`/api/event-subscriptions/${id}/test-connection`, {
        method: "POST",
        body: JSON.stringify({ expected_config_version }),
      }),
    deleteSubscription: (
      id: number,
      expected_config_version: number,
    ): Promise<void> =>
      request(`/api/event-subscriptions/${id}`, {
        method: "DELETE",
        body: JSON.stringify({ expected_config_version }),
      }),
    resetSubscription: (
      id: number,
      payload: SubscriptionDisposition & { config: KafkaSourceConfig },
    ): Promise<EventSubscription> =>
      request(`/api/event-subscriptions/${id}/reset`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    saveSubscriptionRepair: (
      id: number,
      payload: {
        expected_config_version: number;
        code: string;
        requirements: string;
      },
    ): Promise<{ revision_id: number }> =>
      request(`/api/event-subscriptions/${id}/repair/revisions`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    getSubscriptionEvent: (
      id: number,
      receiptId: number,
    ): Promise<SubscriptionEvent> =>
      request(`/api/event-subscriptions/${id}/events/${receiptId}`),
    listSubscriptionEvents: (
      id: number,
      before_id?: number,
    ): Promise<SubscriptionEventPage> =>
      request(
        `/api/event-subscriptions/${id}/events?limit=20${before_id === undefined ? "" : `&before_id=${before_id}`}`,
      ),
    subscriptionReplayContext: (
      id: number,
      receiptId: number,
    ): Promise<SubscriptionReplayContext> =>
      request(
        `/api/event-subscriptions/${id}/events/${receiptId}/replay-context`,
      ),
    replaySubscriptionEvent: (
      id: number,
      receiptId: number,
      payload: SubscriptionDisposition,
    ): Promise<{ delivery_id: number }> =>
      request(`/api/event-subscriptions/${id}/events/${receiptId}/replay`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    skipSubscriptionEvent: (
      id: number,
      receiptId: number,
      payload: SubscriptionDisposition,
    ): Promise<{ disposition_id: number }> =>
      request(`/api/event-subscriptions/${id}/events/${receiptId}/skip`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  };
}
