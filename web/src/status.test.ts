import { expect, it } from "vitest";

import {
  hasLastProductionExecutionFailure,
  productionDisplayState,
  productionRunningVersionId,
} from "./status";
import type { Adapter } from "./types";

function adapter(overrides: Partial<Adapter>): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
    latest_version_id: 10,
    published_version_id: 10,
    production_state: "running",
    created_at: "2026-08-12T00:00:00Z",
    updated_at: "2026-08-12T00:00:00Z",
    ...overrides,
  };
}

it("keeps a started production entry healthy and idle after a successful run", () => {
  const value = adapter({
    running_execution_id: null,
    running_version_id: null,
    last_production_execution_id: 77,
    last_production_execution_status: "succeeded",
    last_production_version_id: 10,
  });

  expect(productionDisplayState(value)).toBe("running");
  expect(productionRunningVersionId(value)).toBe(10);
});

it("keeps a started entry running and idle even when the previous lifecycle failed (M5.1)", () => {
  // Start no longer creates an Execution: running + no active Execution is the
  // legal idle state, and a failed/timeout Execution from the previous
  // lifecycle must not derive a lifecycle-abnormal entry (no Stop → Start loop).
  for (const lastStatus of ["failed", "timeout", "cancelled"] as const) {
    expect(
      productionDisplayState(
        adapter({ running_execution_id: null, last_production_execution_status: lastStatus }),
      ),
    ).toBe("running");
  }
  const failed = adapter({
    running_execution_id: null,
    last_production_execution_id: 91,
    last_production_execution_status: "failed",
  });
  expect(hasLastProductionExecutionFailure(failed)).toBe(true);
  expect(hasLastProductionExecutionFailure(adapter({ running_execution_id: null }))).toBe(false);
});

it("derives stopping only while a stopped entry still owns an active execution", () => {
  expect(
    productionDisplayState(
      adapter({ production_state: "stopped", running_execution_id: 77 }),
    ),
  ).toBe("stopping");
  expect(
    productionDisplayState(
      adapter({ production_state: "stopped", running_execution_id: null }),
    ),
  ).toBe("stopped");
});
