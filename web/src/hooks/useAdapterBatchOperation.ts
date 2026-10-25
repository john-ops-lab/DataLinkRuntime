import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { ADAPTER_BATCH_LIMIT, type AdapterBatchAction, type AdapterBatchOperation, type AdapterBatchRequest, type AdapterBatchTarget } from "../adapter-batch-types";
import { isTerminal } from "../status";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Store only an operation identity; server reads remain authoritative after reload. */
export function adapterBatchStorageKey(scope: string): string {
  return `dlr:adapter-batch:v1:${scope}`;
}

function readId(key: string): string | null {
  try {
    const id = localStorage.getItem(key);
    return id !== null && UUID.test(id) ? id : null;
  } catch { return null; }
}

function saveId(key: string, id: string): void {
  try { localStorage.setItem(key, id); } catch { /* Operation remains usable in this tab. */ }
}

export function useAdapterBatchOperation(options: { scope: string; onChanged: () => Promise<void> }) {
  const key = adapterBatchStorageKey(options.scope);
  const [operationId, setOperationId] = useState<string | null>(() => readId(key));
  const [operation, setOperation] = useState<AdapterBatchOperation | null>(null);
  const [open, setOpen] = useState(false);
  const [working, setWorking] = useState<"prepare" | "execute" | "retry" | null>(null);
  const [querying, setQuerying] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const session = useRef({ key, generation: 0, id: operationId, mutating: false, querying: false, readEpoch: 0 });
  const preparation = useRef<AdapterBatchRequest | null>(null);
  const lastObserved = useRef<string | null>(null);
  const onChanged = useRef(options.onChanged);
  useEffect(() => { onChanged.current = options.onChanged; }, [options.onChanged]);

  const query = useCallback(async () => {
    const current = session.current;
    if (current.id === null || current.querying) return;
    const id = current.id;
    const epoch = ++current.readEpoch;
    current.querying = true;
    setQuerying(true);
    const isCurrent = () => session.current === current && current.id === id && current.readEpoch === epoch;
    try {
      const result = await api.getAdapterBatch(id);
      if (!isCurrent()) return;
      setOperation(result);
      setUnknown(false);
      setMissing(false);
      setError(null);
      const signature = JSON.stringify(result);
      if (signature !== lastObserved.current) {
        lastObserved.current = signature;
        void onChanged.current().catch(() => undefined);
      }
    } catch (reason) {
      if (!isCurrent()) return;
      setError(reason);
      const notFound = reason instanceof ApiError && reason.status === 404;
      setMissing(notFound);
      setUnknown(!notFound);
      if (notFound || reason instanceof ApiError && [401, 403].includes(reason.status)) setOperation(null);
    } finally {
      current.querying = false;
      if (session.current === current) setQuerying(false);
    }
  }, []);

  useLayoutEffect(() => {
    const next = { key, generation: session.current.generation + 1, id: readId(key), mutating: false, querying: false, readEpoch: 0 };
    session.current = next;
    preparation.current = null;
    lastObserved.current = null;
    // A changed login scope must discard the preceding account's in-memory receipt.
    setOperationId(next.id);
    setOperation(null);
    setOpen(false);
    setWorking(null);
    setQuerying(false);
    setUnknown(next.id !== null);
    setMissing(false);
    setError(null);
    if (next.id !== null) void query();
    return () => {
      if (session.current === next) session.current = { ...next, generation: next.generation + 1 };
    };
  }, [key, query]);

  const close = useCallback(() => setOpen(false), []);

  const prepare = async (action: AdapterBatchAction, targets: AdapterBatchTarget[]) => {
    const current = session.current;
    if (current.key !== key || current.mutating || current.querying || unknown
      || targets.length === 0 || targets.length > ADAPTER_BATCH_LIMIT
      || new Set(targets.map(target => target.adapter_id)).size !== targets.length
      || targets.some(target => !target.expected_token)) return;
    const request: AdapterBatchRequest = {
      id: crypto.randomUUID(), action, targets: targets.map(target => ({ ...target })),
    };
    preparation.current = request;
    current.id = request.id;
    current.readEpoch += 1;
    saveId(key, request.id);
    setOperationId(request.id);
    setOperation(null);
    setOpen(true);
    await submitPreparation(request);
  };

  async function submitPreparation(request: AdapterBatchRequest) {
    const current = session.current;
    if (current.mutating || current.id !== request.id) return;
    current.mutating = true;
    current.readEpoch += 1;
    setWorking("prepare");
    setError(null);
    setMissing(false);
    try {
      const result = await api.prepareAdapterBatch(request);
      if (session.current !== current || current.id !== request.id) return;
      current.readEpoch += 1;
      setOperation(result);
      lastObserved.current = JSON.stringify(result);
      setUnknown(false);
    } catch (reason) {
      if (session.current !== current) return;
      setError(reason);
      setUnknown(true);
    } finally {
      current.mutating = false;
      if (session.current === current) setWorking(null);
    }
  }

  async function mutate(kind: "execute" | "retry") {
    const current = session.current;
    if (current.key !== key || current.mutating || current.querying || unknown || missing
      || operation === null || current.id !== operation.id) return;
    const retries = operation.items.filter(item => item.status === "failed" && item.retryable).map(item => item.adapter_id);
    if (kind === "execute" && !operation.items.some(item => item.status === "pending")) return;
    if (kind === "retry" && retries.length === 0) return;
    current.mutating = true;
    current.readEpoch += 1;
    setWorking(kind);
    setError(null);
    try {
      const result = kind === "execute"
        ? await api.executeAdapterBatch(operation.id)
        : await api.retryAdapterBatch(operation.id, retries);
      if (session.current !== current) return;
      current.readEpoch += 1;
      setOperation(result);
      lastObserved.current = JSON.stringify(result);
      setUnknown(false);
    } catch (reason) {
      if (session.current !== current) return;
      setError(reason);
      setUnknown(true);
    } finally {
      current.mutating = false;
      if (session.current === current) {
        setWorking(null);
        // A failed catalog refresh never turns an accepted action into a failed action.
        void onChanged.current().catch(() => undefined);
      }
    }
  }

  const polling = open && !unknown && !missing && operation !== null && operation.items.some(item =>
    item.status === "pending" || item.status === "stopping"
      || item.status === "accepted" && item.action === "enable_subscription"
      || item.executions.some(execution => !isTerminal(execution.status)));
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => { if (document.visibilityState !== "hidden") void query(); }, 3000);
    return () => window.clearInterval(timer);
  }, [polling, query]);

  return {
    operationId, operation, open, working, querying, unknown, missing, error,
    canPrepare: working === null && !querying && !unknown,
    prepare,
    execute: () => mutate("execute"),
    retry: () => mutate("retry"),
    query,
    recoverPreparation: missing && preparation.current?.id === operationId
      ? () => submitPreparation(preparation.current!) : null,
    show: () => { setOpen(true); if (working === null) void query(); },
    close,
  };
}

export type AdapterBatchController = ReturnType<typeof useAdapterBatchOperation>;
