export interface KafkaSourceConfig {
  source_type: "kafka";
  cluster_id: string;
  bootstrap_servers: string[];
  topics: string[];
  group_id?: string | null;
  initial_position: "earliest" | "latest";
  security_protocol: "PLAINTEXT" | "SSL" | "SASL_SSL";
  sasl_mechanism?: "SCRAM-SHA-256" | "SCRAM-SHA-512" | null;
  credential_id?: number | null;
  ca_pem?: string | null;
  mode: "per_event";
  ordering: "partition_serial";
  limits: Record<string, number>;
}

export interface EventSubscription {
  id: number;
  adapter_id: number;
  name: string;
  description: string;
  source_type: string;
  desired_state: "disabled" | "enabled";
  connection_state: string;
  delivery_state: string;
  config_version: number;
  source_epoch: string;
  activation_id: number | null;
  repair_revision_id: number | null;
  last_error_code: string | null;
  config: KafkaSourceConfig;
  quota: Record<string, number>;
  limits: Record<string, number>;
  global_limits: Record<string, number>;
  oldest_pending_at: string | null;
  event_counts: Record<string, number>;
  available: boolean;
  access_level?: "admin" | "owner" | "edit" | "read" | null;
  created_at: string;
  updated_at: string;
}

export interface SubscriptionCapabilities {
  enabled: boolean;
  available_sources: string[];
}
export interface SubscriptionPage {
  items: EventSubscription[];
  total: number;
  next_before_id: number | null;
}
export interface SubscriptionEvent {
  id: number;
  activation_id: number;
  source_event_id: string;
  source_epoch: string;
  state: string;
  shard: string;
  sequence: number;
  envelope: Record<string, unknown> | null;
  payload_expired: boolean;
  first_received_at: string;
  duplicate_count: number;
  deliveries: {
    id: number;
    ordinal: number;
    execution_id: number | null;
    state: string;
    revision_override_id: number | null;
    activation_override_id: number | null;
    effective_activation_id: number;
  }[];
}
export interface SubscriptionEventPage {
  items: SubscriptionEvent[];
  total: number;
  next_before_id: number | null;
}
export interface SubscriptionDisposition {
  expected_config_version: number;
  request_id: string;
  reason: string;
  revision_id?: number;
  use_current_configuration?: boolean;
  expected_preview_token?: string;
}
export interface SourceConnectionResult {
  source_type: string;
  cluster_id: string;
  topic_count?: number;
  partition_count?: number;
  group_available?: boolean;
  identity_verified?: boolean;
}

export interface SourceCheckpoint {
  id: number;
  shard: string;
  source_epoch: string;
  position: Record<string, unknown>;
  confirmed_position: Record<string, unknown>;
  assignment_generation: number;
  blocked_receipt_id: number | null;
  blocked_unaccepted: {
    code: string;
    source_event_id?: string;
    [key: string]: unknown;
  } | null;
  source_lag: number | null;
}
export interface CheckpointPage {
  items: SourceCheckpoint[];
  total: number;
  next_before_id: number | null;
}
export interface SubscriptionActivation {
  id: number;
  revision_id: number;
  worker_id: number;
  config_version: number;
  source_epoch: string;
  source_credential_id: number | null;
  credential_refs: { env_key: string; credential_id: number; field: string }[];
}

export function subscriptionDrained(sub: EventSubscription): boolean {
  return (
    sub.desired_state === "disabled" &&
    sub.connection_state === "stopped" &&
    sub.delivery_state !== "repair" &&
    ["pending", "processing", "failed"].every(
      (state) => (sub.event_counts[state] ?? 0) === 0,
    )
  );
}

export interface SubscriptionDraftState {
  dirty: boolean;
  busy: boolean;
}

export interface SubscriptionReplayContext {
  original: SubscriptionActivation;
  current: Omit<SubscriptionActivation, "id">;
  preview_token: string;
}
