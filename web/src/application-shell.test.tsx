import { render, screen } from "@testing-library/react";
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

it("renders the ProLayout shell, PageContainer hierarchy, and a disabled workbench menu before selection", async () => {
  renderShell();

  expect(document.querySelector(".ant-pro-layout")).toBeTruthy();
  expect(document.querySelector(".ant-pro-page-container")).toBeTruthy();
  expect((await screen.findByTestId("page-title")).textContent).toBe("适配器工作区");
  expect(await screen.findByRole("menu", { name: "控制台导航" })).toBeTruthy();
  expect(await screen.findByRole("menuitem", { name: "适配器" })).toBeTruthy();
  expect((await screen.findByRole("menuitem", { name: "工作台" })).getAttribute("aria-disabled")).toBe("true");
  expect(await screen.findByTestId("shell-child")).toBeTruthy();
});

it("keeps global account actions named and available through keyboard-accessible buttons", async () => {
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

  expect(await screen.findByRole("button", { name: "用户管理" })).toBeTruthy();
  expect(await screen.findByRole("button", { name: "系统设置" })).toBeTruthy();
  expect(await screen.findByRole("button", { name: "账号资料" })).toBeTruthy();
  expect(await screen.findByRole("button", { name: "退出登录" })).toBeTruthy();
  expect((await screen.findByTestId("control-status")).getAttribute("aria-live")).toBe("polite");
});
