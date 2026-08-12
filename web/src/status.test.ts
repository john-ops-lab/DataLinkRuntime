import { expect, it } from "vitest";

import { productionDisplayState, productionRunningVersionId } from "./status";
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

it("derives abnormal only from a failed or timed-out latest production run", () => {
  expect(
    productionDisplayState(
      adapter({ running_execution_id: null, last_production_execution_status: "failed" }),
    ),
  ).toBe("abnormal");
  expect(
    productionDisplayState(
      adapter({ running_execution_id: null, last_production_execution_status: "timeout" }),
    ),
  ).toBe("abnormal");
  expect(
    productionDisplayState(
      adapter({ running_execution_id: null, last_production_execution_status: "cancelled" }),
    ),
  ).toBe("running");
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
