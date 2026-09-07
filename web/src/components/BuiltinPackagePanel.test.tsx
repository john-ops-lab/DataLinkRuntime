import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { applySystemLocale } from "../i18n";
import type { BuiltinPackage, BuiltinPackageLibrary } from "../types";
import BuiltinPackagePanel from "./BuiltinPackagePanel";

const file: BuiltinPackage = {
  id: 1,
  kind: "pypi",
  name: "offline-demo",
  version: "1.0",
  environment: "py3-none-any",
  filename: "offline_demo-1.0-py3-none-any.whl",
  repository_path: "offline_demo-1.0-py3-none-any.whl",
  size_bytes: 1024,
  sha256: "a".repeat(64),
  status: "uploaded",
  created_at: "2026-09-06T00:00:00Z",
};
let library: BuiltinPackageLibrary;
beforeEach(async () => {
  await applySystemLocale("zh-CN");
  library = {
    files: [file],
    capacity: { used_bytes: 1024, reserved_bytes: 0, quota_bytes: 1073741824 },
    uploads: [],
  };
  vi.spyOn(api, "listBuiltinPackages").mockImplementation(async () => library);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "listAdapters").mockResolvedValue([]);
  vi.spyOn(api, "listWorkers").mockResolvedValue([]);
  vi.spyOn(api, "listBuiltinChecks").mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

it("distinguishes uploaded materials from installability and searches files", async () => {
  render(<BuiltinPackagePanel />);
  await screen.findByText("offline-demo");
  expect(screen.getByText("已上传 · 未验证安装")).toBeTruthy();
  expect(screen.getByText(/上传预留 0 B/)).toBeTruthy();
  fireEvent.change(
    screen.getByRole("textbox", { name: "搜索包名、版本或文件名" }),
    { target: { value: "does-not-exist" } },
  );
  expect(screen.queryByText("offline-demo")).toBeNull();
});

it("requires explicit deletion confirmation and keeps failure visible", async () => {
  const remove = vi
    .spyOn(api, "deleteBuiltinPackage")
    .mockRejectedValue(
      new ApiError(409, "builtin_package_in_use", "依赖文件被任务占用"),
    );
  render(<BuiltinPackagePanel />);
  fireEvent.click(
    await screen.findByRole("button", { name: `删除 ${file.filename}` }),
  );
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/已有 Worker 环境可能继续运行/)).toBeTruthy();
  expect(remove).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "永久删除" }));
  await waitFor(() =>
    expect(within(dialog).getByRole("alert").textContent).toContain(
      "依赖文件被任务占用",
    ),
  );
  expect(screen.getByText("offline-demo")).toBeTruthy();
  remove.mockImplementation(async () => {
    library = { ...library, files: [] };
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "永久删除" }));
  await screen.findByText("文件及下载入口已删除。");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("keeps dependency-source selection in the dedicated source settings only", async () => {
  render(<BuiltinPackagePanel />);
  await screen.findByText("offline-demo");
  expect(screen.queryByRole("button", { name: "设为该类默认源" })).toBeNull();
  expect(api.listPackageSources).not.toHaveBeenCalled();
});

it("uploads a selected batch and reports each result", async () => {
  const upload = vi
    .spyOn(api, "uploadBuiltinPackage")
    .mockResolvedValue({ file, already_exists: true });
  const start = vi.fn();
  const end = vi.fn();
  render(<BuiltinPackagePanel onMutationStart={start} onMutationEnd={end} />);
  await screen.findByText("offline-demo");
  fireEvent.click(screen.getByRole("button", { name: "批量上传" }));
  const dialog = await screen.findByRole("dialog");
  const input = dialog.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).toBeTruthy();
  const first = new File(["first"], "first.whl");
  const second = new File(["second"], "second.whl");
  fireEvent.change(input!, { target: { files: [first, second] } });
  await within(dialog).findByText("second.whl");
  fireEvent.click(within(dialog).getByRole("button", { name: "开始上传" }));
  await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
  await within(dialog).findByText("first.whl：相同文件已存在");
  await within(dialog).findByText("second.whl：相同文件已存在");
  expect(start).toHaveBeenCalledTimes(1);
  expect(end).toHaveBeenCalledTimes(1);
});

it("shows capacity rejection and keeps the dialog open", async () => {
  vi.spyOn(api, "setBuiltinPackageCapacity").mockRejectedValue(
    new ApiError(
      409,
      "builtin_capacity_in_use",
      "容量不能小于已保存文件与上传预留的合计大小",
    ),
  );
  render(<BuiltinPackagePanel />);
  await screen.findByText("offline-demo");
  fireEvent.click(screen.getByRole("button", { name: "调整容量" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: /确\s*定|OK/ }));
  await waitFor(() =>
    expect(within(dialog).getByRole("alert").textContent).toContain(
      "容量不能小于",
    ),
  );
  expect(dialog).toBeTruthy();
});
