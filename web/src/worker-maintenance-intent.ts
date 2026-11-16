/** At most two non-secret pending Worker operations per tab and auth scope. */
import { isMaintenanceUuid } from "./worker-maintenance";
import type { WorkerMaintenanceAction } from "./types";

export const LEGACY_WORKER_MAINTENANCE_INTENT_KEY = "dlr-worker-maintenance-v1";
export const WORKER_MAINTENANCE_INTENT_KEY = "dlr-worker-maintenance-v2";

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

function invalid(): never { throw new Error("worker_maintenance_storage_invalid"); }
function bytes(value: string): number { return new TextEncoder().encode(value).byteLength; }
function parseIntent(value: unknown): WorkerMaintenanceIntent {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return invalid();
  const item = value as Record<string, unknown>;
  const keys = ["schema", "origin", "scope", "worker_id", "worker_identity_id", "action", "request_id", "expected_version"];
  if (Object.keys(item).length !== keys.length || !keys.every((key) => Object.hasOwn(item, key))
    || item.schema !== 1 || typeof item.origin !== "string" || typeof item.scope !== "string"
    || item.scope.length > 128 || !Number.isSafeInteger(item.worker_id) || Number(item.worker_id) < 1
    || !isMaintenanceUuid(item.worker_identity_id) || !isMaintenanceUuid(item.request_id)
    || !(item.action === "drain" || item.action === "activate")
    || !Number.isSafeInteger(item.expected_version) || Number(item.expected_version) < 1) return invalid();
  const intent: WorkerMaintenanceIntent = {
    schema: 1, origin: item.origin, scope: item.scope, worker_id: Number(item.worker_id),
    worker_identity_id: item.worker_identity_id, action: item.action,
    request_id: item.request_id, expected_version: Number(item.expected_version),
  };
  if (bytes(JSON.stringify(intent)) > 2048) return invalid();
  return intent;
}

interface StoredIntents { schema: 2; origin: string; scope: string; intents: WorkerMaintenanceIntent[] }
function parseStore(raw: string): StoredIntents {
  if (bytes(raw) > 8192) return invalid();
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || value === null || Array.isArray(value)) return invalid();
  const item = value as Record<string, unknown>;
  if (Object.keys(item).length !== 4 || item.schema !== 2 || typeof item.scope !== "string"
    || item.scope.length > 128 || typeof item.origin !== "string"
    || !Array.isArray(item.intents) || item.intents.length > 2) return invalid();
  const intents = item.intents.map(parseIntent);
  if (new Set(intents.map((intent) => intent.worker_id)).size !== intents.length
    || intents.some((intent) => intent.origin !== item.origin || intent.scope !== item.scope)) return invalid();
  return { schema: 2, origin: item.origin, scope: item.scope, intents };
}
function same(a: WorkerMaintenanceIntent, b: WorkerMaintenanceIntent): boolean {
  return a.schema === b.schema && a.origin === b.origin && a.scope === b.scope
    && a.worker_id === b.worker_id && a.worker_identity_id === b.worker_identity_id
    && a.action === b.action && a.request_id === b.request_id && a.expected_version === b.expected_version;
}
function writeStore(scope: string, intents: WorkerMaintenanceIntent[]): void {
  const raw = JSON.stringify({ schema: 2, origin: window.location.origin, scope, intents });
  parseStore(raw);
  if (intents.length === 0) sessionStorage.removeItem(WORKER_MAINTENANCE_INTENT_KEY);
  else {
    sessionStorage.setItem(WORKER_MAINTENANCE_INTENT_KEY, raw);
    if (sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY) !== raw) {
      throw new Error("worker_maintenance_storage_unavailable");
    }
  }
}
function readAll(scope: string): WorkerMaintenanceIntent[] {
  const raw = sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY);
  let intents: WorkerMaintenanceIntent[] = [];
  if (raw !== null) {
    const store = parseStore(raw);
    if (store.scope === scope && store.origin === window.location.origin) intents = store.intents;
    else sessionStorage.removeItem(WORKER_MAINTENANCE_INTENT_KEY);
  }
  const legacy = sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY);
  if (legacy !== null) {
    if (bytes(legacy) > 2048) return invalid();
    const intent = parseIntent(JSON.parse(legacy));
    if (intent.scope === scope && intent.origin === window.location.origin) {
      const existing = intents.find((item) => item.worker_id === intent.worker_id);
      if (existing && !same(existing, intent)) return invalid();
      if (!existing) {
        if (intents.length === 2) return invalid();
        intents = [...intents, intent];
      }
      // Keep v1 until the entire v2 value has been durably read back.
      writeStore(scope, intents);
    }
    if (sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY) === legacy) {
      sessionStorage.removeItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY);
    }
  }
  return intents;
}

export function readWorkerMaintenanceIntent(scope: string, workerId?: number): WorkerMaintenanceIntent | null {
  const intents = readAll(scope);
  if (workerId === undefined && intents.length > 1) return invalid();
  return intents.find((intent) => workerId === undefined || intent.worker_id === workerId) ?? null;
}

export function persistWorkerMaintenanceIntent(value: WorkerMaintenanceIntent): void {
  const intent = parseIntent(value);
  if (intent.origin !== window.location.origin) return invalid();
  const intents = readAll(intent.scope);
  const existing = intents.find((item) => item.worker_id === intent.worker_id);
  if (existing && !same(existing, intent)) return invalid();
  if (!existing) {
    if (intents.length === 2) return invalid();
    intents.push(intent);
  }
  writeStore(intent.scope, intents);
}

/** Late callbacks cannot erase another Worker, Principal or operation. */
export function clearWorkerMaintenanceIntent(intent?: WorkerMaintenanceIntent): void {
  try {
    if (!intent) {
      sessionStorage.removeItem(WORKER_MAINTENANCE_INTENT_KEY);
      sessionStorage.removeItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY);
      return;
    }
    const raw = sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY);
    if (raw !== null) {
      const store = parseStore(raw);
      if (store.scope === intent.scope && store.origin === intent.origin
        && store.intents.some((item) => same(item, intent))) {
        writeStore(store.scope, store.intents.filter((item) => !same(item, intent)));
      }
    }
    const legacy = sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY);
    if (legacy !== null && same(parseIntent(JSON.parse(legacy)), intent)) {
      sessionStorage.removeItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY);
    }
  } catch {
    // Retain unknown state on storage errors; the next POST still requires readback.
  }
}
