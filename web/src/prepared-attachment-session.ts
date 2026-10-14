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

interface PreparationJob {
  signal: AbortSignal;
  run: (() => Promise<AiPreparedAttachment>) | null;
  resolve: ((value: AiPreparedAttachment) => void) | null;
  reject: ((error: unknown) => void) | null;
  onAbort: (() => void) | null;
}

const MAX_LIVE_PREPARATIONS = 8;

/** This registry owns resources only. Visible rounds/Candidates stay in the panel. */
export class PreparedAttachmentSession {
  private entries = new Map<string, Entry>();
  private listeners = new Set<() => void>();
  private state: Readonly<Record<string, PreparedAttachmentState>> = {};
  private closed = false;
  private mounts = 0;
  private used = false;
  private preparationQueue: PreparationJob[] = [];
  private activePreparation: PreparationJob | null = null;

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

  private settlePreparation(job: PreparationJob, value: AiPreparedAttachment | null, error?: unknown) {
    if (job.onAbort !== null) job.signal.removeEventListener("abort", job.onAbort);
    const resolve = job.resolve;
    const reject = job.reject;
    job.run = null;
    job.onAbort = null;
    job.resolve = null;
    job.reject = null;
    if (value !== null) resolve?.(value);
    else reject?.(error);
    if (this.activePreparation === job) this.activePreparation = null;
    this.startNextPreparation();
  }

  private startNextPreparation() {
    if (this.closed || this.activePreparation !== null) return;
    const job = this.preparationQueue.shift();
    if (job === undefined) return;
    this.activePreparation = job;
    const run = job.run!;
    job.run = null;
    // Keep only the running operation's closure. A cancelled queued job is
    // removed immediately; cancelled active work still owns late-ref cleanup.
    void Promise.resolve().then(() => {
      job.signal.throwIfAborted();
      return run();
    }).then(
      (value) => this.settlePreparation(job, value),
      (error: unknown) => this.settlePreparation(job, null, error),
    );
  }

  private enqueuePreparation(entry: Entry, run: () => Promise<AiPreparedAttachment>) {
    if (this.preparationQueue.length + (this.activePreparation === null ? 0 : 1) >= MAX_LIVE_PREPARATIONS) {
      return Promise.reject(new ApiError(422, "ai_attachment_count_exceeded", "Too many pending attachments"));
    }
    return new Promise<AiPreparedAttachment>((resolve, reject) => {
      const job: PreparationJob = { signal: entry.controller.signal, run, resolve, reject, onAbort: null };
      job.onAbort = () => {
        const index = this.preparationQueue.indexOf(job);
        if (index !== -1) this.preparationQueue.splice(index, 1);
        this.settlePreparation(job, null, new DOMException("Preparation cancelled", "AbortError"));
      };
      job.signal.addEventListener("abort", job.onAbort, { once: true });
      this.preparationQueue.push(job);
      this.startNextPreparation();
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
    }).catch((error: unknown) => {
      if (this.entries.get(id) === entry && !controller.signal.aborted) {
        this.update(id, { phase: "error", metadata: null, error });
      }
      throw error;
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
      // Freeze identity only. Assist can renew the server TTL after prepare;
      // the authenticated GET remains authoritative for reference validity.
      return { attachment_ref: metadata.attachment_ref, sha256: metadata.sha256,
        representation_revision: metadata.representation_revision };
    });
    entries.forEach((entry) => { entry!.frozen += 1; });
    return refs;
  }

  async validate(refs: readonly AiPreparedAttachmentRef[], signal: AbortSignal) {
    signal.throwIfAborted();
    for (const ref of refs) {
      const match = [...this.entries].find(([, entry]) => entry.metadata?.attachment_ref === ref.attachment_ref);
      if (this.closed || match?.[1].metadata == null) {
        throw new ApiError(410, "ai_attachment_expired", "Upload the file again");
      }
      const [id, entry] = match;
      const validationSignal = AbortSignal.any([signal, entry.controller.signal]);
      validationSignal.throwIfAborted();
      try {
        const actual = await this.transport.getPreparedAiAttachment(this.adapterId, this.scopeId, ref.attachment_ref, validationSignal);
        validationSignal.throwIfAborted();
        if (this.closed || this.entries.get(id) !== entry) {
          throw new ApiError(410, "ai_attachment_expired", "Upload the file again");
        }
        if (actual.attachment_ref !== ref.attachment_ref || actual.sha256 !== ref.sha256
          || actual.representation_revision !== ref.representation_revision) {
          throw new ApiError(409, "ai_attachment_expired", "Frozen attachment representation changed");
        }
        entry.metadata = actual;
        this.update(id, { phase: "ready", metadata: actual, error: null });
      } catch (error) {
        validationSignal.throwIfAborted();
        if (error instanceof ApiError && error.code === "ai_attachment_expired" && this.entries.get(id) === entry) {
          this.update(id, { phase: "expired", metadata: entry.metadata, error });
        }
        throw error;
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
      // A frozen round may have renewed its TTL during Assist after the last
      // metadata GET. Only a server check can declare that reference expired.
      if (entry.frozen === 0 && entry.active === 0 && entry.metadata !== null && Date.parse(entry.metadata.expires_at) <= Date.now()
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
    this.preparationQueue = [];
    this.state = {};
    this.listeners.clear();
    if (this.used) void this.transport.releaseAiAttachmentScope(this.adapterId, this.scopeId).catch(() => {});
  }
}
