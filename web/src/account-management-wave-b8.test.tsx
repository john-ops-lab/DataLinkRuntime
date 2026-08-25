/** Issue #117 Batch 8：账号资料与用户管理的操作/状态回归基线（task 8.1）。
 *
 * 这些测试固定既有 API payload、权限拒绝、加载/空/成功/错误反馈和敏感值边界，
 * 布局统一（8.2）只允许改变展示结构，不允许放松这里的任何行为断言。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { setAuthToken } from "./api";
import { applySystemLocale } from "./i18n";
import AccountUserPage from "./components/AccountUserPage";
import UserManagementDrawer from "./components/UserManagementDrawer";
import type { AccountPrincipal, AccountUser } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as Response;
}

const principal: AccountPrincipal = {
  id: 1,
  username: "admin",
  role: "admin",
  enabled: true,
  must_change_password: false,
};

const users: AccountUser[] = [
  {
    id: 1,
    username: "admin",
    role: "admin",
    enabled: true,
    must_change_password: false,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  },
  {
    id: 2,
    username: "ordinary",
    role: "user",
    enabled: true,
    must_change_password: false,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  },
];

interface RecordedRequest {
  url: string;
  method: string;
  body: string | null;
  headers: Record<string, string>;
}

function installAccountFetch(
  handler: (request: RecordedRequest) => Response | Promise<Response>,
): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request: RecordedRequest = {
      url: String(input),
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? init.body : null,
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    requests.push(request);
    return handler(request);
  }));
  return requests;
}

function renderAccountUserPage(overrides: Partial<Parameters<typeof AccountUserPage>[0]> = {}) {
  const props = {
    principal,
    onPrincipalChange: vi.fn(),
    onPasswordChanged: vi.fn(),
    onLogout: vi.fn(async () => undefined),
    ...overrides,
  };
  render(<AccountUserPage {...props} />);
  return props;
}

function renderUserManagement() {
  render(<UserManagementDrawer open onClose={() => undefined} />);
}

beforeEach(async () => {
  window.__DLR_ENTRY_MODE__ = "account";
  document.cookie = "dlr_account_csrf=csrf-test-token; path=/";
  setAuthToken(null);
  vi.stubGlobal("confirm", vi.fn(() => true));
  await applySystemLocale("zh-CN");
});

afterEach(async () => {
  window.__DLR_ENTRY_MODE__ = "token";
  document.cookie = "dlr_account_csrf=; Max-Age=0; path=/";
  setAuthToken(null);
  vi.unstubAllGlobals();
  await applySystemLocale("zh-CN");
});

it("uses the workbench baseline structure for the account profile surface", () => {
  renderAccountUserPage();

  // 抽屉标题由 AccountApp 承载；页面主体不再重复渲染同名标题。
  expect(screen.queryByRole("heading", { name: "我的账号" })).toBeNull();
  expect(screen.getByText(/其他账号管理仅限管理员/)).toBeTruthy();
  expect(screen.getByRole("heading", { name: "修改密码" })).toBeTruthy();
  const passwordSection = screen.getByRole("heading", { name: "修改密码" }).closest("section");
  expect(passwordSection).not.toBeNull();
  expect(passwordSection?.getAttribute("aria-labelledby")).toBe(
    screen.getByRole("heading", { name: "修改密码" }).id,
  );
  expect(screen.getByTestId("account-profile-save")).toBeTruthy();
  expect(screen.getByTestId("account-user-password-submit")).toBeTruthy();
  expect(screen.getByTestId("account-user-logout")).toBeTruthy();
});

it("updates the username with the established PATCH payload and local success feedback", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/users/1" && request.method === "PATCH") {
      return jsonResponse({ ...principal, username: "renamed-admin" });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  const props = renderAccountUserPage();

  fireEvent.change(screen.getByTestId("account-profile-username"), {
    target: { value: "renamed-admin" },
  });
  fireEvent.click(screen.getByTestId("account-profile-save"));

  const notice = await screen.findByRole("status");
  expect(notice.textContent).toContain("资料已保存");
  const patch = requests.find((request) => request.method === "PATCH");
  expect(patch).toBeDefined();
  expect(JSON.parse(patch?.body ?? "null")).toEqual({ username: "renamed-admin" });
  expect(patch?.headers["X-CSRF-Token"]).toBe("csrf-test-token");
  expect(props.onPrincipalChange).toHaveBeenCalledWith(
    expect.objectContaining({ username: "renamed-admin" }),
  );
  expect(screen.queryByText(/scrypt|password_hash/i)).toBeNull();
});

it("keeps a profile conflict as an in-form error without echoing the raw server text", async () => {
  installAccountFetch((request) => {
    if (request.url === "/api/users/1" && request.method === "PATCH") {
      return jsonResponse(
        { detail: { code: "account_username_conflict", message: "raw server conflict text" } },
        409,
      );
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  const props = renderAccountUserPage();

  fireEvent.change(screen.getByTestId("account-profile-username"), {
    target: { value: "taken-name" },
  });
  fireEvent.click(screen.getByTestId("account-profile-save"));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("资料更新失败");
  expect(alert.textContent).toContain("account_username_conflict");
  expect(alert.textContent).not.toContain("raw server conflict text");
  expect(props.onPrincipalChange).not.toHaveBeenCalled();
});

it("validates password confirmation locally and only submits current/new password", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/auth/account/change-password" && request.method === "POST") {
      return jsonResponse({ status: "ok" });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  const props = renderAccountUserPage();

  fireEvent.change(screen.getByTestId("account-user-current-password"), {
    target: { value: "current-secret-1" },
  });
  fireEvent.change(screen.getByTestId("account-user-new-password"), {
    target: { value: "next-secret-2" },
  });
  fireEvent.change(screen.getByTestId("account-user-confirm-password"), {
    target: { value: "mismatch-3" },
  });
  fireEvent.click(screen.getByTestId("account-user-password-submit"));

  const mismatch = await screen.findByRole("alert");
  expect(mismatch.textContent).toContain("两次输入的新密码不一致");
  expect(requests).toHaveLength(0);

  fireEvent.change(screen.getByTestId("account-user-confirm-password"), {
    target: { value: "next-secret-2" },
  });
  fireEvent.click(screen.getByTestId("account-user-password-submit"));

  await waitFor(() => expect(props.onPasswordChanged).toHaveBeenCalledTimes(1));
  const change = requests.find((request) => request.method === "POST");
  expect(change).toBeDefined();
  expect(JSON.parse(change?.body ?? "null")).toEqual({
    current_password: "current-secret-1",
    new_password: "next-secret-2",
  });
  // 确认密码不进入请求；成功后表单清空，密码不以文本形式回显。
  expect(JSON.stringify(change?.body)).not.toContain("mismatch-3");
  expect((screen.getByTestId("account-user-current-password") as HTMLInputElement).value).toBe("");
  expect(document.body.textContent ?? "").not.toContain("current-secret-1");
  expect(document.body.textContent ?? "").not.toContain("next-secret-2");
});

it("keeps a wrong-current-password failure near the password form", async () => {
  installAccountFetch((request) => {
    if (request.url === "/api/auth/account/change-password" && request.method === "POST") {
      return jsonResponse(
        { detail: { code: "account_current_password_invalid", message: "raw mismatch text" } },
        400,
      );
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  const props = renderAccountUserPage();

  fireEvent.change(screen.getByTestId("account-user-current-password"), {
    target: { value: "wrong-secret" },
  });
  fireEvent.change(screen.getByTestId("account-user-new-password"), {
    target: { value: "next-secret-2" },
  });
  fireEvent.change(screen.getByTestId("account-user-confirm-password"), {
    target: { value: "next-secret-2" },
  });
  fireEvent.click(screen.getByTestId("account-user-password-submit"));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("密码修改失败");
  expect(alert.textContent).toContain("account_current_password_invalid");
  expect(alert.textContent).not.toContain("raw mismatch text");
  expect(props.onPasswordChanged).not.toHaveBeenCalled();
});

it("locks profile and password controls while a save is in flight", async () => {
  const patchGate: { resolve: ((response: Response) => void) | null } = { resolve: null };
  installAccountFetch((request) => {
    if (request.url === "/api/users/1" && request.method === "PATCH") {
      return new Promise<Response>((resolve) => {
        patchGate.resolve = resolve;
      });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderAccountUserPage();

  fireEvent.change(screen.getByTestId("account-profile-username"), {
    target: { value: "renamed-admin" },
  });
  fireEvent.click(screen.getByTestId("account-profile-save"));

  await waitFor(() => {
    expect((screen.getByTestId("account-profile-username") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByTestId("account-profile-save") as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByTestId("account-user-password-submit") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  patchGate.resolve?.(jsonResponse({ ...principal, username: "renamed-admin" }));
  await screen.findByRole("status");
  expect((screen.getByTestId("account-profile-save") as HTMLButtonElement).disabled).toBe(false);
});

it("lists users, shows loading and keeps the empty state inside the table region", async () => {
  const listGate: { resolve: ((response: Response) => void) | null } = { resolve: null };
  installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return new Promise<Response>((resolve) => {
        listGate.resolve = resolve;
      });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  await screen.findByText("用户管理");
  await waitFor(() => {
    expect(document.querySelector(".ant-spin-spinning")).not.toBeNull();
  });

  listGate.resolve?.(jsonResponse([]));
  await screen.findByText("暂无账号用户");
});

it("surfaces a permission-denied list load as an in-panel error", async () => {
  installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(
        { detail: { code: "account_admin_required", message: "raw forbidden text" } },
        403,
      );
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("用户列表加载失败");
  expect(alert.textContent).toContain("account_admin_required");
  expect(alert.textContent).not.toContain("raw forbidden text");
});

it("uses the workbench baseline structure for the user management surface", async () => {
  installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(users);
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  await screen.findByText("ordinary");
  expect(screen.getByText(/密码提交后不会再次返回或展示/)).toBeTruthy();
  expect(screen.getByRole("heading", { name: "创建账号" })).toBeTruthy();
  const toolbar = screen.getByTestId("user-management-toolbar");
  // antd Button 会在两个汉字之间插入空格（"刷 新"），用正则匹配文本内容。
  expect(
    within(toolbar).getAllByRole("button").some((button) => /刷\s*新/.test(button.textContent ?? "")),
  ).toBe(true);
  expect(within(toolbar).getByTestId("users-bulk-enable")).toBeTruthy();
  expect(within(toolbar).getByTestId("users-bulk-disable")).toBeTruthy();
  expect(within(toolbar).getByText("已选择 0 个账号")).toBeTruthy();
});

it("creates a user with the established payload and keeps the password hidden", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(users);
    }
    if (request.url === "/api/users" && request.method === "POST") {
      return jsonResponse(
        { ...users[1], id: 3, username: "created-user", must_change_password: true },
        201,
      );
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  await screen.findByText("ordinary");
  fireEvent.change(screen.getByTestId("user-create-username"), {
    target: { value: "created-user" },
  });
  fireEvent.change(screen.getByTestId("user-create-password"), {
    target: { value: "created-secret-1" },
  });
  fireEvent.click(screen.getByTestId("user-create-submit"));

  const notice = await screen.findByRole("status");
  expect(notice.textContent).toContain("账号已创建，首次登录必须修改密码。");
  const create = requests.find((request) => request.method === "POST");
  expect(JSON.parse(create?.body ?? "null")).toEqual({
    username: "created-user",
    password: "created-secret-1",
    role: "user",
  });
  await screen.findByText("created-user");
  expect((screen.getByTestId("user-create-password") as HTMLInputElement).value).toBe("");
  expect(document.body.textContent ?? "").not.toContain("created-secret-1");
});

async function openDropdownOption(label: string) {
  const dropdown = await waitFor(() => {
    const element = document.querySelector<HTMLElement>(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
    );
    expect(element).not.toBeNull();
    return element as HTMLElement;
  });
  fireEvent.click(within(dropdown).getByText(label));
}

it("changes roles, toggles enabled state and resets passwords with the established payloads", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(users);
    }
    if (request.url === "/api/users/2" && request.method === "PATCH") {
      const body = JSON.parse(request.body ?? "{}") as Record<string, unknown>;
      return jsonResponse({ ...users[1], ...body });
    }
    if (request.url === "/api/users/2/reset-password" && request.method === "POST") {
      return jsonResponse({ ...users[1], must_change_password: true });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  const row = (await screen.findByText("ordinary")).closest("tr");
  expect(row).not.toBeNull();

  // 角色调整：既有 PATCH {role} 合同。
  fireEvent.mouseDown(within(row as HTMLElement).getByRole("combobox"));
  await openDropdownOption("管理员");
  expect(window.confirm).toHaveBeenCalled();
  await screen.findByText("角色已更新，已有 Session 已失效。");
  const rolePatch = requests.find(
    (request) => request.method === "PATCH" && request.url === "/api/users/2",
  );
  expect(JSON.parse(rolePatch?.body ?? "null")).toEqual({ role: "admin" });

  // 启停：既有 PATCH {enabled} 合同。
  fireEvent.click(screen.getByTestId("user-toggle-2"));
  await screen.findByText("账号已禁用，已有 Session 已失效。");
  const togglePatch = requests.filter(
    (request) => request.method === "PATCH" && request.url === "/api/users/2",
  )[1];
  expect(JSON.parse(togglePatch?.body ?? "null")).toEqual({ enabled: false });

  // 重置密码：既有 POST {new_password} 合同，模态框带标题与独立表单。
  // jsdom 中 rc-util useUUID 在 test 环境固定返回 "test-id"，dialog 可访问名称的
  // 唯一性断言由 Playwright 真实浏览器用例承担。
  fireEvent.click(screen.getByTestId("user-reset-2"));
  await screen.findByTestId("user-reset-password");
  await waitFor(() => {
    expect(document.querySelector(".ant-modal-title")?.textContent).toBe("重置 ordinary 的密码");
  });
  fireEvent.change(screen.getByTestId("user-reset-password"), {
    target: { value: "reset-secret-9" },
  });
  fireEvent.click(screen.getByTestId("user-reset-submit"));
  await screen.findByText("密码已重置，下一次登录必须修改；密码不会再次展示。");
  const reset = requests.find((request) => request.url.endsWith("/reset-password"));
  expect(JSON.parse(reset?.body ?? "null")).toEqual({ new_password: "reset-secret-9" });
  expect(document.body.textContent ?? "").not.toContain("reset-secret-9");
});

it("runs bulk enable/disable as per-user PATCHes and clears the selection", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(users);
    }
    if (/^\/api\/users\/\d+$/.test(request.url) && request.method === "PATCH") {
      const id = Number(request.url.split("/").pop());
      const body = JSON.parse(request.body ?? "{}") as Record<string, unknown>;
      return jsonResponse({ ...users[id - 1], ...body });
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  await screen.findByText("ordinary");
  const selectAll = screen.getAllByRole("checkbox")[0];
  fireEvent.click(selectAll);
  await screen.findByText("已选择 2 个账号");

  fireEvent.click(screen.getByTestId("users-bulk-disable"));
  await screen.findByText("已批量禁用 2 个账号；已有 Session 已失效。");
  const bulkPatches = requests.filter((request) => request.method === "PATCH");
  expect(bulkPatches).toHaveLength(2);
  for (const patch of bulkPatches) {
    expect(JSON.parse(patch.body ?? "null")).toEqual({ enabled: false });
  }
  await screen.findByText("已选择 0 个账号");
});

it("filters by keyword, role and status locally without extra list requests", async () => {
  const requests = installAccountFetch((request) => {
    if (request.url === "/api/users" && request.method === "GET") {
      return jsonResponse(users);
    }
    throw new Error(`unexpected request: ${request.method} ${request.url}`);
  });
  renderUserManagement();

  await screen.findByText("ordinary");
  const listRequests = () => requests.filter((request) => request.method === "GET").length;

  fireEvent.change(screen.getByLabelText("搜索账号"), { target: { value: "adm" } });
  await waitFor(() => {
    expect(screen.queryByText("ordinary")).toBeNull();
    expect(screen.getByText("admin")).toBeTruthy();
  });

  fireEvent.change(screen.getByLabelText("搜索账号"), { target: { value: "" } });
  await screen.findByText("ordinary");

  const queryFilter = screen.getByTestId("user-filter-form");
  expect(queryFilter).not.toBeNull();
  expect(queryFilter.querySelector(".user-filter-keyword")).not.toBeNull();
  expect(queryFilter.querySelector(".user-filter-role")).not.toBeNull();
  expect(queryFilter.querySelector(".user-filter-status")).not.toBeNull();
  const filterSelects = within(queryFilter).getAllByRole("combobox");
  const [roleFilter, statusFilter] = filterSelects;

  fireEvent.mouseDown(roleFilter);
  await openDropdownOption("管理员");
  await waitFor(() => {
    expect(screen.queryByText("ordinary")).toBeNull();
  });

  fireEvent.mouseDown(roleFilter);
  await openDropdownOption("全部");
  fireEvent.mouseDown(statusFilter);
  await openDropdownOption("已禁用");
  await screen.findByText("暂无账号用户");
  expect(listRequests()).toBe(1);
});
