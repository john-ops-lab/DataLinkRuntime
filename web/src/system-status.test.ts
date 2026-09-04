import { describe, expect, it } from "vitest";

import {
  aggregateSystemStatus,
  isHealthPayload,
  isWorkerExecutionReady,
  toHealthStatus,
  workerFleetStatus,
} from "./system-status";
import type { Worker } from "./types";

function readyWorker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 1,
    name: "worker-a",
    status: "online",
    last_heartbeat: "2026-09-04T00:00:00Z",
    capabilities: ["python", "javascript", "java"],
    protocol_version: 3,
    isolation_preflight_status: "passed",
    isolation_preflight_at: "2026-09-04T00:00:00Z",
    rabbitmq_execution_v3: true,
    isolation_capabilities: {
      cgroup_v2: true,
      mount_namespace: true,
      pid_namespace: true,
      memory_hard_limit: true,
      pids_hard_limit: true,
      tmpfs_hard_limit: true,
      bounded_output: true,
    },
    ...overrides,
  };
}

describe("system status aggregation", () => {
  it("accepts only structurally safe Control health payloads", () => {
    expect(isHealthPayload({ status: "ok", database: true, rabbitmq: {} })).toBe(true);
    expect(isHealthPayload({ status: "ok", database: true, rabbitmq: [] })).toBe(false);
    expect(isHealthPayload({ status: "ok", database: "true" })).toBe(false);
  });

  it("treats a healthy database with a degraded Control as a warning", () => {
    expect(toHealthStatus({ status: "degraded", database: true })).toBe("degraded");
    expect(toHealthStatus({ status: "degraded", database: false })).toBe("unreachable");
  });

  it("requires every execution gate instead of trusting the heartbeat", () => {
    expect(isWorkerExecutionReady(readyWorker())).toBe(true);
    expect(isWorkerExecutionReady(readyWorker({ rabbitmq_execution_v3: false }))).toBe(false);
    expect(isWorkerExecutionReady(readyWorker({
      isolation_capabilities: {
        ...readyWorker().isolation_capabilities,
        tmpfs_hard_limit: false,
      },
    }))).toBe(false);
  });

  it("maps all-ready, partially-ready, and unavailable fleets", () => {
    const ready = readyWorker();
    const unavailable = readyWorker({ id: 2, status: "offline" });
    expect(workerFleetStatus([ready], false, null)).toBe("normal");
    expect(workerFleetStatus([ready, unavailable], false, null)).toBe("warning");
    expect(workerFleetStatus([unavailable], false, null)).toBe("error");
    expect(workerFleetStatus([], false, null)).toBe("error");
    expect(workerFleetStatus([], true, null)).toBe("checking");
    expect(workerFleetStatus([ready], false, "network error")).toBe("error");
  });

  it("uses the worst confirmed fact and keeps unknown facts neutral", () => {
    const ready = readyWorker();
    expect(aggregateSystemStatus("ok", [ready], false, null)).toBe("normal");
    expect(aggregateSystemStatus("degraded", [ready], false, null)).toBe("warning");
    expect(aggregateSystemStatus("loading", [ready], false, null)).toBe("checking");
    expect(aggregateSystemStatus("loading", [], false, null)).toBe("error");
    expect(aggregateSystemStatus("unreachable", [], true, null)).toBe("error");
  });
});
