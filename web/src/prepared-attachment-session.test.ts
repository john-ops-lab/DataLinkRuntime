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
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2));
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
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce()); session.close();
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, failed) => { resolve = done; reject = failed; });
  return { promise, resolve, reject };
}

describe("same-session bounded preparation queue", () => {
  it("serializes batch requests and allows freezing only once both original files are ready", async () => {
    const { session, transport, read } = setup();
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(metadata({ attachment_ref: "second-reference" }));
    const one = session.prepare("one", read);
    const two = session.prepare("two", read);
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    expect(read).toHaveBeenCalledOnce();
    expect(() => session.freeze(["one", "two"])).toThrow("not prepared");
    first.resolve(metadata()); await one; await two;
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2);
    expect(session.freeze(["one", "two"]).map((ref) => ref.attachment_ref))
      .toEqual(["abcdefghijklmnop", "second-reference"]);
  });

  it("skips a removed queued row without letting its successor overtake the active request", async () => {
    const { session, transport, read } = setup();
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise);
    const one = session.prepare("one", read);
    const removed = session.prepare("removed", read);
    const three = session.prepare("three", read);
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    session.removeComposer("removed");
    await expect(removed).rejects.toMatchObject({ name: "AbortError" });
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    first.resolve(metadata()); await one; await three;
    expect(read).toHaveBeenCalledTimes(2);
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2);
    expect(session.snapshot().removed).toBeUndefined();
  });

  it("scope close rejects queued rows without reading them or issuing HTTP", async () => {
    const { session, transport, read } = setup();
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise);
    const one = session.prepare("one", read);
    const two = session.prepare("two", read);
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    session.close();
    expect((session as unknown as { preparationQueue: unknown[] }).preparationQueue).toEqual([]);
    expect((session as unknown as { activePreparation: unknown }).activePreparation).toBeNull();
    await expect(one).rejects.toMatchObject({ name: "AbortError" });
    await expect(two).rejects.toMatchObject({ name: "AbortError" });
    first.resolve(metadata()); await vi.waitFor(() => expect(transport.releasePreparedAiAttachment).toHaveBeenCalledOnce());
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
    expect(read).toHaveBeenCalledOnce();
    expect(session.snapshot()).toEqual({});
  });

  it("advances after an active failure without disguising genuine busy errors", async () => {
    const { session, transport, read } = setup();
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise);
    const one = session.prepare("one", read);
    const two = session.prepare("two", read);
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    const busy = { code: "ai_attachment_converter_busy" };
    first.reject(busy); await expect(one).rejects.toBe(busy); await two;
    expect(session.snapshot().one?.phase).toBe("error");
    expect(session.snapshot().one?.error).toBe(busy);
    expect(session.snapshot().two?.phase).toBe("ready");
  });

  it("advances after active cancellation even with a non-cooperative transport and cleans a late ref", async () => {
    const { session, transport, read } = setup();
    const first = deferred<AiPreparedAttachment>();
    transport.prepareAiAttachment.mockReturnValueOnce(first.promise);
    const one = session.prepare("one", read);
    const two = session.prepare("two", read);
    await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
    session.removeComposer("one");
    await expect(one).rejects.toMatchObject({ name: "AbortError" }); await two;
    expect(transport.prepareAiAttachment).toHaveBeenCalledTimes(2);
    expect(transport.prepareAiAttachment.mock.calls[0]?.[3]?.aborted).toBe(true);
    first.resolve(metadata({ attachment_ref: "cancelled-late-ref" }));
    await vi.waitFor(() => expect(transport.releasePreparedAiAttachment)
      .toHaveBeenCalledWith(7, "scope-id", "cancelled-late-ref"));
    expect(session.snapshot().one).toBeUndefined();
    expect(session.snapshot().two?.phase).toBe("ready");
  });
});

it("bounds live preparation tasks across repeated queued add/delete and drops cancelled closures immediately", async () => {
  const { session, transport, read } = setup();
  const active = deferred<AiPreparedAttachment>();
  transport.prepareAiAttachment.mockReturnValueOnce(active.promise);
  const first = session.prepare("active", read);
  await vi.waitFor(() => expect(transport.prepareAiAttachment).toHaveBeenCalledOnce());
  // Inspect retained tasks: a composer-count assertion alone cannot detect
  // File/read closures retained behind a long active conversion.
  const registry = session as unknown as {
    preparationQueue: { run: unknown; resolve: unknown; reject: unknown; onAbort: unknown }[];
    activePreparation: unknown;
  };
  const deletedReads: ReturnType<typeof vi.fn>[] = [];
  for (let round = 0; round < 25; round += 1) {
    const rows = Array.from({ length: 7 }, (_, index) => `queued-${round}-${index}`);
    const pending = rows.map((id) => {
      const neverRead = vi.fn(read); deletedReads.push(neverRead);
      return session.prepare(id, neverRead);
    });
    expect(registry.preparationQueue).toHaveLength(7);
    expect(registry.activePreparation).not.toBeNull();
    const retained = [...registry.preparationQueue];
    await expect(session.prepare(`overflow-${round}`, read)).rejects.toMatchObject({ code: "ai_attachment_count_exceeded" });
    session.removeComposer(`overflow-${round}`);
    rows.forEach((id) => session.removeComposer(id));
    for (const promise of pending) await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(registry.preparationQueue).toHaveLength(0);
    for (const job of retained) expect(job).toMatchObject({ run: null, resolve: null, reject: null, onAbort: null });
    expect(transport.prepareAiAttachment).toHaveBeenCalledOnce();
  }
  deletedReads.forEach((neverRead) => expect(neverRead).not.toHaveBeenCalled());
  const secondRead = vi.fn(async () => ({ ...(await read()), filename: "second.pdf" }));
  const thirdRead = vi.fn(async () => ({ ...(await read()), filename: "third.pdf" }));
  const second = session.prepare("second", secondRead);
  const third = session.prepare("third", thirdRead);
  expect(registry.preparationQueue).toHaveLength(2);
  active.resolve(metadata()); await first; await second; await third;
  expect(transport.prepareAiAttachment.mock.calls.map((call) => call[2].filename))
    .toEqual(["sample.pdf", "second.pdf", "third.pdf"]);
  expect(registry.preparationQueue).toHaveLength(0);
  expect(registry.activePreparation).toBeNull();
  session.close();
  expect(session.snapshot()).toEqual({});
  expect(registry.preparationQueue).toHaveLength(0);
});
