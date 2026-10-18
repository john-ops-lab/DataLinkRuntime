import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function renderPanel(node: Worker = worker) {
  return render(
    <DlrDesignSystemProvider>
      <WorkerCachePanel worker={node} open onClose={() => undefined} />
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

async function expandVersions(id = 11) {
  fireEvent.click(await screen.findByRole("button", { name: `查看版本：适配器 ${id}` }));
}

const cleanItem: CacheSnapshotItem = { ...item, version_seq: 2, rebuildability: "confirmed", reasons: [] };

function mockCache(view: CacheAdminView, post?: (body: Record<string, unknown>) => Promise<Response> | Response,
  history: CacheOperation[] = []) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return post ? post(JSON.parse(String(init.body))) : response({}, 400);
    if (String(input).includes("/cache?")) return response(view);
    return response({ items: history, next_cursor: null });
  });
}

function completedPreview(items = [cleanItem], overrides: Partial<CacheOperation> = {}) {
  return operation({ status: "completed", result: { complete: true, cursor: null, items }, ...overrides });
}

async function startClean() {
  const button = await screen.findByRole("button", { name: "清理缓存" });
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
}

async function confirmClean() {
  fireEvent.click(await screen.findByRole("button", { name: "确认清理" }));
}

describe("Worker cache administration", () => {
  it("keeps the Worker entry absent for non-admin direct renders", () => {
    const view = render(<WorkerStatus workers={[worker]} loading={false} error={null} canManageCache={false} />);
    expect(screen.queryByRole("button", { name: "缓存管理" })).toBeNull();
    view.rerender(<WorkerStatus workers={[worker]} loading={false} error={null} canManageCache />);
    expect(screen.getByRole("button", { name: "缓存管理" })).toBeTruthy();
  });

  it.each(["incomplete", "offline", "unsupported", "owner_unconfirmed", "stale", "missing"] as const)(
    "preserves %s state and never represents it as complete zero usage", async (status) => {
      vi.stubGlobal("fetch", mockCache({ ...cacheView(status), items: [cleanItem] }));
      renderPanel();
      await screen.findByText(status === "incomplete" ? "部分快照" : status === "offline" ? "节点离线"
        : status === "unsupported" ? "不支持" : status === "owner_unconfirmed" ? "缓存所有者未确认"
          : status === "stale" ? "快照已过期" : "等待节点首次上报");
      expect(screen.getByRole("button", { name: "清理缓存" })).toHaveProperty("disabled", status !== "incomplete");
    },
  );

  it("promotes reclaimable space, hides empty history and removes duplicate internal parameter sections", async () => {
    const view = cacheView();
    view.summary!.accounting.reserved_bytes = 2048;
    vi.stubGlobal("fetch", mockCache(view));
    renderPanel();
    expect(await screen.findByText("2.0 KiB")).toBeTruthy();
    expect(screen.getByText("自动清理")).toBeTruthy();
    expect(screen.queryByText("操作记录")).toBeNull();
    expect(screen.queryByText("查看采集记录")).toBeNull();
    expect(screen.queryByText("查看全部只读策略参数")).toBeNull();
    fireEvent.click(screen.getByText("缓存清理规则"));
    expect(screen.getByText("共享缓存仅统计，不清理。")).toBeTruthy();
    expect(screen.getByText(/安装预留空间/)).toBeTruthy();
  });

  it("groups versions, shows real save sequence and protects unknown caches from selection", async () => {
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem,
      { ...item, cache_key: "11-13", version_id: 13, pinned: true, reasons: ["cache_pinned", "cache_pinned"] },
      { ...item, cache_key: "11-14", version_id: 14, reasons: ["cache_rebuild_unavailable", "cache_rebuild_unknown"] }] }));
    renderPanel();
    expect(await screen.findByText("适配器 11")).toBeTruthy();
    expect(screen.queryByText("第 2 版")).toBeNull();
    await expandVersions();
    const row = screen.getByText("第 2 版").closest("tr")!;
    expect(row.querySelector('input[type="checkbox"]')).toHaveProperty("disabled", false);
    expect(screen.queryByText("代码版本 12")).toBeNull();
    const pinned = screen.getByText("11-13").closest("tr")!;
    expect(pinned.querySelector('input[type="checkbox"]')).toHaveProperty("disabled", true);
    expect(pinned.querySelectorAll(".ant-tag")).toHaveLength(2);
    const unavailable = screen.getByText("11-14").closest("tr")!;
    expect(unavailable.querySelectorAll(".ant-tag")).toHaveLength(1);
    expect(unavailable.textContent).toContain("安装来源暂不可用");
    expect(screen.getAllByText("版本详情")).toHaveLength(2);
  });

  it("uses a single retain switch bound to observed identity and prevents unchanged resubmission", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(cacheView(), (body) => { posts.push(body); return response(operation({ kind: "protect" }), 202); }));
    renderPanel();
    await expandVersions();
    const toggle = screen.getByRole("switch", { name: "保留：适配器 11 · 版本详情" });
    fireEvent.click(toggle);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ kind: "protect", protect: [{ adapter_id: 11, version_id: 12, identity: item.identity, digest: item.digest, pinned: true }] });
    expect(toggle).toHaveProperty("disabled", true);
    fireEvent.click(toggle);
    expect(posts).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "取消保留" })).toBeNull();
  });

  it("retains uncertain pin idempotency and refreshes snapshot conflicts without resubmitting", async () => {
    const posts: Record<string, unknown>[] = [];
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/cache?")) { reads += 1; return response(cacheView()); }
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        if (posts.length < 3) throw new TypeError("uncertain");
        return response({ detail: { code: "cache_snapshot_changed", message: "raw backend text" } }, 409);
      }
      return response({ items: [], next_cursor: null });
    }));
    renderPanel();
    await expandVersions();
    for (let n = 1; n <= 3; n += 1) {
      fireEvent.click(screen.getByRole("switch", { name: /^保留：/ }));
      await waitFor(() => expect(posts).toHaveLength(n));
      await waitFor(() => expect(screen.getByRole("switch", { name: /^保留：/ })).toHaveProperty("disabled", false));
    }
    expect(posts[0].idempotency_key).toBe(posts[1].idempotency_key);
    expect(posts[1].idempotency_key).toBe(posts[2].idempotency_key);
    await screen.findByText(/缓存状态已变化，已刷新/);
    expect(reads).toBe(2);
    expect(document.body.textContent).not.toContain("raw backend text");
  });

  it("keeps manual source confirmation separate from retention and bounds its expiry", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(cacheView(), (body) => { posts.push(body); return response(operation({ kind: "protect" }), 202); }));
    renderPanel();
    await expandVersions();
    fireEvent.click(screen.getByText("详情"));
    fireEvent.click(screen.getByRole("button", { name: "确认安装来源" }));
    expect(screen.getByRole("button", { name: /^确\s*认$/ })).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("核实说明"), { target: { value: "checked immutable bundle" } });
    fireEvent.click(screen.getByRole("button", { name: /^确\s*认$/ }));
    await waitFor(() => expect(posts).toHaveLength(1));
    const proof = (posts[0].protect as Record<string, unknown>[])[0];
    expect(proof).toMatchObject({ identity: item.identity, digest: item.digest, source_policy: "verified_offline", evidence_note: "checked immutable bundle" });
    expect(proof).not.toHaveProperty("pinned");
    expect(new Date(String(proof.valid_until)).getTime() - Date.now()).toBeLessThanOrEqual(3_600_000);
  });

  it("preserves the manual draft on cancel, closes it across worker changes, and never submits by viewing", async () => {
    const fetcher = mockCache(cacheView());
    vi.stubGlobal("fetch", fetcher);
    const panel = (id: number) => <DlrDesignSystemProvider><WorkerCachePanel worker={{ ...worker, id }} open onClose={() => undefined} /></DlrDesignSystemProvider>;
    const view = render(panel(worker.id));
    await expandVersions();
    fireEvent.click(screen.getByText("详情"));
    fireEvent.click(screen.getByRole("button", { name: "确认安装来源" }));
    fireEvent.change(screen.getByLabelText("核实说明"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: /^取\s*消$/ }));
    await waitFor(() => expect(screen.queryByLabelText("核实说明")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "确认安装来源" }));
    expect(screen.getByLabelText("核实说明")).toHaveProperty("value", "draft");
    view.rerender(panel(worker.id + 1));
    await waitFor(() => expect(screen.queryByLabelText("核实说明")).toBeNull());
    expect(fetcher.mock.calls.every((call) => call[1]?.method !== "POST")).toBe(true);
  });

  it("previews only explicitly selected safe keys, then confirms exact scope before cleaning", async () => {
    const posts: Record<string, unknown>[] = [];
    const second = { ...cleanItem, cache_key: "11-13", version_id: 13, version_seq: 3 };
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem, second, { ...item, cache_key: "11-14", version_id: 14 }] }, (body) => {
      posts.push(body);
      return response(body.kind === "preview" ? completedPreview() : operation({ kind: "clean" }), 202);
    }));
    renderPanel();
    await expandVersions();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "清理所选" }));
    await screen.findByText("清理这些缓存？");
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ kind: "preview", keys: [{ adapter_id: 11, version_id: 12 }] });
    const dialog = screen.getAllByRole("dialog").at(-1)!;
    expect(within(dialog).getByText("适配器 11 · 第 2 版")).toBeTruthy();
    expect(within(dialog).queryByText("适配器 11 · 第 3 版")).toBeNull();
    await confirmClean();
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toMatchObject({ kind: "clean", keys: [{ adapter_id: 11, version_id: 12 }] });
  });

  it.each([
    { complete: false, items: [cleanItem] },
    { items: [cleanItem] },
    { complete: true, items: [] },
    { complete: true, items: [cleanItem, cleanItem] },
    { complete: true, items: [{ ...cleanItem, cache_key: "other" }] },
    { complete: true, items: [{ ...cleanItem, kind: "staging" }] },
    { complete: true, items: [{ ...cleanItem, digest: "c".repeat(64) }] },
    { complete: true, items: [{ ...cleanItem, identity: { ...cleanItem.identity, source_sha256: "c".repeat(64) } }] },
    { complete: true, items: [{ ...cleanItem, reasons: ["cache_in_use"] }] },
  ])("never cleans after a partial or changed preview: %j", async (result) => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, (body) => {
      posts.push(body); return response(completedPreview([cleanItem], { result }), 202);
    }));
    renderPanel(); await startClean();
    await screen.findByText("缓存状态已变化，请重新选择。");
    expect(screen.queryByRole("button", { name: "确认清理" })).toBeNull();
    expect(posts.map((post) => post.kind)).toEqual(["preview"]);
  });

  it("accepts equivalent identity field ordering and staging with its original null identity", async () => {
    const staging: CacheSnapshotItem = { ...item, kind: "staging", identity: null, digest: null, pinned: false, reasons: [] };
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [staging] }, () => response(completedPreview([staging]), 202)));
    renderPanel(); await startClean();
    expect(await screen.findByText("清理这些缓存？")).toBeTruthy();
  });

  it("invalidates preview at expiry and prevents later confirmation", async () => {
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, () => response(completedPreview(), 202)));
    renderPanel(); await startClean(); await screen.findByText("清理这些缓存？");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    try {
      await act(async () => { vi.advanceTimersByTime(30_001); });
      fireEvent.click(screen.getByRole("button", { name: "确认清理" }));
      expect(screen.queryByRole("button", { name: "确认清理" })).toBeNull();
      expect(screen.getByText("预览已过期，请重新清理。")).toBeTruthy();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it("replays an uncertain clean with the same idempotency key rather than starting another preview", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, (body) => {
      posts.push(body);
      if (body.kind === "preview") return response(completedPreview(), 202);
      if (posts.length === 2) throw new TypeError("response lost after acceptance");
      return response(operation({ kind: "clean" }), 202);
    }));
    renderPanel(); await startClean(); await confirmClean();
    fireEvent.click(await screen.findByRole("button", { name: "重试提交" }));
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts.map((post) => post.kind)).toEqual(["preview", "clean", "clean"]);
    expect(posts[2]).toEqual(posts[1]);
  });

  it("does not silently select every cache after refresh clears an explicit selection", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, (body) => { posts.push(body); return response(completedPreview(), 202); }));
    renderPanel(); await expandVersions();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: /刷新缓存状态/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "清理所选" })).toHaveProperty("disabled", true));
    expect(posts).toHaveLength(0);
  });

  it("polls the accepted asynchronous preview through completion before allowing cleanup", async () => {
    let polls = 0;
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/cache?")) return response({ ...cacheView(), items: [cleanItem] });
      if (init?.method === "POST") return response(operation(), 202);
      if (String(input).endsWith(operation().operation_id)) return response(++polls === 1 ? operation({ status: "running" }) : completedPreview());
      return response({ items: [], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetcher); renderPanel(); await startClean();
    expect(screen.queryByRole("button", { name: "确认清理" })).toBeNull();
    expect(await screen.findByText("清理这些缓存？", {}, { timeout: 4000 })).toBeTruthy();
    expect(polls).toBe(2);
  }, 6000);

  it("unifies failures while preserving all three retry target types", async () => {
    const guardId = "20000000-0000-4000-8000-000000000001";
    const view: CacheAdminView = { ...cacheView(), failed_guard_items: [{ guard_operation_id: guardId, generation: 3, adapter_id: 11,
      version_id: 12, operation_kind: "gc", local_phase: "failed", resume_phase: "deleting", failure_count: 3,
      error_code: "cache_operation_failed", sampled_at: "2026-09-23T00:00:00Z" }],
      failed_cleanup_items: [{ cleanup_id: 31, adapter_id: 11, attempts: 3, error_code: null }] };
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(view, (body) => { posts.push(body); throw new TypeError("uncertain"); }, [operation({ status: "failed" })]));
    renderPanel(); await screen.findByText("需要处理");
    const retry = () => screen.getAllByRole("button", { name: /重\s*试/ });
    fireEvent.click(retry()[0]); await waitFor(() => expect(posts).toHaveLength(1));
    fireEvent.click(retry()[1]); await waitFor(() => expect(posts).toHaveLength(2));
    fireEvent.click(retry()[2]); await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts[0]).toMatchObject({ kind: "retry", management_operation_id: operation().operation_id });
    expect(posts[1]).toMatchObject({ kind: "retry", guard_operation_id: guardId });
    expect(posts[2]).toMatchObject({ kind: "retry", cleanup_id: 31 });
    expect(screen.queryByText("失败的缓存保护操作")).toBeNull();
    expect(screen.queryByText("失败的适配器清理")).toBeNull();
  });

  it("replaces failed cleanup rows when pagination sees a different sample", async () => {
    const first: CacheAdminView = { ...cacheView(), failed_cleanup_items: [{ cleanup_id: 31, adapter_id: 11, attempts: 3, error_code: null }], failed_cleanup_next_cursor: 30 };
    const next: CacheAdminView = { ...cacheView(), sample_id: "30000000-0000-4000-8000-000000000001",
      failed_cleanup_items: [{ cleanup_id: 40, adapter_id: 14, attempts: 3, error_code: null }], failed_cleanup_next_cursor: null };
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => String(input).includes("/cache?")
      ? response(String(input).includes("failed_cursor=30") || ++reads > 1 ? next : first) : response({ items: [], next_cursor: null })));
    renderPanel(); await screen.findByText("31");
    fireEvent.click(screen.getByRole("button", { name: "加载更多失败清理" }));
    expect(await screen.findByText("40")).toBeTruthy();
    expect(screen.queryByText("31")).toBeNull();
  });
  it.each(["failed", "running", "completed", null] as const)("routes a managed guard through its parent and gates parent status %s", async (parentStatus) => {
    const parentId = "30000000-0000-4000-8000-000000000001";
    const view: CacheAdminView = { ...cacheView(), failed_guard_items: [{ guard_operation_id: "20000000-0000-4000-8000-000000000001",
      management_operation_id: parentId, management_operation_status: parentStatus,
      generation: 1, adapter_id: 11, version_id: 12, operation_kind: "gc", local_phase: "failed", resume_phase: "deleting",
      failure_count: 1, error_code: "cache_operation_failed", sampled_at: "2026-09-23T00:00:00Z" }] };
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(view, (body) => { posts.push(body); return response(operation({ kind: "retry" }), 202); }));
    renderPanel();
    const button = await screen.findByRole("button", { name: /重\s*试/ });
    expect(button).toHaveProperty("disabled", parentStatus !== "failed");
    if (parentStatus === "failed") {
      fireEvent.click(button); await waitFor(() => expect(posts).toHaveLength(1));
      expect(posts[0]).toMatchObject({ kind: "retry", management_operation_id: parentId });
      expect(posts[0]).not.toHaveProperty("guard_operation_id");
    }
  });

  it("deduplicates a managed guard already represented by a failed management operation", async () => {
    const parent = operation({ status: "failed" });
    const view: CacheAdminView = { ...cacheView(), failed_guard_items: [{ guard_operation_id: "20000000-0000-4000-8000-000000000001",
      management_operation_id: parent.operation_id, management_operation_status: "failed",
      generation: 1, adapter_id: 11, version_id: 12, operation_kind: "gc", local_phase: "failed", resume_phase: "deleting",
      failure_count: 1, error_code: "cache_operation_failed", sampled_at: "2026-09-23T00:00:00Z" }] };
    vi.stubGlobal("fetch", mockCache(view, undefined, [parent])); renderPanel();
    await screen.findByText("需要处理");
    expect(screen.getAllByRole("button", { name: /重\s*试/ })).toHaveLength(1);
  });

  it("keeps explicit version selection across pages without adding other versions", async () => {
    const items = Array.from({ length: 6 }, (_, index): CacheSnapshotItem => ({ ...cleanItem,
      version_id: 12 + index, version_seq: index + 1, cache_key: `11-${12 + index}`,
      identity: { ...cleanItem.identity!, version_id: 12 + index } }));
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items }, (body) => {
      posts.push(body); return response(completedPreview([items[0], items[5]]), 202);
    })); renderPanel(); await expandVersions();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByTitle("下一页"));
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "清理所选" }));
    await screen.findByText("清理这些缓存？");
    expect(posts[0]).toMatchObject({ kind: "preview", keys: [{ adapter_id: 11, version_id: 12 }, { adapter_id: 11, version_id: 17 }] });
  });

  it("does not open a late preview after switching worker", async () => {
    let accept: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, () => new Promise<Response>((resolve) => { accept = resolve; })));
    const panel = (id: number) => <DlrDesignSystemProvider><WorkerCachePanel worker={{ ...worker, id }} open onClose={() => undefined} /></DlrDesignSystemProvider>;
    const view = render(panel(worker.id)); await startClean();
    await waitFor(() => expect(accept).toBeDefined());
    view.rerender(panel(worker.id + 1));
    await act(async () => { accept!(response(completedPreview(), 202)); });
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认清理" })).toBeNull());
    expect(screen.getByRole("button", { name: "清理缓存" })).toHaveProperty("disabled", false);
  });

  it("shows the original uncertain submission scope after refreshing the selection", async () => {
    vi.stubGlobal("fetch", mockCache({ ...cacheView(), items: [cleanItem] }, (body) => {
      if (body.kind === "preview") return response(completedPreview(), 202);
      throw new TypeError("lost clean response");
    }));
    renderPanel(); await startClean(); await confirmClean();
    await screen.findByRole("button", { name: "重试提交" });
    fireEvent.click(screen.getByRole("button", { name: /刷新缓存状态/ }));
    expect(await screen.findByText("待确认 1 项")).toBeTruthy();
    fireEvent.click(screen.getByText("本次范围"));
    expect(screen.getByText("适配器 11 · 第 2 版")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "重试提交" })).toHaveProperty("disabled", false));
  });

  it("keeps resolved failed audits in history without showing them as pending work", async () => {
    const failed = operation({ status: "failed", retryable: false });
    vi.stubGlobal("fetch", mockCache(cacheView(), undefined, [failed]));
    renderPanel(); await screen.findByText("操作记录");
    expect(screen.queryByText("需要处理")).toBeNull();
    fireEvent.click(screen.getByText("操作记录"));
    expect(screen.getByText("失败")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull();
  });

  it("hides resolved managed guards even when their parent audit is outside the history page", async () => {
    const view: CacheAdminView = { ...cacheView(), failed_guard_items: [{ guard_operation_id: "20000000-0000-4000-8000-000000000001",
      management_operation_id: "30000000-0000-4000-8000-000000000001", management_operation_status: "failed", management_operation_retryable: false,
      generation: 1, adapter_id: 11, version_id: 12, operation_kind: "gc", local_phase: "failed", resume_phase: "deleting",
      failure_count: 1, error_code: "cache_operation_failed", sampled_at: "2026-09-23T00:00:00Z" }] };
    vi.stubGlobal("fetch", mockCache(view)); renderPanel(); await screen.findByText("适配器 11");
    expect(screen.queryByText("需要处理")).toBeNull();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull();
  });

  it("uses a known successful management retry to avoid stale pending work from an older Control", async () => {
    const failed = operation({ status: "failed" });
    const retry = operation({ operation_id: "30000000-0000-4000-8000-000000000001", kind: "retry", status: "completed",
      target_kind: "management", target_operation_id: failed.operation_id });
    vi.stubGlobal("fetch", mockCache(cacheView(), undefined, [retry, failed])); renderPanel(); await screen.findByText("操作记录");
    expect(screen.queryByText("需要处理")).toBeNull();
  });

  it("keeps an authoritative current failure actionable after an older successful retry", async () => {
    const failed = operation({ status: "failed", retryable: true });
    const retry = operation({ operation_id: "30000000-0000-4000-8000-000000000001", kind: "retry", status: "completed",
      target_kind: "management", target_operation_id: failed.operation_id });
    vi.stubGlobal("fetch", mockCache(cacheView(), undefined, [retry, failed])); renderPanel(); await screen.findByText("需要处理");
    expect(screen.getByRole("button", { name: /重\s*试/ })).toHaveProperty("disabled", false);
  });

  it("discards a late accepted preview when refresh occurred while its submit was pending", async () => {
    let accept: ((response: Response) => void) | undefined;
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { accept = resolve; });
      if (String(input).includes("/cache?")) { reads += 1; return response({ ...cacheView(), items: [cleanItem] }); }
      return response({ items: [], next_cursor: null });
    })); renderPanel(); await startClean();
    await waitFor(() => expect(accept).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: /刷新缓存状态/ }));
    await waitFor(() => expect(reads).toBe(2));
    await act(async () => { accept!(response(completedPreview(), 202)); });
    await waitFor(() => expect(screen.getByRole("button", { name: "清理缓存" })).toHaveProperty("disabled", false));
    expect(screen.queryByRole("button", { name: "确认清理" })).toBeNull();
  });

  it("refreshes retryability after a successful retry without erasing the original failed audit", async () => {
    const failed = operation({ status: "failed", retryable: true });
    const retry = operation({ operation_id: "30000000-0000-4000-8000-000000000001", kind: "retry",
      target_kind: "management", target_operation_id: failed.operation_id });
    let completed = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") return response(retry, 202);
      if (url.endsWith(retry.operation_id)) { completed = true; return response({ ...retry, status: "completed" }); }
      if (url.includes("/cache?")) return response(cacheView());
      return response({ items: completed ? [{ ...retry, status: "completed" }, { ...failed, retryable: false }] : [failed], next_cursor: null });
    })); renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(screen.queryByText("需要处理")).toBeNull(), { timeout: 3000 });
    fireEvent.click(screen.getByText("操作记录"));
    expect(screen.getByText("失败")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull();
  }, 5000);

});

describe("automatic cleanup switches", () => {
  const capableWorker: Worker = {
    ...worker, isolation_capabilities: { cache_governance_v1: true, cache_policy_control_v1: true },
  };
  const names = { gc_enabled: "定期清理", pressure_gc_enabled: "空间不足时清理" } as const;

  it.each([
    ["gc_enabled", false], ["gc_enabled", true],
    ["pressure_gc_enabled", false], ["pressure_gc_enabled", true],
  ] as const)("changes only %s from %s and reads the applied state", async (field, initial) => {
    const view = cacheView();
    view.summary!.policy[field] = initial;
    const before = { ...view.summary!.policy };
    const requests: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(view, (body) => {
      requests.push(body);
      view.summary!.policy[field] = !initial;
      return response(operation({ kind: "protect", status: "completed", request: { policy: { [field]: !initial } } }));
    }));
    renderPanel(capableWorker);
    const toggle = await screen.findByRole("switch", { name: names[field] });
    await waitFor(() => expect(toggle).toHaveProperty("disabled", false));
    expect(toggle.getAttribute("aria-checked")).toBe(String(initial));
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe(String(!initial)));
    expect(requests).toHaveLength(1);
    expect(requests[0]).toEqual({ kind: "protect", idempotency_key: expect.any(String), policy: { [field]: !initial } });
    expect(view.summary!.policy).toEqual({ ...before, [field]: !initial });
  });

  it("keeps the old value and disables both switches until the node confirms", async () => {
    const view = cacheView();
    const submitted = operation({ kind: "protect", request: { policy: { gc_enabled: true } } });
    let finished = false;
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") { posts.push(init.body); return response(submitted); }
      if (String(input).includes("/operations/")) return response({ ...submitted, status: finished ? "completed" : "running" });
      if (String(input).includes("/cache?")) return response(view);
      return response({ items: [], next_cursor: null });
    }));
    renderPanel(capableWorker);
    const periodic = await screen.findByRole("switch", { name: names.gc_enabled });
    const pressure = screen.getByRole("switch", { name: names.pressure_gc_enabled });
    await waitFor(() => expect(periodic).toHaveProperty("disabled", false));
    fireEvent.click(periodic);
    await waitFor(() => expect(periodic).toHaveProperty("disabled", true));
    expect(pressure).toHaveProperty("disabled", true);
    expect(periodic.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(pressure);
    expect(posts).toHaveLength(1);
    view.summary!.policy.gc_enabled = true;
    finished = true;
    await waitFor(() => expect(periodic.getAttribute("aria-checked")).toBe("true"), { timeout: 3000 });
    await waitFor(() => expect(periodic).toHaveProperty("disabled", false));
  });

  it("preserves the old value after failure and retries the same logical request", async () => {
    const view = cacheView();
    const requests: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", mockCache(view, (body) => {
      requests.push(body);
      if (requests.length === 1) return response({ detail: { code: "unavailable" } }, 503);
      view.summary!.policy.gc_enabled = true;
      return response(operation({ kind: "protect", status: "completed", request: { policy: { gc_enabled: true } } }));
    }));
    renderPanel(capableWorker);
    const toggle = await screen.findByRole("switch", { name: names.gc_enabled });
    await waitFor(() => expect(toggle).toHaveProperty("disabled", false));
    fireEvent.click(toggle);
    await waitFor(() => expect(requests).toHaveLength(1));
    await waitFor(() => expect(toggle).toHaveProperty("disabled", false));
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
  });

  it.each(["offline", "stale", "owner_unconfirmed"] as const)("disables switches when %s", async (status) => {
    vi.stubGlobal("fetch", mockCache(cacheView(status)));
    renderPanel(capableWorker);
    expect(await screen.findByRole("switch", { name: names.gc_enabled })).toHaveProperty("disabled", true);
    expect(screen.getByRole("switch", { name: names.pressure_gc_enabled })).toHaveProperty("disabled", true);
  });

  it("keeps old nodes read-only for both settings", async () => {
    vi.stubGlobal("fetch", mockCache(cacheView()));
    renderPanel();
    expect(await screen.findByRole("switch", { name: names.gc_enabled })).toHaveProperty("disabled", true);
    expect(screen.getByRole("switch", { name: names.pressure_gc_enabled })).toHaveProperty("disabled", true);
  });
});
