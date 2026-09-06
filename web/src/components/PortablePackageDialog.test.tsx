import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { i18n } from "../i18n";
import type {
  Adapter,
  PortablePackage,
  TemplateScenarioDetail,
} from "../types";
import PortablePackageDialog from "./PortablePackageDialog";

function packageValue(
  type: "adapter" | "template" = "adapter",
): PortablePackage {
  return {
    format_version: 1,
    object_type: type,
    name: "Portable fixture",
    description: "Reusable logic",
    instructions: "Configure target",
    category: "other",
    tags: [],
    adapter_type: "webhook",
    variants: [
      {
        language: "javascript",
        code: "// fixture",
        requirements: "",
        runtime_config: { customer_url: "secret.example", mapping: "id" },
        required_parameters: ["customer_url"],
        input_skeleton: {},
        output_example: {},
      },
    ],
    timeout_seconds: 300,
    schedule: null,
    webhook: { response_mode: "completed", response_timeout_seconds: 30 },
    input: {
      source_type: "none",
      included: false,
      json_value: null,
      files: [],
    },
    example_files: [],
    provenance: "",
    license: "",
  };
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  vi.spyOn(api, "listTemplateThemes").mockResolvedValue([
    {
      slug: "other",
      name: { "zh-CN": "其他", en: "Other" },
      description: { "zh-CN": "", en: "" },
      sort_order: 999,
      scenario_count: 0,
    },
  ]);
});
afterEach(() => vi.restoreAllMocks());

it("exports a reviewed saved snapshot with inputs off and removes pending values", async () => {
  const preview = vi
    .spyOn(api, "previewAdapterPackage")
    .mockResolvedValue({
      ...packageValue(),
      adapter_type: "task",
      webhook: null,
    });
  const exported = vi
    .spyOn(api, "exportPortablePackage")
    .mockResolvedValue(new Blob(["zip"]));
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:fixture"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
  render(
    <PortablePackageDialog
      mode="exportAdapter"
      adapterId={17}
      hasUnsavedChanges
      onClose={() => undefined}
    />,
  );
  expect(screen.getByText(/存在未保存修改/)).toBeTruthy();
  await screen.findByLabelText("名称");
  expect(screen.queryByRole("button", { name: "预览已保存内容" })).toBeNull();
  expect(
    (screen.getByLabelText("明确携带已保存的 JSON 输入") as HTMLInputElement)
      .checked,
  ).toBe(false);
  expect(
    (screen.getByLabelText("明确携带已选择的托管输入文件") as HTMLInputElement)
      .checked,
  ).toBe(false);
  await screen.findByLabelText("可复用运行参数（JSON 对象）");
  expect(preview).toHaveBeenCalledWith(17, {
    as_template: false,
    include_json: false,
    include_files: false,
  });
  fireEvent.click(
    screen.getByText("我已检查此预览中的内容，并确认目标环境仍需重新配置。"),
  );
  fireEvent.click(screen.getByRole("button", { name: "导出 ZIP" }));
  await waitFor(() => expect(exported).toHaveBeenCalledOnce());
  expect(exported.mock.calls[0][0].variants[0].runtime_config).toEqual({
    mapping: "id",
  });
  await screen.findByText("ZIP 已生成。");
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledOnce(), {
    timeout: 2000,
  });
  vi.unstubAllGlobals();
});

it("template save keeps only the existing language and requires explicit gallery sharing", async () => {
  vi.spyOn(api, "previewAdapterPackage").mockResolvedValue(
    packageValue("template"),
  );
  const save = vi.spyOn(api, "saveTemplatePackage").mockResolvedValue({
    slug: "user-created",
    title: { "zh-CN": "Portable fixture", en: "Portable fixture" },
  } as TemplateScenarioDetail);
  render(
    <PortablePackageDialog
      mode="saveTemplate"
      adapterId={9}
      onClose={() => undefined}
    />,
  );
  expect(screen.queryByLabelText("明确携带已保存的 JSON 输入")).toBeNull();
  await screen.findByRole("tab", { name: "JavaScript" });
  expect(screen.queryByRole("tab", { name: "Python" })).toBeNull();
  expect(
    (screen.getByRole("button", { name: "确认保存" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(
    screen.getByText(
      "我已检查代码、参数和示例，并确认将这些内容共享到当前部署的模板广场。",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0].variants).toHaveLength(1);
  expect(save.mock.calls[0][1].adapterId).toBe(9);
});

it("uploads first, rejects wrong package type, and preserves rename input on conflict", async () => {
  const preview = vi
    .spyOn(api, "previewPortableFile")
    .mockResolvedValueOnce(packageValue("template"))
    .mockResolvedValueOnce(packageValue());
  const imported = vi
    .spyOn(api, "importAdapterPackage")
    .mockRejectedValue(new ApiError(409, "adapter_name_conflict", ""));
  render(
    <PortablePackageDialog mode="importAdapter" onClose={() => undefined} />,
  );
  const input = screen.getByLabelText("选择 DLR ZIP 包（最大 16 MiB）");
  fireEvent.change(input, {
    target: { files: [new File(["zip"], "wrong.zip")] },
  });
  await screen.findByText(/包无效、格式不受支持/);
  expect(imported).not.toHaveBeenCalled();
  fireEvent.change(input, {
    target: { files: [new File(["zip"], "adapter.zip")] },
  });
  const name = await screen.findByLabelText("名称");
  await waitFor(() => expect(document.activeElement).toBe(name));
  fireEvent.change(name, { target: { value: "Chosen name" } });
  fireEvent.click(
    screen.getByText("我已检查此预览中的内容，并确认目标环境仍需重新配置。"),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
  await screen.findByText("名称已存在，请改名后重试。");
  expect((name as HTMLInputElement).value).toBe("Chosen name");
  expect(preview).toHaveBeenCalledTimes(2);
  expect(imported).toHaveBeenCalledWith(
    expect.objectContaining({ name: "Chosen name" }),
    null,
  );
});

it("Task import does not silently choose a Worker", async () => {
  const value = packageValue();
  value.adapter_type = "task";
  value.webhook = null;
  vi.spyOn(api, "previewPortableFile").mockResolvedValue(value);
  render(
    <PortablePackageDialog
      mode="importAdapter"
      workers={[]}
      onClose={() => undefined}
    />,
  );
  fireEvent.change(screen.getByLabelText("选择 DLR ZIP 包（最大 16 MiB）"), {
    target: { files: [new File(["zip"], "task.zip")] },
  });
  await screen.findByText("Task 首次保存需要指定支持该语言的目标 Worker。");
  fireEvent.click(
    screen.getByText("我已检查此预览中的内容，并确认目标环境仍需重新配置。"),
  );
  expect(
    (screen.getByRole("button", { name: "确认保存" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

it("provides English preview and confirmation text", async () => {
  await i18n.changeLanguage("en");
  render(
    <PortablePackageDialog mode="importTemplate" onClose={() => undefined} />,
  );
  expect(screen.getByText("Import template")).toBeTruthy();
  expect(
    screen.getByLabelText("Choose a DLR ZIP package (up to 16 MiB)"),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "Confirm save" })).toBeTruthy();
});

it("opens the complete template editor immediately and saves the edited instructions", async () => {
  const value = packageValue("template");
  vi.spyOn(api, "getTemplatePackage").mockResolvedValue(value);
  const save = vi.spyOn(api, "saveTemplatePackage").mockResolvedValue({
    slug: "user-edit",
    title: { "zh-CN": value.name, en: value.name },
  } as TemplateScenarioDetail);
  render(
    <PortablePackageDialog
      mode="editTemplate"
      slug="user-edit"
      expectedVersion="7"
      onClose={() => undefined}
    />,
  );
  const instructions =
    await screen.findByLabelText("使用说明与目标环境配置提醒");
  expect(screen.queryByRole("button", { name: "预览已保存内容" })).toBeNull();
  expect(screen.queryByText("运行与触发设置（导入后默认停止）")).toBeNull();
  expect(screen.queryByLabelText("许可证说明")).toBeNull();
  fireEvent.change(instructions, { target: { value: "Updated guidance" } });
  fireEvent.click(
    screen.getByText(
      "我已检查代码、参数和示例，并确认将这些内容共享到当前部署的模板广场。",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ instructions: "Updated guidance" }),
      expect.objectContaining({ slug: "user-edit", expectedVersion: "7" }),
    ),
  );
  await screen.findByText("模板已保存到模板广场。");
});

it("changing exported input options preserves all other edits", async () => {
  const value = packageValue();
  value.adapter_type = "task";
  value.webhook = null;
  const preview = vi
    .spyOn(api, "previewAdapterPackage")
    .mockResolvedValueOnce(value)
    .mockResolvedValueOnce({
      ...value,
      input: {
        source_type: "json",
        included: true,
        json_value: { sample: 1 },
        files: [],
      },
    });
  render(
    <PortablePackageDialog
      mode="exportAdapter"
      adapterId={17}
      onClose={() => undefined}
    />,
  );
  fireEvent.change(await screen.findByLabelText("名称"), {
    target: { value: "Reviewed title" },
  });
  fireEvent.change(screen.getByLabelText("可复用运行参数（JSON 对象）"), {
    target: { value: '{"mapping":"reviewed"}' },
  });
  fireEvent.click(screen.getByLabelText("明确携带已保存的 JSON 输入"));
  await waitFor(() =>
    expect(preview).toHaveBeenLastCalledWith(17, {
      as_template: false,
      include_json: true,
      include_files: false,
    }),
  );
  await screen.findByText(/已明确选择携带/);
  expect((screen.getByLabelText("名称") as HTMLInputElement).value).toBe(
    "Reviewed title",
  );
  expect(
    (
      screen.getByLabelText(
        "可复用运行参数（JSON 对象）",
      ) as HTMLTextAreaElement
    ).value,
  ).toBe('{"mapping":"reviewed"}');
});

it("shows the actual automatically renamed result after import", async () => {
  vi.spyOn(api, "previewPortableFile").mockResolvedValue(packageValue());
  vi.spyOn(api, "importAdapterPackage").mockResolvedValue({
    name: "Portable fixture(1)",
  } as Adapter);
  render(
    <PortablePackageDialog mode="importAdapter" onClose={() => undefined} />,
  );
  fireEvent.change(screen.getByLabelText("选择 DLR ZIP 包（最大 16 MiB）"), {
    target: { files: [new File(["zip"], "adapter.zip")] },
  });
  await screen.findByLabelText("名称");
  fireEvent.click(
    screen.getByText("我已检查此预览中的内容，并确认目标环境仍需重新配置。"),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
  await screen.findByText("Portable fixture(1)");
});
