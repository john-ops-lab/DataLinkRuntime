import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import App, { ANT_DESIGN_LOCALES } from "./App";
import { api } from "./api";
import {
  applySystemLocale,
  currentSystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  SYSTEM_LOCALE_STORAGE_KEY,
} from "./i18n";
import {
  cacheLoginLocalePreference,
  LOGIN_LOCALE_STORAGE_KEY,
  preferredLoginLocale,
  readLoginLocalePreference,
} from "./login-locale";
import LoginPage from "./components/LoginPage";
import SystemSettingsDrawer from "./components/SystemSettingsDrawer";

function mockSettingsPanelApis(): void {
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue({
    pypi: { kind: "pypi", name: "PyPI", index_url: "https://pypi.org/simple/" },
    npm: { kind: "npm", name: "npm", index_url: "https://registry.npmjs.org/" },
    maven: { kind: "maven", name: "Maven", index_url: "https://repo1.maven.org/maven2/" },
  });
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
}

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  window.localStorage.removeItem(SYSTEM_LOCALE_STORAGE_KEY);
  window.localStorage.removeItem(LOGIN_LOCALE_STORAGE_KEY);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("switches the System Settings language control immediately", async () => {
  mockSettingsPanelApis();
  const update = vi.spyOn(api, "updateSystemLocale").mockResolvedValue({ locale: "en" });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  const select = screen.getByTestId("system-locale-select");
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);
  fireEvent.click(await screen.findByText("English"));

  await waitFor(() => expect(update).toHaveBeenCalledWith("en"));
  expect(await screen.findByText("System settings")).toBeTruthy();
  expect(screen.getByText("Language")).toBeTruthy();
  expect(document.documentElement.lang).toBe("en");
});

it("renders the login page in English and never exposes a missing key", async () => {
  await applySystemLocale("en");
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "en");

  render(<LoginPage notice={null} onSubmit={vi.fn()} />);

  expect(screen.getByRole("heading", { name: "Welcome to the DLR Console" })).toBeTruthy();
  expect(screen.getByPlaceholderText("Enter administrator Token")).toBeTruthy();
  expect(document.body.textContent).not.toContain("auth.loginTitle");
});

it("defaults the login page to zh-CN and then respects an explicit saved choice", async () => {
  await applySystemLocale("en");
  window.localStorage.removeItem(LOGIN_LOCALE_STORAGE_KEY);

  const first = render(<LoginPage notice={null} onSubmit={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "欢迎登录 DLR 控制台" })).toBeTruthy());
  first.unmount();

  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "en");
  render(<LoginPage notice={null} onSubmit={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "Welcome to the DLR Console" })).toBeTruthy());
});

it("ignores invalid or unavailable login-locale storage without blocking login", () => {
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "fr");
  expect(readLoginLocalePreference()).toBeNull();
  expect(preferredLoginLocale()).toBe(DEFAULT_SYSTEM_LOCALE);

  const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("storage denied", "SecurityError");
  });
  const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("storage denied", "SecurityError");
  });
  expect(readLoginLocalePreference()).toBeNull();
  expect(() => cacheLoginLocalePreference("en")).not.toThrow();
  getItem.mockRestore();
  setItem.mockRestore();
});

it("keeps the login default independent from a changing deployment locale", async () => {
  await applySystemLocale("en");
  const localeResponse = { locale: "zh-CN" };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) !== "/api/locale") {
        throw new Error(`Unexpected request: ${String(input)}`);
      }
      return {
        ok: true,
        status: 200,
        json: async () => localeResponse,
      };
    }),
  );

  const first = render(<App />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "欢迎登录 DLR 控制台" })).toBeTruthy());
  expect(window.localStorage.getItem(SYSTEM_LOCALE_STORAGE_KEY)).toBe("zh-CN");

  first.unmount();
  localeResponse.locale = "en";
  window.localStorage.setItem(SYSTEM_LOCALE_STORAGE_KEY, "zh-CN");
  render(<App />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "欢迎登录 DLR 控制台" })).toBeTruthy());
  expect(window.localStorage.getItem(SYSTEM_LOCALE_STORAGE_KEY)).toBe("en");
});

it("re-reads the backend locale after administrator login", async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  sessionStorage.clear();
  // The login page may prefer Chinese, but the authenticated Console must
  // still apply the server-owned English locale after verification.
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "zh-CN");
  let localeReads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/locale") {
        localeReads += 1;
        return {
          ok: true,
          status: 200,
          json: async () => ({ locale: localeReads === 1 ? "zh-CN" : "en" }),
        };
      }
      if (url === "/api/auth/admin/verify") {
        return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
      }
      if (url === "/api/health") {
        return { ok: true, status: 200, json: async () => ({ status: "ok", database: true }) };
      }
      if (url === "/api/adapters" || url === "/api/workers") {
        return { ok: true, status: 200, json: async () => [] };
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );

  render(<App />);
  await screen.findByRole("heading", { name: "欢迎登录 DLR 控制台" });
  fireEvent.change(screen.getByTestId("admin-token-input"), { target: { value: "admin" } });
  fireEvent.click(screen.getByTestId("admin-token-submit"));

  await screen.findByTestId("control-status");
  expect(localeReads).toBe(2);
  expect(currentSystemLocale()).toBe("en");
});

it("uses the cached system locale when its post-login refresh fails", async () => {
  await applySystemLocale("en");
  sessionStorage.clear();
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "zh-CN");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/locale") {
        throw new TypeError("locale temporarily unavailable");
      }
      if (url === "/api/auth/admin/verify") {
        return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
      }
      if (url === "/api/health") {
        return { ok: true, status: 200, json: async () => ({ status: "ok", database: true }) };
      }
      if (url === "/api/adapters" || url === "/api/workers") {
        return { ok: true, status: 200, json: async () => [] };
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );

  render(<App />);
  await screen.findByRole("heading", { name: "欢迎登录 DLR 控制台" });
  fireEvent.change(screen.getByTestId("admin-token-input"), { target: { value: "admin" } });
  fireEvent.click(screen.getByTestId("admin-token-submit"));

  await screen.findByTestId("control-status");
  expect(currentSystemLocale()).toBe("en");
});

it("maps both supported system locales to matching Ant Design locales", () => {
  expect(ANT_DESIGN_LOCALES["zh-CN"].locale).toBeDefined();
  expect(ANT_DESIGN_LOCALES.en.locale).toBeDefined();
  expect(ANT_DESIGN_LOCALES["zh-CN"].Pagination?.items_per_page).not.toBe(
    ANT_DESIGN_LOCALES.en.Pagination?.items_per_page,
  );
});
