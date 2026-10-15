import { StrictMode, useState, type PropsWithChildren } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import type { PreparedAttachmentTransport } from "../prepared-attachment-session";
import type { AiAttachment, AiPreparedAttachment } from "../types";
import { usePreparedAttachmentSession } from "./usePreparedAttachmentSession";

function metadata(ref = "original-reference"): AiPreparedAttachment {
  return { attachment_ref: ref, sha256: "a".repeat(64), representation_revision: "rev-1",
    filename: "sample.pdf", content_type: "application/pdf", size_bytes: 1, status: "ready", page_count: 1,
    pages: [{ page: 1, status: "available" }], sheets: [], coverage: { pages: "available" },
    warnings: [], expires_at: new Date(Date.now() + 60_000).toISOString() };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, failed) => { resolve = done; reject = failed; });
  return { promise, resolve, reject };
}
function setup() {
  const transport = {
    prepareAiAttachment: vi.fn<PreparedAttachmentTransport["prepareAiAttachment"]>(async () => metadata()),
    getPreparedAiAttachment: vi.fn<PreparedAttachmentTransport["getPreparedAiAttachment"]>(async () => metadata()),
    releasePreparedAiAttachment: vi.fn<PreparedAttachmentTransport["releasePreparedAiAttachment"]>(async () => {}),
    releaseAiAttachmentScope: vi.fn<PreparedAttachmentTransport["releaseAiAttachmentScope"]>(async () => {}),
  };
  const wire: AiAttachment = { filename: "sample.pdf", content_type: "application/pdf", data_base64: "eA==" };
  const read = vi.fn(async () => wire);
  return { transport, read, wire };
}
const closedScope = () => new ApiError(409, "ai_attachment_cancelled", "cancelled", { reason: "scope_closed" });
afterEach(() => vi.restoreAllMocks());

describe("attachment-only scope recovery hook", () => {
  it("keeps ordinary prepare deduplicated and preserves the resource owner across renders", async () => {
    const { transport, read } = setup();
    const view = renderHook(() => usePreparedAttachmentSession(7, "account.adapter.page", transport));
    const session = view.result.current.session;
    const scopeId = session.scopeId;
    await act(async () => {
      const first = session.prepare("row", read);
      expect(session.prepare("row", read)).toBe(first);
      await first;
    });
    view.rerender();
    expect(view.result.current.session).toBe(session);
    expect(session.scopeId).toBe(scopeId);
    expect(view.result.current.states.row?.phase).toBe("ready");
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    expect(read).toHaveBeenCalledOnce();
  });

  it("renews a closed resource scope on the same page without replacing old frozen refs or panel drafts", async () => {
    const { transport, read, wire } = setup();
    const view = renderHook(() => {
      const [draft] = useState("keep this question");
      const [file] = useState(() => new File(["x"], "sample.pdf", { type: "application/pdf" }));
      const [history] = useState([{ text: "old answer", candidate: "keep candidate" }]);
      return { ...usePreparedAttachmentSession(7, "account.adapter.page", transport), draft, file, history };
    });
    const { session, file, history } = view.result.current;
    const oldScope = session.scopeId;
    await act(async () => { await session.prepare("old", read); });
    const frozen = session.freeze(["old"]);
    session.removeComposer("old");
    transport.getPreparedAiAttachment.mockRejectedValueOnce(new ApiError(410, "ai_attachment_expired", "expired"));
    await act(async () => {
      await expect(session.validate(frozen, new AbortController().signal)).rejects.toMatchObject({ code: "ai_attachment_expired" });
    });
    transport.prepareAiAttachment.mockRejectedValueOnce(closedScope()).mockResolvedValueOnce(metadata("new-reference"));
    await act(async () => { await session.prepare("new", read); });
    const newScope = session.scopeId;
    expect(newScope).not.toBe(oldScope);
    expect(view.result.current.session).toBe(session);
    expect(view.result.current.draft).toBe("keep this question");
    expect(view.result.current.file).toBe(file);
    expect(view.result.current.history).toBe(history);
    expect(view.result.current.states.old?.phase).toBe("expired");
    expect(view.result.current.states.new?.phase).toBe("ready");
    expect(transport.prepareAiAttachment.mock.calls.map(call => call[1])).toEqual([oldScope, oldScope, newScope]);
    expect(transport.prepareAiAttachment.mock.calls[1]?.[2]).toBe(wire);
    expect(transport.prepareAiAttachment.mock.calls[2]?.[2]).toBe(wire);
    expect(transport.releaseAiAttachmentScope).toHaveBeenCalledExactlyOnceWith(7, oldScope);
    await expect(session.validate(frozen, new AbortController().signal)).rejects.toMatchObject({ code: "ai_attachment_expired" });
    expect(transport.getPreparedAiAttachment).toHaveBeenCalledOnce();
    expect(() => session.freeze(["old"])).toThrow(ApiError);
    expect(frozen).toEqual([{ attachment_ref: "original-reference", sha256: "a".repeat(64), representation_revision: "rev-1" }]);
    expect(session.freeze(["new"])[0]?.attachment_ref).toBe("new-reference");
  });

  it("keeps one serial queue through renewal and re-prepares surviving composer files in the new generation", async () => {
    const { transport, read } = setup();
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport));
    const { session } = view.result.current;
    const oldScope = session.scopeId;
    await act(async () => { await session.prepare("survivor", read); });
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(metadata("new-first"))
      .mockResolvedValueOnce(metadata("new-second"))
      .mockResolvedValueOnce(metadata("new-survivor"));
    let one!: Promise<AiPreparedAttachment>;
    let two!: Promise<AiPreparedAttachment>;
    act(() => { one = session.prepare("one", read); two = session.prepare("two", read); });
    await waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2));
    expect(read).toHaveBeenCalledTimes(2);
    await act(async () => { first.reject(closedScope()); await one; await two; });
    const newScope = session.scopeId;
    expect(transport.prepareAiAttachment.mock.calls.map(call => call[1])).toEqual([oldScope, oldScope, newScope, newScope]);
    expect(view.result.current.states.survivor?.phase).toBe("expired");
    await act(async () => { await session.prepare("survivor", read); });
    expect(transport.prepareAiAttachment.mock.calls[4]?.[1]).toBe(newScope);
    expect(session.freeze(["survivor", "one", "two"]).map(ref => ref.attachment_ref))
      .toEqual(["new-survivor", "new-first", "new-second"]);
  });

  it("rejects late validation from a retired generation without reviving its historical metadata", async () => {
    const { transport, read } = setup();
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport));
    const { session } = view.result.current;
    await act(async () => { await session.prepare("old", read); });
    const frozen = session.freeze(["old"]);
    const late = deferred<AiPreparedAttachment>();
    transport.getPreparedAiAttachment.mockReturnValueOnce(late.promise);
    let validation!: Promise<void>;
    act(() => { validation = session.validate(frozen, new AbortController().signal); });
    transport.prepareAiAttachment.mockRejectedValueOnce(closedScope()).mockResolvedValueOnce(metadata("new-reference"));
    await act(async () => { await session.prepare("new", read); });
    await act(async () => {
      late.resolve({ ...metadata(), expires_at: new Date(Date.now() + 120_000).toISOString() });
      await expect(validation).rejects.toMatchObject({ code: "ai_attachment_expired" });
    });
    expect(view.result.current.states.old?.phase).toBe("expired");
    expect(view.result.current.states.old?.metadata?.attachment_ref).toBe("original-reference");
    expect(view.result.current.states.new?.phase).toBe("ready");
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(3);
  });

  it.each([
    new ApiError(409, "ai_attachment_cancelled", "cancelled"),
    new ApiError(422, "ai_attachment_invalid", "bad file"),
    new ApiError(409, "ai_attachment_converter_busy", "busy", { reason: "scope_closed" }),
  ])("does not automatically retry ordinary cancellation, bad files or unrelated reasons: $code", async error => {
    const { transport, read } = setup();
    transport.prepareAiAttachment.mockRejectedValue(error);
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport));
    const { session } = view.result.current;
    const scopeId = session.scopeId;
    await act(async () => { await expect(session.prepare("row", read)).rejects.toBe(error); });
    expect(session.scopeId).toBe(scopeId);
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    expect(view.result.current.states.row).toMatchObject({ phase: "error", error });
    expect(transport.releaseAiAttachmentScope).not.toHaveBeenCalled();
  });

  it("bounds recovery to one renewal per preparation and permits an explicit row retry", async () => {
    const { transport, read } = setup();
    transport.prepareAiAttachment.mockRejectedValue(closedScope());
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport));
    const { session } = view.result.current;
    await act(async () => { await expect(session.prepare("row", read)).rejects.toMatchObject({ params: { reason: "scope_closed" } }); });
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2);
    expect(read).toHaveBeenCalledOnce();
    expect(view.result.current.states.row?.phase).toBe("error");
    const errorScope = session.scopeId;
    transport.prepareAiAttachment.mockResolvedValueOnce(metadata("manual-retry"));
    await act(async () => { session.retry("row"); await session.prepare("row", read); });
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(3);
    expect(session.scopeId).toBe(errorScope);
    expect(view.result.current.states.row?.metadata?.attachment_ref).toBe("manual-retry");
  });

  it.each(["remove", "pagehide", "unmount", "owner change"] as const)("does not resurrect a scope after %s while a non-cooperative request fails late", async action => {
    const { transport, read } = setup();
    const pending = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(pending.promise);
    const view = renderHook(({ owner }) => usePreparedAttachmentSession(7, owner, transport), { initialProps: { owner: "old" } });
    const { session } = view.result.current;
    const oldScope = session.scopeId;
    let request!: Promise<AiPreparedAttachment>;
    act(() => { request = session.prepare("row", read); });
    await waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    await act(async () => {
      if (action === "remove") session.removeComposer("row");
      else if (action === "pagehide") session.close();
      else if (action === "unmount") view.unmount();
      else view.rerender({ owner: "new" });
      await Promise.resolve();
    });
    expect(transport.prepareAiAttachment.mock.calls[0]?.[3]?.aborted).toBe(true);
    await act(async () => { pending.reject(closedScope()); await expect(request).rejects.toMatchObject({ name: "AbortError" }); });
    expect(session.scopeId).toBe(oldScope);
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    expect(session.snapshot()).toEqual({});
  });

  it("releases a late renewed reference against its captured scope and closes each used generation only once", async () => {
    const { transport, read } = setup();
    const late = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockRejectedValueOnce(closedScope()).mockReturnValueOnce(late.promise);
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport));
    const { session } = view.result.current;
    const oldScope = session.scopeId;
    let request!: Promise<AiPreparedAttachment>;
    act(() => { request = session.prepare("row", read); });
    await waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2));
    const renewedScope = session.scopeId;
    await act(async () => { view.unmount(); await Promise.resolve(); });
    late.resolve(metadata("late-renewed-reference"));
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    await waitFor(() => expect(transport.releasePreparedAiAttachment).toHaveBeenCalledExactlyOnceWith(7, renewedScope, "late-renewed-reference"));
    session.close();
    expect(transport.releaseAiAttachmentScope.mock.calls).toEqual([[7, oldScope], [7, renewedScope]]);
    expect(session.snapshot()).toEqual({});
  });

  it("keeps StrictMode effect replay usable without prematurely closing a scope", async () => {
    const { transport, read } = setup();
    const wrapper = ({ children }: PropsWithChildren) => <StrictMode>{children}</StrictMode>;
    const view = renderHook(() => usePreparedAttachmentSession(7, "owner", transport), { wrapper });
    await act(async () => { await view.result.current.session.prepare("row", read); });
    expect(view.result.current.states.row?.phase).toBe("ready");
    expect(transport.releaseAiAttachmentScope).not.toHaveBeenCalled();
    await act(async () => { view.unmount(); await Promise.resolve(); });
    expect(transport.releaseAiAttachmentScope).toHaveBeenCalledOnce();
  });
});
