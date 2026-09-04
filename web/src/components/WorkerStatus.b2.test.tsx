import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { applyUiLocale } from "../i18n";
import type { Worker } from "../types";
import WorkerStatus from "./WorkerStatus";

const worker: Worker = {
  id: 3,
  name: "b2-worker",
  status: "online",
  last_heartbeat: "2026-08-28T00:00:00Z",
  capabilities: ["python"],
  protocol_version: 3,
  rabbitmq_execution_v3: false,
  isolation_preflight_status: "passed",
  isolation_capabilities: {
    cgroup_v2: true,
    mount_namespace: true,
    pid_namespace: true,
    memory_hard_limit: true,
    pids_hard_limit: true,
    tmpfs_hard_limit: true,
    bounded_output: true,
  },
};

afterEach(async () => {
  await applyUiLocale("zh-CN");
});

describe("Issue #130 B2 Worker capability facts", () => {
  it("shows isolation preflight and keeps the v3 gate visibly paused", () => {
    render(<WorkerStatus workers={[worker]} loading={false} error={null} />);

    expect(screen.getByText("隔离预检通过")).toBeTruthy();
    expect(screen.getByText("Worker v3 已暂停")).toBeTruthy();
    expect(screen.getByText("不可执行")).toBeTruthy();
    expect(screen.getByText(/cgroup v2/)).toBeTruthy();
  });

  it("does not infer sandbox readiness from an online heartbeat", () => {
    render(<WorkerStatus workers={[{
      ...worker,
      isolation_preflight_status: "failed",
      isolation_capabilities: {},
    }]} loading={false} error={null} />);

    expect(screen.getByText("隔离预检失败")).toBeTruthy();
    expect(screen.queryByText("隔离预检通过")).toBeNull();
  });
});
