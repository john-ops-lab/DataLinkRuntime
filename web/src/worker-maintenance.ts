/** Strict, secret-free admin observation parsing; no execution authority. */
import { REQUIRED_ISOLATION_CAPABILITIES } from "./system-status";
import type {
  WorkerDrainOperation,
  WorkerMaintenanceReceipt,
  WorkerMaintenanceSnapshot,
  WorkerRecoveryDisposition,
} from "./types";

export const WORKER_OBSERVATION_MAX_AGE_MS = 30_000;
export const WORKER_MAINTENANCE_TIMEOUT_MS = 5_000;
const UUID_PATTERN = /^(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isMaintenanceUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function integer(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
}

function time(value: unknown): value is string {
  return typeof value === "string"
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
}

function nullableTime(value: unknown): value is string | null {
  return value === null || time(value);
}

function scheduling(value: unknown): value is WorkerMaintenanceSnapshot["scheduling_state"] {
  return value === "active" || value === "draining" || value === "disabled";
}

function drainOperation(value: unknown): value is WorkerDrainOperation {
  return record(value) && isMaintenanceUuid(value.request_id) && value.action === "drain"
    && integer(value.expected_version, 1) && integer(value.result_version, 2)
    && value.result_version === value.expected_version + 1 && typeof value.same_actor === "boolean";
}

function disposition(value: unknown): value is WorkerRecoveryDisposition {
  return record(value) && isMaintenanceUuid(value.operation_id) && isMaintenanceUuid(value.disposition_id)
    && value.kind === "platform_terminal" && value.reason === "result_uncommitted_outcome_unknown"
    && integer(value.execution_id, 1) && integer(value.attempt_id, 1)
    && ((value.attempt_status === "worker_lost"
      && (value.execution_status === "dead_letter" || value.execution_status === "cancelled"))
      || (value.attempt_status === "cancelled" && value.execution_status === "cancelled"));
}

function pickDrain(value: WorkerDrainOperation): WorkerDrainOperation {
  return {
    request_id: value.request_id, action: "drain", expected_version: value.expected_version,
    result_version: value.result_version, same_actor: value.same_actor,
  };
}

function pickDisposition(value: WorkerRecoveryDisposition): WorkerRecoveryDisposition {
  return {
    operation_id: value.operation_id, disposition_id: value.disposition_id,
    kind: value.kind, reason: value.reason, execution_id: value.execution_id,
    attempt_id: value.attempt_id, attempt_status: value.attempt_status, execution_status: value.execution_status,
  };
}

/** Missing/unknown contracts remain unknown, including old Control responses. */
export function parseWorkerMaintenanceSnapshot(value: unknown, workerId: number): WorkerMaintenanceSnapshot | null {
  if (!record(value) || value.observation_schema !== 1 || value.protocol_version !== 4
    || !integer(value.worker_id, 1) || value.worker_id !== workerId
    || !isMaintenanceUuid(value.worker_identity_id) || !time(value.observed_at)
    || typeof value.enrolled !== "boolean" || !scheduling(value.scheduling_state)
    || !integer(value.maintenance_version, 1)
    || !(value.capacity_state === "unknown" || value.capacity_state === "reconciling" || value.capacity_state === "ready" || value.capacity_state === "quarantined")
    || typeof value.online !== "boolean"
    || !(value.instance_id === null || isMaintenanceUuid(value.instance_id))
    || !integer(value.instance_generation) || !nullableTime(value.instance_lease_until)
    || !(value.isolation_preflight_status === "unknown" || value.isolation_preflight_status === "passed" || value.isolation_preflight_status === "failed")
    || !nullableTime(value.isolation_preflight_at) || !record(value.isolation_capabilities)
    || !REQUIRED_ISOLATION_CAPABILITIES.every((key) => typeof (value.isolation_capabilities as Record<string, unknown>)[key] === "boolean")
    || !Array.isArray(value.capabilities) || value.capabilities.length > 5
    || !value.capabilities.every((item) => typeof item === "string" && ["python", "javascript", "typescript", "java", "go"].includes(item))
    || !integer(value.active_attempts) || !integer(value.unsettled_attempts)
    || value.active_attempts > value.unsettled_attempts || typeof value.has_unreconciled_attempts !== "boolean"
    || !(value.current_drain_operation === null || drainOperation(value.current_drain_operation))
    || !(value.current_maintenance_action === null || value.current_maintenance_action === "drain"
      || value.current_maintenance_action === "activate" || value.current_maintenance_action === "reconcile")
    || !(value.last_recovery_disposition === null || disposition(value.last_recovery_disposition))) return null;
  if ((value.instance_id === null && value.instance_lease_until !== null)
    || (value.instance_id !== null && (value.instance_generation === 0 || value.instance_lease_until === null))
    || (value.current_drain_operation !== null
      && (value.current_drain_operation.result_version !== value.maintenance_version
        || value.current_maintenance_action !== "drain"))
    || (value.current_maintenance_action === "drain" && value.current_drain_operation === null)) return null;
  // Copy only the documented observation. Unknown fields cannot become UI or
  // storage data even if a mismatched Control accidentally returns more.
  return {
    observation_schema: 1, observed_at: value.observed_at,
    worker_id: value.worker_id, worker_identity_id: value.worker_identity_id,
    protocol_version: 4, enrolled: value.enrolled, scheduling_state: value.scheduling_state,
    maintenance_version: value.maintenance_version,
    capacity_state: value.capacity_state as WorkerMaintenanceSnapshot["capacity_state"],
    online: value.online, instance_id: value.instance_id,
    instance_generation: value.instance_generation, instance_lease_until: value.instance_lease_until,
    isolation_preflight_status: value.isolation_preflight_status as WorkerMaintenanceSnapshot["isolation_preflight_status"],
    isolation_preflight_at: value.isolation_preflight_at,
    isolation_capabilities: Object.fromEntries(REQUIRED_ISOLATION_CAPABILITIES.map((key) => [key, (value.isolation_capabilities as Record<string, boolean>)[key]])),
    capabilities: [...value.capabilities], active_attempts: value.active_attempts,
    unsettled_attempts: value.unsettled_attempts, has_unreconciled_attempts: value.has_unreconciled_attempts,
    current_drain_operation: value.current_drain_operation === null ? null : pickDrain(value.current_drain_operation),
    current_maintenance_action: value.current_maintenance_action,
    last_recovery_disposition: value.last_recovery_disposition === null ? null : pickDisposition(value.last_recovery_disposition),
  };
}

export function parseWorkerMaintenanceReceipt(value: unknown, workerId: number, requestId: string): WorkerMaintenanceReceipt | null {
  if (!record(value) || !record(value.operation) || !record(value.current)) return null;
  const op = value.operation;
  const current = value.current;
  if (!isMaintenanceUuid(op.request_id) || op.request_id !== requestId || !integer(op.worker_id, 1) || op.worker_id !== workerId
    || !(op.action === "drain" || op.action === "activate") || !integer(op.expected_version, 1)
    || !integer(op.result_version, 2) || op.result_version !== op.expected_version + 1
    || typeof op.same_actor !== "boolean" || !time(op.created_at)
    || !isMaintenanceUuid(current.worker_identity_id) || !integer(current.maintenance_version, 1)
    || !scheduling(current.scheduling_state) || !time(current.observed_at)
    || typeof value.applies_to_current_version !== "boolean"
    || value.applies_to_current_version !== (op.result_version === current.maintenance_version)) return null;
  return {
    operation: {
      request_id: op.request_id, worker_id: op.worker_id, action: op.action,
      expected_version: op.expected_version, result_version: op.result_version,
      created_at: op.created_at, same_actor: op.same_actor,
    },
    current: {
      worker_identity_id: current.worker_identity_id, maintenance_version: current.maintenance_version,
      scheduling_state: current.scheduling_state, observed_at: current.observed_at,
    },
    applies_to_current_version: value.applies_to_current_version,
  };
}
