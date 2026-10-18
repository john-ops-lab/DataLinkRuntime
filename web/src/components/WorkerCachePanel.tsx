import { ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type Key } from "react";
import { useTranslation } from "react-i18next";

import "./admin-ux.css";

import { api, ApiError } from "../api";
import type {
  Adapter,
  CacheAdminView,
  CacheKeySelection,
  CacheOperation,
  CacheOperationCreate,
  CacheProtectSelection,
  CacheSnapshotItem,
  Worker,
} from "../types";

const CATEGORY_NAMES = ["versions", "shared", "staging", "trash", "unknown"] as const;
const TERMINAL_STATUSES = new Set(["completed", "failed"]);
const POLL_INTERVAL_MS = 1_000;
const POLL_LIMIT_MS = 120_000;

interface WorkerCachePanelProps {
  worker: Worker;
  adapters?: Adapter[];
  open: boolean;
  onClose: () => void;
}

interface ProofDraft {
  sourcePolicy: "verified_offline" | "managed_online";
  note: string;
  hours: number;
}

type CacheOperationDraft =
  | { kind: "preview" | "clean"; keys: CacheKeySelection[] }
  | { kind: "protect"; protect: CacheProtectSelection[] }
  | {
      kind: "retry";
      management_operation_id?: string;
      guard_operation_id?: string;
      cleanup_id?: number;
    };

interface PendingSubmission {
  payload: CacheOperationCreate;
  family: string;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function summaryIsKnown(view: CacheAdminView | null): boolean {
  return view?.summary != null && view.status !== "owner_unconfirmed";
}

function formatPolicyBoolean(value: unknown, t: (key: string) => string): string {
  return typeof value === "boolean" ? t(value ? "cache.enabled" : "cache.disabled") : "—";
}

function formatBytes(value: unknown): string {
  const bytes = finiteNumber(value);
  if (bytes === null || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = bytes;
  let unit = "B";
  for (const next of units) {
    amount /= 1024;
    unit = next;
    if (amount < 1024 || next === units[units.length - 1]) break;
  }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function formatTime(value: string | null, locale: "zh-CN" | "en"): string {
  if (value === null) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function newIdempotencyKey(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function operationFingerprint(payload: CacheOperationDraft): string {
  return JSON.stringify(payload);
}

function operationFamily(payload: CacheOperationDraft): string {
  if (payload.kind !== "retry") return payload.kind === "protect"
    ? `protect-${payload.protect.some((item) => item.pinned !== undefined) ? "pin" : "proof"}`
    : payload.kind;
  if (payload.management_operation_id !== undefined) return "retry-management";
  if (payload.guard_operation_id !== undefined) return "retry-guard";
  return "retry-cleanup";
}

function resultReasonEntries(operation: CacheOperation): Array<[string, string[]]> {
  const result = operation.result;
  if (result === null) return [];
  if ((operation.kind === "preview" || operation.kind === "retry") && Array.isArray(result.items)) {
    return result.items.flatMap((value) => {
      if (value === null || typeof value !== "object" || Array.isArray(value)) return [];
      const item = value as Record<string, unknown>;
      if (typeof item.cache_key !== "string" || !Array.isArray(item.reasons)) return [];
      const reasons = item.reasons.filter((reason): reason is string => typeof reason === "string");
      return [[item.cache_key, reasons] as [string, string[]]];
    });
  }
  if (operation.kind === "clean" || operation.kind === "retry") {
    const retained = result.retained_reasons;
    if (retained !== null && typeof retained === "object" && !Array.isArray(retained)) {
      return Object.entries(retained).flatMap(([reason, count]) => (
        typeof count === "number" && Number.isFinite(count) && count > 0
          ? [[String(count), [reason]] as [string, string[]]]
          : []
      ));
    }
  }
  return [];
}

export default function WorkerCachePanel({ worker, open, onClose, adapters = [] }: WorkerCachePanelProps) {
  const { i18n, t } = useTranslation("settings");
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const [view, setView] = useState<CacheAdminView | null>(null);
  const [operations, setOperations] = useState<CacheOperation[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<Key[]>([]);
  const [selectionTouched, setSelectionTouched] = useState(false);
  const [expandedAdapters, setExpandedAdapters] = useState<Key[]>([]);
  const [proofTarget, setProofTarget] = useState<CacheSnapshotItem | null>(null);
  const [cleanRequest, setCleanRequest] = useState<{
    workerId: number; operationId: string; items: CacheSnapshotItem[];
  } | null>(null);
  const [uncertainClean, setUncertainClean] = useState<{ workerId: number; items: CacheSnapshotItem[] } | null>(null);
  const interactionGeneration = useRef(0);
  const previewGeneration = useRef(0);
  const [cleanReview, setCleanReview] = useState<{
    workerId: number; items: CacheSnapshotItem[]; expiresAt: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingFailedCleanups, setLoadingFailedCleanups] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [proofOpen, setProofOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeOperation, setActiveOperation] = useState<CacheOperation | null>(null);
  const [pollStartedAt, setPollStartedAt] = useState(0);
  const [pollRevision, setPollRevision] = useState(0);
  const [pollTimedOut, setPollTimedOut] = useState(false);
  const [proof, setProof] = useState<ProofDraft>({
    sourcePolicy: "verified_offline",
    note: "",
    hours: 1,
  });
  const pendingSubmissions = useRef(new Map<string, PendingSubmission>());
  const cacheRequestGeneration = useRef(0);

  const stableError = useCallback((caught: unknown, fallbackKey = "requestFailed") => {
    if (caught instanceof ApiError) {
      return t(`cache.errors.${caught.code}`, {
        defaultValue: t(`cache.${fallbackKey}`),
      });
    }
    return t(`cache.${fallbackKey}`);
  }, [t]);

  const refreshCache = useCallback(async (preserveError = false) => {
    if (!open) return;
    const generation = ++cacheRequestGeneration.current;
    setLoading(true);
    if (!preserveError) setError(null);
    try {
      const nextView = await api.getWorkerCache(worker.id);
      if (cacheRequestGeneration.current !== generation) return;
      setView(nextView);
      setSelectedKeys([]);
      setCleanReview(null);
    } catch (caught) {
      if (cacheRequestGeneration.current === generation) {
        setError(stableError(caught, "loadFailed"));
      }
    } finally {
      if (cacheRequestGeneration.current === generation) setLoading(false);
    }
  }, [open, stableError, worker.id]);

  const load = useCallback(async () => {
    if (!open) return;
    const generation = ++cacheRequestGeneration.current;
    setLoading(true);
    setError(null);
    try {
      const [nextView, page] = await Promise.all([
        api.getWorkerCache(worker.id),
        api.listWorkerCacheOperations(worker.id, { limit: 20 }),
      ]);
      if (cacheRequestGeneration.current !== generation) return;
      setView(nextView);
      setOperations(page.items);
      setActiveOperation((current) => current === null ? null : page.items.find((item) => item.operation_id === current.operation_id) ?? current);
      setPollStartedAt(Date.now());
      setPollTimedOut(false);
      setPollRevision((current) => current + 1);
      setNextCursor(page.next_cursor);
      setSelectedKeys([]);
      setCleanReview(null);
    } catch (caught) {
      if (cacheRequestGeneration.current === generation) {
        setError(stableError(caught, "loadFailed"));
      }
    } finally {
      if (cacheRequestGeneration.current === generation) setLoading(false);
    }
  }, [open, stableError, worker.id]);

  useEffect(() => {
    interactionGeneration.current += 1;
    previewGeneration.current += 1;
    const timer = window.setTimeout(() => {
      setProofOpen(false);
      setProofTarget(null);
      setCleanRequest(null);
      setCleanReview(null);
      setSelectionTouched(false);
      setExpandedAdapters([]);
    }, 0);
    return () => { interactionGeneration.current += 1; window.clearTimeout(timer); };
  }, [open, worker.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setActiveOperation(null);
      setUncertainClean(null);
      pendingSubmissions.current.clear();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [worker.id]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, open]);

  useEffect(() => {
    if (
      !open
      || activeOperation === null
      || TERMINAL_STATUSES.has(activeOperation.status)
    ) return;
    let cancelled = false;
    const remaining = POLL_LIMIT_MS - (Date.now() - pollStartedAt);
    const timer = window.setTimeout(() => {
      if (remaining <= 0) {
        setPollTimedOut(true);
        return;
      }
      void api.getWorkerCacheOperation(worker.id, activeOperation.operation_id)
        .then((latest) => {
          if (cancelled) return;
          setActiveOperation(latest);
          setOperations((current) => [latest, ...current.filter(
            (operation) => operation.operation_id !== latest.operation_id,
          )]);
          if (TERMINAL_STATUSES.has(latest.status) && latest.kind !== "preview") void load();
        })
        .catch((caught) => {
          if (!cancelled) {
            setError(stableError(caught, "operationPollFailed"));
            setPollRevision((current) => current + 1);
          }
        });
    }, Math.max(0, Math.min(POLL_INTERVAL_MS, remaining)));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeOperation, load, open, pollRevision, pollStartedAt, stableError, worker.id]);

  const selectedItems = useMemo(() => {
    const selected = new Set(selectedKeys.map(String));
    return (view?.items ?? []).filter((item) => selected.has(item.cache_key));
  }, [selectedKeys, view?.items]);

  const proofItems = proofTarget === null ? [] : [proofTarget];
  const selectedProtectable = proofItems.filter((item) => (
    item.kind !== "staging" && item.identity !== null && item.digest !== null
  ));

  const submit = useCallback(async (draft: CacheOperationDraft, logicalFingerprint?: string) => {
    const generation = interactionGeneration.current;
    const fingerprint = logicalFingerprint ?? operationFingerprint(draft);
    const family = operationFamily(draft);
    let pending = pendingSubmissions.current.get(fingerprint);
    if (pending === undefined) {
      for (const [key, value] of pendingSubmissions.current) {
        if (value.family === family) pendingSubmissions.current.delete(key);
      }
      pending = undefined;
    }
    const payload: CacheOperationCreate = pending?.payload ?? {
      ...draft,
      idempotency_key: newIdempotencyKey(),
    } as CacheOperationCreate;
    pendingSubmissions.current.set(fingerprint, { payload, family });
    setSubmitting(true);
    setError(null);
    try {
      const operation = await api.createWorkerCacheOperation(worker.id, payload);
      pendingSubmissions.current.delete(fingerprint);
      if (generation !== interactionGeneration.current) return operation;
      setActiveOperation(operation);
      setPollStartedAt(Date.now());
      setPollRevision(0);
      setPollTimedOut(false);
      setOperations((current) => [operation, ...current.filter(
        (item) => item.operation_id !== operation.operation_id,
      )]);
      if (TERMINAL_STATUSES.has(operation.status) && operation.kind !== "preview") void load();
      return operation;
    } catch (caught) {
      if (caught instanceof ApiError && 0 < caught.status && caught.status < 500) {
        pendingSubmissions.current.delete(fingerprint);
      }
      if (generation !== interactionGeneration.current) return;
      setError(stableError(caught));
      if (caught instanceof ApiError && [
        "cache_snapshot_changed",
        "cache_snapshot_key_missing",
        "cache_snapshot_unavailable",
        "cache_operation_active",
      ].includes(caught.code)) {
        await refreshCache(true);
      }
    } finally {
      setSubmitting(false);
    }
  }, [load, refreshCache, stableError, worker.id]);

  const submitPin = useCallback((item: CacheSnapshotItem, pinned: boolean) => {
    if (item.pinned === pinned || item.kind === "staging" || item.identity === null || item.digest === null) return;
    void submit({ kind: "protect", protect: [{
      adapter_id: item.adapter_id,
      version_id: item.version_id,
      identity: item.identity,
      digest: item.digest,
      pinned,
    }] });
  }, [submit]);

  const submitProof = useCallback(() => {
    const note = proof.note.trim();
    if (selectedProtectable.length === 0 || selectedProtectable.length !== proofItems.length
        || !note || proof.hours <= 0 || proof.hours > 24) return;
    const validUntil = new Date(Date.now() + proof.hours * 60 * 60 * 1_000).toISOString();
    const protect: CacheProtectSelection[] = selectedProtectable.map((item) => ({
      adapter_id: item.adapter_id,
      version_id: item.version_id,
      identity: item.identity!,
      digest: item.digest!,
      source_policy: proof.sourcePolicy,
      evidence_note: note,
      valid_until: validUntil,
    }));
    const logicalProtect = selectedProtectable.map((item) => ({
      adapter_id: item.adapter_id,
      version_id: item.version_id,
      identity: item.identity,
      digest: item.digest,
      source_policy: proof.sourcePolicy,
      evidence_note: note,
      valid_hours: proof.hours,
    }));
    void submit({ kind: "protect", protect }, JSON.stringify({ kind: "protect-proof", protect: logicalProtect }));
  }, [proof, proofItems.length, selectedProtectable, submit]);

  const loadMoreOperations = useCallback(async () => {
    if (nextCursor === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listWorkerCacheOperations(worker.id, { cursor: nextCursor, limit: 20 });
      setOperations((current) => {
        const known = new Set(current.map((item) => item.operation_id));
        return [...current, ...page.items.filter((item) => !known.has(item.operation_id))];
      });
      setNextCursor(page.next_cursor);
    } catch (caught) {
      setError(stableError(caught, "operationsLoadFailed"));
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, nextCursor, stableError, worker.id]);

  const loadMoreFailedCleanups = useCallback(async () => {
    const cursor = view?.failed_cleanup_next_cursor;
    const sampleId = view?.sample_id;
    if (cursor == null || loadingFailedCleanups) return;
    const generation = cacheRequestGeneration.current;
    setLoadingFailedCleanups(true);
    try {
      const page = await api.getWorkerCache(worker.id, { failed_cursor: cursor, failed_limit: 50 });
      if (cacheRequestGeneration.current !== generation) return;
      if (page.sample_id !== sampleId) {
        const firstPage = await api.getWorkerCache(worker.id, { failed_limit: 50 });
        if (cacheRequestGeneration.current !== generation) return;
        setView(firstPage);
        setSelectedKeys([]);
      } else {
        setView((current) => {
          if (current === null || current.sample_id !== page.sample_id) return current;
          const known = new Set(current.failed_cleanup_items.map((item) => item.cleanup_id));
          return {
            ...current,
            failed_cleanup_items: [
              ...current.failed_cleanup_items,
              ...page.failed_cleanup_items.filter((item) => !known.has(item.cleanup_id)),
            ],
            failed_cleanup_next_cursor: page.failed_cleanup_next_cursor,
          };
        });
      }
    } catch (caught) {
      if (cacheRequestGeneration.current === generation) {
        setError(stableError(caught, "failedCleanupsLoadFailed"));
      }
    } finally {
      setLoadingFailedCleanups(false);
    }
  }, [loadingFailedCleanups, stableError, view?.failed_cleanup_next_cursor, view?.sample_id, worker.id]);

  const status = view?.status;
  const mutatingAllowed = status === "complete" || status === "incomplete";
  const capacityKnown = summaryIsKnown(view);
  const summary = view?.summary;
  const accounting = summary?.accounting;
  const policy = summary?.policy;
  const categories = capacityKnown ? summary?.categories : undefined;
  const estimated = capacityKnown ? CATEGORY_NAMES.reduce((total, name) => {
    return total + (categories?.[name].reclaimable_bytes ?? 0);
  }, 0) : null;

  function idleDuration(value: unknown): string {
    const seconds = integer(value);
    if (seconds === null) return "—";
    if (seconds % 86400 === 0) return t("cache.durationDays", { value: seconds / 86400 });
    if (seconds % 3600 === 0) return t("cache.durationHours", { value: seconds / 3600 });
    return t("cache.durationSeconds", { value: seconds });
  }

  const busy = loading || submitting || (activeOperation !== null && !TERMINAL_STATUSES.has(activeOperation.status));
  const canClean = (item: CacheSnapshotItem) => !item.pinned && item.reasons.length === 0
    && (item.kind === "staging" ? item.identity === null && item.digest === null
      : item.rebuildability === "confirmed" && item.identity !== null && item.digest !== null
        && item.identity.adapter_id === item.adapter_id && item.identity.version_id === item.version_id);
  const cleanableItems = (view?.items ?? []).filter(canClean);
  const cleanTargets = selectionTouched ? selectedItems.filter(canClean) : cleanableItems;
  const adapterName = (id: number) => adapters.find((adapter) => adapter.id === id)?.name
    ?? t("managedInput.adapterFallback", { id });
  const versionName = (item: CacheSnapshotItem) => item.kind === "staging"
    ? t("cache.itemKinds.staging")
    : item.version_seq != null ? t("cache.versionLabel", { version: item.version_seq }) : t("cache.versionUnknown");
  const targetName = (item: CacheSnapshotItem) => `${adapterName(item.adapter_id)} · ${versionName(item)}`;
  const groups = Array.from(new Set((view?.items ?? []).map((item) => item.adapter_id))).map((id) => ({
    adapter_id: id,
    items: (view?.items ?? []).filter((item) => item.adapter_id === id),
  }));

  const startCleanPreview = async () => {
    if (!mutatingAllowed || busy || cleanTargets.length === 0) return;
    const items = [...cleanTargets];
    const generation = previewGeneration.current;
    const operation = await submit({ kind: "preview", keys: items.map(({ adapter_id, version_id }) => ({ adapter_id, version_id })) });
    if (operation !== undefined && generation === previewGeneration.current) setCleanRequest({ workerId: worker.id, operationId: operation.operation_id, items });
  };

  useEffect(() => {
    if (!open || cleanRequest === null || cleanRequest.workerId !== worker.id || activeOperation?.operation_id !== cleanRequest.operationId
        || !TERMINAL_STATUSES.has(activeOperation.status)) return;
    const result = activeOperation.result;
    const observations = result?.items;
    const expected = new Map(cleanRequest.items.map((item) => [item.cache_key, item]));
    const valid = activeOperation.status === "completed" && result?.complete === true && Array.isArray(observations)
      && observations.length === expected.size && observations.every((value) => {
        if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
        const observed = value as CacheSnapshotItem;
        const original = expected.get(observed.cache_key);
        if (original === undefined || observed.kind !== original.kind || observed.adapter_id !== original.adapter_id || observed.version_id !== original.version_id
          || observed.digest !== original.digest || (observed.identity?.language !== original.identity?.language
            || observed.identity?.source_sha256 !== original.identity?.source_sha256
            || observed.identity?.adapter_id !== original.identity?.adapter_id
            || observed.identity?.version_id !== original.identity?.version_id)
          || !Array.isArray(observed.reasons) || observed.reasons.length !== 0 || observed.pinned !== false
          || (observed.kind === "staging" && observed.identity !== null)
          || (observed.kind !== "staging" && observed.rebuildability !== "confirmed")) return false;
        expected.delete(observed.cache_key);
        return true;
      });
    const timer = window.setTimeout(() => {
      setCleanRequest(null);
      if (valid) setCleanReview({ workerId: worker.id, items: cleanRequest.items, expiresAt: Date.now() + 30_000 });
      else {
        setError(t("cache.previewChanged"));
        void refreshCache(true);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [activeOperation, cleanRequest, open, refreshCache, t, worker.id]);

  useEffect(() => {
    if (cleanReview === null) return;
    const timer = window.setTimeout(() => {
      setCleanReview(null);
      setError(t("cache.previewExpired"));
    }, Math.max(0, cleanReview.expiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [cleanReview, t]);

  const confirmClean = async () => {
    if (cleanReview === null || cleanReview.workerId !== worker.id || !mutatingAllowed || busy) return;
    if (Date.now() >= cleanReview.expiresAt) {
      setCleanReview(null);
      setError(t("cache.previewExpired"));
      return;
    }
    const keys = cleanReview.items.map(({ adapter_id, version_id }) => ({ adapter_id, version_id }));
    const request = { workerId: worker.id, items: cleanReview.items };
    const draft = { kind: "clean" as const, keys };
    const generation = interactionGeneration.current;
    setCleanReview(null);
    await submit(draft);
    if (generation === interactionGeneration.current && pendingSubmissions.current.has(operationFingerprint(draft))) setUncertainClean(request);
  };

  const retryCleanSubmission = async () => {
    if (uncertainClean === null || uncertainClean.workerId !== worker.id || busy || !mutatingAllowed) return;
    const draft = { kind: "clean" as const, keys: uncertainClean.items.map(({ adapter_id, version_id }) => ({ adapter_id, version_id })) };
    const generation = interactionGeneration.current;
    await submit(draft);
    if (generation === interactionGeneration.current && !pendingSubmissions.current.has(operationFingerprint(draft))) setUncertainClean(null);
  };

  const cacheColumns: TableColumnsType<CacheSnapshotItem> = [
    {
      title: t("cache.version"), key: "cache_key", render: (_, item) => (
        <div>
          <Typography.Text>{versionName(item)}</Typography.Text>
          <details className="admin-help cache-source-details">
            <summary>{t("cache.sourceDetails")}</summary>
            <Typography.Text>{item.cache_key}</Typography.Text>
            <p>{item.identity?.language ?? "—"}</p>
            <p>{t(`cache.rebuildability.${item.rebuildability}`)}</p>
            {item.kind !== "staging" && <Button size="small" disabled={busy || !mutatingAllowed || item.identity === null || item.digest === null}
              onClick={() => { setProofTarget(item); setProofOpen(true); }}>{t("cache.proofDetails")}</Button>}
          </details>
        </div>
      ),
    },
    { title: t("cache.bytes"), dataIndex: "bytes", key: "bytes", render: formatBytes },
    {
      title: t("cache.currentRestrictions"), key: "restrictions", render: (_, item) => {
        const reasons = new Set(item.reasons);
        if (item.pinned) reasons.add("cache_pinned");
        if (item.kind !== "staging" && item.rebuildability !== "confirmed" && !reasons.has("cache_rebuild_unavailable")) reasons.add("cache_rebuild_unknown");
        if (reasons.has("cache_rebuild_unavailable")) reasons.delete("cache_rebuild_unknown");
        return reasons.size === 0 ? <Typography.Text type="secondary">{t("cache.cleanable")}</Typography.Text>
          : <Space size={[4, 4]} wrap>{Array.from(reasons).map((reason) => <Tag key={reason}>{t(`cache.reasonsMap.${reason}`, {
            defaultValue: t("cache.protectedUnknown"),
          })}</Tag>)}</Space>;
      },
    },
    {
      title: t("cache.pinned"), key: "pinned", render: (_, item) => <Switch
        size="small" checked={item.pinned} aria-label={t("cache.pinLabel", { target: targetName(item) })}
        disabled={busy || !mutatingAllowed || item.kind === "staging" || item.identity === null || item.digest === null}
        onChange={(pinned) => submitPin(item, pinned)} />,
    },
  ];

  const operationColumns: TableColumnsType<CacheOperation> = [
    { title: t("cache.operationTime"), key: "time", render: (_, operation) => formatTime(operation.created_at, locale) },
    {
      title: t("cache.operation"), key: "operation", render: (_, operation) => <div>
        {t(`cache.operationKinds.${operation.kind}`)}
        <details className="admin-help cache-source-details"><summary>{t("cache.recordDetails")}</summary>
          <Typography.Text copyable>{operation.operation_id}</Typography.Text>
          {resultReasonEntries(operation).map(([subject, reasons], index) => <p key={`${subject}-${index}`}>{reasons.map((reason) => t(`cache.reasonsMap.${reason}`, {
            defaultValue: t("cache.protectedUnknown"),
          })).join("、") || t("cache.cleanable")}</p>)}
        </details>
      </div>,
    },
    {
      title: t("cache.actualResult"), key: "result", render: (_, operation) => {
        const result = operation.result;
        const deleted = finiteNumber(result?.deleted);
        const changed = finiteNumber(result?.changed);
        return <Space direction="vertical" size={0}>
          <Typography.Text type={operation.status === "failed" ? "danger" : undefined}>{t(`cache.operationStatuses.${operation.status}`)}</Typography.Text>
          {deleted !== null && <span>{t("cache.cleanupResult", { count: deleted, bytes: formatBytes(result?.freed_bytes) })}</span>}
          {changed !== null && <span>{t("cache.changedResult", { count: changed })}</span>}
          {operation.error_code && <Typography.Text type="danger">{t(`cache.errors.${operation.error_code}`, { defaultValue: t("cache.operationFailed") })}</Typography.Text>}
        </Space>;
      },
    },
  ];

  type Failure = { key: string; subject: string; error: string | null; details: string; retry?: CacheOperationDraft; route?: string; retryDisabled?: boolean };
  const completedRetries = new Set(operations.filter((operation) => operation.kind === "retry"
    && operation.target_kind === "management" && operation.status === "completed").map((operation) => operation.target_operation_id));
  const activeRetries = new Set(operations.filter((operation) => operation.kind === "retry"
    && operation.target_kind === "management" && !TERMINAL_STATUSES.has(operation.status)).map((operation) => operation.target_operation_id));
  const actionableManagementFailure = (operation: CacheOperation) => operation.status === "failed"
    && (typeof operation.retryable === "boolean" ? operation.retryable : !completedRetries.has(operation.operation_id));
  const failures: Failure[] = [
    ...operations.filter(actionableManagementFailure).map((operation): Failure => ({
      key: `operation-${operation.operation_id}`, subject: t(`cache.operationKinds.${operation.kind}`), error: operation.error_code,
      details: operation.operation_id, retry: { kind: "retry", management_operation_id: operation.operation_id },
      retryDisabled: activeRetries.has(operation.operation_id),
    })),
    ...(view?.failed_guard_items ?? []).filter((item) => item.management_operation_retryable !== false
      && (!item.management_operation_id || (item.management_operation_retryable === true || !completedRetries.has(item.management_operation_id))
        && !operations.some((operation) => operation.operation_id === item.management_operation_id && actionableManagementFailure(operation))))
      .map((item): Failure => ({
      key: `guard-${item.guard_operation_id}`, subject: adapterName(item.adapter_id), error: item.error_code,
      details: `${item.guard_operation_id} · ${item.generation} · ${item.resume_phase}`,
      ...(item.management_operation_id ? {
        retry: { kind: "retry", management_operation_id: item.management_operation_id } as CacheOperationDraft,
        retryDisabled: item.management_operation_status !== "failed" || activeRetries.has(item.management_operation_id),
      } : item.operation_kind === "gc" ? { retry: { kind: "retry", guard_operation_id: item.guard_operation_id } as CacheOperationDraft }
        : { route: t(`cache.guardRetryRoutes.${item.operation_kind}`, { defaultValue: t("cache.operationFailed") }) }),
    })),
    ...(view?.failed_cleanup_items ?? []).map((item): Failure => ({
      key: `cleanup-${item.cleanup_id}`, subject: adapterName(item.adapter_id), error: item.error_code,
      details: String(item.cleanup_id), retry: { kind: "retry", cleanup_id: item.cleanup_id },
    })),
  ];
  const failureColumns: TableColumnsType<Failure> = [
    { title: t("cache.failureObject"), key: "subject", render: (_, item) => <div>{item.subject}
      <details className="admin-help cache-source-details"><summary>{t("cache.recordDetails")}</summary><Typography.Text copyable>{item.details}</Typography.Text></details>
    </div> },
    { title: t("cache.failureCode"), key: "reason", render: (_, item) => item.error
      ? t(`cache.errors.${item.error}`, { defaultValue: t("cache.operationFailed") }) : t("cache.operationFailed") },
    { title: t("cache.actions"), key: "actions", render: (_, item) => item.retry
      ? <Button size="small" disabled={busy || !mutatingAllowed || item.retryDisabled} onClick={() => void submit(item.retry!)}>{t("cache.retry")}</Button>
      : <Typography.Text type="secondary">{item.route}</Typography.Text> },
  ];

  const reviewBytes = cleanReview?.items.reduce((total, item) => total + item.bytes, 0);
  const footer = <div className="worker-cache-footer">
    <Typography.Text type="secondary">{t(uncertainClean !== null ? "cache.pendingSelection" : "cache.cleanSelection", { count: uncertainClean?.items.length ?? cleanTargets.length })}</Typography.Text>
    <Button type="primary" danger disabled={!mutatingAllowed || busy || (uncertainClean === null && cleanTargets.length === 0)} loading={submitting}
      onClick={() => void (uncertainClean === null ? startCleanPreview() : retryCleanSubmission())}>{t(uncertainClean !== null
        ? "cache.retrySubmission" : selectionTouched ? "cache.clean" : "cache.cleanAvailable")}</Button>
  </div>;

  return <>
    <Modal open={open} title={t("cache.title", { worker: worker.name })} width={1000}
      className="worker-cache-modal" footer={footer} styles={{ body: { maxHeight: "calc(100dvh - 220px)", overflowY: "auto" } }}
      destroyOnHidden onCancel={onClose}>
      <div className="worker-cache-panel" data-testid="worker-cache-panel">
        <div className="worker-cache-toolbar">
          <Typography.Text type="secondary">{t("cache.sampledAt")}: {formatTime(view?.sampled_at ?? null, locale)}</Typography.Text>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => { previewGeneration.current += 1; setCleanRequest(null); setCleanReview(null); void load(); }}>{t("cache.refresh")}</Button>
        </div>
        {error !== null && <Alert type="error" showIcon message={error} closable onClose={() => setError(null)} />}
        {view !== null && status !== "complete" && <Alert showIcon type={status === "incomplete" || status === "stale" ? "warning" : "error"}
          message={t(`cache.states.${status}`)} description={t(`cache.stateDescriptions.${status}`)} />}
        {uncertainClean !== null && <Alert type="warning" showIcon message={t("cache.submissionUncertain")} description={
          <details className="cache-pending-scope"><summary>{t("cache.requestScope")}</summary>
            <ul className="cache-confirm-targets">{uncertainClean.items.map((item) => <li key={item.cache_key}>{targetName(item)}</li>)}</ul>
          </details>
        } />}
        {pollTimedOut && <Alert type="warning" showIcon message={t("cache.pollStopped")} />}
        <Card size="small" title={t("cache.usageTitle")} loading={loading && view === null}>
          <div className="admin-stat-grid">
            <div className="admin-stat"><strong>{capacityKnown ? formatBytes(accounting?.committed_bytes) : "—"}</strong><span>{t("cache.committed")}</span></div>
            <div className="admin-stat"><strong>{formatBytes(estimated)}</strong><span>{t("cache.estimatedReclaimable")}</span></div>
            <div className="admin-stat"><strong>{formatBytes(policy?.max_bytes)}</strong><span>{t("cache.maxBytes")}</span></div>
          </div>
          <div className="worker-cache-automatic"><Typography.Text>{t("cache.automaticCleanup")}</Typography.Text>
            <Tag>{formatPolicyBoolean(typeof policy?.gc_enabled === "boolean" && typeof policy?.pressure_gc_enabled === "boolean"
              ? policy.gc_enabled || policy.pressure_gc_enabled : undefined, t)}</Tag>
          </div>
          <details className="admin-help"><summary>{t("cache.policyTitle")}</summary>
            <div className="admin-policy-summary">
              <p>{t("cache.periodicRule", { duration: idleDuration(policy?.idle_ttl_seconds) })} {formatPolicyBoolean(policy?.gc_enabled, t)}</p>
              <p>{t("cache.installRule", { duration: idleDuration(policy?.min_idle_seconds) })} {formatPolicyBoolean(policy?.pressure_gc_enabled, t)}</p>
              <p>{t("cache.sharedHint")}</p>
              {finiteNumber(accounting?.reserved_bytes) !== null && accounting?.reserved_bytes !== 0
                && <p>{t("cache.reserved")}: {formatBytes(accounting?.reserved_bytes)}</p>}
            </div>
          </details>
        </Card>
        <Card size="small" title={t("cache.entriesTitle")}>
          <Table size="small" rowKey="adapter_id" loading={loading && view !== null} dataSource={groups}
            pagination={{ pageSize: 6, hideOnSinglePage: true, showSizeChanger: false }}
            columns={[
              { title: t("cache.adapter"), key: "adapter", render: (_, group) => <Typography.Text strong>{adapterName(group.adapter_id)}</Typography.Text> },
              { title: t("cache.versionCount"), key: "versions", render: (_, group) => group.items.length },
              { title: t("cache.bytes"), key: "bytes", render: (_, group) => formatBytes(group.items.reduce((total, item) => total + item.bytes, 0)) },
              { title: t("cache.actions"), key: "actions", render: (_, group) => <Button type="link" size="small"
                aria-label={t("cache.viewVersionsLabel", { adapter: adapterName(group.adapter_id) })}
                aria-expanded={expandedAdapters.includes(group.adapter_id)}
                onClick={() => setExpandedAdapters((current) => current.includes(group.adapter_id)
                  ? current.filter((id) => id !== group.adapter_id) : [...current, group.adapter_id])}>
                {t(expandedAdapters.includes(group.adapter_id) ? "cache.hideVersions" : "cache.viewVersions")}</Button> },
            ]}
            expandable={{ showExpandColumn: false, expandedRowKeys: expandedAdapters,
              expandedRowRender: (group) => <Table size="small" rowKey="cache_key" dataSource={group.items} columns={cacheColumns}
                pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }} scroll={{ x: 580 }}
                rowSelection={{ columnWidth: 44, selectedRowKeys: selectedKeys, preserveSelectedRowKeys: true,
                  onChange: (keys) => {
                    previewGeneration.current += 1; setSelectionTouched(true); setCleanRequest(null); setCleanReview(null);
                    const otherGroups = selectedKeys.filter((key) => !group.items.some((item) => item.cache_key === key));
                    setSelectedKeys([...new Set([...otherGroups, ...keys])]);
                  }, getCheckboxProps: (item) => ({ disabled: busy || !mutatingAllowed || !canClean(item) }) }} />,
            }} />
          <Typography.Paragraph type="secondary" className="cache-safety-note">{t("cache.protectionHint")}</Typography.Paragraph>
        </Card>
        {(failures.length > 0 || view?.failed_guard_complete === false) && <Card size="small" title={t("cache.needsAttention")}>
          {view?.failed_guard_complete === false && <Alert type="warning" showIcon message={t("cache.failedGuardsIncomplete")} />}
          <Table size="small" rowKey="key" dataSource={failures} columns={failureColumns}
            pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }} scroll={{ x: 580 }} />
          {view?.failed_cleanup_next_cursor != null && <Button loading={loadingFailedCleanups} onClick={() => void loadMoreFailedCleanups()}>{t("cache.loadMoreFailedCleanups")}</Button>}
        </Card>}
        {operations.length > 0 && <details className="admin-help cache-operation-history"><summary>{t("cache.operationsTitle")}</summary>
          <Table size="small" rowKey="operation_id" dataSource={operations} columns={operationColumns}
            pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }} scroll={{ x: 580 }} />
          {nextCursor !== null && <Button loading={loadingMore} onClick={() => void loadMoreOperations()}>{t("cache.loadMore")}</Button>}
        </details>}
      </div>
    </Modal>
    <Modal open={open && cleanReview !== null} title={t("cache.cleanConfirmTitle")} okText={t("cache.cleanConfirmAction")}
      cancelText={t("cache.cancel")} okButtonProps={{ danger: true, disabled: busy || !mutatingAllowed }}
      onOk={() => void confirmClean()} onCancel={() => setCleanReview(null)} destroyOnHidden>
      <p>{t("cache.cleanScope", { count: cleanReview?.items.length ?? 0,
        bytes: formatBytes(reviewBytes) })}</p>
      <ul className="cache-confirm-targets">{cleanReview?.items.map((item) => <li key={item.cache_key}>{targetName(item)}</li>)}</ul>
      <Typography.Text type="secondary">{t("cache.cleanConfirmDescription")}</Typography.Text>
    </Modal>
    <Modal open={open && proofOpen} title={t("cache.proofDetails")} footer={null} onCancel={() => setProofOpen(false)} destroyOnHidden>
      <p>{proofTarget && targetName(proofTarget)}</p>
      <Form layout="vertical" className="worker-cache-proof-form">
        <Alert type="warning" showIcon message={t("cache.proofCommitmentTitle")} description={t("cache.proofCommitmentDescription")} />
        <Form.Item label={t("cache.sourcePolicy")} htmlFor="cache-source-policy"
          extra={t(proof.sourcePolicy === "verified_offline" ? "cache.proofOfflineHint" : "cache.proofOnlineHint")}>
          <Select id="cache-source-policy" value={proof.sourcePolicy} options={[
            { value: "verified_offline", label: t("cache.sourcePolicies.verified_offline") },
            { value: "managed_online", label: t("cache.sourcePolicies.managed_online") },
          ]} onChange={(sourcePolicy) => setProof((current) => ({ ...current, sourcePolicy }))} />
        </Form.Item>
        <Form.Item label={t("cache.evidenceNote")} htmlFor="cache-evidence-note"><Input.TextArea id="cache-evidence-note"
          value={proof.note} maxLength={256} showCount onChange={(event) => setProof((current) => ({ ...current, note: event.target.value }))} /></Form.Item>
        <Form.Item label={t("cache.validHours")} htmlFor="cache-valid-hours"><InputNumber id="cache-valid-hours" min={1} max={24}
          precision={0} value={proof.hours} onChange={(hours) => setProof((current) => ({ ...current, hours: hours ?? 1 }))} /></Form.Item>
        <Space className="cache-proof-actions" wrap><Button type="primary"
          disabled={selectedProtectable.length === 0 || !proof.note.trim() || busy || !mutatingAllowed} onClick={submitProof}>{t("cache.confirmRebuildability")}</Button>
          <Button disabled={submitting} onClick={() => setProofOpen(false)}>{t("cache.cancel")}</Button></Space>
      </Form>
    </Modal>
  </>;
}
