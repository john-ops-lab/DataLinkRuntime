import { expect, test, type Page } from "@playwright/test";

/** Synthetic API fixtures only: these tests never contact a deployment or create objects. */
async function installEmptyConsole(page: Page, locale: "zh-CN" | "en") {
  const mutations: string[] = [];
  await page.addInitScript(() => sessionStorage.setItem("dlr-admin-token", "synthetic-test-token"));
  await page.route("**/entry-mode.js", (route) => route.fulfill({
    contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "token";',
  }));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") mutations.push(`${route.request().method()} ${path}`);
    const body = path === "/api/locale" ? { locale }
      : path === "/api/health" ? { status: "ok", database: true } : [];
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return mutations;
}

async function expectVisibleNamedFocus(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const element = document.activeElement as HTMLElement;
    const rect = element?.getBoundingClientRect();
    const input = element as HTMLInputElement;
    const name = element?.getAttribute("aria-label") || input.labels?.[0]?.textContent || element?.textContent;
    return Boolean(rect && rect.width > 0 && rect.height > 0 && name?.trim()
      && element.closest('[role="dialog"]') && !element.hasAttribute("data-sentinel"));
  })).toBe(true);
}

for (const locale of ["zh-CN", "en"] as const) {
  test(`create Adapter initial focus, Tab boundaries and exit: ${locale}`, async ({ page }) => {
    const mutations = await installEmptyConsole(page, locale);
    await page.goto("/adapters");
    await page.waitForLoadState("networkidle");
    const trigger = page.getByTestId("show-create-form");
    await trigger.click();
    const name = page.getByTestId("new-adapter-name");
    await expect(name).toBeFocused();
    await expectVisibleNamedFocus(page);
    for (let index = 0; index < 16; index += 1) {
      await page.keyboard.press("Tab");
      await expectVisibleNamedFocus(page);
    }
    for (let index = 0; index < 16; index += 1) {
      await page.keyboard.press("Shift+Tab");
      await expectVisibleNamedFocus(page);
    }
    const dialog = page.getByRole("dialog");
    const close = dialog.locator(".ant-drawer-close");
    const cancel = dialog.getByRole("button", { name: locale === "en" ? "Cancel" : /取\s*消/ });
    await cancel.focus();
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(cancel).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(name).toBeHidden();
    await expect(trigger).toBeFocused();
    await trigger.click();
    await expect(name).toBeFocused();
    await page.getByRole("dialog").locator(".ant-drawer-close").click();
    await expect(name).toBeHidden();
    await expect(trigger).toBeFocused();
    await trigger.click();
    await expect(name).toBeFocused();
    await page.getByRole("dialog").getByRole("button", { name: locale === "en" ? "Cancel" : /取\s*消/ }).click();
    await expect(name).toBeHidden();
    await expect(trigger).toBeFocused();
    expect(mutations).toEqual([]);
  });

  test(`credential required field association and keyboard correction: ${locale}`, async ({ page }) => {
    const mutations = await installEmptyConsole(page, locale);
    await page.goto("/settings/credentials");
    await page.waitForLoadState("networkidle");
    const trigger = page.getByTestId("new-credential");
    await trigger.click();
    const name = page.getByTestId("credential-name");
    const submit = page.getByTestId("submit-credential");
    await submit.focus();
    await page.keyboard.press("Enter");
    await expect(name).toHaveAttribute("aria-invalid", "true");
    const id = await name.getAttribute("aria-describedby");
    expect(id).toBeTruthy();
    await expect(name).toHaveAttribute("aria-errormessage", id!);
    const associated = await name.evaluate((element) => {
      const error = document.getElementById(element.getAttribute("aria-describedby")!);
      return { text: error?.textContent, inside: Boolean(element.closest('[role="dialog"]')?.contains(error)) };
    });
    expect(associated.inside).toBe(true);
    expect(associated.text).toBe(locale === "en" ? "Credential name cannot be empty" : "凭据名称不能为空");
    await expect(name).toHaveAccessibleDescription(locale === "en" ? "Credential name cannot be empty" : "凭据名称不能为空");
    await name.focus();
    await page.keyboard.type("non-secret draft");
    await expect(name).toHaveAttribute("aria-invalid", "false");
    await expect(name).not.toHaveAttribute("aria-describedby");
    await submit.focus();
    await page.keyboard.press("Enter");
    const username = page.getByTestId("credential-field-username");
    await expect(username).toHaveAttribute("aria-invalid", "true");
    await username.focus();
    await page.keyboard.type("synthetic-user");
    await expect(username).toHaveAttribute("aria-invalid", "false");
    await page.getByRole("dialog").getByRole("button", { name: locale === "en" ? "Cancel" : /取\s*消/ }).click();
    await expect(name).toBeHidden();
    await expect(trigger).toBeFocused();
    expect(mutations).toEqual([]);
  });
}
