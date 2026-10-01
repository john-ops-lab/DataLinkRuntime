/** Opt-in durable AI session behavior at the browser boundary. */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps, ReactNode } from "react";

import { api, ApiError } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type { Adapter, AiAssistRequest, AiSessionDetail } from "./types";

vi.mock("@monaco-editor/react", () => ({
  default: () => <textarea readOnly />,
  DiffEditor: () => <div />,
  loader: { init: () => Promise.resolve({ editor: { setTheme: () => undefined } }) },
}));

// This suite exercises the confirmed action's async fencing; Popconfirm's
// portal/motion interaction is covered by Ant Design itself.
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    Popconfirm: ({ children, onConfirm }: { children: ReactNode; onConfirm: () => void }) =>
      <span onClick={onConfirm}>{children}</span>,
  };
});

const sessionId = "d7b402f4-b1b8-45d7-8b12-55415e5b4101";
const turnId = "f5f499fc-a72c-4f68-b3da-2b1502299341";
const adapter: Adapter = {
  id: 1, name: "adapter-a", description: "", language: "python", adapter_type: "task",
  run_mode: "manual", timeout_seconds: 300, runtime_worker_id: 1, latest_version_id: 10,
  created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
};

function detail(revision: number, messages: AiSessionDetail["messages"] = []): AiSessionDetail {
  return {
    id: sessionId, adapter_id: 1, revision,
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
    expires_at: "2026-12-01T00:00:00Z", summary_covered_through: 0,
    summary_valid: false, messages,
  };
}

function panel(overrides: Partial<ComponentProps<typeof AiAssistantPanel>> = {}) {
  const props: ComponentProps<typeof AiAssistantPanel> = {
    open: true, adapter, selectedVersionId: 10, selectedVersionSeq: 1,
    workingCopy: { code: "def handle(context, input): return input", requirements: "", runtimeConfigText: "{}" },
    contentReady: true, busy: false, contextSnippets: [], theme: "vs-dark",
    onOpen: vi.fn(), onClose: vi.fn(), onApply: vi.fn(),
    onRemoveContextSnippet: vi.fn(), onClearContextSnippets: vi.fn(),
    ...overrides,
  };
  const view = render(<AiAssistantPanel {...props} />);
  return { ...view, rerenderPanel: (next: Partial<typeof props>) => view.rerender(<AiAssistantPanel {...props} {...next} />) };
}

async function send(text: string) {
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: text } });
  await waitFor(() => expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByTestId("ai-send"));
}

beforeEach(() => {
  sessionStorage.clear();
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
  vi.spyOn(api, "listAiSessions").mockResolvedValue({ sessions: [detail(0)] });
});
afterEach(async () => {
  sessionStorage.clear();
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

it("restores only saved visible history after a refresh and uses the server revision for a new turn", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(7, [
    { sequence: 1, turn_id: turnId, role: "user", content: "Earlier request", source_revision: 1, generation: 1, request_status: "completed" },
    { sequence: 2, turn_id: turnId, role: "assistant", content: "Saved answer", source_revision: 1, generation: 1, request_status: null },
  ]);
  const read = vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(async (_id, payload) => {
    saved = detail(9, [
      ...saved.messages,
      { sequence: 3, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "completed" },
      { sequence: 4, turn_id: payload.turn_id!, role: "assistant", content: "New answer", source_revision: 1, generation: 1, request_status: null },
    ]);
    return { message: "New answer", provider: "openai", model: "test-model", candidate: null };
  });
  const first = panel();
  await screen.findByText("Saved answer");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.getByTestId("ai-session-note").textContent).toContain("附件");
  first.unmount();
  panel();
  await screen.findByText("Saved answer");
  expect(read).toHaveBeenCalledTimes(2);
  expect(sessionStorage.length).toBe(1);
  await send("Continue");
  await screen.findByText("New answer");
  const payload = assist.mock.calls[0][1];
  expect(payload.session_id).toBe(sessionId);
  expect(payload.expected_session_revision).toBe(7);
  expect(payload.expected_generation).toBe(0);
  expect(payload.recent_messages).toEqual([]);
  expect(payload.turn_id).toBeTruthy();
  expect(payload.idempotency_key).toBeTruthy();
});

it("retries a failed durable turn with the exact frozen key, revision and Working Copy", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(0);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(async (_id, payload) => {
    if (assist.mock.calls.length === 1) {
      saved = detail(2, [
        { sequence: 1, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "failed" },
      ]);
      throw new ApiError(503, "ai_timeout", "timeout");
    }
    saved = detail(4, [
      { sequence: 1, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "completed" },
      { sequence: 2, turn_id: payload.turn_id!, role: "assistant", content: "Recovered", source_revision: 1, generation: 1, request_status: null },
    ]);
    return { message: "Recovered", provider: "openai", model: "test-model", candidate: null };
  });
  const view = panel();
  fireEvent.change(screen.getByTestId("ai-attachment-input"), {
    target: { files: [new File(["temporary attachment"], "note.txt", { type: "text/plain" })] },
  });
  await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(1));
  await send("Frozen request");
  await screen.findByTestId("ai-panel-error");
  view.rerenderPanel({ workingCopy: { code: "changed code", requirements: "new", runtimeConfigText: "{}" } });
  fireEvent.click(screen.getByTestId("ai-retry"));
  await screen.findByText("Recovered");
  const first = assist.mock.calls[0][1] as AiAssistRequest;
  const second = assist.mock.calls[1][1] as AiAssistRequest;
  expect(second).toEqual(first);
  expect(first.attachments).toHaveLength(1);
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
});

it("replays a committed turn with the original key after its Candidate response is lost", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(0);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  const committed = {
    message: "Committed reply", provider: "openai" as const, model: "test-model",
    candidate: { summary: "Recovered Candidate", code: "def handle(context, input): return 42", required_secret_keys: [] },
  };
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(async (_id, payload) => {
    if (assist.mock.calls.length === 1) {
      saved = detail(2, [
        { sequence: 1, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "completed" },
        { sequence: 2, turn_id: payload.turn_id!, role: "assistant", content: committed.message, source_revision: 1, generation: 1, request_status: null },
      ]);
      throw new ApiError(503, "network_lost", "response lost");
    }
    return committed;
  });
  const view = panel();
  await send("Build it");
  await screen.findByText("Committed reply");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.getByTestId("ai-retry")).toBeTruthy();
  view.rerenderPanel({ workingCopy: { code: "changed after commit", requirements: "", runtimeConfigText: "{}" } });
  fireEvent.click(screen.getByTestId("ai-retry"));
  await screen.findByText("Recovered Candidate");
  expect(assist).toHaveBeenCalledTimes(2);
  expect(assist.mock.calls[1][1]).toEqual(assist.mock.calls[0][1]);
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
  expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(1);
  expect(screen.getByTestId("ai-regenerate")).toBeTruthy();
});

it("clear fences a late response and restores the send button; identity changes hide old results", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.account-1.1", sessionId);
  let resolveAssist: ((value: Awaited<ReturnType<typeof api.assistAdapter>>) => void) | undefined;
  vi.spyOn(api, "readAiSession").mockResolvedValue(detail(0));
  vi.spyOn(api, "clearAiSession").mockResolvedValue(detail(2));
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(() => new Promise((resolve) => { resolveAssist = resolve; }));
  const view = panel({ accountOwnerId: 1 });
  await send("Old request");
  await waitFor(() => expect(assist).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByTestId("ai-session-clear"));
  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "New request" } });
  await waitFor(() => expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(false));
  await act(async () => {
    resolveAssist?.({ message: "Late answer", provider: "openai", model: "test-model", candidate: null });
  });
  expect(screen.queryByText("Late answer")).toBeNull();
  expect(screen.queryByText("Old request")).toBeNull();
  view.rerenderPanel({ accountOwnerId: 2 });
  await waitFor(() => expect(screen.getByTestId("ai-conversation-empty")).toBeTruthy());
  expect(sessionStorage.getItem("dlr.ai.selected-session.account-1.1")).toBe(sessionId);
  expect(sessionStorage.getItem("dlr.ai.selected-session.account-2.1")).toBeNull();
});

it("ignores a delayed history read after the account owner changes", async () => {
  const secondId = "0c1e9d57-aac8-4bf1-9374-7052c00dbfaf";
  sessionStorage.setItem("dlr.ai.selected-session.account-1.1", sessionId);
  sessionStorage.setItem("dlr.ai.selected-session.account-2.1", secondId);
  let resolveOld: ((value: AiSessionDetail) => void) | undefined;
  vi.spyOn(api, "readAiSession").mockImplementation(async (_adapter, id) => {
    if (id === sessionId) return new Promise<AiSessionDetail>((resolve) => { resolveOld = resolve; });
    return {
      ...detail(3, [
        { sequence: 1, turn_id: turnId, role: "user", content: "Owner two text", source_revision: 1, generation: 1, request_status: "completed" },
      ]),
      id: secondId,
    };
  });
  const view = panel({ accountOwnerId: 1 });
  await waitFor(() => expect(resolveOld).toBeDefined());
  view.rerenderPanel({ accountOwnerId: 2 });
  await screen.findByText("Owner two text");
  await act(async () => resolveOld?.(detail(1, [
    { sequence: 1, turn_id: turnId, role: "user", content: "Owner one secret", source_revision: 1, generation: 1, request_status: "completed" },
  ])));
  expect(screen.queryByText("Owner one secret")).toBeNull();
  expect(screen.getByText("Owner two text")).toBeTruthy();
});

it("regenerates restored text from the current Working Copy without restoring old Candidate", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(5, [
    { sequence: 1, turn_id: turnId, role: "user", content: "Improve this", source_revision: 1, generation: 1, request_status: "completed" },
    { sequence: 2, turn_id: turnId, role: "assistant", content: "Old reply", source_revision: 1, generation: 1, request_status: null },
  ]);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(async () => {
    saved = detail(7, [
      { ...saved.messages[0], generation: 2 },
      { ...saved.messages[1], content: "Regenerated", source_revision: 2, generation: 2 },
    ]);
    return { message: "Regenerated", provider: "openai", model: "test-model", candidate: null };
  });
  const apply = vi.fn();
  panel({
    onApply: apply,
    workingCopy: { code: "CURRENT WORKING COPY", requirements: "", runtimeConfigText: "{}" },
  });
  await screen.findByText("Old reply");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.getByTestId("ai-regenerate").getAttribute("aria-label")).toContain("当前工作副本");
  fireEvent.click(screen.getByTestId("ai-regenerate"));
  await screen.findByText("Regenerated");
  const payload = assist.mock.calls[0][1];
  expect(payload.regenerate_turn_id).toBe(turnId);
  expect(payload.expected_generation).toBe(1);
  expect(payload.expected_session_revision).toBe(5);
  expect(payload.working_copy.code).toBe("CURRENT WORKING COPY");
  expect(payload.attachments).toBeUndefined();
  expect(apply).not.toHaveBeenCalled();
});

it("keeps a current-page Candidate on close/reopen but not after remount", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(0);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  vi.spyOn(api, "assistAdapter").mockImplementation(async (_id, payload) => {
    saved = detail(2, [
      { sequence: 1, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "completed" },
      { sequence: 2, turn_id: payload.turn_id!, role: "assistant", content: "Candidate reply", source_revision: 1, generation: 1, request_status: null },
    ]);
    return {
      message: "Candidate reply", provider: "openai", model: "test-model",
      candidate: { summary: "Candidate", code: "def handle(context, input): return 2", required_secret_keys: [] },
    };
  });
  const view = panel();
  await send("Change code");
  await screen.findByTestId("ai-candidate");
  view.rerenderPanel({ open: false });
  view.rerenderPanel({ open: true });
  await screen.findByTestId("ai-candidate");
  view.unmount();
  panel();
  await screen.findByText("Candidate reply");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
});

it("keeps unique visible rounds after a failed turn, continuation and another send", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(0);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  let call = 0;
  vi.spyOn(api, "assistAdapter").mockImplementation(async (_id, payload) => {
    call += 1;
    if (call === 1) {
      saved = detail(2, [
        { sequence: 1, turn_id: payload.turn_id!, role: "user", content: "First", source_revision: 1, generation: 1, request_status: "failed" },
      ]);
      throw new ApiError(503, "ai_timeout", "timeout");
    }
    const sequence = call === 2 ? 3 : 5;
    saved = detail(call === 2 ? 4 : 6, [
      ...(call === 2 ? [
        saved.messages[0],
        { sequence: 2, turn_id: saved.messages[0].turn_id, role: "assistant" as const, content: "Failed placeholder", source_revision: 1, generation: 1, request_status: null },
      ] : saved.messages),
      { sequence, turn_id: payload.turn_id!, role: "user", content: payload.message, source_revision: 1, generation: 1, request_status: "completed" },
      { sequence: sequence + 1, turn_id: payload.turn_id!, role: "assistant", content: `Reply ${call}`, source_revision: 1, generation: 1, request_status: null },
    ]);
    return { message: `Reply ${call}`, provider: "openai", model: "test-model", candidate: null };
  });
  panel();
  await send("First");
  await screen.findByTestId("ai-panel-error");
  await send("Second");
  await screen.findByText("Reply 2");
  await send("Third");
  await screen.findByText("Reply 3");
  expect(screen.getAllByTestId("ai-message-user")).toHaveLength(3);
  expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(3);
  expect(screen.getByText("Failed placeholder")).toBeTruthy();
});

it("lists, selects, creates and deletes opt-in sessions without saving message bodies locally", async () => {
  const secondId = "e2dd537e-3275-4028-87af-6f7e4b843005";
  const old = detail(3, [
    { sequence: 1, turn_id: turnId, role: "user", content: "Saved text", source_revision: 1, generation: 1, request_status: "completed" },
  ]);
  const fresh = { ...detail(0), id: secondId };
  let listed = [old];
  vi.spyOn(api, "listAiSessions").mockImplementation(async () => ({ sessions: listed }));
  vi.spyOn(api, "readAiSession").mockImplementation(async (_adapter, id) => id === sessionId ? old : fresh);
  const create = vi.spyOn(api, "createAiSession").mockImplementation(async () => {
    listed = [fresh, old];
    return fresh;
  });
  const remove = vi.spyOn(api, "deleteAiSession").mockResolvedValue();
  panel();
  await waitFor(() => expect(screen.getByTestId("ai-session-select")).toBeTruthy());
  fireEvent.mouseDown(screen.getByTestId("ai-session-select").querySelector(".ant-select-selector")!);
  await screen.findByRole("option", { name: /d7b402f4/ });
  const option = [...document.querySelectorAll(".ant-select-item-option")].find(
    (node) => node.textContent?.includes("d7b402f4"),
  );
  expect(option).toBeDefined();
  fireEvent.mouseDown(option!);
  fireEvent.click(option!);
  expect(sessionStorage.getItem("dlr.ai.selected-session.deployment.1")).toBe(sessionId);
  await screen.findByText("Saved text");
  expect(sessionStorage.getItem("dlr.ai.selected-session.deployment.1")).toBe(sessionId);
  fireEvent.click(screen.getByTestId("ai-session-create"));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(sessionStorage.getItem("dlr.ai.selected-session.deployment.1")).toBe(secondId));
  expect(sessionStorage.length).toBe(1);
  expect(sessionStorage.getItem("dlr.ai.selected-session.deployment.1")).not.toContain("Saved text");
  fireEvent.click(screen.getByTestId("ai-session-delete"));
  await waitFor(() => expect(remove).toHaveBeenCalledWith(1, secondId));
  expect(sessionStorage.getItem("dlr.ai.selected-session.deployment.1")).toBeNull();
});

it("returns unsaved text to the composer when a stale revision is rejected before reservation", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", sessionId);
  let saved = detail(0);
  vi.spyOn(api, "readAiSession").mockImplementation(async () => saved);
  vi.spyOn(api, "assistAdapter").mockImplementation(async () => {
    saved = detail(1);
    throw new ApiError(409, "ai_session_stale_revision", "stale");
  });
  panel();
  await send("Keep this text");
  await screen.findByTestId("ai-panel-error");
  await waitFor(() => expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value)
    .toBe("Keep this text"));
  expect(screen.queryByTestId("ai-message-user")).toBeNull();
});
