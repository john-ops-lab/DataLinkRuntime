import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Credential, KnowledgeSource } from "../types";
import SystemSettingsDrawer from "./SystemSettingsDrawer";

const credential: Credential = { id: 7, name: "fixture-access", type: "access_key", created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z" };
const source: KnowledgeSource = { source_id: "ima", kind: "ima", name: "Tencent ima", endpoint: "https://ima.qq.com", enabled: true, status: "configured", credential_id: 7, credential_name: "fixture-access", credential_type: "access_key", config_source: "database", created_at: null, updated_at: null };
afterEach(() => vi.restoreAllMocks());

it("v3 usage loads real references only when requested and explains permission scope", async () => {
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential]);
  const usage = vi.spyOn(api, "getCredentialUsage").mockResolvedValue({ items: [{ kind: "adapter_binding", name: "fixture task", adapter_id: 42, detail: "API_TOKEN" }] });
  render(<SystemSettingsDrawer open category="credentials" onClose={vi.fn()} />);
  await screen.findByText("fixture-access");
  expect(usage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看使用位置" }));
  await screen.findByText("fixture task");
  expect(usage).toHaveBeenCalledWith(7);
  expect(screen.getAllByText("仅显示你有权限查看的配置；未显示不代表未使用。")[0]).toBeTruthy();
});

it("v3 empty usage does not claim global non-use", async () => {
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential]);
  vi.spyOn(api, "getCredentialUsage").mockResolvedValue({ items: [] });
  render(<SystemSettingsDrawer open category="credentials" onClose={vi.fn()} />);
  await screen.findByText("fixture-access");
  fireEvent.click(screen.getByRole("button", { name: "查看使用位置" }));
  await screen.findByText("当前查询未返回可显示的引用，不代表全系统未使用。");
});

it("v3 knowledge draft requires save before testing saved configuration", async () => {
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential]);
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue(source);
  const save = vi.spyOn(api, "updateKnowledgeSource").mockImplementation(async (_id, draft) => ({ ...source, ...draft }));
  const test = vi.spyOn(api, "testKnowledgeSource");
  render(<SystemSettingsDrawer open category="knowledge-sources" onClose={vi.fn()} />);
  await screen.findByTestId("knowledge-source-summary");
  expect(screen.getByTestId("test-knowledge-source")).not.toHaveProperty("disabled", true);
  expect(screen.getByText("尚未取得列表。保存后测试连接，再查看可访问的知识库。")).toBeTruthy();
  fireEvent.click(screen.getByTestId("knowledge-source-enabled"));
  expect(screen.getByTestId("test-knowledge-source")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  expect(test).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId("save-knowledge-source"));
  await waitFor(() => expect(save).toHaveBeenCalledWith("ima", { enabled: false, credential_id: 7 }));
});

it("v3 missing access-key credential blocks save and test", async () => {
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue({ ...source, credential_id: null });
  const save = vi.spyOn(api, "updateKnowledgeSource");
  render(<SystemSettingsDrawer open category="knowledge-sources" onClose={vi.fn()} />);
  await screen.findByTestId("knowledge-source-summary");
  expect(screen.getByTestId("save-knowledge-source")).toHaveProperty("disabled", true);
  expect(screen.getByTestId("test-knowledge-source")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByTestId("save-knowledge-source"));
  expect(save).not.toHaveBeenCalled();
});

it("does not attach a late credential usage response to a different credential", async () => {
  const other = { ...credential, id: 8, name: "second-access" };
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential, other]);
  let finishFirst!: (value: Awaited<ReturnType<typeof api.getCredentialUsage>>) => void;
  vi.spyOn(api, "getCredentialUsage").mockImplementation((id) => id === 7
    ? new Promise<Awaited<ReturnType<typeof api.getCredentialUsage>>>((resolve) => { finishFirst = resolve; })
    : Promise.resolve<Awaited<ReturnType<typeof api.getCredentialUsage>>>({ items: [{ kind: "knowledge_source", name: "second credential reference" }] }));
  render(<SystemSettingsDrawer open category="credentials" onClose={vi.fn()} />);
  await screen.findByText("second-access");
  fireEvent.click(screen.getAllByRole("button", { name: "查看使用位置" })[0]);
  const close = document.querySelector(".ant-modal-close");
  if (!(close instanceof HTMLElement)) throw new Error("Usage modal close button missing");
  fireEvent.click(close);
  fireEvent.click(screen.getAllByRole("button", { name: "查看使用位置" })[1]);
  await screen.findByText("second credential reference");
  finishFirst({ items: [{ kind: "adapter_binding", name: "obsolete first credential reference" }] });
  await waitFor(() => expect(screen.getByText("second credential reference")).toBeTruthy());
  expect(screen.queryByText("obsolete first credential reference")).toBeNull();
});

it("blocks testing and editing until the knowledge configuration save completes", async () => {
  const other = { ...credential, id: 8, name: "next-access" };
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential, other]);
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue(source);
  let finishSave!: (value: KnowledgeSource) => void;
  const save = vi.spyOn(api, "updateKnowledgeSource").mockReturnValue(new Promise((resolve) => { finishSave = resolve; }));
  const test = vi.spyOn(api, "testKnowledgeSource").mockResolvedValue({ ok: true, status: "connected", error_code: null, message: "connected", knowledge_bases: [] });
  render(<SystemSettingsDrawer open category="knowledge-sources" onClose={vi.fn()} />);
  await screen.findByTestId("knowledge-source-summary");
  const select = screen.getByTestId("knowledge-source-credential");
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);
  fireEvent.click(await screen.findByText("next-access"));
  expect(screen.getByTestId("test-knowledge-source")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByTestId("save-knowledge-source"));
  await waitFor(() => expect(save).toHaveBeenCalledWith("ima", { enabled: true, credential_id: 8 }));
  expect(screen.getByTestId("knowledge-source-enabled")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  expect(test).not.toHaveBeenCalled();
  finishSave({ ...source, credential_id: 8, credential_name: "next-access" });
  await waitFor(() => expect(screen.getByTestId("test-knowledge-source")).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  await waitFor(() => expect(test).toHaveBeenCalledWith("ima"));
  await screen.findByText("连接成功，当前未返回可访问的知识库。");
});

it("clears an earlier knowledge list when a fresh connection check fails", async () => {
  vi.spyOn(api, "listCredentials").mockResolvedValue([credential]);
  vi.spyOn(api, "getKnowledgeSource").mockResolvedValue(source);
  vi.spyOn(api, "testKnowledgeSource")
    .mockResolvedValueOnce({ ok: true, status: "connected", error_code: null, message: "connected", knowledge_bases: [{ id: "old-kb", name: "Earlier accessible library", status: "accessible" }] })
    .mockRejectedValueOnce(new Error("network failed"));
  render(<SystemSettingsDrawer open category="knowledge-sources" onClose={vi.fn()} />);
  await screen.findByTestId("knowledge-source-summary");
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  await screen.findByText("Earlier accessible library");
  await waitFor(() => expect(screen.getByTestId("test-knowledge-source")).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByTestId("test-knowledge-source"));
  await screen.findByTestId("knowledge-source-test-error");
  expect(screen.queryByText("Earlier accessible library")).toBeNull();
  expect(screen.getByText("尚未取得列表。保存后测试连接，再查看可访问的知识库。")).toBeTruthy();
});
