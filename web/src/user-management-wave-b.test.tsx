import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App from "./App";
import { setAuthToken } from "./api";
import { applySystemLocale } from "./i18n";
import UserManagementDrawer from "./components/UserManagementDrawer";
import { LOGIN_LOCALE_STORAGE_KEY } from "./login-locale";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as Response;
}

const users = [
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
    must_change_password: true,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  },
] as const;

beforeEach(() => {
  window.__DLR_ENTRY_MODE__ = "token";
  document.cookie = "dlr_account_csrf=csrf-test-token; path=/";
  window.localStorage.removeItem(LOGIN_LOCALE_STORAGE_KEY);
  setAuthToken("test-superadmin-token");
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(async () => {
  window.__DLR_ENTRY_MODE__ = "token";
  document.cookie = "dlr_account_csrf=; Max-Age=0; path=/";
  window.localStorage.removeItem(LOGIN_LOCALE_STORAGE_KEY);
  setAuthToken(null);
  vi.unstubAllGlobals();
  await applySystemLocale("zh-CN");
});

it("renders bilingual user management and never displays a submitted password", async () => {
  await applySystemLocale("en");
  const requests: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url === "/api/users" && (init?.method ?? "GET") === "GET") {
      return jsonResponse(users);
    }
    if (url === "/api/users" && init?.method === "POST") {
      return jsonResponse({
        ...users[1],
        id: 3,
        username: "created-user",
        role: "user",
      }, 201);
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  render(<UserManagementDrawer open onClose={() => undefined} />);
  await screen.findByText("User management");
  expect(screen.getByTestId("user-create-password")).toBeTruthy();
  expect(screen.queryByText(/scrypt|password_hash/i)).toBeNull();

  fireEvent.change(screen.getByTestId("user-create-username"), {
    target: { value: "created-user" },
  });
  fireEvent.change(screen.getByTestId("user-create-password"), {
    target: { value: "created-user-1" },
  });
  fireEvent.click(screen.getByTestId("user-create-submit"));
  await waitFor(() => expect(screen.getByText("Account created. The first login must change its password.")).toBeTruthy());
  expect((screen.getByTestId("user-create-password") as HTMLInputElement).value).toBe("");
  const createRequest = requests.find((request) => request.init?.method === "POST");
  expect(String(createRequest?.init?.body)).toContain("created-user-1");
  expect(screen.queryByText("created-user-1")).toBeNull();

  await applySystemLocale("zh-CN");
  await waitFor(() => expect(screen.getByText("用户管理")).toBeTruthy());
  expect(screen.getByText(/密码提交后不会再次返回或展示/)).toBeTruthy();
});

it("shows the ACL-scoped business console while keeping system management hidden", async () => {
  const requests: string[] = [];
  // A saved Chinese login preference must not override the authenticated
  // account Console's backend-owned English locale.
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "zh-CN");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    requests.push(url);
    if (url === "/api/locale") {
      return jsonResponse({ locale: "en" });
    }
    if (url === "/api/auth/account/csrf") {
      document.cookie = "dlr_account_csrf=csrf-test-token; path=/";
      return jsonResponse({ status: "ok" });
    }
    if (url === "/api/auth/account/me") {
      return jsonResponse({ principal: { ...users[1], must_change_password: false } });
    }
    if (url === "/api/health") {
      return jsonResponse({ status: "ok", database: true });
    }
    if (url === "/api/adapters") {
      return jsonResponse([]);
    }
    if (url === "/api/workers") {
      return jsonResponse([]);
    }
    throw new Error(`unexpected request: ${url}`);
  }));

  window.__DLR_ENTRY_MODE__ = "account";
  render(<App />);
  await screen.findByTestId("account-principal");
  await waitFor(() => expect(screen.getByTestId("control-status").textContent).toContain("Control service healthy"));
  expect(screen.queryByTestId("account-profile")).toBeNull();
  expect(screen.queryByTestId("user-management")).toBeNull();
  expect(screen.queryByTestId("system-settings")).toBeNull();
  expect(requests).toContain("/api/adapters");
  expect(requests).toContain("/api/workers");

  fireEvent.click(screen.getByTestId("user-menu"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Account profile" }));
  await screen.findByTestId("account-profile-username");
  expect(screen.getByDisplayValue("ordinary")).toBeTruthy();
});
