/** Shared secret-free fixtures for the W1-D component and Console tests. */
import { REQUIRED_ISOLATION_CAPABILITIES } from "./system-status";
import type { Worker, WorkerMaintenanceSnapshot } from "./types";

export function makeMaintenanceSnapshot(overrides: Partial<WorkerMaintenanceSnapshot> = {}): WorkerMaintenanceSnapshot {
  return {
    observation_schema: 1,
    observed_at: new Date().toISOString(),
    worker_id: 7,
    worker_identity_id: "11111111-1111-4111-8111-111111111111",
    protocol_version: 4,
    enrolled: true,
    scheduling_state: "active",
    maintenance_version: 4,
    capacity_state: "ready",
    online: true,
    instance_id: "22222222-2222-4222-8222-222222222222",
    instance_generation: 1,
    instance_lease_until: new Date(Date.now() + 30_000).toISOString(),
    isolation_preflight_status: "passed",
    isolation_preflight_at: new Date().toISOString(),
    isolation_capabilities: Object.fromEntries(REQUIRED_ISOLATION_CAPABILITIES.map((key) => [key, true])),
    capabilities: ["python"],
    active_attempts: 0,
    unsettled_attempts: 0,
    has_unreconciled_attempts: false,
    current_drain_operation: null,
    current_maintenance_action: "activate",
    last_recovery_disposition: null,
    ...overrides,
  };
}

export function makeObservedWorker(overrides: Partial<WorkerMaintenanceSnapshot> = {}): Worker {
  const snapshot = makeMaintenanceSnapshot(overrides);
  return {
    id: snapshot.worker_id, name: "remote-python", status: "online",
    last_heartbeat: snapshot.observed_at, capabilities: ["python"],
    protocol_version: 4, rabbitmq_execution_v3: false,
    maintenance: { snapshot, fresh: true, error: null, scope: "token:deployment" },
  };
}
