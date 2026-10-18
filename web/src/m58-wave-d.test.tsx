/** Product copy: concise v3 login and extended authenticated account copy in both languages. */

import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import LoginPage from "./components/LoginPage";
import LoginShell from "./components/LoginShell";
import {
  applySystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  SYSTEM_LOCALE_STORAGE_KEY,
} from "./i18n";
import { LOGIN_LOCALE_STORAGE_KEY } from "./login-locale";

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  window.localStorage.removeItem(SYSTEM_LOCALE_STORAGE_KEY);
  window.localStorage.removeItem(LOGIN_LOCALE_STORAGE_KEY);
});

it("renders the confirmed zh-CN product copy on the login page", () => {
  render(<LoginPage notice={null} onSubmit={vi.fn()} />);

  expect(screen.getByText("DataLinkRuntime")).toBeTruthy();
  expect(screen.getByText("用代码连接你的数据")).toBeTruthy();
  expect(screen.getByText("编写 → 运行 → 查看结果")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "欢迎登录 DLR 控制台" })).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")).toBeNull();
  expect(screen.queryByText("把数据连接逻辑写成 Adapter（适配器），然后直接运行。")).toBeNull();
  expect(screen.getByText("© 2026 DataLinkRuntime · MIT License")).toBeTruthy();
  // 旧 footer 与功能列表已移除。
  expect(document.body.textContent).not.toContain("© DataLinkRuntime (DLR)");
  expect(screen.queryByText("轻量易用")).toBeNull();
});

it("renders the natural English equivalent on the en login page", async () => {
  await applySystemLocale("en");
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, "en");
  render(<LoginPage notice={null} onSubmit={vi.fn()} />);

  expect(screen.getByText("DataLinkRuntime")).toBeTruthy();
  expect(screen.getByText("Connect your data with code")).toBeTruthy();
  expect(screen.getByText("Write → Run → Inspect results")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Welcome to the DLR Console" })).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")).toBeNull();
  expect(screen.queryByText("Write your data connection logic as Adapters, then run them directly.")).toBeNull();
  expect(screen.getByText("© 2026 DataLinkRuntime · MIT License")).toBeTruthy();
  expect(document.body.textContent).not.toContain("© DataLinkRuntime (DLR)");
});


it.each([
  ["zh-CN", "把数据连接逻辑写成 Adapter（适配器），然后直接运行。", "从代码编辑、依赖配置到执行、日志与历史追踪，\nDataLinkRuntime 提供一个轻量、自托管的完整运行环境。"],
  ["en", "Write your data connection logic as Adapters, then run them directly.", "From code editing and dependency setup to execution, logs, and history tracking,\nDataLinkRuntime provides a lightweight, self-hosted, complete runtime environment."],
] as const)("preserves extended product copy on authenticated account surfaces in %s", async (locale, intro, detail) => {
  await applySystemLocale(locale);
  render(<LoginShell loginSurface={false}><div>Account settings</div></LoginShell>);
  expect(screen.getByText(intro)).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")?.textContent).toBe(detail);
  expect(screen.getByRole("combobox")).toHaveProperty("disabled", true);
});
