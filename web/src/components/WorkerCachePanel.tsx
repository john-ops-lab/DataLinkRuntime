import { ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type Key } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError } from "../api";
import type {
  CacheAdminView,
  CacheKeySelection,
  CacheOperation,
  CacheOperationCreate,
  CacheProtectSelection,
  CacheSnapshotItem,
  FailedCacheCleanupItem,
  FailedCacheGuardItem,
  Worker,
} from "../types";

const CATEGORY_NAMES = ["versions", "shared", "staging", "trash", "unknown"] as const;
const TERMINAL_STATUSES = new Set(["completed", "failed"]);
const POLL_INTERVAL_MS = 1_000;
const POLL_LIMIT_MS = 120_000;

interface WorkerCachePanelProps {
  worker: Worker;
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

function operationResultFacts(operation: CacheOperation): Array<[string, number | string | boolean]> {
  const result = operation.result;
  if (result === null) return [];
  const facts: Array<[string, number | string | boolean]> = [];
  const allowed = operation.kind === "preview"
    ? ["complete"]
    : operation.kind === "clean"
      ? ["status", "scanned", "candidates", "deleted", "freed_bytes", "cursor"]
      : operation.kind === "protect"
        ? ["changed"]
        : operation.target_kind === "cleanup"
          ? ["cleanup_id", "attempts", "success"]
          : ["complete", "status", "scanned", "candidates", "deleted", "freed_bytes", "cursor", "changed", "retry_status", "guard_phase"];
  for (const key of allowed) {
    const value = result[key];
    if (typeof value === "number" && Number.isFinite(value)) facts.push([key, value]);
    if (typeof value === "string" || typeof value === "boolean") facts.push([key, value]);
  }
  if ((operation.kind === "preview" || operation.kind === "retry") && Array.isArray(result.items)) {
    facts.push(["items", result.items.length]);
  }
  if (operation.kind === "clean" || operation.kind === "retry") {
    const retained = result.retained_reasons;
    if (retained !== null && typeof retained === "object" && !Array.isArray(retained)) {
      const count = Object.values(retained).reduce(
        (total, value) => total + (typeof value === "number" && Number.isFinite(value) ? value : 0),
        0,
      );
      facts.push(["retained", count]);
    }
  }
  return facts;
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

export default function WorkerCachePanel({ worker, open, onClose }: WorkerCachePanelProps) {
  const { i18n, t } = useTranslation("settings");
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const [view, setView] = useState<CacheAdminView | null>(null);
  const [operations, setOperations] = useState<CacheOperation[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<Key[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingFailedCleanups, setLoadingFailedCleanups] = useState(false);
  const [submitting, setSubmitting] = useState(false);
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
      }) + t("cache.errorCodeSuffix", { code: caught.code });
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
      setNextCursor(page.next_cursor);
      setSelectedKeys([]);
    } catch (caught) {
      if (cacheRequestGeneration.current === generation) {
        setError(stableError(caught, "loadFailed"));
      }
    } finally {
      if (cacheRequestGeneration.current === generation) setLoading(false);
    }
  }, [open, stableError, worker.id]);

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
          if (TERMINAL_STATUSES.has(latest.status)) void refreshCache();
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
  }, [activeOperation, open, pollRevision, pollStartedAt, refreshCache, stableError, worker.id]);

  const selectedItems = useMemo(() => {
    const selected = new Set(selectedKeys.map(String));
    return (view?.items ?? []).filter((item) => selected.has(item.cache_key));
  }, [selectedKeys, view?.items]);

  const selectedPairs = useMemo<CacheKeySelection[]>(() => selectedItems.map((item) => ({
    adapter_id: item.adapter_id,
    version_id: item.version_id,
  })), [selectedItems]);

  const selectedProtectable = useMemo(() => selectedItems.filter((item) => (
    item.kind !== "staging" && item.identity !== null && item.digest !== null
  )), [selectedItems]);

  const submit = useCallback(async (draft: CacheOperationDraft, logicalFingerprint?: string) => {
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
      setActiveOperation(operation);
      setPollStartedAt(Date.now());
      setPollRevision(0);
      setPollTimedOut(false);
      setOperations((current) => [operation, ...current.filter(
        (item) => item.operation_id !== operation.operation_id,
      )]);
    } catch (caught) {
      if (caught instanceof ApiError && 0 < caught.status && caught.status < 500) {
        pendingSubmissions.current.delete(fingerprint);
      }
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
  }, [refreshCache, stableError, worker.id]);

  const submitKeyOperation = useCallback((kind: "preview" | "clean") => {
    if (selectedPairs.length === 0) return;
    const perform = () => void submit({ kind, keys: selectedPairs });
    if (kind === "clean") {
      Modal.confirm({
        title: t("cache.cleanConfirmTitle"),
        content: t("cache.cleanConfirmDescription", { count: selectedPairs.length }),
        okText: t("cache.clean"),
        okButtonProps: { danger: true },
        cancelText: t("cache.cancel"),
        onOk: perform,
      });
      return;
    }
    perform();
  }, [selectedPairs, submit, t]);

  const submitPin = useCallback((pinned: boolean) => {
    if (selectedProtectable.length === 0 || selectedProtectable.length !== selectedItems.length) return;
    const protect: CacheProtectSelection[] = selectedProtectable.map((item) => ({
      adapter_id: item.adapter_id,
      version_id: item.version_id,
      identity: item.identity!,
      digest: item.digest!,
      pinned,
    }));
    void submit({ kind: "protect", protect });
  }, [selectedItems.length, selectedProtectable, submit]);

  const submitProof = useCallback(() => {
    const note = proof.note.trim();
    if (selectedProtectable.length === 0 || selectedProtectable.length !== selectedItems.length
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
  }, [proof, selectedItems.length, selectedProtectable, submit]);

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

  const cacheColumns: TableColumnsType<CacheSnapshotItem> = [
    {
      title: t("cache.key"), key: "cache_key", render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{item.cache_key}</Typography.Text>
          <Tag>{t(`cache.itemKinds.${item.kind ?? "version"}`)}</Tag>
        </Space>
      ),
    },
    { title: t("cache.bytes"), dataIndex: "bytes", key: "bytes", render: formatBytes },
    {
      title: t("cache.protection"),
      key: "protection",
      render: (_, item) => item.kind === "staging" ? (
        <Tag>{t("cache.identityUnavailable")}</Tag>
      ) : (
        <Space size={[4, 4]} wrap>
          <Tag color={item.pinned ? "blue" : "default"}>
            {item.pinned ? t("cache.pinned") : t("cache.unpinned")}
          </Tag>
          <Tag color={item.rebuildability === "confirmed" ? "green" : "gold"}>
            {t(`cache.rebuildability.${item.rebuildability}`)}
          </Tag>
        </Space>
      ),
    },
    {
      title: t("cache.reasons"),
      dataIndex: "reasons",
      key: "reasons",
      render: (reasons: string[]) => reasons.length === 0 ? "—" : (
        <Space size={[4, 4]} wrap>{reasons.map((reason) => (
          <Tag key={reason}>{t(`cache.reasonsMap.${reason}`, {
            defaultValue: t("cache.unknownReason", { code: reason }),
          })}</Tag>
        ))}</Space>
      ),
    },
  ];

  const operationColumns: TableColumnsType<CacheOperation> = [
    {
      title: t("cache.operation"), key: "operation", render: (_, operation) => (
        <Space direction="vertical" size={0}>
          <span>{t(`cache.operationKinds.${operation.kind}`)}</span>
          <Typography.Text type="secondary" copyable>{operation.operation_id}</Typography.Text>
        </Space>
      ),
    },
    {
      title: t("cache.operationStatus"), dataIndex: "status", key: "status",
      render: (status: CacheOperation["status"]) => (
        <Tag color={status === "completed" ? "green" : status === "failed" ? "red" : "blue"}>
          {t(`cache.operationStatuses.${status}`)}
        </Tag>
      ),
    },
    {
      title: t("cache.actualResult"), key: "result", render: (_, operation) => {
        const facts = operationResultFacts(operation);
        const reasonEntries = resultReasonEntries(operation);
        const errorText = operation.error_code ? t(`cache.errors.${operation.error_code}`, {
            defaultValue: t("cache.unknownReason", { code: operation.error_code }),
          }) : null;
        if (errorText === null && facts.length === 0 && reasonEntries.length === 0) return "—";
        return (
          <Space direction="vertical" size={0}>
            {errorText !== null && <Typography.Text type="danger">{errorText}</Typography.Text>}
            {facts.map(([key, value]) => (
              <span key={key}>{t(`cache.resultFields.${key}`)}: {
                key.includes("bytes") ? formatBytes(value) : String(value)
              }</span>
            ))}
            {reasonEntries.map(([subject, reasons]) => operation.kind === "clean" ? (
              <span key={`retained-${reasons[0]}`}>{t("cache.retainedReason", {
                count: subject,
                reason: t(`cache.reasonsMap.${reasons[0]}`, {
                  defaultValue: t("cache.unknownReason", { code: reasons[0] }),
                }),
              })}</span>
            ) : (
              <span key={`preview-${subject}`}>{subject}: {reasons.length === 0
                ? t("cache.previewCandidate")
                : reasons.map((reason) => (
                    <Tag key={reason}>{t(`cache.reasonsMap.${reason}`, {
                      defaultValue: t("cache.unknownReason", { code: reason }),
                    })}</Tag>
                  ))}</span>
            ))}
          </Space>
        );
      },
    },
    {
      title: t("cache.actions"), key: "actions", render: (_, operation) => (
        operation.status === "failed" ? (
          <Button
            size="small"
            disabled={submitting}
            onClick={() => void submit({
              kind: "retry",
              management_operation_id: operation.operation_id,
            })}
          >
            {t("cache.retry")}
          </Button>
        ) : "—"
      ),
    },
  ];

  const failedGuardColumns: TableColumnsType<FailedCacheGuardItem> = [
    {
      title: t("cache.guardOperation"), key: "guard", render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text copyable>{item.guard_operation_id}</Typography.Text>
          <Typography.Text type="secondary">
            {t("cache.guardGeneration", { generation: item.generation })}
          </Typography.Text>
        </Space>
      ),
    },
    { title: t("cache.key"), key: "key", render: (_, item) => `${item.adapter_id}-${item.version_id}` },
    { title: t("cache.guardKind"), dataIndex: "operation_kind", key: "operation_kind" },
    { title: t("cache.resumePhase"), dataIndex: "resume_phase", key: "resume_phase" },
    { title: t("cache.attempts"), dataIndex: "failure_count", key: "failure_count" },
    {
      title: t("cache.failureCode"), key: "error", render: (_, item) => (
        t(`cache.errors.${item.error_code}`, {
          defaultValue: t("cache.unknownReason", { code: item.error_code }),
        })
      ),
    },
    {
      title: t("cache.actions"), key: "actions", render: (_, item) => item.operation_kind === "gc" ? (
        <Button
          size="small"
          disabled={submitting || !mutatingAllowed}
          onClick={() => void submit({ kind: "retry", guard_operation_id: item.guard_operation_id })}
        >
          {t("cache.retry")}
        </Button>
      ) : <Typography.Text type="secondary">{
        t(`cache.guardRetryRoutes.${item.operation_kind}`, { defaultValue: "—" })
      }</Typography.Text>,
    },
  ];

  const failedCleanupColumns: TableColumnsType<FailedCacheCleanupItem> = [
    { title: t("cache.cleanupId"), dataIndex: "cleanup_id", key: "cleanup_id" },
    { title: t("cache.adapterId"), dataIndex: "adapter_id", key: "adapter_id" },
    { title: t("cache.attempts"), dataIndex: "attempts", key: "attempts" },
    {
      title: t("cache.failureCode"), key: "error", render: (_, item) => (
        item.error_code === null ? "—" : t(`cache.errors.${item.error_code}`, {
          defaultValue: t("cache.unknownReason", { code: item.error_code }),
        })
      ),
    },
    {
      title: t("cache.actions"), key: "actions", render: (_, item) => (
        <Button
          size="small"
          disabled={submitting || !mutatingAllowed}
          onClick={() => void submit({ kind: "retry", cleanup_id: item.cleanup_id })}
        >
          {t("cache.retry")}
        </Button>
      ),
    },
  ];

  return (
    <Modal
      open={open}
      title={t("cache.title", { worker: worker.name })}
      width={1000}
      footer={null}
      destroyOnHidden
      onCancel={onClose}
    >
      <div className="worker-cache-panel" data-testid="worker-cache-panel">
        <div className="worker-cache-toolbar">
          <Typography.Text type="secondary">{t("cache.scopeHint")}</Typography.Text>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            {t("cache.refresh")}
          </Button>
        </div>
        {error !== null && <Alert type="error" showIcon message={error} closable onClose={() => setError(null)} />}
        {view !== null && status !== "complete" && (
          <Alert
            showIcon
            type={status === "incomplete" || status === "stale" ? "warning" : "error"}
            message={t(`cache.states.${status}`)}
            description={t(`cache.stateDescriptions.${status}`)}
          />
        )}
        {pollTimedOut && <Alert type="warning" showIcon message={t("cache.pollStopped")} />}

        <Card size="small" title={t("cache.snapshotTitle")} loading={loading && view === null}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
            { key: "status", label: t("cache.snapshotStatus"), children: status ? t(`cache.states.${status}`) : "—" },
            { key: "sampled", label: t("cache.sampledAt"), children: formatTime(view?.sampled_at ?? null, locale) },
            { key: "received", label: t("cache.receivedAt"), children: formatTime(view?.received_at ?? null, locale) },
            { key: "sampleId", label: t("cache.sampleId"), children: view?.sample_id ? <Typography.Text copyable>{view.sample_id}</Typography.Text> : "—" },
            { key: "sequence", label: t("cache.sequence"), children: view?.sequence ?? "—" },
            { key: "complete", label: t("cache.completeness"), children: view?.complete ? t("cache.complete") : t("cache.partial") },
            { key: "cursor", label: t("cache.cursor"), children: view?.cursor ?? "—" },
            { key: "committed", label: t("cache.committed"), children: capacityKnown ? formatBytes(accounting?.committed_bytes) : "—" },
            { key: "reserved", label: t("cache.reserved"), children: capacityKnown ? formatBytes(accounting?.reserved_bytes) : "—" },
            { key: "estimated", label: t("cache.estimatedReclaimable"), children: formatBytes(estimated) },
            { key: "gc", label: t("cache.gcEnabled"), children: typeof policy?.gc_enabled === "boolean" ? t(policy.gc_enabled ? "cache.enabled" : "cache.disabled") : "—" },
            { key: "pressure", label: t("cache.pressureGcEnabled"), children: typeof policy?.pressure_gc_enabled === "boolean" ? t(policy.pressure_gc_enabled ? "cache.enabled" : "cache.disabled") : "—" },
            { key: "max", label: t("cache.maxBytes"), children: formatBytes(policy?.max_bytes) },
            { key: "minIdle", label: t("cache.minIdleSeconds"), children: integer(policy?.min_idle_seconds) ?? "—" },
          ]} />
          <div className="worker-cache-categories">
            {CATEGORY_NAMES.map((name) => {
              const category = capacityKnown ? categories?.[name] : undefined;
              return (
                <Card size="small" key={name} title={t(`cache.categories.${name}`)}>
                  <strong>{category?.entries ?? "—"}</strong>
                  <span>{formatBytes(category?.bytes)}</span>
                  <Typography.Text type="secondary">
                    {t("cache.reclaimableValue", { value: formatBytes(category?.reclaimable_bytes) })}
                  </Typography.Text>
                  {category !== undefined && Object.keys(category.reasons).length > 0 && (
                    <Typography.Text type="secondary">
                      {t("cache.reasonCount", { count: Object.values(category.reasons).reduce((total, count) => total + count, 0) })}
                    </Typography.Text>
                  )}
                </Card>
              );
            })}
          </div>
          <details className="worker-cache-policy">
            <summary>{t("cache.policyDetails")}</summary>
            <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
              { key: "gc_enabled", label: t("cache.policyFields.gc_enabled"), children: formatPolicyBoolean(policy?.gc_enabled, t) },
              { key: "pressure_gc_enabled", label: t("cache.policyFields.pressure_gc_enabled"), children: formatPolicyBoolean(policy?.pressure_gc_enabled, t) },
              { key: "scan_interval_seconds", label: t("cache.policyFields.scan_interval_seconds"), children: integer(policy?.scan_interval_seconds) ?? "—" },
              { key: "idle_ttl_seconds", label: t("cache.policyFields.idle_ttl_seconds"), children: integer(policy?.idle_ttl_seconds) ?? "—" },
              { key: "min_idle_seconds", label: t("cache.policyFields.min_idle_seconds"), children: integer(policy?.min_idle_seconds) ?? "—" },
              { key: "high_watermark_percent", label: t("cache.policyFields.high_watermark_percent"), children: finiteNumber(policy?.high_watermark_percent) ?? "—" },
              { key: "low_watermark_percent", label: t("cache.policyFields.low_watermark_percent"), children: finiteNumber(policy?.low_watermark_percent) ?? "—" },
              { key: "max_delete_entries_per_round", label: t("cache.policyFields.max_delete_entries_per_round"), children: integer(policy?.max_delete_entries_per_round) ?? "—" },
              { key: "max_delete_bytes_per_round", label: t("cache.policyFields.max_delete_bytes_per_round"), children: formatBytes(policy?.max_delete_bytes_per_round) },
              { key: "max_round_seconds", label: t("cache.policyFields.max_round_seconds"), children: finiteNumber(policy?.max_round_seconds) ?? "—" },
              { key: "max_scan_entries_per_round", label: t("cache.policyFields.max_scan_entries_per_round"), children: integer(policy?.max_scan_entries_per_round) ?? "—" },
              { key: "max_scan_nodes_per_round", label: t("cache.policyFields.max_scan_nodes_per_round"), children: integer(policy?.max_scan_nodes_per_round) ?? "—" },
              { key: "max_scan_depth", label: t("cache.policyFields.max_scan_depth"), children: integer(policy?.max_scan_depth) ?? "—" },
              { key: "max_scan_hash_bytes_per_round", label: t("cache.policyFields.max_scan_hash_bytes_per_round"), children: formatBytes(policy?.max_scan_hash_bytes_per_round) },
              { key: "max_bytes", label: t("cache.policyFields.max_bytes"), children: formatBytes(policy?.max_bytes) },
              { key: "disk_reserve_bytes", label: t("cache.policyFields.disk_reserve_bytes"), children: formatBytes(policy?.disk_reserve_bytes) },
              { key: "staging_ttl_seconds", label: t("cache.policyFields.staging_ttl_seconds"), children: integer(policy?.staging_ttl_seconds) ?? "—" },
              { key: "offline_protection", label: t("cache.policyFields.offline_protection"), children: formatPolicyBoolean(policy?.offline_protection, t) },
              { key: "offline_mode", label: t("cache.policyFields.offline_mode"), children: formatPolicyBoolean(policy?.offline_mode, t) },
              { key: "shared_cache_mode", label: t("cache.policyFields.shared_cache_mode"), children: policy?.shared_cache_mode ?? "—" },
            ]} />
          </details>
        </Card>

        <Card size="small" title={t("cache.entriesTitle")}>
          <Alert type="info" showIcon message={t("cache.previewRecheckHint")} />
          <Table
            size="small"
            rowKey="cache_key"
            loading={loading && view !== null}
            dataSource={view?.items ?? []}
            columns={cacheColumns}
            pagination={false}
            scroll={{ x: 760 }}
            rowSelection={{
              selectedRowKeys: selectedKeys,
              preserveSelectedRowKeys: false,
              onChange: setSelectedKeys,
              getCheckboxProps: () => ({ disabled: !mutatingAllowed }),
            }}
          />
          <Space wrap className="worker-cache-actions">
            <Button disabled={selectedPairs.length === 0 || submitting || !mutatingAllowed} onClick={() => submitKeyOperation("preview")}>
              {t("cache.preview")}
            </Button>
            <Button danger disabled={selectedPairs.length === 0 || submitting || !mutatingAllowed} onClick={() => submitKeyOperation("clean")}>
              {t("cache.clean")}
            </Button>
            <Button disabled={selectedProtectable.length === 0 || selectedProtectable.length !== selectedItems.length || submitting || !mutatingAllowed} onClick={() => submitPin(true)}>
              {t("cache.pin")}
            </Button>
            <Button disabled={selectedProtectable.length === 0 || selectedProtectable.length !== selectedItems.length || submitting || !mutatingAllowed} onClick={() => submitPin(false)}>
              {t("cache.unpin")}
            </Button>
          </Space>
          <Form layout="vertical" className="worker-cache-proof-form">
            <Alert
              type="warning"
              showIcon
              message={t("cache.proofCommitmentTitle")}
              description={t("cache.proofCommitmentDescription")}
            />
            <Form.Item label={t("cache.sourcePolicy")} htmlFor="cache-source-policy">
              <Select
                id="cache-source-policy"
                value={proof.sourcePolicy}
                options={[
                  { value: "verified_offline", label: t("cache.sourcePolicies.verified_offline") },
                  { value: "managed_online", label: t("cache.sourcePolicies.managed_online") },
                ]}
                onChange={(sourcePolicy) => setProof((current) => ({ ...current, sourcePolicy }))}
              />
            </Form.Item>
            <Form.Item label={t("cache.evidenceNote")} htmlFor="cache-evidence-note">
              <Input.TextArea
                id="cache-evidence-note"
                value={proof.note}
                maxLength={256}
                showCount
                onChange={(event) => setProof((current) => ({ ...current, note: event.target.value }))}
              />
            </Form.Item>
            <Form.Item label={t("cache.validHours")} htmlFor="cache-valid-hours">
              <InputNumber
                id="cache-valid-hours"
                min={1}
                max={24}
                precision={0}
                value={proof.hours}
                onChange={(hours) => setProof((current) => ({ ...current, hours: hours ?? 1 }))}
              />
            </Form.Item>
            <Button
              type="primary"
              disabled={selectedProtectable.length === 0 || selectedProtectable.length !== selectedItems.length || !proof.note.trim() || submitting || !mutatingAllowed}
              onClick={submitProof}
            >
              {t("cache.confirmRebuildability")}
            </Button>
          </Form>
        </Card>

        {view !== null && (view.failed_guard_items.length > 0 || view.failed_guard_complete === false) && (
          <Card size="small" title={t("cache.failedGuardsTitle")}>
            <Alert type="warning" showIcon message={t("cache.failedRetryHint")} />
            {view.failed_guard_complete === false && (
              <Alert
                type="warning"
                showIcon
                message={t("cache.failedGuardsIncomplete")}
                description={(
                  <Space direction="vertical" size={0}>
                    <span>{t("cache.failedGuardCursor")}</span>
                    <Typography.Text copyable>{view.failed_guard_cursor ?? "—"}</Typography.Text>
                  </Space>
                )}
              />
            )}
            <Table
              size="small"
              rowKey="guard_operation_id"
              dataSource={view?.failed_guard_items ?? []}
              columns={failedGuardColumns}
              pagination={false}
              scroll={{ x: 820 }}
            />
          </Card>
        )}

        {(view?.failed_cleanup_items.length ?? 0) > 0 && (
          <Card size="small" title={t("cache.failedCleanupsTitle")}>
            <Alert type="warning" showIcon message={t("cache.failedRetryHint")} />
            <Table
              size="small"
              rowKey="cleanup_id"
              dataSource={view?.failed_cleanup_items ?? []}
              columns={failedCleanupColumns}
              pagination={false}
              scroll={{ x: 680 }}
            />
            {view?.failed_cleanup_next_cursor != null && (
              <Button loading={loadingFailedCleanups} onClick={() => void loadMoreFailedCleanups()}>
                {t("cache.loadMoreFailedCleanups")}
              </Button>
            )}
          </Card>
        )}

        <Card size="small" title={t("cache.operationsTitle")}>
          <Table
            size="small"
            rowKey="operation_id"
            dataSource={operations}
            columns={operationColumns}
            pagination={false}
            scroll={{ x: 720 }}
          />
          {nextCursor !== null && (
            <Button loading={loadingMore} onClick={() => void loadMoreOperations()}>
              {t("cache.loadMore")}
            </Button>
          )}
        </Card>
      </div>
    </Modal>
  );
}
