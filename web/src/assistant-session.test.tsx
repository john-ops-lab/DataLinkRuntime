/** #166 memory-only AI debugging and obsolete restoration boundary. */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { api } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import type { Adapter, AiAssistResponse } from "./types";

vi.mock("@monaco-editor/react", () => ({
  default: () => <textarea readOnly />,
  DiffEditor: () => <div />,
  loader: { init: () => Promise.resolve({ editor: { setTheme: () => undefined } }) },
}));

const adapter: Adapter = {
  id: 1, name: "adapter-a", description: "", language: "python", adapter_type: "task",
  run_mode: "manual", timeout_seconds: 300, runtime_worker_id: 1, latest_version_id: 10,
  created_at: "2026-08-11T00:00:00Z", updated_at: "2026-08-11T00:00:00Z",
};


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

const reply: AiAssistResponse = {
  message: "Temporary reply", candidate: null, provider: "minimax", model: "fake", tool_calls: [],
};
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
  vi.spyOn(api, "getAiAttachmentCapabilities").mockRejectedValue(new Error("use defaults"));
});
afterEach(() => vi.restoreAllMocks());

it("ignores the old saved-session selection and sends bounded temporary history", async () => {
  sessionStorage.setItem("dlr.ai.selected-session.deployment.1", "obsolete-session");
  const assist = vi.spyOn(api, "assistAdapter").mockResolvedValue(reply);
  panel();
  expect(screen.queryByTestId("ai-session-controls")).toBeNull();
  await send("First question");
  await screen.findByText(reply.message);
  await send("Second question");
  await waitFor(() => expect(assist).toHaveBeenCalledTimes(2));
  const payload = assist.mock.calls[1][1];
  expect(payload.recent_messages).toEqual([
    { role: "user", content: "First question" },
    { role: "assistant", content: reply.message },
  ]);
  expect(payload).not.toHaveProperty("session_id");
  expect(payload).not.toHaveProperty("turn_id");
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(1);
});

it("keeps same-page messages on close and reopen but remount starts empty", async () => {
  vi.spyOn(api, "assistAdapter").mockResolvedValue(reply);
  const view = panel();
  await send("Only on this page");
  await screen.findByText(reply.message);
  view.rerenderPanel({ open: false });
  view.rerenderPanel({ open: true });
  expect(screen.getByText(reply.message)).toBeTruthy();
  view.unmount();
  panel();
  expect(screen.queryByText("Only on this page")).toBeNull();
  expect(screen.queryByText(reply.message)).toBeNull();
});

it.each(["account", "adapter"])("discards a late reply after %s scope changes", async (scope) => {
  let resolve!: (value: AiAssistResponse) => void;
  const assist = vi.spyOn(api, "assistAdapter").mockImplementation(() => new Promise((r) => { resolve = r; }));
  const view = panel({ accountOwnerId: 1 });
  await send("Old scope question");
  await waitFor(() => expect(assist).toHaveBeenCalledTimes(1));
  view.rerenderPanel(scope === "account" ? { accountOwnerId: 2 } : { adapter: { ...adapter, id: 2 } });
  await act(async () => resolve(reply));
  expect(screen.queryByText("Old scope question")).toBeNull();
  expect(screen.queryByText(reply.message)).toBeNull();
});
