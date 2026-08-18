import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api } from "../api";
import type { AiModelSetting, AiModelSettingDraft } from "../types";
import AiModelSettingsPanel from "./AiModelSettingsPanel";

function modelSetting(overrides: Partial<AiModelSetting> = {}): AiModelSetting {
  return {
    id: 1,
    provider: "openai",
    base_url: "https://models.example.com/v1",
    model: "reasoning-model",
    credential_id: null,
    credential_name: null,
    reasoning_mode: "default",
    reasoning_effort: null,
    created_at: "2026-08-12T00:00:00Z",
    updated_at: "2026-08-12T00:00:00Z",
    ...overrides,
  };
}

function mockLoad(setting: AiModelSetting | null): void {
  vi.spyOn(api, "getAiSetting").mockResolvedValue(setting);
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
}

async function openSelect(testId: string): Promise<HTMLElement> {
  const select = screen.getByTestId(testId);
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);

  let dropdown: HTMLElement | undefined;
  await waitFor(() => {
    dropdown = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
    ).find(
      (candidate) =>
        !candidate.classList.contains("ant-select-dropdown-hidden") &&
        candidate.querySelector(".ant-select-item-option-content") !== null,
    );
    expect(dropdown).not.toBeUndefined();
  });
  return dropdown as HTMLElement;
}

async function chooseOption(testId: string, label: string): Promise<void> {
  const dropdown = await openSelect(testId);
  clickOption(dropdown, label);
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

afterEach(() => {
  vi.restoreAllMocks();
});

it("locks the AI model form while a request is in flight so stale responses cannot overwrite edits", async () => {
  mockLoad(modelSetting({ reasoning_mode: "enabled", reasoning_effort: "high" }));
  let resolveSave: ((setting: AiModelSetting) => void) | undefined;
  const pendingSave = new Promise<AiModelSetting>((resolve) => {
    resolveSave = resolve;
  });
  vi.spyOn(api, "updateAiSetting").mockReturnValue(pendingSave);

  render(<AiModelSettingsPanel onError={vi.fn()} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await waitFor(() => {
    expect((screen.getByTestId("ai-model-input") as HTMLInputElement).disabled).toBe(true);
  });
  expect(screen.getByTestId("ai-provider").classList.contains("ant-select-disabled")).toBe(true);
  expect(screen.getByTestId("ai-reasoning-mode").classList.contains("ant-select-disabled")).toBe(
    true,
  );
  expect(screen.getByText(/高级：推理策略/).closest(".ant-collapse-item")?.className).toContain(
    "ant-collapse-item-disabled",
  );

  await act(async () => {
    resolveSave?.(modelSetting({ reasoning_mode: "enabled", reasoning_effort: "high" }));
    await pendingSave;
  });
  expect((screen.getByTestId("ai-model-input") as HTMLInputElement).disabled).toBe(false);
});

it("shows Provider-specific effort options and clears stale values on Provider or mode changes", async () => {
  mockLoad(
    modelSetting({
      provider: "openai",
      reasoning_mode: "enabled",
      // A value persisted by the previous shared enum is illegal for OpenAI.
      reasoning_effort: "max",
    }),
  );
  const onError = vi.fn();
  const updateSetting = vi
    .spyOn(api, "updateAiSetting")
    .mockImplementation(async (draft: AiModelSettingDraft) => modelSetting(draft));

  render(<AiModelSettingsPanel onError={onError} />);
  await screen.findByTestId("ai-model-settings-panel");

  expect(screen.getByTestId("ai-reasoning-effort").textContent).toContain(
    "请选择推理强度",
  );
  fireEvent.click(screen.getByTestId("ai-save-settings"));
  expect(updateSetting).not.toHaveBeenCalled();
  expect(onError).toHaveBeenLastCalledWith(
    "OpenAI 开启推理时必须显式选择受支持的推理强度",
  );
  const openAiOptions = await openSelect("ai-reasoning-effort");
  expect(optionLabels(openAiOptions)).toEqual(["low", "medium", "high", "xhigh"]);
  clickOption(openAiOptions, "xhigh");

  await chooseOption("ai-provider", "DeepSeek");
  expect(screen.getByTestId("ai-reasoning-effort").textContent).toContain(
    "不覆盖模型默认强度",
  );
  const deepSeekOptions = await openSelect("ai-reasoning-effort");
  expect(optionLabels(deepSeekOptions)).toEqual(["high", "max"]);
  clickOption(deepSeekOptions, "max");
  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await waitFor(() => expect(updateSetting).toHaveBeenCalledTimes(1));
  expect(updateSetting).toHaveBeenLastCalledWith({
    provider: "deepseek",
    base_url: "https://models.example.com/v1",
    model: "reasoning-model",
    credential_id: null,
    reasoning_mode: "enabled",
    reasoning_effort: "max",
  });

  await chooseOption("ai-provider", "OpenAI");
  expect(screen.getByTestId("ai-reasoning-effort").textContent).toContain(
    "请选择推理强度",
  );
  await chooseOption("ai-reasoning-effort", "high");
  await chooseOption("ai-reasoning-mode", "跟随模型默认");
  expect(screen.queryByTestId("ai-reasoning-effort")).toBeNull();

  await chooseOption("ai-reasoning-mode", "开启推理");
  expect(screen.getByTestId("ai-reasoning-effort").textContent).toContain(
    "请选择推理强度",
  );
  await chooseOption("ai-reasoning-effort", "high");

  for (const provider of ["Kimi", "MiniMax", "自定义 OpenAI 兼容服务"]) {
    await chooseOption("ai-provider", provider);
    expect(screen.queryByTestId("ai-reasoning-effort")).toBeNull();
  }
  await chooseOption("ai-provider", "OpenAI");
  expect(screen.getByTestId("ai-reasoning-effort").textContent).toContain(
    "请选择推理强度",
  );
});

it("requires an explicit OpenAI effort and sends only the selected or default payload", async () => {
  mockLoad(null);
  const onError = vi.fn();
  const updateSetting = vi
    .spyOn(api, "updateAiSetting")
    .mockImplementation(async (draft: AiModelSettingDraft) => modelSetting(draft));
  const testSetting = vi
    .spyOn(api, "testAiSetting")
    .mockResolvedValue({ ok: true, message: "Connection successful" });

  render(<AiModelSettingsPanel onError={onError} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.change(screen.getByTestId("ai-base-url"), {
    target: { value: "https://models.example.com/v1" },
  });
  fireEvent.change(screen.getByTestId("ai-model-input"), {
    target: { value: "reasoning-model" },
  });
  fireEvent.click(screen.getByText("高级：推理策略（跟随模型默认）"));
  await chooseOption("ai-reasoning-mode", "开启推理");

  fireEvent.click(screen.getByTestId("ai-save-settings"));
  fireEvent.click(screen.getByTestId("ai-test-connection"));
  expect(updateSetting).not.toHaveBeenCalled();
  expect(testSetting).not.toHaveBeenCalled();
  expect(onError).toHaveBeenLastCalledWith(
    "OpenAI 开启推理时必须显式选择受支持的推理强度",
  );

  await chooseOption("ai-reasoning-effort", "xhigh");
  fireEvent.click(screen.getByTestId("ai-test-connection"));
  await waitFor(() => expect(testSetting).toHaveBeenCalledTimes(1));
  // M5.6 Wave 4 E: the success sentence is DLR-owned and localized in the
  // bundle; the server compatibility message is never interpolated verbatim.
  expect((await screen.findByTestId("ai-settings-notice")).textContent).toBe(
    "连接测试通过：模型返回可解析的最小响应。",
  );
  expect(testSetting).toHaveBeenLastCalledWith({
    provider: "openai",
    base_url: "https://models.example.com/v1",
    model: "reasoning-model",
    credential_id: null,
    reasoning_mode: "enabled",
    reasoning_effort: "xhigh",
  });

  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await waitFor(() => expect(updateSetting).toHaveBeenCalledTimes(1));
  expect(updateSetting).toHaveBeenLastCalledWith({
    provider: "openai",
    base_url: "https://models.example.com/v1",
    model: "reasoning-model",
    credential_id: null,
    reasoning_mode: "enabled",
    reasoning_effort: "xhigh",
  });

  await chooseOption("ai-reasoning-mode", "跟随模型默认");
  expect(screen.queryByTestId("ai-reasoning-effort")).toBeNull();
  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await waitFor(() => expect(updateSetting).toHaveBeenCalledTimes(2));
  expect(updateSetting).toHaveBeenLastCalledWith({
    provider: "openai",
    base_url: "https://models.example.com/v1",
    model: "reasoning-model",
    credential_id: null,
    reasoning_mode: "default",
    reasoning_effort: null,
  });
  await screen.findByTestId("ai-settings-notice");
  fireEvent.change(screen.getByTestId("ai-model-input"), {
    target: { value: "edited-after-save" },
  });
  expect(screen.queryByTestId("ai-settings-notice")).toBeNull();
});

it("keeps the AI connection failure detail after the Chinese primary message", async () => {
  mockLoad(modelSetting());
  const onError = vi.fn();
  vi.spyOn(api, "testAiSetting").mockResolvedValue({
    ok: false,
    message: "Authentication failed for the selected model",
  });

  render(<AiModelSettingsPanel onError={onError} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.click(screen.getByTestId("ai-test-connection"));

  await waitFor(() => {
    expect(onError).toHaveBeenCalledWith(
      "连接测试失败：Authentication failed for the selected model",
    );
  });
  expect(screen.getByTestId("ai-settings-error").textContent).toBe(
    "连接测试失败：Authentication failed for the selected model",
  );
});

it("刷新失败后仍可手工输入模型 ID，错误主信息中文且可行动", async () => {
  mockLoad(modelSetting({ base_url: "https://models.example.com/v1" }));
  const refresh = vi.spyOn(api, "refreshAiModels").mockRejectedValue(
    new ApiError(
      502,
      "ai_provider_unreachable",
      "无法连接模型服务：请检查基础 URL、网络与 DNS 是否可达，确认无误后重试",
    ),
  );

  render(<AiModelSettingsPanel onError={vi.fn()} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.click(screen.getByTestId("ai-refresh-models"));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));

  const error = screen.getByTestId("ai-settings-error");
  expect(error.textContent).toContain("无法连接模型服务");
  expect(error.textContent).toContain("仍可手工输入模型 ID");
  expect(error.textContent).toContain("错误码：ai_provider_unreachable");

  // 刷新失败不锁定输入：Model ID 仍可手工填写。
  const input = screen.getByTestId("ai-model-input") as HTMLInputElement;
  expect(input.disabled).toBe(false);
  fireEvent.change(input, { target: { value: "manual-after-failure" } });
  expect(input.value).toBe("manual-after-failure");
});

it("刷新成功但列表为空时给出可行动提示", async () => {
  mockLoad(modelSetting({ base_url: "https://models.example.com/v1" }));
  const refresh = vi.spyOn(api, "refreshAiModels").mockResolvedValue({ models: [] });

  render(<AiModelSettingsPanel onError={vi.fn()} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.click(screen.getByTestId("ai-refresh-models"));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));

  expect(screen.getByTestId("ai-settings-notice").textContent).toBe(
    "刷新成功，但未发现可用模型；可手工填写模型 ID。",
  );
});

it("模型列表接口不兼容时展示专属中文提示，不重复手工填写建议", async () => {
  mockLoad(modelSetting({ base_url: "https://models.example.com/v1" }));
  const message = "无法自动获取模型列表：该服务未提供兼容的模型列表接口，可手工填写模型 ID";
  const refresh = vi
    .spyOn(api, "refreshAiModels")
    .mockRejectedValue(new ApiError(502, "ai_models_not_supported", message));

  render(<AiModelSettingsPanel onError={vi.fn()} />);
  await screen.findByTestId("ai-model-settings-panel");
  fireEvent.click(screen.getByTestId("ai-refresh-models"));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));

  // 服务端文案已含手工填写建议，前端不重复追加"仍可手工输入模型 ID"。
  expect(screen.getByTestId("ai-settings-error").textContent).toBe(
    `${message}（错误码：ai_models_not_supported）`,
  );
});

it("测试连接与模型刷新是独立操作，互不代替", async () => {
  mockLoad(modelSetting({ base_url: "https://models.example.com/v1" }));
  const testSetting = vi
    .spyOn(api, "testAiSetting")
    .mockResolvedValue({ ok: true, message: "模型服务返回了可解析的最小响应" });
  const refresh = vi.spyOn(api, "refreshAiModels").mockResolvedValue({ models: ["model-1"] });

  render(<AiModelSettingsPanel onError={vi.fn()} />);
  await screen.findByTestId("ai-model-settings-panel");

  // 测试连接只走 chat/completions 路径，不触发模型列表刷新。
  fireEvent.click(screen.getByTestId("ai-test-connection"));
  await waitFor(() => expect(testSetting).toHaveBeenCalledTimes(1));
  expect(refresh).not.toHaveBeenCalled();

  // 刷新模型只走 /v1/models 路径，不触发连接测试，也不改变已选模型。
  fireEvent.click(screen.getByTestId("ai-refresh-models"));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  expect(testSetting).toHaveBeenCalledTimes(1);
  expect((screen.getByTestId("ai-model-input") as HTMLInputElement).value).toBe(
    "reasoning-model",
  );
});
