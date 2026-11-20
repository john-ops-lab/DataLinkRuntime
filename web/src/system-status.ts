import type { Worker } from "./types";

export type HealthStatus = "loading" | "ok" | "degraded" | "unreachable";
export type SystemStatusLevel = "checking" | "normal" | "warning" | "error";

export interface RuntimeHealthComponent {
  configured?: boolean;
  enabled?: boolean;
  status?: string;
  ready?: boolean;
  last_error_code?: string | null;
  worker_count?: number;
}

export interface BrokerHealth {
  queue_max_length?: number;
  queue_max_bytes?: number;
  configured_headroom_messages?: number;
  configured_headroom_bytes?: number;
  headroom_messages?: number | null;
  headroom_bytes?: number | null;
  alerts?: unknown[];
}

export interface RabbitMqHealth extends RuntimeHealthComponent {
  ingress?: RuntimeHealthComponent;
  repair?: RuntimeHealthComponent;
  broker?: BrokerHealth;
}

export interface OutboxHealth {
  status?: string;
  pending_count?: number | null;
  pending_bytes?: number | null;
  oldest_age_seconds?: number | null;
  pending_oldest_age_seconds?: number | null;
  protection_reasons?: string[];
  error_code?: string | null;
}

export interface ControlHealthPayload {
  service?: string;
  status: string;
  database: boolean;
  rabbitmq?: RabbitMqHealth;
  outbox?: OutboxHealth;
  control_ha?: {
    node: { slot: "a" | "b"; registered: boolean; capacity_compatible: boolean | null };
    cluster: { registered: number | null; max_nodes: number };
    features: Record<ControlHaFeature, string>;
  };
}

export const CONTROL_HA_FEATURES = [
  "new_ingress", "postgres_capacity", "mq_publish", "managed_files",
  "builtin_packages", "ai_attachments",
] as const;
type ControlHaFeature = (typeof CONTROL_HA_FEATURES)[number];
const CONTROL_HA_STATES: ReadonlySet<unknown> = new Set([
  "available", "unavailable", "low", "conflict", "awaiting_shared_backend",
]);

export const REQUIRED_ISOLATION_CAPABILITIES = [
  "cgroup_v2",
  "cgroup_namespace_private",
  "mount_namespace",
  "pid_namespace",
  "memory_hard_limit",
  "pids_hard_limit",
  "tmpfs_hard_limit",
  "bounded_output",
  "preflight_passed",
  "resource_envelope_verified",
  "cpu_hard_limit",
  "swap_hard_limit",
  "nofile_hard_limit",
  "no_new_privileges",
  "cgroup_kill",
  "adapter_control_plane_hidden",
  "adapter_mount_blocked",
  "sandbox_cleanup",
] as const;

export type RequiredIsolationCapability = (typeof REQUIRED_ISOLATION_CAPABILITIES)[number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalRecord(value: unknown): boolean {
  return value === undefined || isRecord(value);
}

function isControlHa(value: unknown): boolean {
  if (value === undefined) return true;
  if (!isRecord(value) || !isRecord(value.node) || !isRecord(value.cluster) || !isRecord(value.features)) {
    return false;
  }
  const { node, cluster, features } = value;
  return (node.slot === "a" || node.slot === "b")
    && typeof node.registered === "boolean"
    && (node.capacity_compatible === null || typeof node.capacity_compatible === "boolean")
    && cluster.max_nodes === 2
    && (cluster.registered === null || cluster.registered === 0 || cluster.registered === 1 || cluster.registered === 2)
    && CONTROL_HA_FEATURES.every((feature) => CONTROL_HA_STATES.has(features[feature]));
}

/** Reject malformed health payloads instead of turning unknown facts green. */
export function isHealthPayload(value: unknown): value is ControlHealthPayload {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.status === "string"
    && typeof value.database === "boolean"
    && (value.service === undefined || typeof value.service === "string")
    && isOptionalRecord(value.rabbitmq)
    && isOptionalRecord(value.outbox)
    && isControlHa(value.control_ha)
  );
}

export function toHealthStatus(payload: ControlHealthPayload): HealthStatus {
  if (!payload.database) {
    return "unreachable";
  }
  if (
    payload.rabbitmq?.enabled === true
    && (
      payload.rabbitmq.ready !== true
      || payload.rabbitmq.status === "degraded"
      || payload.rabbitmq.ingress?.status === "degraded"
    )
  ) {
    return "degraded";
  }
  if (payload.status === "ok") {
    return "ok";
  }
  if (payload.status === "degraded") {
    return "degraded";
  }
  return "unreachable";
}

export function missingWorkerCapabilities(worker: Worker): RequiredIsolationCapability[] {
  return REQUIRED_ISOLATION_CAPABILITIES.filter(
    (capability) => worker.isolation_capabilities?.[capability] !== true,
  );
}

/** A heartbeat alone is insufficient: execution and isolation capabilities must pass. */
export function isWorkerExecutionReady(worker: Worker): boolean {
  return (
    worker.status === "online"
    && worker.protocol_version === 3
    && worker.isolation_preflight_status === "passed"
    && worker.rabbitmq_execution_v3 === true
    && missingWorkerCapabilities(worker).length === 0
  );
}

/** v4 is an administrator observation, never a substitute for Claim authority. */
export function workerStatusLevel(worker: Worker): SystemStatusLevel {
  if (worker.protocol_version !== 4) return isWorkerExecutionReady(worker) ? "normal" : "error";
  const view = worker.maintenance;
  const snapshot = view?.snapshot;
  if (!snapshot || !view.fresh || view.error !== null) return "checking";
  return snapshot.worker_id === worker.id && snapshot.enrolled && snapshot.online
    && snapshot.scheduling_state === "active" && snapshot.capacity_state === "ready"
    && snapshot.isolation_preflight_status === "passed"
    && REQUIRED_ISOLATION_CAPABILITIES.every((key) => snapshot.isolation_capabilities[key] === true)
    && restrictedWorkerCapabilities(snapshot.capabilities)
    && !snapshot.has_unreconciled_attempts ? "normal" : "warning";
}

export function restrictedWorkerCapabilities(value: readonly string[]): boolean {
  return value[0] === "python" && (value.length === 1 || (value[1] === "javascript"
    && (value.length === 2 || (value[2] === "typescript" && value[3] === "java"
      && (value.length === 4 || (value.length === 5 && value[4] === "go"))))));
}

export function workerFleetStatus(
  workers: Worker[],
  loading: boolean,
  error: string | null,
): SystemStatusLevel {
  if (error !== null) {
    return "error";
  }
  if (loading) {
    return "checking";
  }
  if (workers.length === 0) {
    return "error";
  }
  const levels = workers.map(workerStatusLevel);
  const readyCount = levels.filter((level) => level === "normal").length;
  if (readyCount === workers.length) {
    return "normal";
  }
  if (levels.includes("error")) return readyCount === 0 ? "error" : "warning";
  return levels.includes("warning") ? "warning" : "checking";
}

function controlStatusLevel(health: HealthStatus): SystemStatusLevel {
  if (health === "ok") {
    return "normal";
  }
  if (health === "degraded") {
    return "warning";
  }
  return health === "unreachable" ? "error" : "checking";
}

const LEVEL_PRIORITY: Record<SystemStatusLevel, number> = {
  normal: 0,
  checking: 1,
  warning: 2,
  error: 3,
};

export function aggregateSystemStatus(
  health: HealthStatus,
  workers: Worker[],
  workersLoading: boolean,
  workersError: string | null,
): SystemStatusLevel {
  const control = controlStatusLevel(health);
  const workerFleet = workerFleetStatus(workers, workersLoading, workersError);
  return LEVEL_PRIORITY[control] >= LEVEL_PRIORITY[workerFleet] ? control : workerFleet;
}

export function systemStatusBadgeStatus(
  level: SystemStatusLevel,
): "success" | "warning" | "error" | "default" {
  if (level === "normal") {
    return "success";
  }
  if (level === "warning") {
    return "warning";
  }
  return level === "error" ? "error" : "default";
}
