import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1920] as const;

const worker = {
  id: 1,
  name: "D1 fixture worker",
  status: "online",
  last_heartbeat: "2026-08-28T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const adapterBase = {
  id: 1,
  name: "D1 managed input fixture",
  description: "Managed Input browser fixture",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "fixture-owner",
  latest_version_id: 10,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-08-28T00:00:00Z",
  updated_at: "2026-08-28T00:00:00Z",
  access_level: "admin",
};

interface FixtureArtifact {
  id: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: "STAGED" | "READY";
  ordinal?: number;
  retention_mode?: "system_default" | "custom" | "manual_delete";
  created_at: string;
  expires_at: string | null;
}

const artifact: FixtureArtifact = {
  id: 901,
  original_filename: "report.csv",
  content_type: "text/csv",
  size_bytes: 12,
  sha256: "a".repeat(64),
  status: "STAGED",
  created_at: "2026-08-28T00:00:00Z",
  expires_at: "2026-08-28T01:00:00Z",
};

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: jsonBody(body) });
}

function inputConfig(revision: number, artifacts: FixtureArtifact[] = [], sourceType = artifacts.length > 0 ? "managed_files" : "none") {
  return {
    adapter_id: 1,
    revision,
    source_type: sourceType,
    json_value: null,
    retention: { mode: "system_default", seconds: null },
    artifacts,
    valid_for_run: artifacts.length > 0,
    invalid_reason: artifacts.length > 0 ? null : sourceType === "managed_files" ? "managed_files_empty" : null,
  };
}

async function installManagedInputFixture(page: Page, locale: Locale) {
  let revision = 1;
  let savedArtifacts: FixtureArtifact[] = [];
  let stagedArtifacts: FixtureArtifact[] = [];
  let inputSource = "none";
  const unknownRequests: string[] = [];
  const inputWrites: Record<string, unknown>[] = [];

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "token";' });
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;

    if (path === "/api/locale" && method === "GET") return fulfillJson(route, { locale });
    if (path === "/api/health" && method === "GET") return fulfillJson(route, { status: "ok", database: true });
    if (path === "/api/auth/admin/verify" && method === "GET") return fulfillJson(route, { status: "ok" });
    if (path === "/api/workers" && method === "GET") return fulfillJson(route, [worker]);
    if (path === "/api/adapters" && method === "GET") return fulfillJson(route, [adapterBase]);
    if (path === "/api/adapters/1" && method === "GET") return fulfillJson(route, adapterBase);
    if (path === "/api/adapters/1/versions" && method === "GET") return fulfillJson(route, [{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-28T00:00:00Z" }]);
    if (path === "/api/adapters/1/versions/10" && method === "GET") {
      return fulfillJson(route, {
        id: 10,
        adapter_id: 1,
        seq: 1,
        code: "def handle(context, input):\n    return input\n",
        requirements: "",
        runtime_config: {},
        created_at: "2026-08-28T00:00:00Z",
      });
    }
    if (path === "/api/adapters/1/credential-bindings" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/adapters/1/credential-options" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/adapters/1/schedule" && method === "GET") {
      return fulfillJson(route, {
        adapter_id: 1,
        enabled: false,
        cron: "*/5 * * * *",
        timezone: "Asia/Shanghai",
        input: null,
        next_run_at: null,
        updated_at: "2026-08-28T00:00:00Z",
      });
    }
    if (path === "/api/system/managed-input-capability" && method === "GET") {
      return fulfillJson(route, {
        managed_files_enabled: true,
        ready: true,
        default_retention_seconds: 86_400,
        max_custom_retention_seconds: 2_592_000,
        allow_manual_delete: true,
        allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
      });
    }
    if (path === "/api/adapters/1/input-config" && method === "GET") {
      return fulfillJson(route, inputConfig(revision, savedArtifacts, inputSource));
    }
    if (path === "/api/adapters/1/input-artifacts" && method === "GET") {
      return fulfillJson(route, stagedArtifacts);
    }
    if (path === "/api/adapters/1/input-artifacts" && method === "POST") {
      stagedArtifacts = [artifact];
      return fulfillJson(route, artifact, 201);
    }
    if (path === "/api/adapters/1/input-config" && method === "PUT") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      inputWrites.push(payload);
      if (payload.expected_revision !== revision) {
        return fulfillJson(route, {
          detail: {
            code: "input_config_revision_conflict",
            message: "fixture conflict",
            params: { expected_revision: payload.expected_revision, current_revision: revision },
          },
        }, 409);
      }
      revision += 1;
      inputSource = String(payload.source_type);
      if (inputSource === "managed_files") {
        savedArtifacts = stagedArtifacts.map((item, ordinal) => ({
          ...item,
          status: "READY" as const,
          ordinal,
          retention_mode: "system_default" as const,
          expires_at: "2026-08-29T00:00:00Z",
        }));
        stagedArtifacts = [];
      } else {
        savedArtifacts = [];
      }
      return fulfillJson(route, inputConfig(revision, savedArtifacts, inputSource));
    }
    if (path === "/api/adapters/1/input-artifacts/901" && method === "DELETE") {
      stagedArtifacts = [];
      return route.fulfill({ status: 204, body: "" });
    }

    unknownRequests.push(`${method} ${path}`);
    return fulfillJson(route, { detail: { code: "fixture_unhandled_request", message: "fixture route not defined" } }, 404);
  });

  return { inputWrites, unknownRequests };
}

async function loginAndOpenRuntime(page: Page, locale: Locale): Promise<void> {
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await expect(page.getByTestId("admin-token-input")).toBeVisible();
  await page.getByTestId("admin-token-input").fill("fixture-token");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await page.getByTestId("adapter-item").first().click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await page.getByRole("tab", { name: locale === "zh-CN" ? "运行设置" : "Runtime settings" }).click();
  await expect(page.getByTestId("task-input-config")).toBeVisible();
}

test.describe("Issue #127 D1 managed input browser fixture", () => {
  test.describe.configure({ mode: "serial" });

  for (const locale of LOCALES) {
    for (const width of VIEWPORTS) {
      test(`${locale} ${width}px uploads, restores and saves staged files`, async ({ page }) => {
        await page.setViewportSize({ width, height: width === 1280 ? 720 : 1080 });
        const { inputWrites, unknownRequests } = await installManagedInputFixture(page, locale);
        await loginAndOpenRuntime(page, locale);

        const managedCard = page.getByTestId("task-input-source-managed_files");
        await expect(managedCard).toHaveAttribute("aria-disabled", "false");
        await managedCard.click();
        await expect(page.getByTestId("managed-input-count")).toContainText("0/8");

        await page.locator("input[type=file]").setInputFiles({
          name: "report.csv",
          mimeType: "text/csv",
          buffer: Buffer.from("fixture,data\n1,2\n"),
        });
        await expect(page.getByTestId("managed-input-status-901")).toContainText(locale === "zh-CN" ? "待保存" : "Staged");
        await expect(page.getByTestId("managed-input-upload-progress")).toHaveCount(0);

        const saveResponse = page.waitForResponse((response) => {
          return response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/adapters/1/input-config";
        });
        await page.getByTestId("save-task-input").click();
        expect((await saveResponse).status()).toBe(200);
        await expect(page.getByTestId("managed-input-status-901")).toContainText(locale === "zh-CN" ? "已就绪" : "Ready");
        await expect(page.getByTestId("task-input-state")).toContainText(locale === "zh-CN" ? "已保存" : "Saved");
        expect(inputWrites.at(-1)).toMatchObject({
          expected_revision: 1,
          source_type: "managed_files",
          artifact_ids: [901],
        });
        expect(unknownRequests).toEqual([]);
        expect((await page.locator("body").innerText())).not.toContain("task.input.");
      });
    }
  }
});
