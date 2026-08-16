import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api";
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
  expect((await screen.findByTestId("ai-settings-notice")).textContent).toBe(
    "连接测试通过：Connection successful",
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
