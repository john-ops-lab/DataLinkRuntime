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
}

export const REQUIRED_ISOLATION_CAPABILITIES = [
  "cgroup_v2",
  "mount_namespace",
  "pid_namespace",
  "memory_hard_limit",
  "pids_hard_limit",
  "tmpfs_hard_limit",
  "bounded_output",
] as const;

export type RequiredIsolationCapability = (typeof REQUIRED_ISOLATION_CAPABILITIES)[number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalRecord(value: unknown): boolean {
  return value === undefined || isRecord(value);
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
  );
}

export function toHealthStatus(payload: ControlHealthPayload): HealthStatus {
  if (!payload.database) {
    return "unreachable";
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

/** A heartbeat alone is insufficient: every v3 execution gate must be authoritative. */
export function isWorkerExecutionReady(worker: Worker): boolean {
  return (
    worker.status === "online"
    && worker.protocol_version === 3
    && worker.isolation_preflight_status === "passed"
    && worker.rabbitmq_execution_v3 === true
    && missingWorkerCapabilities(worker).length === 0
  );
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
  const readyCount = workers.filter(isWorkerExecutionReady).length;
  if (readyCount === workers.length) {
    return "normal";
  }
  return readyCount === 0 ? "error" : "warning";
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
