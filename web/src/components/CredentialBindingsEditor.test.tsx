import { readFileSync } from "node:fs";
import { join } from "node:path";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api } from "../api";
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
  onError?: ReturnType<typeof vi.fn>;
  onRuntimeConflict?: ReturnType<typeof vi.fn>;
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
      onError={options.onError ?? vi.fn()}
      onRuntimeConflict={options.onRuntimeConflict}
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

it("keeps binding rows dirty and requests the shared runtime refresh after a 409", async () => {
  const onError = vi.fn();
  const onRuntimeConflict = vi.fn();
  vi.spyOn(api, "setAdapterBindings").mockRejectedValue(
    new ApiError(409, "adapter_runtime_locked", "runtime locked"),
  );
  renderEditor({
    platformRole: "admin",
    accessLevel: "owner",
    onError,
    onRuntimeConflict,
  });

  const envKey = await screen.findByTestId("binding-env-key");
  fireEvent.change(envKey, { target: { value: "NEW_API_TOKEN" } });
  fireEvent.click(screen.getByTestId("save-bindings"));

  await waitFor(() => expect(onRuntimeConflict).toHaveBeenCalledTimes(1));
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("NEW_API_TOKEN");
  expect((screen.getByTestId("save-bindings") as HTMLButtonElement).disabled).toBe(false);
  expect(onError).toHaveBeenLastCalledWith(expect.stringContaining("adapter_runtime_locked"));
});


it("loads existing bindings despite initial credential failure and retries while preserving dirty rows", async () => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([binding]);
  vi.spyOn(api, "listAdapterCredentialOptions")
    .mockRejectedValueOnce(new Error("catalog unavailable"))
    .mockResolvedValueOnce([credential]);
  const save = vi.spyOn(api, "setAdapterBindings").mockResolvedValue([{ ...binding, env_key: "DRAFT_TOKEN" }]);
  const onError = vi.fn();
  render(<CredentialBindingsEditor adapterId={11} disabled={false} accessLevel="owner"
    platformRole="user" useScopedCredentialOptions onError={onError} />);
  await screen.findByTestId("binding-credentials-load-failed");
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("API_TOKEN");
  expect(screen.queryByTestId("binding-credential-missing")).toBeNull();
  expect(onError).not.toHaveBeenCalled();
  fireEvent.change(screen.getByTestId("binding-env-key"), { target: { value: "DRAFT_TOKEN" } });
  fireEvent.click(screen.getByTestId("binding-retry-credentials"));
  await waitFor(() => expect(screen.queryByTestId("binding-credentials-load-failed")).toBeNull());
  expect(onError).not.toHaveBeenCalled();
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("DRAFT_TOKEN");
  expect(api.listAdapterBindings).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByTestId("save-bindings"));
  await waitFor(() => expect(save).toHaveBeenCalledWith(11, [{
    env_key: "DRAFT_TOKEN", credential_id: 7, field: "token",
  }]));
});

it("keeps initial bindings unconfirmed after the initial credential result is superseded by a failed refresh", async () => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([binding]);
  let releaseInitial: (items: Credential[]) => void = () => undefined;
  vi.spyOn(api, "listAdapterCredentialOptions")
    .mockImplementationOnce(() => new Promise((resolve) => { releaseInitial = resolve; }))
    .mockRejectedValueOnce(new Error("newer request failed"))
    .mockResolvedValueOnce([credential]);
  render(<CredentialBindingsEditor adapterId={11} disabled={false} accessLevel="owner"
    platformRole="user" useScopedCredentialOptions onError={vi.fn()} />);
  await waitFor(() => expect(api.listAdapterCredentialOptions).toHaveBeenCalledTimes(1));
  act(() => window.dispatchEvent(new Event("focus")));
  await waitFor(() => expect(api.listAdapterCredentialOptions).toHaveBeenCalledTimes(2));
  await act(async () => releaseInitial([credential]));
  await screen.findByTestId("binding-credentials-load-failed");
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("API_TOKEN");
  expect(screen.queryByTestId("binding-credential-missing")).toBeNull();
  fireEvent.change(screen.getByTestId("binding-env-key"), { target: { value: "DRAFT_TOKEN" } });
  fireEvent.click(screen.getByTestId("binding-retry-credentials"));
  await waitFor(() => expect(screen.queryByTestId("binding-credentials-load-failed")).toBeNull());
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("DRAFT_TOKEN");
});

it("continues to reject a confirmed deleted credential, including after a later refresh fails", async () => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([binding]);
  vi.spyOn(api, "listAdapterCredentialOptions").mockResolvedValueOnce([credential])
    .mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("later request failed"));
  const save = vi.spyOn(api, "setAdapterBindings").mockResolvedValue([]);
  const onError = vi.fn();
  render(<CredentialBindingsEditor adapterId={11} disabled={false} accessLevel="owner"
    platformRole="user" useScopedCredentialOptions onError={onError} />);
  const envKey = await screen.findByTestId("binding-env-key");
  fireEvent.change(envKey, { target: { value: "DRAFT_TOKEN" } });
  act(() => window.dispatchEvent(new Event("focus")));
  await screen.findByTestId("binding-credential-missing");
  act(() => window.dispatchEvent(new Event("focus")));
  await screen.findByTestId("binding-credentials-load-failed");
  expect(screen.getByTestId("binding-credential-missing")).toBeTruthy();
  fireEvent.click(screen.getByTestId("save-bindings"));
  expect(save).not.toHaveBeenCalled();
  expect(onError).toHaveBeenLastCalledWith(expect.stringContaining("所选凭据已被删除"));
  expect((envKey as HTMLInputElement).value).toBe("DRAFT_TOKEN");
});

it("retains backend credential rejection for an initially unconfirmed binding", async () => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([binding]);
  vi.spyOn(api, "listAdapterCredentialOptions").mockRejectedValue(new Error("catalog unavailable"));
  const save = vi.spyOn(api, "setAdapterBindings")
    .mockRejectedValue(new ApiError(404, "credential_not_found", "credential unavailable"));
  const onError = vi.fn();
  render(<CredentialBindingsEditor adapterId={11} disabled={false} accessLevel="owner"
    platformRole="user" useScopedCredentialOptions onError={onError} />);
  await screen.findByTestId("binding-credentials-load-failed");
  fireEvent.change(screen.getByTestId("binding-env-key"), { target: { value: "DRAFT_TOKEN" } });
  fireEvent.click(screen.getByTestId("save-bindings"));
  await waitFor(() => expect(save).toHaveBeenCalledWith(11, [{
    env_key: "DRAFT_TOKEN", credential_id: 7, field: "token",
  }]));
  await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining("credential_not_found")));
  expect((screen.getByTestId("binding-env-key") as HTMLInputElement).value).toBe("DRAFT_TOKEN");
});
