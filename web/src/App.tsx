import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import Editor from "@monaco-editor/react";
import { Button, ConfigProvider, message, Modal, Segmented, Space, Tabs } from "antd";
import zhCN from "antd/locale/zh_CN";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import AdapterCatalog from "./components/AdapterCatalog";
import AdapterSettingsDrawer from "./components/AdapterSettingsDrawer";
import AiAssistantPanel from "./components/AiAssistantPanel";
import CredentialBindingsEditor from "./components/CredentialBindingsEditor";
import ExecutionHistoryPanel from "./components/ExecutionHistoryPanel";
import LoginPage from "./components/LoginPage";
import ScheduleTriggerPanel from "./components/ScheduleTriggerPanel";
import SystemSettingsDrawer from "./components/SystemSettingsDrawer";
import TestRunPanel from "./components/TestRunPanel";
import VersionDiffModal, { type DiffPane } from "./components/VersionDiffModal";
import WorkerStatus from "./components/WorkerStatus";
import WorkbenchHeader from "./components/WorkbenchHeader";
import { DEPENDENCY_UI, STARTER_CODE } from "./languages";
import { PRODUCTION_REFRESH_POLICY } from "./production-refresh-policy";
import { statusLabel } from "./status";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  Adapter,
  AdapterLanguage,
  AiCandidate,
  PublishGate,
  VersionDetail,
  VersionSummary,
  Worker,
} from "./types";

type HealthStatus = "loading" | "ok" | "degraded" | "unreachable";

interface HealthPayload {
  status: string;
  database: boolean;
}

// Runtime contract check: only accept objects with a string status and a
// boolean database flag; anything else is treated as an invalid payload.
function isHealthPayload(value: unknown): value is HealthPayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return typeof candidate.status === "string" && typeof candidate.database === "boolean";
}

// Only the two consistent combinations map to a real state; missing fields,
// wrong types or contradictory combinations are treated as unreachable.
function toHealthStatus(payload: HealthPayload): HealthStatus {
  if (payload.status === "ok" && payload.database === true) {
    return "ok";
  }
  if (payload.status === "degraded" && payload.database === false) {
    return "degraded";
  }
  return "unreachable";
}

interface EditorSnapshot {
  code: string;
  requirements: string;
  runtimeConfigText: string;
}

function versionSnapshot(detail: VersionDetail): EditorSnapshot {
  return {
    code: detail.code,
    requirements: detail.requirements,
    runtimeConfigText: JSON.stringify(detail.runtime_config, null, 2),
  };
}

// M2 minimal Token UX: the admin token is kept in sessionStorage only
// (never localStorage, never the database). The api client carries it as a
// Bearer header; a 401 clears it and returns to the token input screen.
export const TOKEN_STORAGE_KEY = "dlr-admin-token";

// M3.1 Monaco 主题：用户偏好只存浏览器本地（localStorage），默认深色；
// “跟随系统”直接用浏览器 prefers-color-scheme，不引入主题框架，也不落库。
type EditorThemePreference = "dark" | "light" | "system";

export const EDITOR_THEME_STORAGE_KEY = "dlr-editor-theme";

function readEditorThemePreference(): EditorThemePreference {
  const stored = window.localStorage.getItem(EDITOR_THEME_STORAGE_KEY);
  return stored === "light" || stored === "system" ? stored : "dark";
}

function subscribeToSystemDark(callback: () => void): () => void {
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  if (!media) {
    return () => {};
  }
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}

function getSystemDarkSnapshot(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function useMonacoTheme(): {
  preference: EditorThemePreference;
  resolvedTheme: "vs-dark" | "light";
  setPreference: (preference: EditorThemePreference) => void;
} {
  const [preference, setPreferenceState] = useState<EditorThemePreference>(readEditorThemePreference);
  // 跟随系统：直接订阅浏览器 prefers-color-scheme，不引入主题框架。
  const systemDark = useSyncExternalStore(subscribeToSystemDark, getSystemDarkSnapshot);

  function setPreference(next: EditorThemePreference) {
    window.localStorage.setItem(EDITOR_THEME_STORAGE_KEY, next);
    setPreferenceState(next);
  }

  const resolvedTheme = preference === "light" ? "light" : preference === "dark" ? "vs-dark" : systemDark ? "vs-dark" : "light";
  return { preference, resolvedTheme, setPreference };
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

/** 发布门禁拒绝原因的稳定中文文案（后端 reason code → 展示）。 */
function publishGateReasonText(gate: PublishGate): string {
  switch (gate.reason) {
    case "no_production_worker":
      return "未配置生产 Worker：请先在 Adapter 设置中指定生产 Worker 后再发布。";
    case "not_tested_on_production_worker":
      return "该版本尚未在生产 Worker 上测试：请先在测试运行页运行一次测试。";
    case "last_test_not_succeeded":
      return gate.last_test !== null
        ? `最近一次生产 Worker 测试未成功（Execution #${gate.last_test.execution_id}，状态：${statusLabel(gate.last_test.status)}）。`
        : "最近一次生产 Worker 测试未成功。";
    default:
      return "发布门禁未通过。";
  }
}

type WorkbenchTabKey = "edit" | "test" | "history" | "triggers";

// 编辑页次级配置区（语言依赖 | 运行参数（JSON） | 凭据绑定）。
type ConfigTabKey = "requirements" | "runtime-config" | "bindings";

/** Diff 弹窗状态：两个入口（Working Copy / 发布对比）共用一个弹窗。 */
interface DiffViewState {
  title: string;
  originalTitle: string;
  modifiedTitle: string;
  panes: DiffPane[];
}

/** 发布确认框状态：门禁信息在打开时拉取，versionId 固定本次目标。 */
interface PublishConfirmState {
  versionId: number;
  versionSeq: number | null;
  gate: PublishGate | null;
  gateError: string | null;
}

// M1 frontend validation mirrors the backend contract: runtime_config must be
// parseable JSON whose top level is an object.
function parseRuntimeConfig(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

function AdapterConsole() {
  const [messageApi, messageContextHolder] = message.useMessage();
  const [health, setHealth] = useState<HealthStatus>("loading");

  const [adapters, setAdapters] = useState<Adapter[]>([]);
  const [selected, setSelected] = useState<Adapter | null>(null);
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [productionWorkerId, setProductionWorkerId] = useState<number | null>(null);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [workersLoading, setWorkersLoading] = useState(true);
  const [workersError, setWorkersError] = useState<string | null>(null);
  const [workerRetestAdapterIds, setWorkerRetestAdapterIds] = useState<Set<number>>(new Set());

  const [snapshot, setSnapshot] = useState<EditorSnapshot>({
    code: "",
    requirements: "",
    runtimeConfigText: "{}",
  });
  const [baseline, setBaseline] = useState<EditorSnapshot>({
    code: "",
    requirements: "",
    runtimeConfigText: "{}",
  });

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // True only after the selected adapter's version list and content loaded successfully.
  // Save/Publish are gated on it so stale or failed loads can never be persisted.
  const [contentReady, setContentReady] = useState(false);
  // Low-frequency Adapter settings live in a drawer, outside the main work area.
  const [settingsOpen, setSettingsOpen] = useState(false);
  // M3.2：受控 Tab，Start 成功后可自动切到执行记录页。
  const [activeTabKey, setActiveTabKey] = useState<WorkbenchTabKey>("edit");
  // Start 创建的 Production Execution id：执行记录页自动打开其详情抽屉。
  const [autoOpenExecutionId, setAutoOpenExecutionId] = useState<number | null>(null);
  // 发布确认框：门禁信息在打开时拉取。发布只更新生产目标，
  // 不会停止或替换当前 Production Execution。
  const [publishConfirm, setPublishConfirm] = useState<PublishConfirmState | null>(null);
  // M3.2：编辑页次级配置 Tabs 与系统设置抽屉（凭据管理 + Python 包源）。
  const [configTabKey, setConfigTabKey] = useState<ConfigTabKey>("requirements");
  const [systemSettingsOpen, setSystemSettingsOpen] = useState(false);
  const [aiPanelOpen, setAiPanelOpen] = useState(false);
  const [diffView, setDiffView] = useState<DiffViewState | null>(null);
  // Known version-id -> seq values, cached once a version list has loaded and
  // kept up to date on save/publish. Unvisited Catalog rows use server-derived
  // production seq fields, so this cache never causes extra list requests.
  const [versionSeqById, setVersionSeqById] = useState<Map<number, number>>(new Map());
  const { preference: themePreference, resolvedTheme: editorTheme, setPreference: setThemePreference } = useMonacoTheme();
  // Monotonic guard: only the newest content-loading request may commit state, so
  // rapid adapter switches cannot mix state or save one adapter's snapshot into another.
  const requestGeneration = useRef(0);

  const dirty =
    snapshot.code !== baseline.code ||
    snapshot.requirements !== baseline.requirements ||
    snapshot.runtimeConfigText !== baseline.runtimeConfigText;
  const selectedAdapterId = selected?.id ?? null;
  const selectedProductionState = selected?.production_state ?? null;
  const activeProductionExecutionId = selected?.running_execution_id ?? null;

  useEffect(() => {
    let cancelled = false;

    async function checkHealth() {
      try {
        const response = await fetch("/api/health");
        const body: unknown = await response.json();
        if (!cancelled) {
          setHealth(isHealthPayload(body) ? toHealthStatus(body) : "unreachable");
        }
      } catch {
        if (!cancelled) {
          setHealth("unreachable");
        }
      }
    }

    void checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  // Stop(wait/terminate) and a naturally completed production run both leave
  // an active pointer until the Worker reports a terminal status. Reconcile
  // only while that pointer exists; cleanup prevents an old Adapter response
  // from overwriting a newly selected one.
  useEffect(() => {
    if (busy || selectedAdapterId === null || activeProductionExecutionId === null) {
      return;
    }
    const adapterId = selectedAdapterId;
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    async function refreshActiveProduction() {
      try {
        const refreshed = await api.getAdapter(adapterId);
        if (cancelled) {
          return;
        }
        setSelected((current) => (current?.id === adapterId ? refreshed : current));
        setAdapters((current) =>
          current.map((item) => (item.id === adapterId ? refreshed : item)),
        );
        if (refreshed.running_execution_id != null) {
          timeoutId = setTimeout(
            () => void refreshActiveProduction(),
            PRODUCTION_REFRESH_POLICY.pollIntervalMs,
          );
        }
      } catch {
        // A transient read failure must not unlock lifecycle actions. Keep the
        // last authoritative active pointer and retry quietly.
        if (!cancelled) {
          timeoutId = setTimeout(
            () => void refreshActiveProduction(),
            PRODUCTION_REFRESH_POLICY.pollIntervalMs,
          );
        }
      }
    }

    timeoutId = setTimeout(
      () => void refreshActiveProduction(),
      PRODUCTION_REFRESH_POLICY.pollIntervalMs,
    );
    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        clearTimeout(timeoutId);
      }
    };
  }, [activeProductionExecutionId, busy, selectedAdapterId, selectedProductionState]);

  // Catalog, Worker badge and Adapter settings share one Worker collection;
  // no component performs its own request and no Adapter row causes N+1.
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    async function loadWorkers(initial = false) {
      if (inFlight) {
        return;
      }
      inFlight = true;
      if (initial) {
        setWorkersLoading(true);
        setWorkersError(null);
      }
      try {
        const list = await api.listWorkers();
        if (!cancelled) {
          setWorkers(list);
          setWorkersError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setWorkersError(errorMessage(err));
        }
      } finally {
        inFlight = false;
        if (!cancelled) {
          setWorkersLoading(false);
        }
      }
    }

    const handleFocus = () => void loadWorkers();
    const intervalId = window.setInterval(
      () => void loadWorkers(),
      WORKER_REFRESH_POLICY.pollIntervalMs,
    );
    window.addEventListener("focus", handleFocus);
    void loadWorkers(true);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", handleFocus);
    };
  }, []);

  const refreshAdapters = useCallback(async (): Promise<Adapter[]> => {
    const list = await api.listAdapters();
    setAdapters(list);
    return list;
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadAdapters() {
      try {
        const list = await api.listAdapters();
        if (!cancelled) {
          setAdapters(list);
        }
      } catch (err) {
        if (!cancelled) {
          setError(errorMessage(err));
        }
      }
    }

    void loadAdapters();
    return () => {
      cancelled = true;
    };
  }, []);

  function applySnapshot(next: EditorSnapshot) {
    setSnapshot(next);
    setBaseline(next);
  }

  // Cache every known version_id -> seq. Unvisited Catalog rows use the
  // server-provided published/running seq fields instead of extra requests.
  function recordVersionSeqs(list: VersionSummary[]) {
    setVersionSeqById((current) => {
      const next = new Map(current);
      for (const version of list) {
        next.set(version.id, version.seq);
      }
      return next;
    });
  }

  function confirmDiscard(): boolean {
    if (!dirty) {
      return true;
    }
    return window.confirm("存在未保存的修改，确定放弃吗？");
  }

  async function loadAdapterContent(adapter: Adapter) {
    const generation = ++requestGeneration.current;
    // Reset content state synchronously so the previous adapter's snapshot can never
    // appear (or be saved) under the newly selected adapter.
    setSelected(adapter);
    setName(adapter.name);
    setDescription(adapter.description);
    setProductionWorkerId(adapter.production_worker_id ?? null);
    setError(null);
    setVersions([]);
    setSelectedVersionId(null);
    setContentReady(false);
    setSettingsOpen(false);
    setActiveTabKey("edit");
    setConfigTabKey("requirements");
    setAutoOpenExecutionId(null);
    setPublishConfirm(null);
    applySnapshot({ code: "", requirements: "", runtimeConfigText: "{}" });
    try {
      const list = await api.listVersions(adapter.id);
      if (generation !== requestGeneration.current) {
        return;
      }
      setVersions(list);
      recordVersionSeqs(list);
      if (adapter.latest_version_id === null) {
        setSelectedVersionId(null);
        applySnapshot({
          code: STARTER_CODE[adapter.language],
          requirements: "",
          runtimeConfigText: "{}",
        });
        setContentReady(true);
        return;
      }
      const detail = await api.getVersion(adapter.id, adapter.latest_version_id);
      if (generation !== requestGeneration.current) {
        return;
      }
      setSelectedVersionId(detail.id);
      applySnapshot(versionSnapshot(detail));
      setContentReady(true);
    } catch (err) {
      if (generation !== requestGeneration.current) {
        return;
      }
      setError(errorMessage(err));
    }
  }

  function handleSelectAdapter(adapter: Adapter) {
    // Interaction lock: no navigation while a mutation is in flight, so a
    // completing mutation can never commit state against a different adapter.
    if (busy) {
      return;
    }
    if (selected?.id === adapter.id) {
      return;
    }
    if (!confirmDiscard()) {
      return;
    }
    void loadAdapterContent(adapter);
  }

  async function handleCreateAdapter(
    createdName: string,
    createdDescription: string,
    language: AdapterLanguage,
  ): Promise<boolean> {
    if (busy) {
      return false;
    }
    if (!confirmDiscard()) {
      return false;
    }
    setBusy(true);
    try {
      setError(null);
      const created = await api.createAdapter({
        name: createdName,
        description: createdDescription,
        language,
      });
      await refreshAdapters();
      await loadAdapterContent(created);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleSelectVersion(versionId: number) {
    // Interaction lock: no version switching while a mutation is in flight.
    if (busy) {
      return;
    }
    if (!selected || versionId === selectedVersionId) {
      return;
    }
    if (!confirmDiscard()) {
      return;
    }
    const generation = ++requestGeneration.current;
    setContentReady(false);
    try {
      setError(null);
      const detail = await api.getVersion(selected.id, versionId);
      if (generation !== requestGeneration.current) {
        return;
      }
      setSelectedVersionId(detail.id);
      applySnapshot(versionSnapshot(detail));
      setContentReady(true);
    } catch (err) {
      if (generation !== requestGeneration.current) {
        return;
      }
      setError(errorMessage(err));
    }
  }

  async function handleSaveVersion() {
    if (!selected || busy || !contentReady) {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(snapshot.runtimeConfigText);
    if (runtimeConfig === null) {
      setError("Runtime config 必须是合法的 JSON 对象");
      return;
    }
    if (!snapshot.code.trim()) {
      setError("代码不能为空");
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const saved = await api.saveVersion(selected.id, {
        code: snapshot.code,
        requirements: snapshot.requirements,
        runtime_config: runtimeConfig,
      });
      // The immutable version exists as soon as POST succeeds: acknowledge it locally
      // right away so a follow-up refresh failure cannot be mistaken for a failed save
      // (which would invite retrying into a duplicate immutable version). Only
      // latest_version_id is derived from the response; Adapter.updated_at stays
      // the server-owned value until a real Adapter refresh succeeds.
      const optimistic: Adapter = { ...selected, latest_version_id: saved.id };
      setSelected(optimistic);
      setAdapters((current) => current.map((item) => (item.id === optimistic.id ? optimistic : item)));
      setVersions((current) => [saved, ...current]);
      // The new immutable version is the latest one: keep the cached catalog
      // summary in sync without waiting for the best-effort list refresh.
      setVersionSeqById((current) => new Map(current).set(saved.id, saved.seq));
      setSelectedVersionId(saved.id);
      applySnapshot(versionSnapshot(saved));
      const refreshFailures: string[] = [];
      try {
        const versionList = await api.listVersions(selected.id);
        setVersions(versionList);
      } catch (refreshErr) {
        refreshFailures.push(`刷新版本列表失败：${errorMessage(refreshErr)}`);
      }
      try {
        // Best-effort refresh of the real Adapter (server-owned updated_at);
        // failure is non-fatal because the save itself is already acknowledged.
        const real = await api.getAdapter(selected.id);
        setSelected(real);
        setAdapters((current) => current.map((item) => (item.id === real.id ? real : item)));
      } catch (refreshErr) {
        refreshFailures.push(`刷新 Adapter 失败：${errorMessage(refreshErr)}`);
      }
      if (refreshFailures.length === 0) {
        messageApi.success(`已保存为 v${saved.seq}`);
      } else {
        setError(`版本已保存（v${saved.seq}），但${refreshFailures.join("；")}`);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  // 发布按钮入口：打开确认框并拉取只读门禁评估（后端 Publish 仍强制门禁）。
  function handlePublishRequest() {
    if (!selected || selectedVersionId === null || busy || !contentReady || selected.archived_at) {
      return;
    }
    const targetAdapterId = selected.id;
    const targetVersionId = selectedVersionId;
    const seq = versions.find((version) => version.id === targetVersionId)?.seq ?? null;
    setPublishConfirm({ versionId: targetVersionId, versionSeq: seq, gate: null, gateError: null });
    void (async () => {
      try {
        const gate = await api.getPublishGate(targetAdapterId, targetVersionId);
        setPublishConfirm((current) =>
          current !== null && current.versionId === targetVersionId
            ? { ...current, gate }
            : current,
        );
      } catch (err) {
        setPublishConfirm((current) =>
          current !== null && current.versionId === targetVersionId
            ? { ...current, gateError: errorMessage(err) }
            : current,
        );
      }
    })();
  }

  async function handlePublishConfirmed() {
    if (!selected || publishConfirm === null || busy) {
      return;
    }
    const targetVersionId = publishConfirm.versionId;
    setPublishConfirm(null);
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.publishVersion(selected.id, targetVersionId);
      // Keep the cached catalog summary in sync with the new published pointer.
      const publishedSeq = versions.find((version) => version.id === targetVersionId)?.seq;
      if (publishedSeq !== undefined) {
        setVersionSeqById((current) => new Map(current).set(targetVersionId, publishedSeq));
      }
      setSelected(refreshed);
      setProductionWorkerId(refreshed.production_worker_id ?? productionWorkerId);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
      messageApi.success(
        `已发布${publishedSeq === undefined ? `版本 #${targetVersionId}` : ` v${publishedSeq}`}；生产运行状态未自动改变`,
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  // M5.1: Start 不再创建 Execution，改为开启生产入口并锁定生产版本。
  async function handleStartProduction() {
    if (!selected || busy) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const adapter = await api.startProduction(selected.id);
      let refreshed: Adapter;
      try {
        refreshed = await api.getAdapter(selected.id);
      } catch {
        // Best-effort refresh failed: derive the known changes locally.
        refreshed = adapter;
      }
      setSelected(refreshed);
      setProductionWorkerId(refreshed.production_worker_id ?? productionWorkerId);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
      const versionSeq = refreshed.production_version_seq ?? "?";
      messageApi.success(`生产入口已开启，生产版本锁定为 v${versionSeq}`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleStopProduction(mode: "wait" | "terminate") {
    if (!selected || busy) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.stopProduction(selected.id, mode);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
      if (refreshed.running_execution_id === null || refreshed.running_execution_id === undefined) {
        messageApi.success("生产已停止");
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleUnpublish() {
    if (!selected || busy) {
      return;
    }
    if (!window.confirm("确定取消发布吗？将清除已发布版本指针（需先停止生产）。")) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.unpublishAdapter(selected.id);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleArchive() {
    if (!selected || busy) {
      return;
    }
    if (
      !window.confirm(
        "归档后该 Adapter 只读：保存、发布、测试与启动均被禁用（可随时恢复）。确定归档吗？",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.archiveAdapter(selected.id);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleRestore() {
    if (!selected || busy) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.restoreAdapter(selected.id);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleClone() {
    if (!selected || busy) {
      return;
    }
    const cloneName = window.prompt("新 Adapter 名称", `${selected.name}-copy`);
    if (cloneName === null || cloneName.trim() === "") {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const created = await api.cloneAdapter(selected.id, { name: cloneName.trim() });
      const list = await refreshAdapters();
      const target = list.find((item) => item.id === created.id) ?? created;
      await loadAdapterContent(target);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  // M3.2 Diff：Working Copy（当前编辑器快照）vs 基准版本（最近一次加载/保存的不可变版本）。
  function handleOpenWorkingDiff() {
    if (!selected) {
      return;
    }
    const baseLabel =
      selectedVersion !== null ? `基准版本 v${selectedVersion.seq}` : "基准版本（无已保存版本）";
    setDiffView({
      title: "版本差异：Working Copy vs 基准版本",
      originalTitle: baseLabel,
      modifiedTitle: "Working Copy（当前编辑内容）",
      panes: [
        {
          key: "code",
          label: "代码",
          language: selected.language,
          original: baseline.code,
          modified: snapshot.code,
        },
        {
          key: "requirements",
          label: DEPENDENCY_UI[selected.language].label,
          language: "plaintext",
          original: baseline.requirements,
          modified: snapshot.requirements,
        },
        {
          key: "runtime-config",
          label: "运行参数",
          language: "json",
          original: baseline.runtimeConfigText,
          modified: snapshot.runtimeConfigText,
        },
      ],
    });
  }

  function handleOpenAiCandidateDiff(candidate: AiCandidate) {
    if (!selected) {
      return;
    }
    setDiffView({
      title: "AI Candidate：与当前 Working Copy 对比",
      originalTitle: "Working Copy（当前编辑内容）",
      modifiedTitle: "AI Candidate",
      panes: [
        {
          key: "code",
          label: "代码",
          language: selected.language,
          original: snapshot.code,
          modified: candidate.code,
        },
        {
          key: "requirements",
          label: DEPENDENCY_UI[selected.language].label,
          language: "plaintext",
          original: snapshot.requirements,
          modified: candidate.requirements,
        },
        {
          key: "runtime-config",
          label: "运行参数",
          language: "json",
          original: snapshot.runtimeConfigText,
          modified: JSON.stringify(candidate.runtime_config, null, 2),
        },
      ],
    });
  }

  function handleApplyAiCandidate(candidate: AiCandidate) {
    if (!selected || selected.archived_at || !contentReady || busy) {
      return;
    }
    // Human-in-the-loop boundary: this updates browser state only. Persisting,
    // testing and lifecycle actions remain separate explicit administrator actions.
    setSnapshot({
      code: candidate.code,
      requirements: candidate.requirements,
      runtimeConfigText: JSON.stringify(candidate.runtime_config, null, 2),
    });
  }

  // M3.2 Diff：发布目标 vs 当前生产版本（覆盖 code/依赖/参数/绑定引用）。
  async function handleOpenPublishDiff() {
    if (!selected || publishConfirm === null) {
      return;
    }
    const targetAdapterId = selected.id;
    const targetVersionId = publishConfirm.versionId;
    try {
      const [target, bindings] = await Promise.all([
        api.getVersion(targetAdapterId, targetVersionId),
        api.listAdapterBindings(targetAdapterId),
      ]);
      const publishedVersionId = selected.published_version_id;
      const current =
        publishedVersionId !== null && publishedVersionId !== undefined
          ? await api.getVersion(targetAdapterId, publishedVersionId)
          : null;
      const targetSeq = versions.find((version) => version.id === targetVersionId)?.seq;
      const currentSeq =
        publishedVersionId !== null && publishedVersionId !== undefined
          ? versions.find((version) => version.id === publishedVersionId)?.seq
          : undefined;
      const bindingText = JSON.stringify(bindings, null, 2);
      setDiffView({
        title: "版本差异：发布目标 vs 当前生产版本",
        originalTitle:
          current !== null
            ? `当前生产版本${currentSeq !== undefined ? ` v${currentSeq}` : ""}`
            : "当前生产版本（未发布）",
        modifiedTitle: `发布目标${targetSeq !== undefined ? ` v${targetSeq}` : ""}`,
        panes: [
          {
            key: "code",
            label: "代码",
            language: selected.language,
            original: current?.code ?? "",
            modified: target.code,
          },
          {
            key: "requirements",
            label: DEPENDENCY_UI[selected.language].label,
            language: "plaintext",
            original: current?.requirements ?? "",
            modified: target.requirements,
          },
          {
            key: "runtime-config",
            label: "运行参数",
            language: "json",
            original:
              current !== null ? JSON.stringify(current.runtime_config, null, 2) : "",
            modified: JSON.stringify(target.runtime_config, null, 2),
          },
          {
            key: "bindings",
            label: "凭据绑定引用",
            language: "json",
            // 绑定是 Adapter 级配置，两侧展示同一份当前绑定（发布不改变绑定）。
            original: bindingText,
            modified: bindingText,
          },
        ],
      });
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function handleUpdateDetails() {
    if (!selected || busy) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.updateAdapter(selected.id, { name, description });
      setSelected(refreshed);
      setName(refreshed.name);
      setDescription(refreshed.description);
      try {
        await refreshAdapters();
        messageApi.success("Adapter 信息已保存");
      } catch {
        setAdapters((current) =>
          current.map((item) => (item.id === refreshed.id ? refreshed : item)),
        );
        setError("Adapter 信息已保存，但列表刷新失败；请手动刷新确认。");
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateProductionWorker() {
    if (
      !selected ||
      busy ||
      productionWorkerId === (selected.production_worker_id ?? null)
    ) {
      return;
    }
    const previousWorkerId = selected.production_worker_id ?? null;
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.updateAdapter(selected.id, {
        production_worker_id: productionWorkerId,
      });
      setSelected(refreshed);
      setProductionWorkerId(refreshed.production_worker_id ?? null);
      setAdapters((current) =>
        current.map((item) => (item.id === refreshed.id ? refreshed : item)),
      );
      if ((refreshed.production_worker_id ?? null) !== previousWorkerId) {
        setWorkerRetestAdapterIds((current) => new Set(current).add(refreshed.id));
      }
      messageApi.success("production Worker 已保存；切换后请重新测试已发布版本");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const handlePublishedVersionTestSucceeded = useCallback((adapterId: number) => {
    setWorkerRetestAdapterIds((current) => {
      if (!current.has(adapterId)) {
        return current;
      }
      const next = new Set(current);
      next.delete(adapterId);
      return next;
    });
  }, []);

  async function handleDelete() {
    if (!selected || busy) {
      return;
    }
    const warning = dirty ? "该 Adapter 存在未保存的编辑器修改。" : "";
    // M2 真实规则：已有 Execution 的 Adapter 后端会拒绝删除（409 adapter_has_executions）。
    if (
      !window.confirm(
        `确定删除 Adapter “${selected.name}” 吗？无执行记录时将移除该 Adapter 及其全部版本；` +
          `已有 Execution 的 Adapter 为保留执行历史不可删除。${warning}`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      await api.deleteAdapter(selected.id);
      requestGeneration.current += 1;
      setSelected(null);
      setSelectedVersionId(null);
      setVersions([]);
      setContentReady(false);
      setSettingsOpen(false);
      setSystemSettingsOpen(false);
      applySnapshot({ code: "", requirements: "", runtimeConfigText: "{}" });
      await refreshAdapters();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const healthText =
    health === "loading"
      ? "Control 检查中…"
      : health === "ok"
        ? "Control 健康"
        : health === "degraded"
          ? "Control 降级"
          : "Control 不可达";

  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const isLatest = selectedVersionId !== null && selected?.latest_version_id === selectedVersionId;
  const isPublished =
    selectedVersionId !== null && selected?.published_version_id === selectedVersionId;

  const healthDotClass =
    health === "ok"
      ? "health-dot-ok"
      : health === "degraded"
        ? "health-dot-degraded"
        : health === "unreachable"
          ? "health-dot-unreachable"
          : "";

  return (
    <div className="app-shell">
      {messageContextHolder}
      <header className="app-header">
        <div className="app-header-brand">
          <h1 className="app-header-logo">DLR</h1>
          <span className="app-header-product">DataLinkRuntime · 轻量数据适配运行平台</span>
        </div>
        <div className="app-header-status">
          <span className="health-status">
            <span className={`health-dot ${healthDotClass}`.trim()} />
            <span data-testid="control-status">{healthText}</span>
          </span>
          <Button
            size="small"
            data-testid="system-settings"
            onClick={() => setSystemSettingsOpen(true)}
          >
            系统设置
          </Button>
          <WorkerStatus workers={workers} loading={workersLoading} error={workersError} />
        </div>
      </header>

      {error && (
        <p className="error-banner" role="alert" data-testid="error-banner">
          {error}
        </p>
      )}

      <div className="console-body">
        <AdapterCatalog
          adapters={adapters}
          selectedId={selected?.id ?? null}
          busy={busy}
          onSelect={handleSelectAdapter}
          onCreate={handleCreateAdapter}
          versionSeqById={versionSeqById}
          workers={workers}
        />

        <main className="workbench">
          {selected === null ? (
            <div className="workbench-empty">请选择一个 Adapter 进行管理。</div>
          ) : (
            <section className="detail">
              <WorkbenchHeader
                adapter={selected}
                versions={versions}
                selectedVersionId={selectedVersionId}
                selectedVersion={selectedVersion}
                isLatest={isLatest}
                isPublished={isPublished}
                dirty={dirty}
                busy={busy}
                contentReady={contentReady}
                productionWorker={
                  workers.find((worker) => worker.id === selected.production_worker_id) ?? null
                }
                workers={workers}
                workersLoading={workersLoading}
                workersError={workersError}
                productionWorkerRetestRequired={workerRetestAdapterIds.has(selected.id)}
                onSelectVersion={(versionId) => void handleSelectVersion(versionId)}
                onSave={() => void handleSaveVersion()}
                onPublishRequest={handlePublishRequest}
                onStartProduction={() => void handleStartProduction()}
                onStopProduction={(mode) => void handleStopProduction(mode)}
                onOpenSettings={() => setSettingsOpen(true)}
              />

              <Tabs
                className="workbench-tabs"
                activeKey={activeTabKey}
                onChange={(key) => setActiveTabKey(key as WorkbenchTabKey)}
                items={[
                  {
                    key: "edit",
                    label: "编辑",
                    children: (
                      <div className="editor-pane">
                        <div className="editor-toolbar">
                          <span className="editor-toolbar-label">编辑器主题</span>
                          <Segmented
                            size="small"
                            data-testid="editor-theme-picker"
                            value={themePreference}
                            options={[
                              { label: "深色", value: "dark" },
                              { label: "浅色", value: "light" },
                              { label: "跟随系统", value: "system" },
                            ]}
                            onChange={(value) => setThemePreference(value as EditorThemePreference)}
                          />
                          <Button
                            size="small"
                            data-testid="working-diff"
                            disabled={busy || !contentReady}
                            onClick={handleOpenWorkingDiff}
                          >
                            查看 Diff
                          </Button>
                        </div>
                        <div className="editor-main" data-testid="editor-main" data-monaco-theme={editorTheme}>
                          <Editor
                            height="100%"
                            theme={editorTheme}
                            language={selected.language}
                            value={snapshot.code}
                            onChange={(value) => setSnapshot((current) => ({ ...current, code: value ?? "" }))}
                            options={{
                              minimap: { enabled: false },
                              readOnly: busy || !contentReady || !!selected.archived_at,
                            }}
                          />
                        </div>

                        <div className="version-fields">
                          <Tabs
                            className="config-tabs"
                            size="small"
                            activeKey={configTabKey}
                            onChange={(key) => setConfigTabKey(key as ConfigTabKey)}
                            items={[
                              {
                                key: "requirements",
                                label: DEPENDENCY_UI[selected.language].label,
                                children: (
                                  <textarea
                                    data-testid="requirements-input"
                                    rows={4}
                                    value={snapshot.requirements}
                                    disabled={busy || !contentReady || !!selected.archived_at}
                                    placeholder={DEPENDENCY_UI[selected.language].placeholder}
                                    onChange={(event) =>
                                      setSnapshot((current) => ({
                                        ...current,
                                        requirements: event.target.value,
                                      }))
                                    }
                                  />
                                ),
                              },
                              {
                                key: "runtime-config",
                                label: "运行参数（JSON）",
                                children: (
                                  <textarea
                                    data-testid="runtime-config-input"
                                    rows={4}
                                    value={snapshot.runtimeConfigText}
                                    disabled={busy || !contentReady || !!selected.archived_at}
                                    onChange={(event) =>
                                      setSnapshot((current) => ({
                                        ...current,
                                        runtimeConfigText: event.target.value,
                                      }))
                                    }
                                  />
                                ),
                              },
                              {
                                key: "bindings",
                                label: "凭据绑定",
                                children: (
                                  <CredentialBindingsEditor
                                    adapterId={selected.id}
                                    disabled={busy || !contentReady || !!selected.archived_at}
                                    onError={setError}
                                  />
                                ),
                              },
                            ]}
                          />
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: "test",
                    label: "测试运行",
                    children: (
                      <TestRunPanel
                        key={selected.id}
                        adapter={selected}
                        productionWorker={
                          workers.find((worker) => worker.id === selected.production_worker_id) ??
                          null
                        }
                        selectedVersionId={selectedVersionId}
                        selectedVersionSeq={selectedVersion?.seq ?? null}
                        isLatest={isLatest}
                        isPublished={isPublished}
                        dirty={dirty}
                        contentReady={contentReady}
                        busy={busy}
                        workers={workers}
                        workersLoading={workersLoading}
                        workersError={workersError}
                        onEdit={() => setActiveTabKey("edit")}
                        onOpenSettings={() => setSettingsOpen(true)}
                        onError={setError}
                        onPublishedVersionTestSucceeded={handlePublishedVersionTestSucceeded}
                      />
                    ),
                  },
                  {
                    key: "history",
                    label: "执行记录",
                    // antd Tabs render lazily: the history API is only called
                    // after this tab is activated for the first time.
                    children: (
                      <ExecutionHistoryPanel
                        key={selected.id}
                        adapterId={selected.id}
                        autoOpenExecutionId={autoOpenExecutionId}
                      />
                    ),
                  },
                  {
                    key: "triggers",
                    label: "触发器",
                    // M5.2：触发器 Tab 只有 Schedule 区域，Webhook 留给 M5.3。
                    children: (
                      <ScheduleTriggerPanel
                        key={selected.id}
                        adapterId={selected.id}
                        productionState={selected.production_state ?? "idle"}
                        archived={!!selected.archived_at}
                      />
                    ),
                  },
                ]}
              />
            </section>
          )}
        </main>

        <AiAssistantPanel
          key={`ai-assistant-${selected?.id ?? "none"}`}
          open={aiPanelOpen}
          adapter={selected}
          selectedVersionId={selectedVersionId}
          selectedVersionSeq={selectedVersion?.seq ?? null}
          workingCopy={snapshot}
          contentReady={contentReady}
          busy={busy}
          onOpen={() => setAiPanelOpen(true)}
          onClose={() => setAiPanelOpen(false)}
          onApply={handleApplyAiCandidate}
          onOpenDiff={handleOpenAiCandidateDiff}
        />
      </div>

      <AdapterSettingsDrawer
        open={settingsOpen}
        adapter={selected}
        name={name}
        description={description}
        productionWorkerId={productionWorkerId}
        workers={workers}
        workersLoading={workersLoading}
        workersError={workersError}
        productionWorkerRetestRequired={
          selected !== null && workerRetestAdapterIds.has(selected.id)
        }
        busy={busy}
        contentReady={contentReady}
        onClose={() => setSettingsOpen(false)}
        onNameChange={setName}
        onDescriptionChange={setDescription}
        onProductionWorkerChange={setProductionWorkerId}
        onUpdate={() => void handleUpdateDetails()}
        onProductionWorkerUpdate={() => void handleUpdateProductionWorker()}
        onDelete={() => void handleDelete()}
        onUnpublish={() => void handleUnpublish()}
        onArchive={() => void handleArchive()}
        onRestore={() => void handleRestore()}
        onClone={() => void handleClone()}
      />

      <Modal
        title="发布确认"
        open={publishConfirm !== null}
        onCancel={() => setPublishConfirm(null)}
        footer={
          <Space>
            <Button
              data-testid="publish-diff"
              onClick={() => void handleOpenPublishDiff()}
            >
              查看差异
            </Button>
            <Button onClick={() => setPublishConfirm(null)}>取消</Button>
            <Button
              type="primary"
              data-testid="confirm-publish"
              loading={busy}
              disabled={
                publishConfirm === null ||
                publishConfirm.gate === null ||
                !publishConfirm.gate.allowed ||
                busy
              }
              onClick={() => void handlePublishConfirmed()}
            >
              确认发布
            </Button>
          </Space>
        }
      >
        {publishConfirm !== null && (
          <div className="publish-confirm">
            <p data-testid="publish-confirm-target">
              将发布版本{" "}
              <strong>
                {publishConfirm.versionSeq !== null
                  ? `v${publishConfirm.versionSeq}`
                  : `#${publishConfirm.versionId}`}
              </strong>
              。发布只更新生产目标；当前运行不会自动切换。如需运行新版本，
              必须人工 Stop 并等待旧 Production Execution 安全结束后再 Start。
            </p>
            {publishConfirm.gateError !== null && (
              <p className="publish-gate-error" data-testid="publish-gate-error">
                门禁检查失败：{publishConfirm.gateError}
              </p>
            )}
            {publishConfirm.gate === null && publishConfirm.gateError === null && (
              <p>正在检查发布门禁…</p>
            )}
            {publishConfirm.gate !== null &&
              (publishConfirm.gate.allowed ? (
                <p className="publish-gate-ok" data-testid="publish-gate-ok">
                  门禁检查通过：该版本已在生产 Worker 上测试成功
                  {publishConfirm.gate.last_test !== null
                    ? `（Execution #${publishConfirm.gate.last_test.execution_id}）`
                    : ""}
                  。
                </p>
              ) : (
                <p className="publish-gate-blocked" data-testid="publish-gate-blocked">
                  {publishGateReasonText(publishConfirm.gate)}
                </p>
              ))}
          </div>
        )}
      </Modal>

      <SystemSettingsDrawer
        open={systemSettingsOpen}
        onClose={() => setSystemSettingsOpen(false)}
      />

      <VersionDiffModal
        open={diffView !== null}
        title={diffView?.title ?? ""}
        originalTitle={diffView?.originalTitle ?? ""}
        modifiedTitle={diffView?.modifiedTitle ?? ""}
        panes={diffView?.panes ?? []}
        onClose={() => setDiffView(null)}
      />
    </div>
  );
}

// Minimal admin token input shown while no valid token is present;
// the M3.1 login page keeps the M2 auth contract unchanged.

export default function App() {
  const [authed, setAuthed] = useState<boolean>(() => {
    const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored !== null) {
      setAuthToken(stored);
      return true;
    }
    return false;
  });
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    onUnauthorized(() => {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      setAuthToken(null);
      setNotice("会话 Token 已被拒绝，请重新登录。");
      setAuthed(false);
    });
  }, []);

  async function handleLogin(token: string) {
    setAuthToken(token);
    try {
      await api.verifyAdminToken();
    } catch (err) {
      setAuthToken(null);
      throw err;
    }
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    setNotice(null);
    setAuthed(true);
  }

  return (
    <ConfigProvider locale={zhCN} theme={{ token: { colorBgLayout: "#f5f6f8", borderRadius: 4 } }}>
      {authed ? <AdapterConsole /> : <LoginPage notice={notice} onSubmit={handleLogin} />}
    </ConfigProvider>
  );
}
