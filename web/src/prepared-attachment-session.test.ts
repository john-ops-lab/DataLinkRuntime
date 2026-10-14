import { afterEach, describe, expect, it, vi } from "vitest";
import { PreparedAttachmentSession, type PreparedAttachmentTransport } from "./prepared-attachment-session";
import type { AiPreparedAttachment } from "./types";

function metadata(overrides: Partial<AiPreparedAttachment> = {}): AiPreparedAttachment {
  return { attachment_ref: "abcdefghijklmnop", sha256: "a".repeat(64), representation_revision: "rev-1",
    filename: "sample.pdf", content_type: "application/pdf", size_bytes: 1, status: "ready", page_count: 2,
    pages: [{ page: 1, status: "blank" }, { page: 2, status: "available" }], sheets: [], coverage: { pages: "available" },
    warnings: [], expires_at: new Date(Date.now() + 60_000).toISOString(), ...overrides };
}
function setup() {
  const transport = {
    prepareAiAttachment: vi.fn<PreparedAttachmentTransport["prepareAiAttachment"]>(async () => metadata()),
    getPreparedAiAttachment: vi.fn<PreparedAttachmentTransport["getPreparedAiAttachment"]>(async () => metadata()),
    releasePreparedAiAttachment: vi.fn<PreparedAttachmentTransport["releasePreparedAiAttachment"]>(async () => {}),
    releaseAiAttachmentScope: vi.fn<PreparedAttachmentTransport["releaseAiAttachmentScope"]>(async () => {}),
  };
  const session = new PreparedAttachmentSession(7, "scope-id", transport);
  const read = vi.fn(async () => ({ filename: "sample.pdf", content_type: "application/pdf", data_base64: "eA==" }));
  return { session, transport, read };
}

afterEach(() => vi.useRealTimers());

describe("prepared attachment resource lifecycle", () => {
  it("prepares once and protects the exact frozen identity after composer removal", async () => {
    const { session, transport, read } = setup();
    const one = session.prepare("row", read);
    expect(session.prepare("row", read)).toBe(one);
    await one;
    expect(read).toHaveBeenCalledOnce();
    const refs = session.freeze(["row"]);
    expect(refs).toEqual([{ attachment_ref: "abcdefghijklmnop", sha256: "a".repeat(64), representation_revision: "rev-1" }]);
    session.removeComposer("row");
    expect(transport.releasePreparedAiAttachment).not.toHaveBeenCalled();
    const release = session.retainActive(refs);
    release();
    session.close(); session.close();
    expect(transport.releaseAiAttachmentScope).toHaveBeenCalledOnce();
  });

  it("releases unused references, aborts preparation and removes late results", async () => {
    const { session, transport, read } = setup();
    await session.prepare("ready", read);
    session.removeComposer("ready");
    expect(transport.releasePreparedAiAttachment).toHaveBeenCalledWith(7, "scope-id", "abcdefghijklmnop");
    let resolve: (value: AiPreparedAttachment) => void = () => {};
    transport.prepareAiAttachment.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const pending = session.prepare("pending", read);
    await Promise.resolve();
    session.removeComposer("pending");
    expect(transport.prepareAiAttachment.mock.calls[1]?.[3]?.aborted).toBe(true);
    resolve(metadata({ attachment_ref: "late-reference-id" }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(session.snapshot().pending).toBeUndefined();
    expect(transport.releasePreparedAiAttachment).toHaveBeenCalledWith(7, "scope-id", "late-reference-id");
  });

  it("fences a closed scope, aborts pending requests and releases late resources", async () => {
    const { session, transport, read } = setup();
    let resolve: (value: AiPreparedAttachment) => void = () => {};
    transport.prepareAiAttachment.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const pending = session.prepare("row", read);
    await Promise.resolve(); session.close();
    expect(transport.prepareAiAttachment.mock.calls[0]?.[3]?.aborted).toBe(true);
    resolve(metadata());
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(session.snapshot()).toEqual({});
    expect(transport.releaseAiAttachmentScope).toHaveBeenCalledOnce();
    expect(transport.releasePreparedAiAttachment).toHaveBeenCalledOnce();
    await expect(session.prepare("new", read)).rejects.toMatchObject({ name: "AbortError" });
  });

  it("does not replace the original when a later composer chooses another file", async () => {
    const { session, transport, read } = setup();
    await session.prepare("old", read);
    const frozen = session.freeze(["old"]); session.removeComposer("old");
    transport.prepareAiAttachment.mockResolvedValue(metadata({ attachment_ref: "new-reference-id", sha256: "b".repeat(64) }));
    await session.prepare("new", read);
    await session.validate(frozen, new AbortController().signal);
    expect(transport.getPreparedAiAttachment).toHaveBeenCalledWith(7, "scope-id", "abcdefghijklmnop", expect.any(AbortSignal));
    expect(frozen[0]?.sha256).toBe("a".repeat(64));
    expect(transport.releasePreparedAiAttachment).not.toHaveBeenCalled();
  });

  it("reports expiry without silently re-preparing frozen references", async () => {
    vi.useFakeTimers();
    const { session, transport, read } = setup();
    await session.prepare("row", read); const refs = session.freeze(["row"]);
    vi.advanceTimersByTime(61_000); session.checkExpired();
    expect(session.snapshot().row?.phase).toBe("expired");
    await expect(session.validate(refs, new AbortController().signal)).rejects.toMatchObject({ code: "ai_attachment_expired" });
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    expect(transport.getPreparedAiAttachment).not.toHaveBeenCalled();
  });

  it("rejects changed frozen representations", async () => {
    const { session, transport, read } = setup();
    await session.prepare("row", read); const refs = session.freeze(["row"]);
    transport.getPreparedAiAttachment.mockResolvedValue(metadata({ representation_revision: "rev-2" }));
    await expect(session.validate(refs, new AbortController().signal)).rejects.toMatchObject({ code: "ai_attachment_expired" });
    expect(refs[0]?.representation_revision).toBe("rev-1");
  });

  it("retries explicitly and preserves resource scopes during StrictMode effect replay", async () => {
    const { session, transport, read } = setup();
    const detach = session.mount(); detach(); const detachReplay = session.mount();
    await Promise.resolve();
    expect(transport.releaseAiAttachmentScope).not.toHaveBeenCalled();
    transport.prepareAiAttachment.mockRejectedValueOnce(new Error("temporary failure"));
    await expect(session.prepare("row", read)).rejects.toThrow("temporary failure");
    expect(session.snapshot().row?.phase).toBe("error");
    session.retry("row"); await session.prepare("row", read);
    expect(session.snapshot().row?.phase).toBe("ready");
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2);
    detachReplay(); await Promise.resolve();
    expect(transport.releaseAiAttachmentScope).toHaveBeenCalledOnce();
    const empty = setup(); empty.session.close();
    expect(empty.transport.releaseAiAttachmentScope).not.toHaveBeenCalled();
  });
});
