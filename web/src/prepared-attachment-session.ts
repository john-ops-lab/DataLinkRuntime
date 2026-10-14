import { ApiError } from "./api";
import type { AiAttachment, AiPreparedAttachment, AiPreparedAttachmentRef } from "./types";

export interface PreparedAttachmentTransport {
  prepareAiAttachment: (adapterId: number, scopeId: string, attachment: AiAttachment, signal?: AbortSignal) => Promise<AiPreparedAttachment>;
  getPreparedAiAttachment: (adapterId: number, scopeId: string, ref: string, signal?: AbortSignal) => Promise<AiPreparedAttachment>;
  releasePreparedAiAttachment: (adapterId: number, scopeId: string, ref: string) => Promise<void>;
  releaseAiAttachmentScope: (adapterId: number, scopeId: string) => Promise<void>;
}

export interface PreparedAttachmentState {
  phase: "preparing" | "ready" | "error" | "expired";
  metadata: AiPreparedAttachment | null;
  error: unknown;
}

interface Entry {
  composer: boolean;
  frozen: number;
  active: number;
  controller: AbortController;
  read: () => Promise<AiAttachment>;
  promise?: Promise<AiPreparedAttachment>;
  metadata: AiPreparedAttachment | null;
}

/** This registry owns resources only. Visible rounds/Candidates stay in the panel. */
export class PreparedAttachmentSession {
  private entries = new Map<string, Entry>();
  private listeners = new Set<() => void>();
  private state: Readonly<Record<string, PreparedAttachmentState>> = {};
  private closed = false;
  private mounts = 0;
  private used = false;
  private prepareTail: Promise<void> = Promise.resolve();

  mount() {
    this.mounts += 1;
    return () => {
      this.mounts -= 1;
      queueMicrotask(() => { if (this.mounts === 0) this.close(); });
    };
  }

  constructor(readonly adapterId: number, readonly scopeId: string, private transport: PreparedAttachmentTransport, readonly ownerKey = "") {}

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  snapshot = () => this.state;

  private update(id: string, state: PreparedAttachmentState | null) {
    if (this.closed) return;
    const next = { ...this.state };
    if (state === null) delete next[id];
    else next[id] = state;
    this.state = next;
    this.listeners.forEach((listener) => listener());
  }

  private enqueuePreparation(entry: Entry, run: () => Promise<AiPreparedAttachment>) {
    const signal = entry.controller.signal;
    let rejectAbort!: (error: DOMException) => void;
    const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
    const onAbort = () => rejectAbort(new DOMException("Preparation cancelled", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    // Cancelled queued rows stay behind their predecessor until their turn,
    // so removing one cannot let later rows overtake an active conversion.
    const queued = this.prepareTail.then(() => {
      signal.throwIfAborted();
      return Promise.race([run(), aborted]);
    });
    this.prepareTail = queued.then(() => {}, () => {});
    // An abort releases an active slot even if a transport ignores its signal.
    // The original run still fences and releases any late returned reference.
    return Promise.race([queued, aborted]).finally(() => {
      signal.removeEventListener("abort", onAbort);
    });
  }

  prepare(id: string, read: () => Promise<AiAttachment>, retry = false): Promise<AiPreparedAttachment> {
    if (this.closed) return Promise.reject(new DOMException("Scope closed", "AbortError"));
    const previous = this.entries.get(id);
    if (previous !== undefined && !retry) return previous.promise!;
    if (previous !== undefined && (previous.frozen > 0 || previous.active > 0)) {
      return Promise.reject(new ApiError(410, "ai_attachment_expired", "Upload the file again"));
    }
    if (previous !== undefined) this.removeComposer(id);
    const controller = new AbortController();
    const entry: Entry = { composer: true, frozen: 0, active: 0, controller, read,
      metadata: null };
    this.entries.set(id, entry);
    this.update(id, { phase: "preparing", metadata: null, error: null });
    entry.promise = this.enqueuePreparation(entry, async () => {
      try {
        const wire = await read();
        controller.signal.throwIfAborted();
        this.used = true;
        const prepared = await this.transport.prepareAiAttachment(this.adapterId, this.scopeId, wire, controller.signal);
        entry.metadata = prepared;
        if (this.closed || controller.signal.aborted || this.entries.get(id) !== entry || !entry.composer) {
          void this.transport.releasePreparedAiAttachment(this.adapterId, this.scopeId, prepared.attachment_ref).catch(() => {});
          throw new DOMException("Preparation cancelled", "AbortError");
        }
        this.update(id, { phase: "ready", metadata: prepared, error: null });
        return prepared;
      } catch (error) {
        if (this.entries.get(id) === entry && !controller.signal.aborted) {
          this.update(id, { phase: "error", metadata: null, error });
        }
        throw error;
      }
    });
    // Automatic preparation may finish before the user sends; error rows remain retryable.
    void entry.promise.catch(() => {});
    return entry.promise;
  }

  retry(id: string) {
    const entry = this.entries.get(id);
    if (entry === undefined || this.state[id]?.phase !== "error") return;
    void this.prepare(id, entry.read, true).catch(() => {});
  }

  removeComposer(id: string) {
    const entry = this.entries.get(id);
    if (entry === undefined) return;
    entry.composer = false;
    if (entry.frozen > 0 || entry.active > 0) return;
    entry.controller.abort();
    this.entries.delete(id);
    this.update(id, null);
    if (entry.metadata !== null) {
      void this.transport.releasePreparedAiAttachment(this.adapterId, this.scopeId, entry.metadata.attachment_ref).catch(() => {});
    }
  }

  freeze(ids: readonly string[]): AiPreparedAttachmentRef[] {
    const entries = ids.map((id) => this.entries.get(id));
    if (entries.some((entry) => entry?.metadata == null)) throw new Error("Attachment is not prepared");
    const refs = entries.map((entry) => {
      const metadata = entry!.metadata!;
      if (Date.parse(metadata.expires_at) <= Date.now()) {
        throw new ApiError(410, "ai_attachment_expired", "Upload the file again");
      }
      return { attachment_ref: metadata.attachment_ref, sha256: metadata.sha256,
        representation_revision: metadata.representation_revision };
    });
    entries.forEach((entry) => { entry!.frozen += 1; });
    return refs;
  }

  async validate(refs: readonly AiPreparedAttachmentRef[], signal: AbortSignal) {
    for (const ref of refs) {
      const entry = [...this.entries.values()].find((item) => item.metadata?.attachment_ref === ref.attachment_ref);
      if (this.closed || entry?.metadata == null || Date.parse(entry.metadata.expires_at) <= Date.now()) {
        throw new ApiError(410, "ai_attachment_expired", "Upload the file again");
      }
      const actual = await this.transport.getPreparedAiAttachment(this.adapterId, this.scopeId, ref.attachment_ref, signal);
      if (actual.attachment_ref !== ref.attachment_ref || actual.sha256 !== ref.sha256
        || actual.representation_revision !== ref.representation_revision) {
        throw new ApiError(409, "ai_attachment_expired", "Frozen attachment representation changed");
      }
    }
  }

  retainActive(refs: readonly AiPreparedAttachmentRef[]) {
    const entries = [...this.entries.values()].filter((entry) => refs.some((ref) => ref.attachment_ref === entry.metadata?.attachment_ref));
    entries.forEach((entry) => { entry.active += 1; });
    return () => { entries.forEach((entry) => { entry.active = Math.max(0, entry.active - 1); }); };
  }

  checkExpired() {
    for (const [id, entry] of this.entries) {
      if (entry.active === 0 && entry.metadata !== null && Date.parse(entry.metadata.expires_at) <= Date.now()
        && this.state[id]?.phase === "ready") {
        this.update(id, { phase: "expired", metadata: entry.metadata, error: null });
      }
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.entries.forEach((entry) => entry.controller.abort());
    this.entries.clear();
    this.state = {};
    this.listeners.clear();
    if (this.used) void this.transport.releaseAiAttachmentScope(this.adapterId, this.scopeId).catch(() => {});
  }
}
