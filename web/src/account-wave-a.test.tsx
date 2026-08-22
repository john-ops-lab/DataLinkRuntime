import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App from "./App";
import { setAuthToken } from "./api";
import { applySystemLocale } from "./i18n";

interface RouteState {
  loggedIn: boolean;
  mustChange: boolean;
  locale: "zh-CN" | "en";
}

const principal = (mustChange: boolean) => ({
  id: 1,
  username: "admin",
  role: "admin",
  enabled: true,
  must_change_password: mustChange,
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as Response;
}

function installAccountFetch(state: RouteState) {
  const requests: { url: string; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url === "/api/locale") {
      return jsonResponse({ locale: state.locale });
    }
    if (url === "/api/auth/account/csrf") {
      document.cookie = "dlr_account_csrf=csrf-test-token; path=/";
      return jsonResponse({ status: "ok" });
    }
    if (url === "/api/auth/account/me") {
      return state.loggedIn
        ? jsonResponse({ principal: principal(state.mustChange) })
        : jsonResponse({ detail: { code: "account_session_required", message: "expired" } }, 401);
    }
    if (url === "/api/auth/account/login") {
      state.loggedIn = true;
      document.cookie = "dlr_account_session=session-test-token; path=/";
      return jsonResponse({ principal: principal(state.mustChange) });
    }
    if (url === "/api/auth/account/change-password") {
      state.loggedIn = false;
      document.cookie = "dlr_account_session=; Max-Age=0; path=/";
      return jsonResponse({ status: "ok" });
    }
    if (url === "/api/auth/account/logout") {
      state.loggedIn = false;
      document.cookie = "dlr_account_session=; Max-Age=0; path=/";
      return jsonResponse({ status: "ok" });
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
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests };
}

beforeEach(() => {
  window.__DLR_ENTRY_MODE__ = "account";
  document.cookie = "dlr_account_csrf=; Max-Age=0; path=/";
  document.cookie = "dlr_account_session=; Max-Age=0; path=/";
  sessionStorage.clear();
  setAuthToken(null);
});

afterEach(async () => {
  window.__DLR_ENTRY_MODE__ = "token";
  document.cookie = "dlr_account_csrf=; Max-Age=0; path=/";
  document.cookie = "dlr_account_session=; Max-Age=0; path=/";
  setAuthToken(null);
  vi.unstubAllGlobals();
  await applySystemLocale("zh-CN");
});

it("keeps the account entry bilingual and gates the first login behind password change", async () => {
  const state: RouteState = { loggedIn: false, mustChange: true, locale: "en" };
  const { requests } = installAccountFetch(state);
  render(<App />);

  await screen.findByTestId("account-username-input");
  expect(screen.getByText("Account login")).toBeTruthy();
  fireEvent.change(screen.getByTestId("account-username-input"), { target: { value: "admin" } });
  fireEvent.change(screen.getByTestId("account-password-input"), { target: { value: "admin123" } });
  fireEvent.click(screen.getByTestId("account-login-submit"));

  await screen.findByTestId("account-current-password-input");
  expect(screen.getByText("Change your password")).toBeTruthy();
  expect(screen.getByText(/must change its password/)).toBeTruthy();
  const loginRequest = requests.find((request) => request.url === "/api/auth/account/login");
  expect(loginRequest).toBeDefined();
  expect(loginRequest?.init?.credentials).toBe("same-origin");
  expect(loginRequest?.init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test-token" });
  expect(String(loginRequest?.init?.body)).not.toContain("scrypt");

  fireEvent.change(screen.getByTestId("account-current-password-input"), {
    target: { value: "admin123" },
  });
  fireEvent.change(screen.getByTestId("account-new-password-input"), {
    target: { value: "new-admin-123" },
  });
  fireEvent.change(screen.getByTestId("account-confirm-password-input"), {
    target: { value: "new-admin-123" },
  });
  fireEvent.click(screen.getByTestId("account-password-submit"));

  await screen.findByTestId("account-username-input");
  expect(screen.getByTestId("account-auth-notice").textContent).toContain("Password changed");
});

it("shows the current Principal on the protected account console and logs out", async () => {
  const state: RouteState = { loggedIn: false, mustChange: false, locale: "zh-CN" };
  const { requests } = installAccountFetch(state);
  render(<App />);

  await screen.findByTestId("account-username-input");
  fireEvent.change(screen.getByTestId("account-username-input"), { target: { value: "admin" } });
  fireEvent.change(screen.getByTestId("account-password-input"), { target: { value: "new-password" } });
  fireEvent.click(screen.getByTestId("account-login-submit"));

  await screen.findByTestId("account-principal");
  expect(screen.getByTestId("account-principal").textContent).toContain("admin");
  expect(screen.getByTestId("account-principal").textContent).toContain("管理员");
  await waitFor(() => expect(screen.getByTestId("control-status").textContent).toContain("控制服务正常"));
  expect(requests.some((request) => request.url === "/api/adapters")).toBe(true);
  expect(requests.some((request) => request.init?.headers && "Authorization" in (request.init.headers as Record<string, string>))).toBe(false);

  fireEvent.click(screen.getByTestId("user-menu"));
  fireEvent.click(await screen.findByRole("menuitem", { name: /退出登录|Log out/ }));
  await screen.findByTestId("account-username-input");
  expect(screen.getByTestId("account-auth-notice").textContent).toContain("已退出登录");
});
