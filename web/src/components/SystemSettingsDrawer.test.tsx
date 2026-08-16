/**
 * M5.5.2（UX-003）：凭据增删改后，依赖凭据元数据的选择器无需 F5 即可同步；
 * 同步链路只传递元数据，Secret 真值永远不进入浏览器共享状态。
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api";
import type { Credential, CredentialType } from "../types";
import SystemSettingsDrawer from "./SystemSettingsDrawer";

function credentialMetadata(id: number, name: string, type: CredentialType): Credential {
  return {
    id,
    name,
    type,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:00Z",
  };
}

async function openSelect(testId: string): Promise<HTMLElement> {
  const select = screen.getByTestId(testId);
  // 先关闭任何残留打开的 dropdown，避免 mouseDown 变成 toggle 关闭。
  fireEvent.mouseDown(document.body);
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);

  let dropdown: HTMLElement | undefined;
  await waitFor(() => {
    dropdown = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
    ).find((candidate) => !candidate.classList.contains("ant-select-dropdown-hidden"));
    expect(dropdown).not.toBeUndefined();
  });
  return dropdown as HTMLElement;
}

function optionLabels(dropdown: HTMLElement): string[] {
  return Array.from(dropdown.querySelectorAll(".ant-select-item-option-content")).map(
    (option) => option.textContent ?? "",
  );
}

function clickOption(dropdown: HTMLElement, label: string): void {
  const content = Array.from(
    dropdown.querySelectorAll<HTMLElement>(".ant-select-item-option-content"),
  ).find((option) => option.textContent === label);
  if (content === undefined) {
    throw new Error(`Select option not found: ${label}`);
  }
  fireEvent.click(content.closest(".ant-select-item-option") ?? content);
}

/** 填表新建一个凭据；返回创建的元数据。 */
async function createCredential(
  name: string,
  secret: string,
  type: "token" | "password" = "token",
): Promise<void> {
  fireEvent.click(screen.getByTestId("new-credential"));
  fireEvent.change(screen.getByTestId("credential-name"), { target: { value: name } });
  const typeDropdown = await openSelect("credential-type");
  clickOption(typeDropdown, type === "token" ? "令牌（token）" : "密码（username + password）");
  if (type === "token") {
    fireEvent.change(screen.getByTestId("credential-field-token"), {
      target: { value: secret },
    });
  } else {
    fireEvent.change(screen.getByTestId("credential-field-username"), {
      target: { value: "service-user" },
    });
    fireEvent.change(screen.getByTestId("credential-field-password"), {
      target: { value: secret },
    });
  }
  fireEvent.click(screen.getByTestId("submit-credential"));
  await screen.findByText("凭据已创建");
}

afterEach(() => {
  vi.restoreAllMocks();
});

it("跨 Tab 同步：新建/更新凭据后 AI 模型凭据选择器无需 F5 即可看到（Secret 真值不可见）", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  const stored: Credential[] = [];
  const listCredentials = vi
    .spyOn(api, "listCredentials")
    .mockImplementation(async () => [...stored]);
  const createCredentialApi = vi
    .spyOn(api, "createCredential")
    .mockImplementation(async (payload) => {
      const credential = credentialMetadata(stored.length + 1, payload.name, payload.type);
      stored.push(credential);
      return credential;
    });
  const updateCredentialApi = vi
    .spyOn(api, "updateCredential")
    .mockImplementation(async (credentialId, payload) => {
      const credential = stored.find((candidate) => candidate.id === credentialId);
      if (credential === undefined) {
        throw new Error(`credential ${credentialId} not found`);
      }
      const updated = { ...credential, name: payload.name ?? credential.name };
      stored.splice(stored.indexOf(credential), 1, updated);
      return updated;
    });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);

  // 先在凭据管理 Tab 新建第一个凭据。
  await createCredential("ai-token-one", "secret-one-value");
  expect(createCredentialApi).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("secret-one-value")).toBeNull();

  // 切到 AI 模型 Tab：挂载时拉取一次凭据，应能看到第一个凭据。
  fireEvent.click(screen.getByRole("tab", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");
  let aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one"]);

  // AI 面板保持挂载（不刷新页面），切回凭据管理再新建第二个凭据。
  fireEvent.click(screen.getByRole("tab", { name: "凭据管理" }));
  await createCredential("ai-token-two", "secret-two-value");
  expect(createCredentialApi).toHaveBeenCalledTimes(2);

  // 订阅触发的刷新必须在"仍挂载"的 AI 面板上生效：无需重新打开/刷新即可看到。
  await waitFor(() => expect(listCredentials.mock.calls.length).toBeGreaterThanOrEqual(5));
  fireEvent.click(screen.getByRole("tab", { name: "AI 模型" }));
  aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one", "ai-token-two"]);

  // 更新既有凭据同样同步（改名后选择器跟随，不要求 F5）。
  fireEvent.click(screen.getByRole("tab", { name: "凭据管理" }));
  fireEvent.click(screen.getAllByTestId("update-credential")[0]);
  fireEvent.change(screen.getByTestId("credential-name"), {
    target: { value: "ai-token-one-renamed" },
  });
  // 更新语义为重新加密并替换已保存的值，必填字段需重新填写。
  fireEvent.change(screen.getByTestId("credential-field-token"), {
    target: { value: "secret-one-value" },
  });
  fireEvent.click(screen.getByTestId("submit-credential"));
  await screen.findByText("凭据已更新");
  expect(updateCredentialApi).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("tab", { name: "AI 模型" }));
  aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one-renamed", "ai-token-two"]);

  // Secret 真值始终不出现在浏览器 DOM 中。
  expect(document.body.textContent).not.toContain("secret-one-value");
  expect(document.body.textContent).not.toContain("secret-two-value");
});

it("跨 Tab 同步：依赖源新建表单中的凭据选择器在凭据增删后同步", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "createPackageSource").mockResolvedValue({
    id: 1,
    name: "sources-sync",
    kind: "pypi",
    index_url: "https://index.example.com/simple",
    is_default: false,
    credential_id: null,
    credential_name: null,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:00Z",
  });
  const stored: Credential[] = [];
  const listCredentials = vi
    .spyOn(api, "listCredentials")
    .mockImplementation(async () => [...stored]);
  vi.spyOn(api, "createCredential").mockImplementation(async (payload) => {
    const credential = credentialMetadata(stored.length + 1, payload.name, payload.type);
    stored.push(credential);
    return credential;
  });
  vi.spyOn(api, "deleteCredential").mockImplementation(async (credentialId) => {
    const index = stored.findIndex((candidate) => candidate.id === credentialId);
    if (index >= 0) {
      stored.splice(index, 1);
    }
  });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);

  await createCredential("source-pass-one", "source-secret-one", "password");
  // 依赖源面板首次挂载即可看到第一个凭据。
  fireEvent.click(screen.getByRole("tab", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  fireEvent.click(screen.getByTestId("new-package-source"));
  let sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-one"]);

  // 面板保持挂载时新建第二个凭据，选择器应自动同步。
  fireEvent.click(screen.getByRole("tab", { name: "凭据管理" }));
  await createCredential("source-pass-two", "source-secret-two", "password");
  await waitFor(() => expect(listCredentials.mock.calls.length).toBeGreaterThanOrEqual(4));

  fireEvent.click(screen.getByRole("tab", { name: "依赖源" }));
  sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-one", "source-pass-two"]);

  // 删除凭据后选择器同样失效（不要求 F5）。
  fireEvent.click(screen.getByRole("tab", { name: "凭据管理" }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getAllByTestId("delete-credential")[0]);
  await screen.findByText("凭据已删除");
  confirm.mockRestore();

  await waitFor(() =>
    expect(screen.getAllByTestId("credential-row").map((row) => row.textContent)).toEqual([
      "source-pass-two",
    ]),
  );
  fireEvent.click(screen.getByRole("tab", { name: "依赖源" }));
  sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-two"]);

  // Secret 真值始终不出现在浏览器 DOM 中。
  expect(document.body.textContent).not.toContain("source-secret-one");
  expect(document.body.textContent).not.toContain("source-secret-two");
});

it("订阅在组件卸载后自动取消，不会影响其他面板", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  const stored: Credential[] = [credentialMetadata(1, "pre-existing", "token")];
  const listCredentials = vi
    .spyOn(api, "listCredentials")
    .mockImplementation(async () => [...stored]);

  const { unmount } = render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  await screen.findByText("pre-existing");

  unmount();
  stored.push(credentialMetadata(2, "after-unmount", "token"));
  const callsBefore = listCredentials.mock.calls.length;
  await act(async () => {
    const { notifyCredentialCatalogChanged } = await import("../credential-catalog");
    notifyCredentialCatalogChanged();
  });
  // 卸载后的订阅不会触发请求；重新挂载会按初始加载拉取最新元数据。
  expect(listCredentials.mock.calls.length).toBe(callsBefore);
});
