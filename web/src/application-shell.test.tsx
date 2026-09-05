import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import ApplicationShell from "./components/ApplicationShell";
import DlrDesignSystemProvider from "./design-system";
import { i18n } from "./i18n";

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
});

afterEach(() => {
  document.body.innerHTML = "";
});

function renderShell(overrides: Partial<ComponentProps<typeof ApplicationShell>> = {}) {
  return render(
    <DlrDesignSystemProvider>
      <ApplicationShell
        systemStatusLevel="normal"
        systemStatusText="系统正常"
        canManageUsers
        onOpenUserManagement={() => undefined}
        onOpenSystemSettings={() => undefined}
        selectedAdapterName={null}
        section="adapters"
        onSectionChange={() => undefined}
        {...overrides}
      >
        <div data-testid="shell-child">content</div>
      </ApplicationShell>
    </DlrDesignSystemProvider>,
  );
}

it("renders the compact shell with one Adapter navigation surface", async () => {
  renderShell();

  expect(document.querySelector(".dlr-app-layout")).toBeTruthy();
  expect(document.querySelector(".app-header")).toBeTruthy();
  expect(document.querySelector(".ant-pro-layout")).toBeNull();
  expect(document.querySelector(".ant-pro-page-container")).toBeNull();
  expect(document.querySelector(".app-header-product")?.textContent).toBe("DataLinkRuntime");
  expect(screen.getByRole("link", { name: "适配器" }).getAttribute("href")).toBe("/adapters");
  expect(screen.getByRole("link", { name: "模板广场" }).getAttribute("href")).toBe("/templates");
  expect(screen.getByRole("link", { name: "适配器" }).getAttribute("aria-current")).toBe("page");
  expect(await screen.findByTestId("shell-child")).toBeTruthy();
});

it("uses SPA navigation and exposes the selected page", () => {
  const onPrimarySectionChange = vi.fn();
  renderShell({ activeSection: "templates", onPrimarySectionChange });

  const templates = screen.getByRole("link", { name: "模板广场" });
  expect(templates.getAttribute("aria-current")).toBe("page");
  fireEvent.click(screen.getByRole("link", { name: "适配器" }));
  expect(onPrimarySectionChange).toHaveBeenCalledWith("adapters");
});

it("blocks conflicting navigation while a management action is busy", () => {
  const onPrimarySectionChange = vi.fn();
  renderShell({ navigationDisabled: true, onPrimarySectionChange });

  const templates = screen.getByRole("link", { name: "模板广场" });
  expect(templates.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(templates);
  expect(onPrimarySectionChange).not.toHaveBeenCalled();
});

it("keeps account actions in the avatar menu with accessible names", async () => {
  renderShell({
    accountPrincipal: {
      id: 7,
      username: "reader-user",
      role: "user",
      enabled: true,
      must_change_password: false,
    },
    onOpenAccountProfile: () => undefined,
    onAccountLogout: async () => undefined,
  });

  fireEvent.click(await screen.findByTestId("user-menu"));
  expect(await screen.findByRole("menuitem", { name: "用户管理" })).toBeTruthy();
  expect(await screen.findByRole("menuitem", { name: "系统设置" })).toBeTruthy();
  expect(await screen.findByRole("menuitem", { name: "账号资料" })).toBeTruthy();
  expect(await screen.findByRole("menuitem", { name: "退出登录" })).toBeTruthy();
  expect((await screen.findByTestId("system-status-summary")).textContent).toContain("系统正常");
  expect(document.querySelector(".system-status-summary-content")?.getAttribute("aria-live")).toBe("polite");
});

it("lets administrators open System Status while keeping the ordinary-user summary read-only", () => {
  const openSystemStatus = vi.fn();
  const first = renderShell({ onOpenSystemStatus: openSystemStatus });
  expect(screen.getByTestId("system-status-summary").tagName).toBe("BUTTON");
  fireEvent.click(screen.getByTestId("system-status-summary"));
  expect(openSystemStatus).toHaveBeenCalledTimes(1);
  first.unmount();

  renderShell({ canManageUsers: false, onOpenSystemStatus: openSystemStatus });
  expect(screen.getByTestId("system-status-summary").tagName).toBe("SPAN");
  fireEvent.click(screen.getByTestId("system-status-summary"));
  expect(openSystemStatus).toHaveBeenCalledTimes(1);
});
