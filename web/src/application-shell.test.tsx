import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, expect, it } from "vitest";

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
        healthText="控制服务正常"
        healthDotClass="health-dot-ok"
        workers={[]}
        workersLoading={false}
        workersError={null}
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
  expect(await screen.findByTestId("shell-child")).toBeTruthy();
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
  expect((await screen.findByTestId("control-status")).getAttribute("aria-live")).toBe("polite");
});
