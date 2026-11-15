/** One bounded, non-secret pending maintenance intent per browser tab. */
import { isMaintenanceUuid } from "./worker-maintenance";
import type { WorkerMaintenanceAction } from "./types";

export const WORKER_MAINTENANCE_INTENT_KEY = "dlr-worker-maintenance-v1";

export interface WorkerMaintenanceIntent {
  schema: 1;
  origin: string;
  scope: string;
  worker_id: number;
  worker_identity_id: string;
  action: WorkerMaintenanceAction;
  request_id: string;
  expected_version: number;
}

function serialize(intent: WorkerMaintenanceIntent): string {
  // Never serialize a response, Principal, credential, proof or binding.
  return JSON.stringify({
    schema: 1, origin: intent.origin, scope: intent.scope, worker_id: intent.worker_id,
    worker_identity_id: intent.worker_identity_id, action: intent.action,
    request_id: intent.request_id, expected_version: intent.expected_version,
  });
}

export function readWorkerMaintenanceIntent(scope: string): WorkerMaintenanceIntent | null {
  const raw = sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY);
  if (raw === null) return null;
  if (raw.length > 2048) throw new Error("worker_maintenance_storage_invalid");
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("worker_maintenance_storage_invalid");
  const item = value as Record<string, unknown>;
  const keys = ["schema", "origin", "scope", "worker_id", "worker_identity_id", "action", "request_id", "expected_version"];
  if (Object.keys(item).length !== keys.length || !keys.every((key) => Object.hasOwn(item, key))
    || item.schema !== 1 || typeof item.origin !== "string" || typeof item.scope !== "string"
    || item.scope.length > 128 || !Number.isSafeInteger(item.worker_id) || Number(item.worker_id) < 1
    || !isMaintenanceUuid(item.worker_identity_id) || !isMaintenanceUuid(item.request_id)
    || !(item.action === "drain" || item.action === "activate")
    || !Number.isSafeInteger(item.expected_version) || Number(item.expected_version) < 1) {
    throw new Error("worker_maintenance_storage_invalid");
  }
  if (item.scope !== scope || item.origin !== window.location.origin) {
    sessionStorage.removeItem(WORKER_MAINTENANCE_INTENT_KEY);
    return null;
  }
  return {
    schema: 1, origin: item.origin, scope: item.scope, worker_id: Number(item.worker_id),
    worker_identity_id: item.worker_identity_id, action: item.action,
    request_id: item.request_id, expected_version: Number(item.expected_version),
  };
}

export function persistWorkerMaintenanceIntent(intent: WorkerMaintenanceIntent): void {
  const serialized = serialize(intent);
  sessionStorage.setItem(WORKER_MAINTENANCE_INTENT_KEY, serialized);
  const readBack = readWorkerMaintenanceIntent(intent.scope);
  if (readBack === null || serialize(readBack) !== serialized) throw new Error("worker_maintenance_storage_unavailable");
}

/** A late response must not erase a newer Principal's or action's intent. */
export function clearWorkerMaintenanceIntent(intent?: WorkerMaintenanceIntent): void {
  try {
    if (intent && sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY) !== serialize(intent)) return;
    sessionStorage.removeItem(WORKER_MAINTENANCE_INTENT_KEY);
  } catch {
    // Authentication transitions still proceed. A blocked store cannot be
    // used to dispatch an action; the next write must persist and read back.
  }
}
