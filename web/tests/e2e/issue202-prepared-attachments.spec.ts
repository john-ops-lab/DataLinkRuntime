import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";

const adapter = { id: 1, name: "prepared-ui-fixture", description: "", language: "python", adapter_type: "task",
  run_mode: "manual", timeout_seconds: 300, runtime_worker_id: 1, latest_version_id: 10, runtime_locked: false,
  running_execution_id: null, archived_at: null, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" };
const version = { id: 10, adapter_id: 1, seq: 1, code: "def handle(context, input):\n    return input\n", requirements: "", runtime_config: {}, created_at: adapter.created_at };
const caps = { limits: { max_attachments: 8, max_file_bytes: 6291456, max_total_bytes: 50331648,
  max_parsed_chars_per_file: 65536, max_parsed_total_chars: 262144, parse_timeout_seconds: 30 },
  supported_content_types: ["application/pdf", "image/png", "text/plain"], providers: [], prepared_attachments_enabled: true,
  selected: { provider: "openai", model: "synthetic-vision", protocol: "openai_compatible", images_native: true,
    native_pdf: true, capability_source: "verified_model_endpoint", max_request_bytes: 64000000 } };

async function install(page: Page, locale: "zh-CN" | "en") {
  const state = { prepared: [] as Array<{ scope: string; metadata: Record<string, unknown> }>, assists: [] as Record<string, unknown>[],
    releasedRefs: [] as string[], releasedScopes: [] as string[], expired: false, delay: false, resume: () => {} };
  await page.addInitScript(() => sessionStorage.setItem("dlr-admin-token", "synthetic-ui-token"));
  await page.route("**/entry-mode.js", (route) => route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__="token";' }));
  await page.context().route("**/api/**", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    let body: unknown = []; let status = 200;
    if (path === "/api/locale") body = { locale };
    else if (path === "/api/health") body = { status: "ok", database: true };
    else if (path === "/api/adapters") body = [adapter];
    else if (path === "/api/adapters/1") body = adapter;
    else if (path === "/api/adapters/1/versions") body = [version];
    else if (path === "/api/adapters/1/versions/10") body = version;
    else if (path === "/api/adapters/1/schedule") { status = 404; body = { detail: { code: "schedule_not_configured" } }; }
    else if (path === "/api/adapters/1/input-config") body = { adapter_id: 1, revision: 1, source_type: "none", json_value: null,
      retention: { mode: "system_default", seconds: null }, artifacts: [], valid_for_run: true, invalid_reason: null };
    else if (path === "/api/system/managed-input-capability") body = { managed_files_enabled: false, ready: false,
      default_retention_seconds: 86400, max_custom_retention_seconds: 2592000, allow_manual_delete: true, allowed_extensions: [".txt"] };
    else if (path === "/api/adapters/1/ai/attachment-capabilities") body = caps;
    else if (path === "/api/adapters/1/ai/attachments" && request.method() === "POST") {
      const payload = request.postDataJSON();
      const metadata = { attachment_ref: `prepared-fixture-${state.prepared.length + 1}`, filename: payload.attachment.filename,
        content_type: payload.attachment.content_type, size_bytes: Buffer.from(payload.attachment.data_base64, "base64").length,
        sha256: createHash("sha256").update(Buffer.from(payload.attachment.data_base64, "base64")).digest("hex"),
        representation_revision: "fixture-v1", status: "partial", page_count: 2,
        pages: [{ page: 1, status: "blank" }, { page: 2, status: "available", source_sheet: "Revenue" }],
        sheets: [{ name: "Revenue", rows: 12, columns: 4, hidden: false, print_area: "A1:D12" }],
        coverage: { structure: "partial", cells: "available" }, warnings: ["formula_cache_missing"], expires_at: new Date(Date.now() + 60000).toISOString() };
      state.prepared.push({ scope: payload.scope_id, metadata }); body = metadata;
      if (state.delay) await new Promise<void>((resolve) => { state.resume = resolve; });
    } else if (path.startsWith("/api/adapters/1/ai/attachments/scopes/") && request.method() === "DELETE") {
      state.releasedScopes.push(path.split("/").at(-1)!); status = 204;
    } else if (path.startsWith("/api/adapters/1/ai/attachments/")) {
      const ref = path.split("/").at(-1)!;
      const item = state.prepared.find((item) => item.metadata.attachment_ref === ref && item.scope === url.searchParams.get("scope_id"));
      if (request.method() === "DELETE") { state.releasedRefs.push(ref); status = 204; }
      else if (state.expired || item === undefined) { status = 410; body = { detail: { code: "ai_attachment_expired", message: "expired" } }; }
      else body = item.metadata;
    } else if (path === "/api/adapters/1/ai/assist") {
      state.assists.push(request.postDataJSON());
      if (state.assists.length === 1) { status = 502; body = { detail: { code: "ai_request_failed", message: "Synthetic recoverable failure" } }; }
      else body = { message: "synthetic prepared reply", candidate: null, provider: "openai", model: "synthetic-vision" };
    }
    await route.fulfill(status === 204 ? { status } : { status, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
  });
  return state;
}

async function openPanel(page: Page) {
  await page.goto("/adapters"); await page.getByTestId("adapter-item").first().click();
  await page.getByTestId("open-ai-assistant").click();
  await expect(page.getByTestId("ai-attachment-capability")).toContainText("synthetic-vision");
}

/** Unload delivery is best-effort: observe the actual lifecycle and request attempt,
 * while server receipt/TTL cleanup belongs to the integration acceptance. */
async function expectScopeReleaseAttemptOnNavigation(page: Page, scope: string) {
  await page.evaluate(() => {
    const originalFetch = window.fetch;
    window.fetch = (input, init) => {
      if (String(input).includes("/attachments/scopes/")) {
        sessionStorage.setItem("test-release-attempt", JSON.stringify({ url: String(input), method: init?.method, keepalive: init?.keepalive }));
      }
      return originalFetch(input, init);
    };
    window.addEventListener("pagehide", () => sessionStorage.setItem("test-observed-pagehide", "yes"));
  });
  await page.goto("/adapters");
  const observed = await page.evaluate(() => ({ hide: sessionStorage.getItem("test-observed-pagehide"),
    release: JSON.parse(sessionStorage.getItem("test-release-attempt") ?? "null") }));
  expect(observed.hide).toBe("yes");
  expect(observed.release).toEqual({ url: `/api/adapters/1/ai/attachments/scopes/${scope}`, method: "DELETE", keepalive: true });
  await page.getByTestId("adapter-item").first().click();
  await page.getByTestId("open-ai-assistant").click();
  await expect(page.getByTestId("ai-conversation-empty")).toBeVisible();
}

for (const locale of ["zh-CN", "en"] as const) {
  test(`prepared refs, frozen Retry, expiry and page release: ${locale}`, async ({ page }) => {
    const state = await install(page, locale); await openPanel(page);
    await page.getByTestId("ai-attachment-input").setInputFiles({ name: "业务.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-synthetic-original") });
    const directory = page.getByTestId("ai-prepared-directory").first();
    await expect(directory).toBeVisible(); await directory.locator("summary").click();
    await expect(directory).toContainText("Revenue");
    await expect(directory).toContainText(locale === "en" ? "original cached values" : "原文件缓存值");
    await page.getByTestId("ai-message-input").fill("read the chart"); await page.getByTestId("ai-send").click();
    await expect(page.getByTestId("ai-retry")).toBeVisible();
    await page.getByTestId("ai-attachment-input").setInputFiles({ name: "new.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-synthetic-replacement") });
    await expect.poll(() => state.prepared.length).toBe(2);
    await page.getByTestId("ai-attachment-remove").click();
    await expect.poll(() => state.releasedRefs).toEqual(["prepared-fixture-2"]);
    await page.getByTestId("ai-retry").click();
    await expect(page.getByText("synthetic prepared reply", { exact: true })).toBeVisible();
    expect(state.assists[0]?.attachments).toBeUndefined();
    expect(state.assists[1]?.attachment_refs).toEqual(state.assists[0]?.attachment_refs);
    expect(state.assists[1]?.attachment_scope_id).toBe(state.assists[0]?.attachment_scope_id);
    expect(state.releasedRefs).not.toContain("prepared-fixture-1");
    state.expired = true;
    await page.getByTestId("ai-regenerate").click();
    await expect(page.getByTestId("ai-panel-error")).toContainText(locale === "en" ? "expired" : "已过期");
    expect(state.assists).toHaveLength(2); expect(state.prepared).toHaveLength(2);
    await expectScopeReleaseAttemptOnNavigation(page, state.prepared[0]!.scope);
  });

  test(`cancel pending attachment preparation without a late row: ${locale}`, async ({ page }) => {
    const state = await install(page, locale); state.delay = true; await openPanel(page);
    await page.getByTestId("ai-attachment-input").setInputFiles({ name: "pending.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-synthetic-pending") });
    await expect.poll(() => state.prepared.length).toBe(1);
    await expect(page.getByTestId("ai-attachment-item")).toContainText(locale === "en" ? "Preparing" : "正在准备");
    await page.getByTestId("ai-attachment-remove").click(); state.resume();
    await expect(page.getByTestId("ai-attachment-item")).toHaveCount(0);
    await expect(page.getByTestId("ai-prepared-directory")).toHaveCount(0);
    expect(state.assists).toHaveLength(0);
    await expectScopeReleaseAttemptOnNavigation(page, state.prepared[0]!.scope);
  });
}
