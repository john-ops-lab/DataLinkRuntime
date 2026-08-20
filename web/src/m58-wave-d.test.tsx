/** M5.8 Wave D：首页产品文案（M5.8-011）双语言精确锁定。 */

import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import LoginPage from "./components/LoginPage";
import {
  applySystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  SYSTEM_LOCALE_STORAGE_KEY,
} from "./i18n";

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  window.localStorage.removeItem(SYSTEM_LOCALE_STORAGE_KEY);
});

it("renders the confirmed zh-CN product copy on the login page", () => {
  render(<LoginPage notice={null} onSubmit={vi.fn()} />);

  expect(screen.getByText("DataLinkRuntime")).toBeTruthy();
  expect(screen.getByText("Code your data connections.")).toBeTruthy();
  expect(
    screen.getByText("把数据连接逻辑写成 Adapter（适配器），然后直接运行。"),
  ).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")?.textContent).toBe(
    "从代码编辑、依赖配置到执行、日志与历史追踪，\nDataLinkRuntime 提供一个轻量、自托管的完整运行环境。",
  );
  expect(screen.getByText("Develop → Run → Observe")).toBeTruthy();
  expect(screen.getByText("© 2026 DataLinkRuntime · MIT License")).toBeTruthy();
  // 旧 footer 与功能列表已移除。
  expect(document.body.textContent).not.toContain("© DataLinkRuntime (DLR)");
  expect(screen.queryByText("轻量易用")).toBeNull();
});

it("renders the natural English equivalent on the en login page", async () => {
  await applySystemLocale("en");
  render(<LoginPage notice={null} onSubmit={vi.fn()} />);

  expect(screen.getByText("DataLinkRuntime")).toBeTruthy();
  expect(screen.getByText("Code your data connections.")).toBeTruthy();
  expect(
    screen.getByText("Write your data connection logic as Adapters, then run them directly."),
  ).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")?.textContent).toBe(
    "From code editing and dependency setup to execution, logs, and history tracking,\n" +
      "DataLinkRuntime provides a lightweight, self-hosted, complete runtime environment.",
  );
  expect(screen.getByText("Develop → Run → Observe")).toBeTruthy();
  expect(screen.getByText("© 2026 DataLinkRuntime · MIT License")).toBeTruthy();
  expect(document.body.textContent).not.toContain("© DataLinkRuntime (DLR)");
});
