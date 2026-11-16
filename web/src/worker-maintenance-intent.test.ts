import { afterEach, expect, it, vi } from "vitest";
import {
  clearWorkerMaintenanceIntent, LEGACY_WORKER_MAINTENANCE_INTENT_KEY,
  persistWorkerMaintenanceIntent, readWorkerMaintenanceIntent, WORKER_MAINTENANCE_INTENT_KEY,
  type WorkerMaintenanceIntent,
} from "./worker-maintenance-intent";

const a: WorkerMaintenanceIntent = {
  schema: 1, scope: "token:deployment", origin: window.location.origin, worker_id: 7,
  worker_identity_id: "11111111-1111-4111-8111-111111111111", action: "activate",
  request_id: "33333333-3333-4333-8333-333333333333", expected_version: 4,
};
const b: WorkerMaintenanceIntent = { ...a, worker_id: 8,
  worker_identity_id: "22222222-2222-4222-8222-222222222222",
  request_id: "44444444-4444-4444-8444-444444444444" };
afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); });

it("retains independent unknowns and clears only the exact matching tuple", () => {
  persistWorkerMaintenanceIntent(a); persistWorkerMaintenanceIntent(b);
  clearWorkerMaintenanceIntent({ ...a, request_id: b.request_id });
  expect(readWorkerMaintenanceIntent(a.scope, 7)).toEqual(a);
  clearWorkerMaintenanceIntent(a);
  expect(readWorkerMaintenanceIntent(a.scope, 7)).toBeNull();
  expect(readWorkerMaintenanceIntent(a.scope, 8)).toEqual(b);
  clearWorkerMaintenanceIntent(b);
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});
it("never replaces an unknown with a new ID, identity or third Worker", () => {
  persistWorkerMaintenanceIntent(a); persistWorkerMaintenanceIntent(b);
  const raw = sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY);
  expect(() => persistWorkerMaintenanceIntent({ ...a, request_id: b.request_id })).toThrow();
  expect(() => persistWorkerMaintenanceIntent({ ...a, worker_identity_id: b.worker_identity_id })).toThrow();
  expect(() => persistWorkerMaintenanceIntent({ ...a, worker_id: 9 })).toThrow();
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBe(raw);
});
it("migrates v1 only after exact v2 persistence and preserves another Worker", () => {
  persistWorkerMaintenanceIntent(b);
  sessionStorage.setItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY, JSON.stringify(a));
  expect(readWorkerMaintenanceIntent(a.scope, 7)).toEqual(a);
  expect(readWorkerMaintenanceIntent(a.scope, 8)).toEqual(b);
  expect(sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});
it("preserves legacy unknown when v2 persistence fails or readback differs", () => {
  const raw = JSON.stringify(a);
  sessionStorage.setItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY, raw);
  const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => undefined);
  expect(() => readWorkerMaintenanceIntent(a.scope, 7)).toThrow();
  expect(sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY)).toBe(raw);
  write.mockRestore();
  expect(readWorkerMaintenanceIntent(a.scope, 7)).toEqual(a);
});
it("does not merge a conflicting legacy tuple or evict a current unknown", () => {
  persistWorkerMaintenanceIntent({ ...a, request_id: b.request_id });
  const raw = JSON.stringify(a);
  sessionStorage.setItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY, raw);
  expect(() => readWorkerMaintenanceIntent(a.scope, 7)).toThrow();
  expect(sessionStorage.getItem(LEGACY_WORKER_MAINTENANCE_INTENT_KEY)).toBe(raw);
});
it("ignores stale clear callbacks after a Principal switch", () => {
  const current = { ...a, scope: "account:other" };
  persistWorkerMaintenanceIntent(current);
  clearWorkerMaintenanceIntent(a);
  expect(readWorkerMaintenanceIntent(current.scope, 7)).toEqual(current);
  clearWorkerMaintenanceIntent();
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toBeNull();
});
it("rejects malformed or oversized records before any new tuple is saved", () => {
  sessionStorage.setItem(WORKER_MAINTENANCE_INTENT_KEY, "x".repeat(8193));
  expect(() => persistWorkerMaintenanceIntent(a)).toThrow();
  expect(sessionStorage.getItem(WORKER_MAINTENANCE_INTENT_KEY)).toHaveLength(8193);
});
