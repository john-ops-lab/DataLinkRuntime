import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DlrDesignSystemProvider from "../design-system";
import { applyUiLocale } from "../i18n";
import type { ControlHealthPayload } from "../system-status";
import type { Worker } from "../types";
import SystemStatusPanel from "./SystemStatusPanel";

const healthPayload: ControlHealthPayload = {
  service: "dlr-control",
  status: "ok",
  database: true,
  rabbitmq: {
    status: "ready",
    ingress: { status: "ready" },
    repair: { status: "ready" },
    broker: {
      queue_max_length: 2_000,
      queue_max_bytes: 67_108_864,
      headroom_messages: 1_980,
      headroom_bytes: 66_781_184,
      alerts: [],
    },
  },
  outbox: {
    status: "ok",
    pending_count: 0,
    pending_bytes: 0,
    oldest_age_seconds: 0,
  },
};

const worker: Worker = {
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
    cgroup_namespace_private: true,
    mount_namespace: true,
    pid_namespace: true,
    memory_hard_limit: true,
    pids_hard_limit: true,
    tmpfs_hard_limit: true,
    bounded_output: true,
    preflight_passed: true,
    resource_envelope_verified: true,
    cpu_hard_limit: true,
    swap_hard_limit: true,
    nofile_hard_limit: true,
    no_new_privileges: true,
    cgroup_kill: true,
    adapter_control_plane_hidden: true,
    adapter_mount_blocked: true,
    sandbox_cleanup: true,
  },
};

beforeEach(async () => {
  await applyUiLocale("zh-CN");
});

afterEach(async () => {
  await applyUiLocale("zh-CN");
});

describe("SystemStatusPanel", () => {
  it("shows Control, reliable-runtime, and Worker facts and refreshes them together", async () => {
    const onRefresh = vi.fn(async () => undefined);
    render(
      <DlrDesignSystemProvider>
        <SystemStatusPanel
          level="normal"
          health="ok"
          healthPayload={healthPayload}
          healthCheckedAt="2026-09-04T01:00:00Z"
          workers={[worker]}
          workersLoading={false}
          workersError={null}
          refreshing={false}
          onRefresh={onRefresh}
        />
      </DlrDesignSystemProvider>,
    );

    expect(screen.getByText("系统正常")).toBeTruthy();
    expect(screen.getByText("控制节点")).toBeTruthy();
    expect(screen.getByText("可靠运行时")).toBeTruthy();
    expect(screen.getByText("运行节点")).toBeTruthy();
    expect(screen.getByText("dlr-control")).toBeTruthy();
    expect(screen.getByText("1980 / 2000")).toBeTruthy();
    expect(screen.getByText("worker-a")).toBeTruthy();
    expect(screen.getByText("消息执行可用")).toBeTruthy();
    expect(screen.getByText("1/1 个运行节点可执行任务")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/v3|切流|灰度/i);

    fireEvent.click(screen.getByTestId("system-status-refresh"));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("uses explicit error copy when Control facts are unavailable", () => {
    render(
      <DlrDesignSystemProvider>
        <SystemStatusPanel
          level="error"
          health="unreachable"
          healthPayload={null}
          healthCheckedAt="2026-09-04T01:00:00Z"
          workers={[]}
          workersLoading={false}
          workersError="network error"
          refreshing={false}
          onRefresh={async () => undefined}
        />
      </DlrDesignSystemProvider>,
    );

    expect(screen.getByText("系统异常")).toBeTruthy();
    expect(screen.getByText("Control 状态不可达或响应无效。")).toBeTruthy();
    expect(screen.getByText("network error").getAttribute("role")).toBe("alert");
  });
});
