import { readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE, resources } from "../i18n";
import type { AccountRole, AdapterAccessLevel, Credential, CredentialBinding } from "../types";
import CredentialBindingsEditor from "./CredentialBindingsEditor";

const credential: Credential = {
  id: 7,
  name: "fixture-credential",
  type: "token",
  created_at: "2026-08-24T00:00:00Z",
  updated_at: "2026-08-24T00:00:00Z",
};

const binding: CredentialBinding = {
  env_key: "API_TOKEN",
  credential_id: credential.id,
  credential_name: credential.name,
  credential_type: credential.type,
  field: "token",
};

function renderEditor(options: {
  platformRole: AccountRole;
  accessLevel: AdapterAccessLevel;
  disabled?: boolean;
}) {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([binding]);
  vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValue([credential]);
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential]);

  render(
    <CredentialBindingsEditor
      adapterId={11}
      disabled={options.disabled ?? false}
      accessLevel={options.accessLevel}
      platformRole={options.platformRole}
      useScopedCredentialOptions
      onError={vi.fn()}
      onOpenSettings={vi.fn()}
    />,
  );
}

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("keeps the authenticated platform admin hint and keyboard-reachable settings entry", async () => {
  renderEditor({ platformRole: "admin", accessLevel: "owner" });

  const settingsButton = await screen.findByTestId("open-settings-for-credentials");
  expect(settingsButton.textContent).toContain("打开系统设置");
  expect(screen.getByText("如需新增凭据，请前往「系统设置 → 凭据管理」新建。")).toBeTruthy();
  expect(screen.queryByTestId("credential-binding-role-hint")).toBeNull();

  (settingsButton as HTMLButtonElement).focus();
  expect(document.activeElement).toBe(settingsButton);
  expect((settingsButton as HTMLButtonElement).tabIndex).toBeGreaterThanOrEqual(0);
});

it("uses platform role rather than Adapter ownership for a non-admin owner", async () => {
  renderEditor({ platformRole: "user", accessLevel: "owner" });

  const hint = await screen.findByTestId("credential-binding-role-hint");
  expect(hint.textContent).toBe("如需新增凭据，请联系管理员前往「系统设置 → 凭据管理」新建。");
  expect(screen.queryByTestId("open-settings-for-credentials")).toBeNull();
  // Adapter-owner binding controls remain available; only the global settings
  // entry is role-gated.
  expect(screen.getByRole("combobox", { name: "绑定 1 凭据" })).toBeTruthy();
  expect(screen.getByTestId("add-binding")).toBeTruthy();
  expect(api.listAdapterCredentialOptions).toHaveBeenCalledWith(11);
  expect(api.listCredentials).not.toHaveBeenCalled();
});

it("keeps read-only non-owner metadata access while hiding binding writes and settings entry", async () => {
  renderEditor({ platformRole: "user", accessLevel: "read", disabled: true });

  const hint = await screen.findByTestId("credential-binding-role-hint");
  expect(hint.textContent).toBe("如需新增凭据，请联系管理员前往「系统设置 → 凭据管理」新建。");
  expect(screen.queryByTestId("open-settings-for-credentials")).toBeNull();
  expect(screen.getByTestId("binding-credential-readonly").textContent).toBe("fixture-credential");
  expect(screen.queryByTestId("add-binding")).toBeNull();
  expect(screen.queryByTestId("save-bindings")).toBeNull();
  expect(api.listAdapterCredentialOptions).not.toHaveBeenCalled();
  expect(api.listAdapterBindings).toHaveBeenCalledWith(11);
});

it("renders the exact English non-admin copy and preserves the admin entry semantics", async () => {
  await applySystemLocale("en");
  renderEditor({ platformRole: "user", accessLevel: "owner" });

  const hint = await screen.findByTestId("credential-binding-role-hint");
  expect(hint.textContent).toBe(
    "To add a credential, ask an administrator to go to “System Settings → Credentials” and create one.",
  );
  expect(screen.queryByTestId("open-settings-for-credentials")).toBeNull();

  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  expect(await screen.findByText("如需新增凭据，请联系管理员前往「系统设置 → 凭据管理」新建。")).toBeTruthy();
});

it("keeps the role-hint resource key exact, parity-complete and out of component source", () => {
  const zhHint = resources["zh-CN"].settings.bindings.nonAdminOpenSettingsHint;
  const enHint = resources.en.settings.bindings.nonAdminOpenSettingsHint;
  expect(zhHint).toBe("如需新增凭据，请联系管理员前往「系统设置 → 凭据管理」新建。");
  expect(enHint).toBe(
    "To add a credential, ask an administrator to go to “System Settings → Credentials” and create one.",
  );
  expect(Object.keys(resources["zh-CN"].settings.bindings).sort()).toEqual(
    Object.keys(resources.en.settings.bindings).sort(),
  );

  const componentSource = readFileSync(
    join(process.cwd(), "src/components/CredentialBindingsEditor.tsx"),
    "utf8",
  );
  expect(componentSource).not.toContain("如需新增凭据，请联系管理员前往");
  expect(componentSource).not.toContain("To add a credential, ask an administrator");
});
