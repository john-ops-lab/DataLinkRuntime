/**
 * M5.5.2（UX-003）：凭据增删改后，依赖凭据元数据的选择器无需 F5 即可同步；
 * 同步链路只传递元数据，Secret 真值永远不进入浏览器共享状态。
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api";
import { applySystemLocale } from "../i18n";
import type {
  Credential,
  CredentialType,
  KnowledgeSource,
  PackageSource,
  PackageSourceDefaults,
} from "../types";
import SystemSettingsDrawer from "./SystemSettingsDrawer";

const CANONICAL_DEFAULTS: PackageSourceDefaults = {
  pypi: { kind: "pypi", name: "阿里云 PyPI 镜像", index_url: "https://mirrors.aliyun.com/pypi/simple/" },
  npm: { kind: "npm", name: "npmmirror npm 镜像", index_url: "https://registry.npmmirror.com/" },
  maven: { kind: "maven", name: "阿里云 Maven 公共仓库", index_url: "https://maven.aliyun.com/repository/public" },
};

function credentialMetadata(id: number, name: string, type: CredentialType): Credential {
  return {
    id,
    name,
    type,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:00Z",
  };
}

function packageSource(overrides: Partial<PackageSource> = {}): PackageSource {
  return {
    id: 1,
    name: "fixture-pypi-source",
    kind: "pypi",
    index_url: "https://packages.example.invalid/simple/",
    is_default: false,
    credential_id: null,
    credential_name: null,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    ...overrides,
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
  // M5.5.7：创建前有一次性的“保存后无法回读”明文提醒，测试中确认通过。
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  try {
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
    await screen.findByText("凭据已创建", undefined, { timeout: 5000 });
  } finally {
    confirm.mockRestore();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

it("跨 Tab 同步：新建/更新凭据后 AI 模型凭据选择器无需 F5 即可看到（Secret 真值不可见）", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
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
  fireEvent.click(screen.getByRole("menuitem", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");
  let aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one"]);

  // 当前内容切换回凭据管理，再新建第二个凭据；返回 AI 时重新加载元数据。
  fireEvent.click(screen.getByRole("menuitem", { name: "凭据" }));
  await screen.findByTestId("credentials-panel");
  await createCredential("ai-token-two", "secret-two-value");
  expect(createCredentialApi).toHaveBeenCalledTimes(2);

  // 订阅触发的刷新必须在"仍挂载"的 AI 面板上生效：无需重新打开/刷新即可看到。
  await waitFor(() => expect(listCredentials.mock.calls.length).toBeGreaterThanOrEqual(5));
  fireEvent.click(screen.getByRole("menuitem", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");
  aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one", "ai-token-two"]);

  // 更新既有凭据同样同步（改名后选择器跟随，不要求 F5）。
  fireEvent.click(screen.getByRole("menuitem", { name: "凭据" }));
  await screen.findByTestId("credentials-panel");
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

  fireEvent.click(screen.getByRole("menuitem", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");
  aiDropdown = await openSelect("ai-credential");
  expect(optionLabels(aiDropdown)).toEqual(["ai-token-one-renamed", "ai-token-two"]);

  // Secret 真值始终不出现在浏览器 DOM 中。
  expect(document.body.textContent).not.toContain("secret-one-value");
  expect(document.body.textContent).not.toContain("secret-two-value");
}, 20000);

it("跨 Tab 同步：依赖源新建表单中的凭据选择器在凭据增删后同步", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
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
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  fireEvent.click(screen.getByTestId("new-package-source"));
  await screen.findByTestId("package-source-form");
  let sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-one"]);

  // 当前内容切换回凭据管理，再新建第二个凭据；返回依赖源时重新加载元数据。
  fireEvent.click(screen.getByRole("menuitem", { name: "凭据" }));
  await screen.findByTestId("credentials-panel");
  await createCredential("source-pass-two", "source-secret-two", "password");
  await waitFor(() => expect(listCredentials.mock.calls.length).toBeGreaterThanOrEqual(4));

  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  fireEvent.click(screen.getByTestId("new-package-source"));
  await screen.findByTestId("package-source-form");
  sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-one", "source-pass-two"]);

  // 删除凭据后选择器同样失效（不要求 F5）。
  fireEvent.click(screen.getByRole("menuitem", { name: "凭据" }));
  await screen.findByTestId("credentials-panel");
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getAllByTestId("delete-credential")[0]);
  await screen.findByText("凭据已删除");
  confirm.mockRestore();

  await waitFor(() =>
    expect(screen.getAllByTestId("credential-row").map((row) => row.textContent)).toEqual([
      "source-pass-two",
    ]),
  );
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  fireEvent.click(screen.getByTestId("new-package-source"));
  await screen.findByTestId("package-source-form");
  sourceDropdown = await openSelect("package-source-credential");
  expect(optionLabels(sourceDropdown)).toEqual(["source-pass-two"]);

  // Secret 真值始终不出现在浏览器 DOM 中。
  expect(document.body.textContent).not.toContain("source-secret-one");
  expect(document.body.textContent).not.toContain("source-secret-two");
}, 20000);

it("切换依赖源类型时清除不兼容的凭据选择", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([
    credentialMetadata(7, "npm-token", "token"),
  ]);
  const createdSource: PackageSource = {
    id: 1,
    name: "kind-switch-source",
    kind: "pypi",
    index_url: "https://index.example.com/simple",
    is_default: false,
    credential_id: null,
    credential_name: null,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:00Z",
  };
  const createPackageSource = vi
    .spyOn(api, "createPackageSource")
    .mockResolvedValue(createdSource);

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  fireEvent.click(screen.getByTestId("new-package-source"));

  let dropdown = await openSelect("package-source-kind");
  clickOption(dropdown, "npm");
  dropdown = await openSelect("package-source-credential");
  clickOption(dropdown, "npm-token");
  dropdown = await openSelect("package-source-kind");
  clickOption(dropdown, "PyPI");

  fireEvent.change(screen.getByTestId("package-source-name"), {
    target: { value: "kind-switch-source" },
  });
  fireEvent.change(screen.getByTestId("package-source-url"), {
    target: { value: "https://index.example.com/simple" },
  });
  fireEvent.click(screen.getByTestId("submit-package-source"));
  await screen.findByText("依赖源已创建");

  expect(createPackageSource).toHaveBeenCalledWith({
    name: "kind-switch-source",
    kind: "pypi",
    index_url: "https://index.example.com/simple",
    is_default: false,
    credential_id: null,
  });
});

it("订阅在组件卸载后自动取消，不会影响其他面板", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
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

it("凭据管理页展示四类凭据说明（访问密钥为 access_key_id + access_key_secret）", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByTestId("credential-help"));
  await screen.findByTestId("credential-type-guide");
  expect(screen.getByTestId("credential-type-guide-password").textContent).toContain(
    "username + password",
  );
  expect(screen.getByTestId("credential-type-guide-password").textContent).toContain("数据库");
  expect(screen.getByTestId("credential-type-guide-token").textContent).toContain("token");
  expect(screen.getByTestId("credential-type-guide-token").textContent).toContain("Webhook Token");
  expect(screen.getByTestId("credential-type-guide-access_key").textContent).toContain(
    "access_key_id + access_key_secret",
  );
  expect(screen.getByTestId("credential-type-guide-access_key").textContent).toContain("AWS");
  expect(screen.getByTestId("credential-type-guide-secret").textContent).toContain("通用密钥");
  expect(screen.getByTestId("credential-type-guide-secret").textContent).toContain(
    "api_key、client_secret、signing_secret、private_key",
  );
  expect(
    screen.getByText("按目标系统需要的字段选择类型；不匹配时使用「通用密钥」。"),
  ).toBeTruthy();
});

it("凭据试点将筛选与主操作收敛到工具栏，行操作保留可访问名称", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([
    credentialMetadata(1, "runtime-token", "token"),
    credentialMetadata(2, "database-password", "password"),
  ]);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const onClose = vi.fn();

  render(<SystemSettingsDrawer open onClose={onClose} />);
  await screen.findByText("runtime-token");

  expect(screen.getByRole("toolbar", { name: "凭据工具栏" })).toBeTruthy();
  expect(screen.getByTestId("credentials-filters")).toBeTruthy();
  expect(screen.getByTestId("new-credential")).toBeTruthy();
  expect(screen.getByTestId("refresh-credentials")).toBeTruthy();
  expect(screen.getByTestId("credential-help").getAttribute("aria-label")).toBe("查看凭据类型");
  expect(screen.getAllByTestId("update-credential")[0]?.getAttribute("aria-label")).toBe(
    "编辑凭据 runtime-token",
  );
  expect(screen.getAllByTestId("delete-credential")[0]?.getAttribute("aria-label")).toBe(
    "删除凭据 runtime-token",
  );

  fireEvent.change(screen.getByRole("textbox", { name: "筛选凭据" }), {
    target: { value: "database" },
  });
  expect(screen.queryByText("runtime-token")).toBeNull();
  expect(screen.getByText("database-password")).toBeTruthy();

  fireEvent.click(screen.getByTestId("settings-back"));
  expect(confirm).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("保存失败时仍保留未保存更改确认", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "updateAiSetting").mockRejectedValue(new Error("fixture save failed"));
  const onClose = vi.fn();
  render(<SystemSettingsDrawer open onClose={onClose} />);

  fireEvent.click(screen.getByRole("menuitem", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.change(screen.getByTestId("ai-base-url"), {
    target: { value: "https://api.example.com" },
  });
  fireEvent.change(screen.getByTestId("ai-model-input"), {
    target: { value: "fixture-model" },
  });
  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await screen.findByTestId("ai-settings-error");

  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByTestId("settings-back"));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
});

it("新建凭据提交前有一次性明文提醒，取消则不创建", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  const createCredentialApi = vi
    .spyOn(api, "createCredential")
    .mockResolvedValue(credentialMetadata(1, "never-saved", "token"));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByTestId("new-credential"));
  fireEvent.change(screen.getByTestId("credential-name"), { target: { value: "never-saved" } });
  fireEvent.change(screen.getByTestId("credential-field-username"), {
    target: { value: "service-user" },
  });
  fireEvent.change(screen.getByTestId("credential-field-password"), {
    target: { value: "plain-secret-53" },
  });
  fireEvent.click(screen.getByTestId("submit-credential"));

  // 提醒必须出现在创建之前，且包含不可回读的明确文案。
  expect(confirm).toHaveBeenCalledWith(
    expect.stringContaining(
      "保存后无法再次查看密码、Token 或密钥，请先复制。",
    ),
  );
  expect(createCredentialApi).not.toHaveBeenCalled();
  confirm.mockRestore();

  // 确认后才会真正创建。
  const confirmAgain = vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByTestId("submit-credential"));
  await screen.findByText("凭据已创建");
  expect(createCredentialApi).toHaveBeenCalledTimes(1);
  confirmAgain.mockRestore();
});

it("M5.5.8：无默认源时展示明确回退提示，恢复默认调用对应接口", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  const restorePypi = vi
    .spyOn(api, "restorePackageSourceDefault")
    .mockResolvedValue({
      id: 7,
      name: "阿里云 PyPI 镜像",
      kind: "pypi",
      index_url: "https://mirrors.aliyun.com/pypi/simple/",
      is_default: true,
      credential_id: null,
      credential_name: null,
      created_at: "2026-08-17T00:00:00Z",
      updated_at: "2026-08-17T00:00:00Z",
    });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");

  // 恢复默认动作收敛在一个菜单中，同时保留三种语言的独立入口。
  fireEvent.click(screen.getByTestId("restore-default-menu"));
  for (const kind of ["pypi", "npm", "maven"] as const) {
    expect(screen.getByTestId(`restore-default-${kind}`)).toBeTruthy();
    expect(screen.getByTestId(`no-default-source-${kind}`).textContent).toContain(
      "不会使用未配置地址",
    );
  }
  expect(screen.getByTestId("no-default-source-pypi").textContent).toContain("本地缓存");

  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByTestId("restore-default-pypi"));
  await screen.findByText("已恢复 PyPI 默认源");
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(restorePypi).toHaveBeenCalledWith("pypi");
  confirm.mockRestore();
});

it("M5.5.8：已有默认源时不显示该类型的回退提示", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([
    {
      id: 1,
      name: "阿里云 PyPI 镜像",
      kind: "pypi",
      index_url: "https://mirrors.aliyun.com/pypi/simple/",
      is_default: true,
      credential_id: null,
      credential_name: null,
      created_at: "2026-08-17T00:00:00Z",
      updated_at: "2026-08-17T00:00:00Z",
    },
  ]);

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");

  expect(screen.queryByTestId("no-default-source-pypi")).toBeNull();
  expect(screen.getByTestId("no-default-source-npm")).toBeTruthy();
});

it("依赖源默认星标只允许非默认项操作，成功后按类型保持唯一默认", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  const initialSources = [
    packageSource({ id: 1, name: "PyPI mirror", is_default: false }),
    packageSource({ id: 2, name: "Current PyPI default", is_default: true }),
  ];
  const updatedSources = [
    packageSource({ id: 1, name: "PyPI mirror", is_default: true }),
    packageSource({ id: 2, name: "Current PyPI default", is_default: false }),
  ];
  const listSources = vi
    .spyOn(api, "listPackageSources")
    .mockResolvedValueOnce(initialSources)
    .mockResolvedValueOnce(updatedSources);
  let resolveUpdate: ((source: PackageSource) => void) | undefined;
  const update = vi.spyOn(api, "updatePackageSource").mockReturnValue(
    new Promise<PackageSource>((resolve) => {
      resolveUpdate = resolve;
    }),
  );

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");

  expect(screen.getAllByTestId("default-source-indicator")).toHaveLength(1);
  expect(screen.getByTestId("default-source-indicator").getAttribute("aria-label")).toBe(
    "当前默认依赖源：Current PyPI default",
  );
  const setDefault = screen.getByTestId("set-default-source") as HTMLButtonElement;
  expect(setDefault.disabled).toBe(false);
  expect(setDefault.getAttribute("aria-label")).toBe("设为默认依赖源：PyPI mirror");

  fireEvent.click(setDefault);
  await waitFor(() => expect(setDefault.disabled).toBe(true));
  expect(setDefault.getAttribute("aria-label")).toBe("正在设为默认：PyPI mirror");
  expect(update).toHaveBeenCalledWith(1, { is_default: true });

  await act(async () => {
    resolveUpdate?.(updatedSources[0]);
  });
  await waitFor(() => expect(listSources).toHaveBeenCalledTimes(2));
  expect(screen.getAllByTestId("default-source-indicator")).toHaveLength(1);
  expect(screen.getByTestId("default-source-indicator").getAttribute("aria-label")).toBe(
    "当前默认依赖源：PyPI mirror",
  );
  expect(screen.getAllByTestId("set-default-source")).toHaveLength(1);
});

it("依赖源设置默认失败后恢复可操作状态，不伪装为默认", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([
    packageSource({ id: 3, name: "Failed PyPI source", is_default: false }),
  ]);
  vi.spyOn(api, "updatePackageSource").mockRejectedValue(new Error("set default failed"));

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");
  const setDefault = screen.getByTestId("set-default-source") as HTMLButtonElement;

  fireEvent.click(setDefault);
  await screen.findByRole("alert");
  await waitFor(() => expect(setDefault.disabled).toBe(false));
  expect(screen.queryByTestId("default-source-indicator")).toBeNull();
  expect(setDefault.getAttribute("aria-label")).toBe("设为默认依赖源：Failed PyPI source");
});

it("依赖源 HTTP 应答保持可达语义，测试请求失败会清除旧状态", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([
    {
      id: 1,
      name: "private-nexus",
      kind: "pypi",
      index_url: "https://nexus.example.com/simple/",
      is_default: true,
      credential_id: 7,
      credential_name: "nexus-credential",
      created_at: "2026-08-17T00:00:00Z",
      updated_at: "2026-08-17T00:00:00Z",
    },
  ]);
  const testSource = vi
    .spyOn(api, "testPackageSource")
    .mockResolvedValueOnce({ ok: true, status_code: 401, error: null })
    .mockRejectedValueOnce(new Error("probe failed"));

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "依赖源" }));
  await screen.findByTestId("package-sources-panel");

  const result = screen.getByTestId("package-source-test-result");
  const testButton = screen.getByTestId("test-package-source") as HTMLButtonElement;
  fireEvent.click(testButton);
  await waitFor(() => expect(result.textContent).toContain("可达"));
  expect(result.getAttribute("role")).toBe("status");

  await waitFor(() => expect(testButton.disabled).toBe(false));
  fireEvent.click(testButton);
  await waitFor(() => expect(result.textContent).toContain("未测试"));
  expect(testSource).toHaveBeenCalledTimes(2);
});

it("M5.8-006：知识库配置只保存 access_key 引用，官方地址不可编辑", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([
    credentialMetadata(7, "ima-access", "access_key"),
    credentialMetadata(8, "not-an-access-key", "token"),
  ]);
  const source: KnowledgeSource = {
    source_id: "ima",
    kind: "ima",
    name: "Tencent ima",
    endpoint: "https://ima.qq.com",
    enabled: true,
    status: "unconfigured",
    credential_id: null,
    credential_name: null,
    credential_type: null,
    config_source: "environment",
    created_at: null,
    updated_at: null,
  };
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue(source);
  const update = vi.spyOn(api, "updateKnowledgeSource").mockResolvedValue({
    ...source,
    status: "configured",
    credential_id: 7,
    credential_name: "ima-access",
    credential_type: "access_key",
    config_source: "database",
  });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "知识库" }));
  await screen.findByTestId("knowledge-source-summary");

  expect(screen.getByTestId("knowledge-source-actions").getAttribute("role")).toBe("toolbar");
  expect(
    Array.from(screen.getByTestId("knowledge-source-actions").querySelectorAll("button"))
      .map((button) => button.dataset.testid),
  ).toEqual(["test-knowledge-source", "save-knowledge-source"]);
  expect(screen.getByTestId("knowledge-source-endpoint").textContent).toBe(
    "https://ima.qq.com",
  );
  expect(screen.queryByRole("textbox", { name: "服务地址" })).toBeNull();

  const credentialDropdown = await openSelect("knowledge-source-credential");
  expect(optionLabels(credentialDropdown)).toEqual(["ima-access"]);
  clickOption(credentialDropdown, "ima-access");
  fireEvent.click(screen.getByTestId("save-knowledge-source"));

  await screen.findByText("知识库配置已保存");
  expect(update).toHaveBeenCalledWith("ima", { enabled: true, credential_id: 7 });
  expect(update.mock.calls[0]?.[1]).not.toHaveProperty("access_key_secret");
  expect(document.body.textContent).not.toContain("ima-api-key-test-sentinel");
});

it("M5.8-006：测试连接后展示可访问知识库名称与状态，错误只显示稳定代码", async () => {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
  vi.spyOn(api, "listCredentials").mockResolvedValue([
    credentialMetadata(7, "ima-access", "access_key"),
  ]);
  const source: KnowledgeSource = {
    source_id: "ima",
    kind: "ima",
    name: "Tencent ima",
    endpoint: "https://ima.qq.com",
    enabled: true,
    status: "configured",
    credential_id: 7,
    credential_name: "ima-access",
    credential_type: "access_key",
    config_source: "database",
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  };
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue(source);
  const testConnection = vi
    .spyOn(api, "testKnowledgeSource")
    .mockResolvedValueOnce({
      ok: true,
      status: "connected",
      error_code: null,
      message: "validated",
      knowledge_bases: [{ id: "kb-1", name: "产品知识库", status: "accessible" }],
    })
    .mockResolvedValueOnce({
      ok: false,
      status: "error",
      error_code: "ks_auth_failed",
      message: "upstream-secret-must-not-render",
      knowledge_bases: [],
    });

  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "知识库" }));
  await screen.findByTestId("knowledge-source-summary");

  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  await screen.findByText("产品知识库");
  expect(screen.getByTestId("knowledge-source-status").textContent).toContain("连接正常");
  expect(screen.getByText("可访问")).toBeTruthy();
  expect(document.body.textContent).not.toContain("upstream-secret-must-not-render");

  await waitFor(() => expect(screen.getByTestId("test-knowledge-source")).not.toHaveProperty("disabled", true));
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  await screen.findByTestId("knowledge-source-test-error");
  expect(screen.getByTestId("knowledge-source-test-error").textContent).toContain(
    "知识库服务拒绝了访问凭据",
  );
  expect(screen.getByTestId("knowledge-source-test-error").textContent).toContain(
    "ks_auth_failed",
  );
  expect(document.body.textContent).not.toContain("upstream-secret-must-not-render");
  expect(testConnection).toHaveBeenCalledTimes(2);
});

it("M5.8-006：知识库设置与错误状态可切换到 English", async () => {
  await applySystemLocale("en");
  try {
    vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
    vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
    vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue(CANONICAL_DEFAULTS);
    vi.spyOn(api, "listCredentials").mockResolvedValue([]);
    vi.spyOn(api, "getKnowledgeSource").mockResolvedValue({
      source_id: "ima",
      kind: "ima",
      name: "Tencent ima",
      endpoint: "https://ima.qq.com",
      enabled: true,
      status: "configured",
      credential_id: null,
      credential_name: null,
      credential_type: null,
      config_source: "environment",
      created_at: null,
      updated_at: null,
    });
    vi.spyOn(api, "testKnowledgeSource").mockResolvedValue({
      ok: false,
      status: "error",
      error_code: "ks_auth_failed",
      message: "server-secret-must-not-render",
      knowledge_bases: [],
    });

    render(<SystemSettingsDrawer open onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("menuitem", { name: "Knowledge base" }));
    await screen.findByTestId("knowledge-source-summary");
    fireEvent.click(screen.getByTestId("test-knowledge-source"));

    const error = await screen.findByTestId("knowledge-source-test-error");
    expect(error.textContent).toContain("The knowledge source rejected the Credential");
    expect(error.textContent).toContain("ks_auth_failed");
    expect(document.body.textContent).not.toContain("server-secret-must-not-render");
  } finally {
    await applySystemLocale("zh-CN");
  }
});
