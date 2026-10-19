import { expect, test, type Page } from "@playwright/test";

const adapter = {
  id: 1, name: "Order normalization", description: "Synthetic UI fixture", language: "python",
  adapter_type: "task", run_mode: "manual", timeout_seconds: 300, owner_user_id: 42,
  latest_version_id: 10, runtime_worker_id: 1, runtime_locked: false, archived_at: null,
  running_execution_id: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", access_level: "admin",
};
const code = "def handle(context, input):\n    return input\n";
const candidateCode = "def handle(context, input):\n    # Preserve the order collection.\n    return {\"orders\": (input or {}).get(\"orders\", [])}\n";

async function fixture(page: Page, locale: string) {
  const unexpected: string[] = [];
  const assists: unknown[] = [];
  await page.route("**/entry-mode.js", (route) => route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "token";' }));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown;
    if (path === "/api/locale") body = { locale };
    else if (path === "/api/health") body = { status: "ok", database: true };
    else if (path === "/api/auth/admin/verify") body = { status: "ok" };
    else if (path === "/api/workers") body = [{ id: 1, name: "fixture-worker", status: "online", capabilities: ["python"], last_heartbeat: "2026-01-01T00:00:00Z" }];
    else if (path === "/api/adapters") body = [adapter];
    else if (path === "/api/adapters/1") body = adapter;
    else if (path === "/api/adapters/1/versions") body = [{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-01-01T00:00:00Z" }];
    else if (path === "/api/adapters/1/versions/10") body = { id: 10, adapter_id: 1, seq: 1, code, requirements: "", runtime_config: {} };
    else if (/credential-(bindings|options)$/.test(path)) body = [];
    else if (path.endsWith("/input-config")) body = { adapter_id: 1, revision: 1, source_type: "none", json_value: null, retention: { mode: "system_default", seconds: null }, artifacts: [], valid_for_run: true, invalid_reason: null };
    else if (path === "/api/system/managed-input-capability") body = { managed_files_enabled: false, ready: false, default_retention_seconds: 86400, max_custom_retention_seconds: 2592000, allow_manual_delete: true, allowed_extensions: [".txt"] };
    else if (path.endsWith("/schedule")) body = { adapter_id: 1, enabled: false, cron: "*/5 * * * *", timezone: "UTC", input: null, next_run_at: null, updated_at: "2026-01-01T00:00:00Z" };
    else if (path.endsWith("/ai/attachment-capabilities")) body = {
      limits: { max_attachments: 8, max_file_bytes: 6291456, max_total_bytes: 50331648, max_parsed_chars_per_file: 65536, max_parsed_total_chars: 262144, parse_timeout_seconds: 30 },
      supported_content_types: ["text/plain", "image/png", "application/pdf", "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
      selected: { provider: "openai", model: "fixture-model", images_native: true, files_native: false, capability_source: "builtin" },
    };
    else if (path === "/api/adapters/1/ai/assist" && request.method() === "POST") {
      assists.push(request.postDataJSON());
      body = { message: "Review the code proposal.", provider: "openai", model: "fixture-model", task_status: "completed", candidate: {
        summary: "Preserve orders", code: candidateCode, requirements: "", runtime_config: {}, required_secret_keys: [],
      } };
    } else {
      unexpected.push(request.method() + " " + path);
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: { code: "fixture_unhandled", message: path } }) });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return { unexpected, assists };
}

for (const locale of ["zh-CN", "en"]) for (const width of [1280, 1440, 1920]) {
  test(`Issue 215 ${locale} ${width}: compact composer, independent details and review`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const { unexpected, assists } = await fixture(page, locale);
    await page.goto("/");
    await page.getByTestId("admin-token-input").fill("fixture-token");
    await page.getByTestId("admin-token-submit").click();
    await page.getByTestId("adapter-item").first().click();
    await page.getByTestId("open-ai-assistant").click();
    const panel = page.getByTestId("ai-assistant-panel");
    await expect(page.getByTestId("ai-current-model")).toHaveText("fixture-model");
    expect((await panel.boundingBox())!.width).toBeLessThanOrEqual(440);
    await expect(page.getByTestId("ai-composer-actions").getByRole("button")).toHaveCount(2);
    await expect(page.getByTestId("ai-send-scope")).toHaveCount(0);
    await page.getByTestId("ai-attachment-input").setInputFiles([
      { name: "order-requirements.txt", mimeType: "text/plain", buffer: Buffer.from("Normalize orders") },
      { name: "long-".repeat(20) + "notes.txt", mimeType: "text/plain", buffer: Buffer.from("Additional notes") },
    ]);
    await expect(page.getByTestId("ai-attachment-item")).toHaveCount(2);
    const draft = "Long draft line\n".repeat(40);
    await page.getByTestId("ai-message-input").fill(draft);
    const sendBefore = await page.getByTestId("ai-send").boundingBox();
    expect(sendBefore!.y + sendBefore!.height).toBeLessThan(900);
    expect(await page.getByTestId("ai-message-input").evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);

    await page.getByTestId("ai-more").click();
    await expect(page.getByRole("menuitem")).toHaveCount(1);
    await expect(page.getByRole("menuitem", { name: /Review outgoing content|查看发送内容/ })).toHaveCount(0);
    await page.getByRole("menuitem", { name: locale === "en" ? "Usage guide" : "使用说明" }).click();
    await expect(page.getByTestId("ai-help")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: /Close|关闭/i }).first().press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("ai-more")).toBeFocused();
    await expect(page.getByTestId("ai-message-input")).toHaveValue(draft);
    expect(await page.getByTestId("ai-send").boundingBox()).toEqual(sendBefore);
    expect(assists).toHaveLength(0);

    await page.getByTestId("maximize-ai-assistant").click();
    await page.getByTestId("ai-more").focus();
    await page.getByTestId("ai-more").press("ArrowDown");
    await page.getByRole("menuitem", { name: locale === "en" ? "Usage guide" : "使用说明" }).click();
    await expect(page.getByTestId("ai-help")).not.toContainText("application/");
    await expect(page.getByTestId("ai-attachment-limits")).toContainText("Excel");
    await page.getByRole("dialog").getByRole("button", { name: /Close|关闭/i }).first().press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(panel).toHaveAttribute("data-layout", "maximized");
    expect(await page.locator(".ai-conversation").evaluate((e) => e.getBoundingClientRect().width)).toBeLessThanOrEqual(904);
    await page.screenshot({ path: testInfo.outputPath("maximized.png") });
    await page.getByTestId("restore-ai-assistant").click();
    await expect(page.getByTestId("ai-attachment-item")).toHaveCount(2);
    await page.getByTestId("ai-send").click();
    await expect(page.getByTestId("ai-candidate")).toBeVisible();
    await expect(page.getByTestId("ai-current-model")).toBeVisible();
    const inputFont = await page.getByTestId("ai-message-input").evaluate((e) => {
      const style = getComputedStyle(e);
      return { size: style.fontSize, family: style.fontFamily, lineHeight: style.lineHeight };
    });
    const replyFont = await page.locator(".ai-message-assistant .ai-markdown").evaluate((e) => {
      const style = getComputedStyle(e);
      return { size: style.fontSize, family: style.fontFamily, lineHeight: style.lineHeight };
    });
    expect(inputFont).toEqual(replyFont);
    await page.getByTestId("ai-preview-code").click();
    await expect(page.getByTestId("ai-code-preview")).toHaveText(candidateCode);
    await page.getByRole("dialog").getByRole("button", { name: /Close|关闭/i }).first().press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByTestId("ai-view-diff").click();
    await expect(page.getByTestId("version-diff")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: /Close|关闭/i }).first().press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(assists).toHaveLength(1);
    expect(unexpected).toEqual([]);
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("sidebar.png") });
  });
}
