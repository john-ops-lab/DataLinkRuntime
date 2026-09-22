import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DlrDesignSystemProvider from "../design-system";
import { applyUiLocale } from "../i18n";
import type {
  CacheAdminStatus,
  CacheAdminView,
  CacheOperation,
  CacheSnapshotItem,
  Worker,
} from "../types";
import WorkerCachePanel from "./WorkerCachePanel";
import WorkerStatus from "./WorkerStatus";

const worker: Worker = {
  id: 7,
  name: "cache-worker",
  status: "online",
  last_heartbeat: "2026-09-23T00:00:00Z",
  capabilities: ["javascript"],
  protocol_version: 3,
  isolation_preflight_status: "passed",
  rabbitmq_execution_v3: true,
  isolation_capabilities: { cache_governance_v1: true },
};

const item: CacheSnapshotItem = {
  cache_key: "11-12",
  kind: "version",
  adapter_id: 11,
  version_id: 12,
  identity: {
    language: "javascript",
    source_sha256: "a".repeat(64),
    adapter_id: 11,
    version_id: 12,
  },
  digest: "b".repeat(64),
  bytes: 4096,
  pinned: false,
  rebuildability: "unknown" as const,
  reasons: ["cache_rebuild_unknown"],
};

function cacheView(status: CacheAdminStatus = "complete"): CacheAdminView {
  return {
    worker_id: worker.id,
    status,
    sampled_at: "2026-09-23T00:00:00Z",
    received_at: "2026-09-23T00:00:01Z",
    sample_id: "10000000-0000-4000-8000-000000000001",
    sequence: 9,
    complete: status === "complete",
    cursor: status === "complete" ? null : "cursor-one-sample-only",
    summary: {
      accounting: { committed_bytes: 8192, reserved_bytes: 0 },
      categories: {
        versions: { entries: 1, bytes: 4096, reclaimable_bytes: 2048, reasons: { cache_rebuild_unknown: 1 } },
        shared: { entries: 0, bytes: 0, reclaimable_bytes: 0, reasons: {} },
        staging: { entries: 0, bytes: 0, reclaimable_bytes: 0, reasons: {} },
        trash: { entries: 0, bytes: 0, reclaimable_bytes: 0, reasons: {} },
        unknown: { entries: 0, bytes: 0, reclaimable_bytes: 0, reasons: {} },
      },
      retained_reasons: { cache_rebuild_unknown: 1 },
      policy: {
        gc_enabled: false,
        pressure_gc_enabled: true,
        scan_interval_seconds: 60,
        idle_ttl_seconds: 3600,
        max_bytes: 268435456,
        min_idle_seconds: 0,
        high_watermark_percent: 85,
        low_watermark_percent: 70,
        disk_reserve_bytes: 1048576,
        max_delete_bytes_per_round: 1048576,
        max_delete_entries_per_round: 10,
        max_scan_entries_per_round: 200,
        max_scan_nodes_per_round: 1000,
        max_scan_hash_bytes_per_round: 1048576,
        max_scan_depth: 8,
        max_round_seconds: 5,
        staging_ttl_seconds: 3600,
        offline_protection: true,
        offline_mode: false,
        shared_cache_mode: "report_only",
      },
    },
    items: status === "unsupported" || status === "offline" || status === "owner_unconfirmed"
      ? []
      : [item],
    failed_guard_items: [],
    failed_guard_cursor: null,
    failed_guard_complete: true,
    failed_cleanup_items: [],
    failed_cleanup_next_cursor: null,
  };
}

function operation(overrides: Partial<CacheOperation> = {}): CacheOperation {
  return {
    operation_id: "00000000-0000-4000-8000-000000000001",
    worker_id: worker.id,
    kind: "preview",
    status: "pending",
    claim_epoch: 0,
    target_kind: null,
    target_operation_id: null,
    target_cleanup_id: null,
    result: null,
    error_code: null,
    created_at: "2026-09-23T00:00:00Z",
    claimed_at: null,
    finished_at: null,
    ...overrides,
  };
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPanel() {
  return render(
    <DlrDesignSystemProvider>
      <WorkerCachePanel worker={worker} open onClose={() => undefined} />
    </DlrDesignSystemProvider>,
  );
}

beforeEach(async () => {
  await applyUiLocale("zh-CN");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Worker cache administration", () => {
  it("keeps the Worker entry absent for non-admin direct renders", () => {
    const view = render(
      <WorkerStatus workers={[worker]} loading={false} error={null} canManageCache={false} />,
    );
    expect(screen.queryByRole("button", { name: "缓存管理" })).toBeNull();

    view.rerender(
      <WorkerStatus workers={[worker]} loading={false} error={null} canManageCache />,
    );
    expect(screen.getByRole("button", { name: "缓存管理" })).toBeTruthy();
  });

  it.each([
    ["incomplete", "部分快照"],
    ["offline", "节点离线"],
    ["unsupported", "不支持"],
    ["owner_unconfirmed", "缓存所有者未确认"],
  ] as const)("distinguishes %s from a complete zero snapshot", async (status, label) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView(status));
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    expect(await screen.findAllByText(label)).not.toHaveLength(0);
    if (status === "incomplete") {
      const checkboxes = await screen.findAllByRole("checkbox");
      fireEvent.click(checkboxes[1]);
      expect((screen.getByRole("button", { name: "预览所选项" }) as HTMLButtonElement).disabled).toBe(false);
      expect(screen.getByText(/不能与其他采样拼接/)).toBeTruthy();
    } else {
      const [selectAll] = screen.queryAllByRole("checkbox") as HTMLInputElement[];
      expect(selectAll.disabled).toBe(true);
    }
  });

  it("submits only selected keys and binds protection to observed identity and digest", async () => {
    const posts: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return response(operation({ kind: "protect" }), 202);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    const checkboxes = screen.getAllByRole("checkbox");
    expect((screen.getByRole("button", { name: "固定所选项" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByRole("button", { name: "固定所选项" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      kind: "protect",
      protect: [{
        adapter_id: 11,
        version_id: 12,
        identity: item.identity,
        digest: item.digest,
        pinned: true,
      }],
    });
    expect(posts[0].idempotency_key).toEqual(expect.any(String));
  });

  it("keeps rebuild proof distinct from pinning and bounds its expiry", async () => {
    let body: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") {
        body = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(operation({ kind: "protect" }), 202);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.change(screen.getByLabelText("重建证据说明"), {
      target: { value: "immutable source verified in the owned bundle" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认所选项可重建" }));

    await waitFor(() => expect(body).not.toBeNull());
    const bodyRecord = body as Record<string, unknown> | null;
    const [proof] = (bodyRecord?.protect ?? []) as Array<Record<string, unknown>>;
    expect(proof).toMatchObject({
      identity: item.identity,
      digest: item.digest,
      source_policy: "verified_offline",
      evidence_note: "immutable source verified in the owned bundle",
    });
    expect(proof).not.toHaveProperty("pinned");
    const validFor = new Date(String(proof.valid_until)).getTime() - Date.now();
    expect(validFor).toBeGreaterThan(0);
    expect(validFor).toBeLessThanOrEqual(60 * 60 * 1_000);
  });

  it("reuses the idempotency key after an uncertain duplicate submit", async () => {
    const keys: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { idempotency_key: string };
        keys.push(body.idempotency_key);
        throw new TypeError("uncertain transport failure");
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    const pin = screen.getByRole("button", { name: "固定所选项" });
    fireEvent.click(pin);
    await waitFor(() => expect(keys).toHaveLength(1));
    await waitFor(() => expect((pin as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(pin);
    await waitFor(() => expect(keys).toHaveLength(2));

    expect(keys[1]).toBe(keys[0]);
  });

  it("starts a new idempotent action when the operator changes the protection intent", async () => {
    const bodies: Array<{ idempotency_key: string; protect: Array<{ pinned?: boolean }> }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)) as typeof bodies[number]);
        throw new TypeError("uncertain transport failure");
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "固定所选项" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    await waitFor(() => expect((screen.getByRole("button", { name: "取消固定" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "取消固定" }));
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[0].protect[0].pinned).toBe(true);
    expect(bodies[1].protect[0].pinned).toBe(false);
    expect(bodies[1].idempotency_key).not.toBe(bodies[0].idempotency_key);
  });

  it("refreshes facts and explains a stale identity conflict without resubmitting", async () => {
    let cacheReads = 0;
    let posts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) {
        cacheReads += 1;
        return response(cacheView());
      }
      if (init?.method === "POST") {
        posts += 1;
        return response({
          detail: { code: "cache_snapshot_changed", message: "raw backend text" },
        }, 409);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "固定所选项" }));

    expect(await screen.findByText(/所选键在观测后发生变化/)).toBeTruthy();
    expect(document.body.textContent).not.toContain("raw backend text");
    await waitFor(() => expect(cacheReads).toBe(2));
    expect(posts).toBe(1);
  });

  it("polls only the accepted asynchronous operation through completion", async () => {
    const calls: string[] = [];
    let detailReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") return response(operation(), 202);
      if (url.endsWith(operation().operation_id)) {
        detailReads += 1;
        return response(operation(detailReads === 1
          ? { status: "running", claim_epoch: 1 }
          : {
              status: "completed",
              claim_epoch: 1,
              result: { complete: true, cursor: null, items: [item] },
              finished_at: "2026-09-23T00:00:03Z",
            }));
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("11-12");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "预览所选项" }));

    expect(await screen.findByText("已完成", {}, { timeout: 4_000 })).toBeTruthy();
    expect(screen.getByText("结果完整: true")).toBeTruthy();
    expect(screen.getByText("命中项: 1")).toBeTruthy();
    expect(detailReads).toBe(2);
    expect(calls.filter((call) => call.includes(operation().operation_id))).toHaveLength(2);
  }, 6_000);

  it("maps a failed management operation retry to management_operation_id", async () => {
    const failed = operation({ status: "failed", error_code: "cache_operation_failed" });
    let retryBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(cacheView());
      if (init?.method === "POST") {
        retryBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(operation({ kind: "retry" }), 202);
      }
      return response({ items: [failed], next_cursor: null });
    }));
    renderPanel();

    const operations = await screen.findByText(failed.operation_id);
    const row = operations.closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /重\s*试/ }));

    await waitFor(() => expect(retryBody).not.toBeNull());
    expect(retryBody).toMatchObject({
      kind: "retry",
      management_operation_id: failed.operation_id,
    });
    expect(retryBody).not.toHaveProperty("guard_operation_id");
    expect(retryBody).not.toHaveProperty("cleanup_id");
  });

  it("retries an observed failed guard by guard_operation_id", async () => {
    const guardId = "20000000-0000-4000-8000-000000000001";
    const view: CacheAdminView = {
      ...cacheView(),
      failed_guard_items: [{
        guard_operation_id: guardId,
        generation: 3,
        adapter_id: 11,
        version_id: 12,
        operation_kind: "gc",
        local_phase: "failed",
        resume_phase: "deleting",
        failure_count: 3,
        error_code: "cache_operation_failed",
        sampled_at: "2026-09-23T00:00:00Z",
      }],
    };
    let retryBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cache?")) return response(view);
      if (init?.method === "POST") {
        retryBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(operation({ kind: "retry", target_kind: "guard" }), 202);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    const guard = await screen.findByText(guardId);
    fireEvent.click(within(guard.closest("tr") as HTMLElement).getByRole("button", { name: /重\s*试/ }));

    await waitFor(() => expect(retryBody).not.toBeNull());
    expect(retryBody).toMatchObject({ kind: "retry", guard_operation_id: guardId });
    expect(retryBody).not.toHaveProperty("management_operation_id");
    expect(retryBody).not.toHaveProperty("cleanup_id");
  });

  it("retries a failed cleanup by cleanup_id and paginates only within one sample", async () => {
    const first: CacheAdminView = {
      ...cacheView(),
      failed_cleanup_items: [{ cleanup_id: 31, adapter_id: 11, attempts: 3, error_code: "cache_operation_failed" }],
      failed_cleanup_next_cursor: 30,
    };
    const second: CacheAdminView = {
      ...cacheView(),
      failed_cleanup_items: [{ cleanup_id: 30, adapter_id: 10, attempts: 4, error_code: null }],
      failed_cleanup_next_cursor: null,
    };
    let retryBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("failed_cursor=30")) return response(second);
      if (url.includes("/cache?")) return response(first);
      if (init?.method === "POST") {
        retryBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(operation({ kind: "retry", target_kind: "cleanup", target_cleanup_id: 31 }), 202);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    const cleanup = await screen.findByText("31");
    fireEvent.click(within(cleanup.closest("tr") as HTMLElement).getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(retryBody).not.toBeNull());
    expect(retryBody).toMatchObject({ kind: "retry", cleanup_id: 31 });
    expect(retryBody).not.toHaveProperty("management_operation_id");
    expect(retryBody).not.toHaveProperty("guard_operation_id");

    fireEvent.click(screen.getByRole("button", { name: "加载更多失败清理" }));
    expect(await screen.findByText("30")).toBeTruthy();
    expect(screen.getByText("31")).toBeTruthy();
  });

  it("replaces cleanup rows instead of merging when the sample changes during pagination", async () => {
    const first: CacheAdminView = {
      ...cacheView(),
      failed_cleanup_items: [{ cleanup_id: 31, adapter_id: 11, attempts: 3, error_code: null }],
      failed_cleanup_next_cursor: 30,
    };
    const changed: CacheAdminView = {
      ...cacheView(),
      sample_id: "30000000-0000-4000-8000-000000000001",
      sequence: 10,
      failed_cleanup_items: [{ cleanup_id: 40, adapter_id: 14, attempts: 3, error_code: null }],
      failed_cleanup_next_cursor: null,
    };
    let firstPageReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("failed_cursor=30")) return response(changed);
      if (url.includes("/cache?")) return response(++firstPageReads === 1 ? first : changed);
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();

    await screen.findByText("31");
    fireEvent.click(screen.getByRole("button", { name: "加载更多失败清理" }));
    expect(await screen.findByText("40")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("31")).toBeNull());
    expect(screen.getByText(changed.sample_id as string)).toBeTruthy();
  });
});
