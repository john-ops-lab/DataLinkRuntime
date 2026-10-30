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

export interface KubernetesSourceConfig {
  source_type: "kubernetes";
  cluster_id: string;
  server: string;
  group: "" | "apps";
  version: "v1";
  resource: "pods" | "deployments";
  namespace: string | null;
  label_selector: string;
  field_selector: string;
  credential_id: number | null;
  ca_pem: string | null;
  mode: "per_event" | "state_sync";
  limits: Record<string, number>;
}
interface QueueSourceConfig {
  cluster_id: string;
  server: string;
  credential_id?: number | null;
  ca_pem?: string | null;
  ordering: "receipt_serial";
  limits: Record<string, number>;
}
export interface RabbitMQSourceConfig extends QueueSourceConfig {
  source_type: "rabbitmq";
  vhost: string;
  queue: string;
  credential_id: number;
  mode: "per_event" | "business_batch";
}
export interface RocketMQSourceConfig extends QueueSourceConfig {
  source_type: "rocketmq";
  topic: string;
  group_id: string;
  mode: "per_event";
}
export interface HttpSourceConfig {
  source_type: "http";
  credential_id: number;
  mode: "per_event" | "business_batch";
  ordering: "receipt_serial";
  limits: Record<string, number>;
}
export type SubscriptionSourceConfig =
  | KafkaSourceConfig
  | KubernetesSourceConfig
  | RabbitMQSourceConfig
  | RocketMQSourceConfig
  | HttpSourceConfig;
export type SubscriptionSourceType = SubscriptionSourceConfig["source_type"];
export const implementedSourceTypes: SubscriptionSourceType[] = [
  "kafka",
  "kubernetes",
  "rabbitmq",
  "rocketmq",
  "http",
];

export interface SyncBatchSummary {
  id: number;
  subscription_id: number;
  source_epoch: string | null;
  activation_id: number | null;
  config_version: number | null;
  scope_fingerprint: string;
  state:
    | "reading"
    | "processing"
    | "completed"
    | "failed"
    | "superseded"
    | "abandoned";
  read_complete: boolean;
  completion_committed: boolean;
  completion_abandoned: boolean;
  closed: boolean;
  drain_open: boolean;
  control_pending: ("sync.started" | "sync.completed" | "sync.failed")[];
  remaining_reserve: { count: number; bytes: number };
  pending_gap: {
    code: string;
    detected_at: string;
    after_completion_committed: boolean;
    relist_required: boolean;
  } | null;
  failure_code: string | null;
  created_at: string;
  finished_at: string | null;
}
export interface SyncControlDiagnostic {
  kind: "sync.started" | "sync.completed" | "sync.failed";
  receipt_id: number;
  state: string | null;
  latest_delivery_id: number | null;
  latest_delivery_ordinal: number | null;
  execution_id: number | null;
  effective_activation_id: number | null;
  revision_override_id: number | null;
}
export interface CompletionAbandonment {
  expected_config_version: number;
  source_epoch: string;
  receipt_id: number;
  expected_latest_delivery_id: number;
  expected_delivery_ordinal: number;
  expected_execution_id: number | null;
  request_id: string;
  reason: string;
  confirm_impact: true;
}
export interface CompletionAbandonmentResult {
  batch_id: number;
  receipt_id: number;
  latest_delivery_id: number;
  execution_id: number | null;
  request_id: string;
  actor_id: number | null;
  reason: string;
  outcome: "abandoned";
  business_result: "unconfirmed";
  cancelled_pending_delivery_id: number | null;
  disposition_id: number | null;
  settled_at: string;
}
export interface SyncBatchDetail extends SyncBatchSummary {
  resource_version: string | null;
  item_count: number;
  page_count: number;
  total_bytes: number;
  controls: SyncControlDiagnostic[];
  abandonment: CompletionAbandonmentResult | null;
}
export interface SyncBatchPage {
  items: SyncBatchSummary[];
  total: number;
  next_before_id: number | null;
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
  config: SubscriptionSourceConfig;
  latest_batch?: SyncBatchSummary | null;
  quota: Record<string, number>;
  limits: Record<string, number>;
  global_limits: Record<string, number>;
  oldest_pending_at: string | null;
  event_counts: Record<string, number>;
  available: boolean;
  capacity_paused?: boolean;
  access_level?: "admin" | "owner" | "edit" | "read" | null;
  created_at: string;
  updated_at: string;
  ingress_path?: string | null;
}

export interface SubscriptionCapabilities {
  enabled: boolean;
  available_sources: string[];
  sources?: { type: string; ordering: string; recovery: string; batches: boolean;
    skip_unaccepted: boolean; max_shards: number; processing_modes?: string[] }[];
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
  batch_id?: number | null;
  source_phase?:
    | "list"
    | "watch"
    | "sync.started"
    | "sync.completed"
    | "sync.failed"
    | null;
  coalesced_count?: number;
  first_source_event_id?: string | null;
  superseded_by_id?: number | null;
  processing_mode?: "per_event" | "business_batch";
  batch_execution_id?: number | null;
  batch_member_index?: number | null;
  batch_member_outcome?: "succeeded" | "failed" | "unknown" | "not_run" | null;
  batch_member_code?: string | null;
  batch_member_count?: number | null;
  deliveries: {
    id: number;
    ordinal: number;
    execution_id: number | null;
    state: string;
    revision_override_id: number | null;
    activation_override_id: number | null;
    effective_activation_id: number;
    processing_mode?: "per_event" | "business_batch";
    batch_execution_id?: number | null;
    batch_member_index?: number | null;
    batch_member_outcome?: "succeeded" | "failed" | "unknown" | "not_run" | null;
    batch_member_code?: string | null;
    batch_member_count?: number | null;
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
  kubernetes?: {
    scope_fingerprint: string;
    trust_verified: boolean;
    list_verified: boolean;
    watch_verified: boolean;
    group: "" | "apps";
    version: "v1";
    resource: "pods" | "deployments";
    namespace: string | null;
  };
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
    sub.delivery_state === "idle" &&
    (!sub.latest_batch || sub.latest_batch.closed) &&
    ["pending", "processing", "failed"].every(
      (state) => (sub.event_counts[state] ?? 0) === 0,
    )
  );
}

export function isCompletionEvent(event: SubscriptionEvent): boolean {
  return event.source_phase === "sync.completed";
}

export function completionReplayBlocked(
  event: SubscriptionEvent,
  batch?: SyncBatchSummary | null,
): boolean {
  return (
    isCompletionEvent(event) &&
    (!batch ||
      batch.completion_abandoned ||
      batch.failure_code !== null ||
      batch.closed ||
      event.state === "abandoned")
  );
}

export function canAbandonCompletion(
  sub: EventSubscription,
  batch: SyncBatchDetail,
): boolean {
  const control = batch.controls.find((item) => item.kind === "sync.completed");
  return (
    sub.desired_state === "disabled" &&
    sub.connection_state === "stopped" &&
    batch.completion_committed &&
    !batch.closed &&
    !batch.completion_abandoned &&
    batch.subscription_id === sub.id &&
    batch.source_epoch === sub.source_epoch &&
    control !== undefined &&
    control.latest_delivery_id !== null &&
    control.latest_delivery_ordinal !== null &&
    (control.state === "failed" ||
      (control.state === "pending" && control.execution_id === null))
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
