import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Adapter } from "../types";
import AdapterCatalog from "./AdapterCatalog";

function makeAdapter(id: number, name: string, overrides: Partial<Adapter> = {}): Adapter {
  return {
    id,
    name,
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: null,
    created_at: "2026-08-15T00:00:00Z",
    updated_at: "2026-08-15T00:00:00Z",
    ...overrides,
  };
}

it("exposes each Task and Webhook runtime status in the catalog item name", () => {
  const adapters = [
    makeAdapter(1, "task-idle"),
    makeAdapter(2, "task-scheduled", { runtime_locked: true }),
    makeAdapter(3, "task-running", { running_execution_id: 31 }),
    makeAdapter(4, "webhook-stopped", { adapter_type: "webhook" }),
    makeAdapter(5, "webhook-receiving", { adapter_type: "webhook", runtime_locked: true }),
    makeAdapter(6, "webhook-calling", {
      adapter_type: "webhook",
      running_execution_id: 61,
    }),
  ];

  render(
    <AdapterCatalog
      adapters={adapters}
      selectedId={null}
      busy={false}
      onSelect={vi.fn()}
      onCreate={vi.fn(async () => false)}
      versionSeqById={new Map()}
      workers={[]}
    />,
  );

  expect(
    screen.getByRole("button", {
      name: "task-idle，Python · 手动运行 · 任务状态：空闲 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-scheduled，Python · 手动运行 · 任务状态：定时运行中 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "task-running，Python · 手动运行 · 任务状态：运行中 · 执行 #31 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-stopped，Python · Webhook 状态：已停止 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-receiving，Python · Webhook 状态：接收中 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", {
      name: "webhook-calling，Python · Webhook 状态：调用中 · 调用 #61 · 未保存 · 运行节点未配置",
    }),
  ).toBeTruthy();
});
