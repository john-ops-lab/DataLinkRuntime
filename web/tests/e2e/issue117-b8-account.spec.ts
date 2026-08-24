/** Issue #117 Batch 8：账号资料与用户管理的真实浏览器验收（task 8.3）。
 *
 * 矩阵：zh-CN/en × 1280/1440/1680/1920，外加非管理员权限拒绝用例。
 * 只使用匿名 fixture API；不断言也不归档任何真实凭据或 Provider 响应。
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_B8_OUTPUT_DIR ?? "../../../docs/evidence/issue117-b8/auxiliary-matrix",
);
const screenshotDir = resolve(evidenceRoot, "browser");

interface FixtureUser {
  id: number;
  username: string;
  role: "admin" | "user";
  enabled: boolean;
  must_change_password: boolean;
  created_at: string;
  updated_at: string;
}

const FIXTURE_PASSWORD = "current-secret-1";
const NEW_PASSWORD = "next-secret-2";

function makeUser(id: number, username: string, role: "admin" | "user"): FixtureUser {
  return {
    id,
    username,
    role,
    enabled: true,
    must_change_password: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

interface AccountRecord {
  locale: Locale;
  width: number;
  role: "admin" | "user";
  screenshot: string;
  structure: {
    catalog_baseline_visible: boolean;
    profile_heading_duplicate: number;
    profile_subtitle: boolean;
    password_section_labelled: boolean;
    // viewer 用例受权限门禁不打开用户管理抽屉，这两项目测为 null（未执行）。
    user_management_toolbar_unified: boolean | null;
    reset_modal_named: boolean | null;
  };
  operations: Record<string, boolean>;
  payloads: {
    profile_patch: unknown;
    create_post: unknown;
    role_patch: unknown;
    toggle_patch: unknown;
    bulk_patch_count: number;
    reset_post: unknown;
    change_password_post: unknown;
  };
  feedback: {
    mismatch_error: boolean;
    wrong_current_error: boolean;
    success_notice: boolean;
    empty_state: boolean;
    permission_denied: boolean;
  };
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
    profile_panel_overflow: boolean;
    user_panel_overflow: boolean;
  };
  requests: {
    non_get_paths: string[];
    unknown_paths: string[];
    users_list_count: number;
  };
  console_errors: string[];
  console_filtered_notices: number;
  page_errors: string[];
}

const records: AccountRecord[] = [];
let browserVersion = "unknown";

async function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

// 报告字段一律从真实捕获的请求体派生，仅口令类字段值脱敏为 <redacted>。
function redactSecrets(body: string | undefined): unknown {
  if (body === undefined) {
    return null;
  }
  const parsed = JSON.parse(body) as Record<string, unknown>;
  for (const key of ["password", "new_password", "current_password"]) {
    if (key in parsed) {
      parsed[key] = "<redacted>";
    }
  }
  return parsed;
}

function capturedBody(state: FixtureState, key: string, index = 0): unknown {
  return redactSecrets(state.payloads.filter((entry) => entry.key === key)[index]?.body);
}

interface FixtureState {
  role: "admin" | "user";
  users: FixtureUser[];
  nonGetPaths: string[];
  unknownPaths: string[];
  payloads: { key: string; body: string }[];
  usersListCount: { value: number };
  changePasswordBodies: string[];
}

async function installFixture(page: Page, locale: Locale, role: "admin" | "user"): Promise<FixtureState> {
  const state: FixtureState = {
    role,
    users: [makeUser(1, "admin", "admin"), makeUser(2, "ordinary", "user"), makeUser(3, "viewer", "user")],
    nonGetPaths: [],
    unknownPaths: [],
    payloads: [],
    usersListCount: { value: 0 },
    changePasswordBodies: [],
  };
  let loggedIn = false;

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: 'window.__DLR_ENTRY_MODE__ = "account";',
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    if (method !== "GET") {
      state.nonGetPaths.push(`${method} ${path}`);
      state.payloads.push({ key: `${method} ${path}`, body: request.postData() ?? "" });
    }

    if (path === "/api/locale" && method === "GET") {
      await fulfillJson(route, { locale });
      return;
    }
    if (path === "/api/health" && method === "GET") {
      await fulfillJson(route, { status: "ok", database: true });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/login" && method === "POST") {
      loggedIn = true;
      await fulfillJson(route, { principal: makeUser(role === "admin" ? 1 : 2, role === "admin" ? "admin" : "ordinary", role) });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET") {
      if (!loggedIn) {
        await fulfillJson(route, { detail: { code: "account_session_required", message: "expired" } }, 401);
        return;
      }
      const me = state.users.find((user) => user.role === state.role && user.id === (role === "admin" ? 1 : 2));
      await fulfillJson(route, { principal: me ?? makeUser(1, "admin", "admin") });
      return;
    }
    if (path === "/api/auth/account/logout" && method === "POST") {
      loggedIn = false;
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/change-password" && method === "POST") {
      const body = request.postData() ?? "";
      state.changePasswordBodies.push(body);
      const payload = JSON.parse(body) as { current_password?: string };
      if (payload.current_password !== FIXTURE_PASSWORD) {
        await fulfillJson(
          route,
          { detail: { code: "account_current_password_invalid", message: "raw mismatch text" } },
          400,
        );
        return;
      }
      loggedIn = false;
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/users" && method === "GET") {
      state.usersListCount.value += 1;
      if (state.role !== "admin") {
        await fulfillJson(route, { detail: { code: "account_admin_required", message: "raw forbidden text" } }, 403);
        return;
      }
      await fulfillJson(route, state.users);
      return;
    }
    if (path === "/api/users" && method === "POST") {
      const payload = JSON.parse(request.postData() ?? "{}") as { username: string; role: "admin" | "user" };
      const created = { ...makeUser(state.users.length + 10, payload.username, payload.role), must_change_password: true };
      state.users.push(created);
      await fulfillJson(route, created, 201);
      return;
    }
    const patchMatch = /^\/api\/users\/(\d+)$/.exec(path);
    if (patchMatch && method === "PATCH") {
      const id = Number(patchMatch[1]);
      const target = state.users.find((user) => user.id === id);
      if (target === undefined) {
        await fulfillJson(route, { detail: { code: "account_not_found", message: "missing" } }, 404);
        return;
      }
      const payload = JSON.parse(request.postData() ?? "{}") as Partial<FixtureUser>;
      Object.assign(target, payload);
      await fulfillJson(route, target);
      return;
    }
    const resetMatch = /^\/api\/users\/(\d+)\/reset-password$/.exec(path);
    if (resetMatch && method === "POST") {
      const id = Number(resetMatch[1]);
      const target = state.users.find((user) => user.id === id);
      if (target !== undefined) {
        target.must_change_password = true;
        await fulfillJson(route, target);
        return;
      }
    }
    if (path === "/api/adapters" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/workers" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    state.unknownPaths.push(requestKey);
    await fulfillJson(route, { detail: { code: "issue117_b8_unhandled_request", message: requestKey } }, 404);
  });

  return state;
}

function labels(locale: Locale) {
  return locale === "zh-CN"
    ? {
        loginHeading: "账号登录",
        profileMenu: "账号资料",
        userManagementMenu: "用户管理",
        profileTitle: "我的账号",
        profileSubtitle: /其他账号管理仅限管理员/,
        passwordTitle: "修改密码",
        saveProfile: "保存资料",
        profileSaved: "资料已保存",
        passwordMismatch: "两次输入的新密码不一致",
        passwordChangeFailed: "密码修改失败",
        passwordChanged: /密码修改成功/,
        userManagementTitle: "用户管理",
        userSubtitle: /密码提交后不会再次返回或展示/,
        createTitle: "创建账号",
        created: /账号已创建/,
        roleUpdated: /角色已更新/,
        disabled: /账号已禁用/,
        bulkDisabled: /已批量禁用 3 个账号/,
        resetTitle: "重置 ordinary 的密码",
        resetNotice: /密码已重置/,
        empty: "暂无账号用户",
        logout: "退出登录",
        roleAdmin: "管理员",
        selectedZero: "已选择 0 个账号",
      }
    : {
        loginHeading: "Account login",
        profileMenu: "Account profile",
        userManagementMenu: "User management",
        profileTitle: "My account",
        profileSubtitle: /restricted to administrators/,
        passwordTitle: "Change password",
        saveProfile: "Save profile",
        profileSaved: "Profile saved",
        passwordMismatch: "The new passwords do not match",
        passwordChangeFailed: "Password change failed",
        passwordChanged: /Password changed/,
        userManagementTitle: "User management",
        userSubtitle: /never returned or displayed/,
        createTitle: "Create account",
        created: /Account created/,
        roleUpdated: /Role updated/,
        disabled: /Account disabled/,
        bulkDisabled: /Disabled 3 users/,
        resetTitle: "Reset password for ordinary",
        resetNotice: /Password reset/,
        empty: "No account users",
        logout: "Log out",
        roleAdmin: "Admin",
        selectedZero: "0 users selected",
      };
}

async function login(page: Page, locale: Locale, username: string) {
  const text = labels(locale);
  await expect(page.getByRole("heading", { name: text.loginHeading })).toBeVisible();
  await page.getByTestId("account-username-input").fill(username);
  await page.getByTestId("account-password-input").fill(FIXTURE_PASSWORD);
  await page.getByTestId("account-login-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function openUserMenu(page: Page, name: string) {
  await page.getByTestId("user-menu").click();
  await page.getByRole("menuitem", { name }).click();
}

function panelOverflow(page: Page, selector: string) {
  return page.evaluate((target) => {
    const element = document.querySelector<HTMLElement>(target);
    if (element === null) {
      return true;
    }
    return element.scrollWidth > element.clientWidth + 1;
  }, selector);
}

async function runAdminCase(page: Page, locale: Locale, width: number): Promise<void> {
  test.setTimeout(120_000);
  const text = labels(locale);
  const state = await installFixture(page, locale, "admin");
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  let filteredNotices = 0;
  page.on("console", (message) => {
    if (message.type() !== "error") {
      return;
    }
    // 网络层 4xx/401 资源加载提示由请求断言覆盖；Ant Design CSS-in-JS 的
    // unmount cleanup 告警是 antd 5.29.3 + React 19 的既有框架噪音。
    // 两者计数进 console_filtered_notices，console_errors 只保留真正的 JS 级错误。
    if (/Failed to load resource|Ant Design CSS-in-JS/.test(message.text())) {
      filteredNotices += 1;
      return;
    }
    consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("dialog", (dialog) => void dialog.accept());

  await page.goto("/");
  await login(page, locale, "admin");

  // 对照基线：同视口下 Catalog/Workbench 头部仍然渲染。
  const catalogBaseline = await page.getByTestId("adapter-catalog-header").isVisible();

  // --- 账号资料抽屉 -------------------------------------------------------
  await openUserMenu(page, text.profileMenu);
  const profileDrawer = page.locator(".ant-drawer", { has: page.getByTestId("account-profile-username") });
  await expect(profileDrawer).toBeVisible();
  await expect(profileDrawer.locator(".ant-drawer-title")).toHaveText(text.profileTitle);
  const profileSubtitleVisible = await profileDrawer.getByText(text.profileSubtitle).isVisible();
  expect(profileSubtitleVisible).toBe(true);
  const duplicateHeadings = await profileDrawer.getByRole("heading", { name: text.profileTitle }).count();
  const passwordHeading = profileDrawer.getByRole("heading", { name: text.passwordTitle });
  await expect(passwordHeading).toBeVisible();
  const passwordSectionLabelled =
    (await profileDrawer
      .locator("section.account-user-password")
      .getAttribute("aria-labelledby")) === "account-user-password-title";
  expect(passwordSectionLabelled).toBe(true);
  await expect(page.getByTestId("account-profile-save")).toBeVisible();
  await expect(page.getByTestId("account-user-logout")).toBeVisible();
  const profilePanelOverflow = await panelOverflow(page, ".account-user-panel");

  // 修改用户名：成功反馈保留在表单附近。
  await page.getByTestId("account-profile-username").fill("renamed-admin");
  await page.getByTestId("account-profile-save").click();
  await expect(page.getByTestId("account-profile-notice")).toContainText(text.profileSaved);
  const successNoticeVisible = await page.getByTestId("account-profile-notice").isVisible();
  const profilePatch = state.nonGetPaths.includes("PATCH /api/users/1");

  // 修改密码：先本地确认不一致，再服务端拒绝，均留在本页反馈区。
  await page.getByTestId("account-user-current-password").fill("wrong-secret");
  await page.getByTestId("account-user-new-password").fill(NEW_PASSWORD);
  await page.getByTestId("account-user-confirm-password").fill("mismatch-3");
  await page.getByTestId("account-user-password-submit").click();
  await expect(page.getByTestId("account-profile-error")).toContainText(text.passwordMismatch);
  const mismatchErrorVisible = await page.getByTestId("account-profile-error").isVisible();
  expect(state.changePasswordBodies).toHaveLength(0);

  await page.getByTestId("account-user-confirm-password").fill(NEW_PASSWORD);
  await page.getByTestId("account-user-password-submit").click();
  await expect(page.getByTestId("account-profile-error")).toContainText(text.passwordChangeFailed);
  await expect(page.getByTestId("account-profile-error")).toContainText("account_current_password_invalid");
  const wrongCurrentErrorVisible = await page.getByTestId("account-profile-error").isVisible();
  await expect(profileDrawer).toBeVisible();
  expect(state.changePasswordBodies).toHaveLength(1);

  await profileDrawer.locator(".ant-drawer-close").click();
  await expect(profileDrawer).toBeHidden();

  // --- 用户管理抽屉 -------------------------------------------------------
  await openUserMenu(page, text.userManagementMenu);
  const userDrawer = page.locator(".ant-drawer", { has: page.getByTestId("user-create-submit") });
  await expect(userDrawer).toBeVisible();
  const userDrawerVisible = await userDrawer.isVisible();
  await expect(userDrawer.locator(".ant-drawer-title")).toHaveText(text.userManagementTitle);
  await expect(userDrawer.getByText(text.userSubtitle)).toBeVisible();
  await expect(userDrawer.getByRole("heading", { name: text.createTitle })).toBeVisible();
  const toolbar = page.getByTestId("user-management-toolbar");
  // 工具栏统一性从真实 DOM 采样：刷新与批量控件必须同属该 toolbar。
  const refreshInToolbar = await toolbar
    .getByRole("button", { name: locale === "zh-CN" ? /刷\s*新/ : "Refresh" })
    .isVisible();
  const toolbarUnified =
    refreshInToolbar &&
    (await toolbar.getByTestId("users-bulk-enable").isVisible()) &&
    (await toolbar.getByTestId("users-bulk-disable").isVisible()) &&
    (await toolbar.getByText(text.selectedZero).isVisible());
  expect(toolbarUnified).toBe(true);
  await expect(page.getByText("ordinary")).toBeVisible();
  const userPanelOverflow = await panelOverflow(page, ".user-management-panel");

  // 创建用户：既有 POST payload；密码不回显。
  await page.getByTestId("user-create-username").fill("created-user");
  await page.getByTestId("user-create-password").fill("created-secret-1");
  await page.getByTestId("user-create-submit").click();
  await expect(userDrawer.getByRole("status")).toContainText(text.created);
  await expect(page.getByText("created-user")).toBeVisible();

  // 角色调整：ordinary → Admin/管理员。
  const ordinaryRow = page.locator("tr", { has: page.getByText("ordinary", { exact: true }) });
  await ordinaryRow.locator(".ant-select-selector").click();
  const openDropdown = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
  await openDropdown.locator(`.ant-select-item-option[title="${text.roleAdmin}"]`).click();
  await expect(userDrawer.getByRole("status")).toContainText(text.roleUpdated);

  // 启停：禁用 ordinary。
  await page.getByTestId("user-toggle-2").click();
  await expect(userDrawer.getByRole("status")).toContainText(text.disabled);

  // 重置密码：模态框可访问名称 + 既有 POST payload。
  await page.getByTestId("user-reset-2").click();
  const resetDialog = page.getByRole("dialog", { name: text.resetTitle });
  const resetModalNamed = await resetDialog.isVisible();
  expect(resetModalNamed).toBe(true);
  await page.getByTestId("user-reset-password").fill("reset-secret-9");
  await page.getByTestId("user-reset-submit").click();
  await expect(userDrawer.getByRole("status")).toContainText(text.resetNotice);

  // 批量操作：全选（此时 admin/viewer/created-user 可变化 3 行中 ordinary 已禁用，
  // 批量禁用只 PATCH 仍启用的行）。
  await page.locator(".user-management-panel thead input[type=checkbox]").first().click();
  await page.getByTestId("users-bulk-disable").click();
  await expect(userDrawer.getByRole("status")).toContainText(text.bulkDisabled);
  await expect(toolbar.getByText(text.selectedZero)).toBeVisible();

  // 筛选 + 空状态：关键字过滤到无结果时出现空状态，且不发新列表请求。
  const listBeforeFilter = state.usersListCount.value;
  await page.getByLabel(locale === "zh-CN" ? "搜索账号" : "Search users").fill("no-such-user");
  const emptyStateVisible = await page.getByText(text.empty).isVisible();
  expect(emptyStateVisible).toBe(true);
  expect(state.usersListCount.value).toBe(listBeforeFilter);
  await page.getByLabel(locale === "zh-CN" ? "搜索账号" : "Search users").fill("");
  await expect(page.getByText("created-user")).toBeVisible();

  // 刷新：触发新的列表 GET。
  await toolbar.getByRole("button", { name: locale === "zh-CN" ? /刷\s*新/ : "Refresh" }).click();
  await expect.poll(() => state.usersListCount.value).toBeGreaterThan(listBeforeFilter);
  const refreshTriggered = state.usersListCount.value > listBeforeFilter;

  // API payload 合同断言（原始值只活在测试进程内，报告只记录脱敏后的真实捕获值）。
  const payloadOf = (key: string): unknown[] =>
    state.payloads.filter((entry) => entry.key === key).map((entry) => JSON.parse(entry.body));
  expect(payloadOf("PATCH /api/users/1")[0]).toEqual({ username: "renamed-admin" });
  expect(payloadOf("POST /api/users")[0]).toEqual({
    username: "created-user",
    password: "created-secret-1",
    role: "user",
  });
  expect(payloadOf("PATCH /api/users/2")[0]).toEqual({ role: "admin" });
  expect(payloadOf("PATCH /api/users/2")[1]).toEqual({ enabled: false });
  const bulkPatches = state.payloads
    .filter((entry) => /^PATCH \/api\/users\/\d+$/.test(entry.key))
    .slice(3);
  expect(bulkPatches).toHaveLength(3);
  for (const patch of bulkPatches) {
    expect(JSON.parse(patch.body)).toEqual({ enabled: false });
  }
  expect(payloadOf("POST /api/users/2/reset-password")[0]).toEqual({ new_password: "reset-secret-9" });

  const overflow = await page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);

  const screenshotName = `account-${locale}-${width}.png`;
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: resolve(screenshotDir, screenshotName), fullPage: true });

  await userDrawer.locator(".ant-drawer-close").click();

  // 修改密码成功路径（终态）：返回登录页并显示既有提示。
  await openUserMenu(page, text.profileMenu);
  await page.getByTestId("account-user-current-password").fill(FIXTURE_PASSWORD);
  await page.getByTestId("account-user-new-password").fill(NEW_PASSWORD);
  await page.getByTestId("account-user-confirm-password").fill(NEW_PASSWORD);
  await page.getByTestId("account-user-password-submit").click();
  const backToLogin = await page.getByRole("heading", { name: text.loginHeading }).isVisible();
  const changeNoticeVisible = await page.getByTestId("account-auth-notice").isVisible();
  expect(backToLogin).toBe(true);
  expect(changeNoticeVisible).toBe(true);
  await expect(page.getByTestId("account-auth-notice")).toContainText(text.passwordChanged);
  const changeSuccess = backToLogin && changeNoticeVisible && state.changePasswordBodies.length === 2;
  expect(JSON.parse(state.changePasswordBodies[1])).toEqual({
    current_password: FIXTURE_PASSWORD,
    new_password: NEW_PASSWORD,
  });

  const bodyText = (await page.locator("body").textContent()) ?? "";
  expect(bodyText).not.toContain("created-secret-1");
  expect(bodyText).not.toContain("reset-secret-9");
  expect(bodyText).not.toContain(NEW_PASSWORD);
  expect(state.unknownPaths).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(await page.locator("html").getAttribute("lang")).toBe(locale);

  records.push({
    locale,
    width,
    role: "admin",
    screenshot: `docs/evidence/issue117-b8/auxiliary-matrix/browser/${screenshotName}`,
    structure: {
      catalog_baseline_visible: catalogBaseline,
      profile_heading_duplicate: duplicateHeadings,
      profile_subtitle: profileSubtitleVisible,
      password_section_labelled: passwordSectionLabelled,
      user_management_toolbar_unified: toolbarUnified,
      reset_modal_named: resetModalNamed,
    },
    operations: {
      profile_rename: profilePatch,
      create_user: state.nonGetPaths.includes("POST /api/users"),
      role_change: payloadOf("PATCH /api/users/2").length > 0,
      enable_disable: payloadOf("PATCH /api/users/2").length > 1,
      reset_password: state.nonGetPaths.some((entry) => entry.endsWith("/reset-password")),
      bulk: bulkPatches.length === 3,
      refresh: refreshTriggered,
      filter_empty_state: emptyStateVisible,
      change_password_success: changeSuccess,
    },
    payloads: {
      profile_patch: capturedBody(state, "PATCH /api/users/1"),
      create_post: capturedBody(state, "POST /api/users"),
      role_patch: capturedBody(state, "PATCH /api/users/2", 0),
      toggle_patch: capturedBody(state, "PATCH /api/users/2", 1),
      bulk_patch_count: bulkPatches.length,
      reset_post: capturedBody(state, "POST /api/users/2/reset-password"),
      change_password_post: redactSecrets(state.changePasswordBodies[1]),
    },
    feedback: {
      mismatch_error: mismatchErrorVisible,
      wrong_current_error: wrongCurrentErrorVisible,
      success_notice: successNoticeVisible,
      empty_state: emptyStateVisible,
      permission_denied: !userDrawerVisible,
    },
    overflow: {
      inner_width: overflow.inner_width,
      document_scroll_width: overflow.document_scroll_width,
      body_scroll_width: overflow.body_scroll_width,
      profile_panel_overflow: profilePanelOverflow,
      user_panel_overflow: userPanelOverflow,
    },
    requests: {
      non_get_paths: [...new Set(state.nonGetPaths)],
      unknown_paths: state.unknownPaths,
      users_list_count: state.usersListCount.value,
    },
    console_errors: consoleErrors,
    console_filtered_notices: filteredNotices,
    page_errors: pageErrors,
  });
}

async function runViewerCase(page: Page, locale: Locale): Promise<void> {
  test.setTimeout(120_000);
  const text = labels(locale);
  const state = await installFixture(page, locale, "user");
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  let filteredNotices = 0;
  page.on("console", (message) => {
    if (message.type() !== "error") {
      return;
    }
    // 网络层 4xx/401 资源加载提示由请求断言覆盖；Ant Design CSS-in-JS 的
    // unmount cleanup 告警是 antd 5.29.3 + React 19 的既有框架噪音。
    // 两者计数进 console_filtered_notices，console_errors 只保留真正的 JS 级错误。
    if (/Failed to load resource|Ant Design CSS-in-JS/.test(message.text())) {
      filteredNotices += 1;
      return;
    }
    consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("dialog", (dialog) => void dialog.accept());

  await page.goto("/");
  await login(page, locale, "ordinary");
  const viewerCatalogBaseline = await page.getByTestId("adapter-catalog-header").isVisible();

  // 非管理员：用户菜单不暴露用户管理/系统设置入口（既有权限拒绝保持）。
  await page.getByTestId("user-menu").click();
  await expect(page.getByRole("menuitem", { name: text.profileMenu })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: text.userManagementMenu })).toHaveCount(0);
  const userMenuHidden =
    (await page.getByRole("menuitem", { name: text.userManagementMenu }).count()) === 0;
  await page.keyboard.press("Escape");

  // 账号资料仍可用；用户名修改走既有 PATCH 合同。
  await openUserMenu(page, text.profileMenu);
  await expect(page.getByTestId("account-profile-username")).toBeVisible();
  const viewerDrawer = page.locator(".ant-drawer", { has: page.getByTestId("account-profile-username") });
  const viewerDuplicateHeadings = await viewerDrawer.getByRole("heading", { name: text.profileTitle }).count();
  expect(viewerDuplicateHeadings).toBe(0);
  const viewerSubtitleVisible = await viewerDrawer.getByText(text.profileSubtitle).isVisible();
  const viewerPasswordLabelled =
    (await viewerDrawer
      .locator("section.account-user-password")
      .getAttribute("aria-labelledby")) === "account-user-password-title";
  await page.getByTestId("account-profile-username").fill("ordinary-renamed");
  await page.getByTestId("account-profile-save").click();
  await expect(page.getByTestId("account-profile-notice")).toContainText(text.profileSaved);
  const viewerNoticeVisible = await page.getByTestId("account-profile-notice").isVisible();
  const viewerRenamePayload = capturedBody(state, "PATCH /api/users/2");
  expect(viewerCatalogBaseline).toBe(true);
  expect(viewerSubtitleVisible).toBe(true);
  expect(viewerPasswordLabelled).toBe(true);
  expect(userMenuHidden).toBe(true);
  expect(viewerNoticeVisible).toBe(true);
  expect(viewerRenamePayload).toEqual({ username: "ordinary-renamed" });
  // 非 GET 中排除登录/登出等会话请求后，业务写操作应只有改名这一笔。
  expect(state.nonGetPaths.filter((entry) => !entry.startsWith("POST /api/auth/"))).toEqual([
    "PATCH /api/users/2",
  ]);
  expect(state.unknownPaths).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);

  const viewerOverflow = await page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(viewerOverflow.document_scroll_width).toBeLessThanOrEqual(viewerOverflow.inner_width);
  expect(viewerOverflow.body_scroll_width).toBeLessThanOrEqual(viewerOverflow.inner_width);
  const viewerPanelOverflow = await panelOverflow(page, ".account-user-panel");
  expect(viewerPanelOverflow).toBe(false);

  const screenshotName = `account-viewer-${locale}-1280.png`;
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: resolve(screenshotDir, screenshotName), fullPage: true });

  records.push({
    locale,
    width: 1280,
    role: "user",
    screenshot: `docs/evidence/issue117-b8/auxiliary-matrix/browser/${screenshotName}`,
    structure: {
      catalog_baseline_visible: viewerCatalogBaseline,
      profile_heading_duplicate: viewerDuplicateHeadings,
      profile_subtitle: viewerSubtitleVisible,
      password_section_labelled: viewerPasswordLabelled,
      user_management_toolbar_unified: null,
      reset_modal_named: null,
    },
    operations: {
      profile_rename: viewerRenamePayload !== null,
      permission_menu_hidden: userMenuHidden,
    },
    payloads: {
      profile_patch: viewerRenamePayload,
      create_post: null,
      role_patch: null,
      toggle_patch: null,
      bulk_patch_count: 0,
      reset_post: null,
      change_password_post: null,
    },
    feedback: {
      mismatch_error: false,
      wrong_current_error: false,
      success_notice: viewerNoticeVisible,
      empty_state: false,
      permission_denied: userMenuHidden,
    },
    overflow: {
      inner_width: viewerOverflow.inner_width,
      document_scroll_width: viewerOverflow.document_scroll_width,
      body_scroll_width: viewerOverflow.body_scroll_width,
      profile_panel_overflow: viewerPanelOverflow,
      user_panel_overflow: false,
    },
    requests: {
      non_get_paths: state.nonGetPaths,
      unknown_paths: state.unknownPaths,
      users_list_count: state.usersListCount.value,
    },
    console_errors: consoleErrors,
    console_filtered_notices: filteredNotices,
    page_errors: pageErrors,
  });
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  records.sort(
    (left, right) =>
      left.role.localeCompare(right.role) || left.locale.localeCompare(right.locale) || left.width - right.width,
  );
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify(
      {
        schema_version: 1,
        product: "DataLinkRuntime",
        dispatch_id: "issue117-b8-account-20260825-r3",
        batch: "8",
        scope: "Account profile and user management UI unification",
        antd: "5.29.3",
        playwright: "1.62.1",
        browser: "chromium",
        browser_version: browserVersion,
        viewport_widths: VIEWPORTS,
        locales: LOCALES,
        fixture_provider: "scoped Playwright route fixture",
        real_provider_credentials: false,
        raw_provider_response_archived: false,
        password_values_redacted_in_report: true,
        report_fields_derived_from_captured_state: true,
        viewer_structure_fields_not_exercised_are_null: true,
        records,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px admin account/user management Batch 8 contract`, async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({ viewport: { width, height: 800 } });
      const page = await context.newPage();
      await runAdminCase(page, locale, width);
      await page.close();
      await context.close();
    });
  }
  test(`${locale} 1280px viewer permission boundary Batch 8 contract`, async ({ browser }) => {
    browserVersion = browser.version();
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await runViewerCase(page, locale);
    await page.close();
    await context.close();
  });
}
