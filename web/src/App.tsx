import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import Editor, { loader } from "@monaco-editor/react";
import type * as monaco from "monaco-editor";
import { Button, ConfigProvider, Input, message, Modal, Segmented, Select, Tabs } from "antd";
import zhCN from "antd/locale/zh_CN";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import AdapterCatalog from "./components/AdapterCatalog";
import AdapterSettingsDrawer from "./components/AdapterSettingsDrawer";
import AiAssistantPanel from "./components/AiAssistantPanel";
import CredentialBindingsEditor from "./components/CredentialBindingsEditor";
import ExecutionHistoryPanel from "./components/ExecutionHistoryPanel";
import LoginPage from "./components/LoginPage";
import LiveLogWorkspace from "./components/LiveLogWorkspace";
import SystemSettingsDrawer from "./components/SystemSettingsDrawer";
import TaskRunSettingsPanel from "./components/TaskRunSettingsPanel";
import type { TaskRunSettingsHandle, TaskRuntimeState } from "./components/TaskRunSettingsPanel";
import TaskWorkbenchHeader from "./components/TaskWorkbenchHeader";
import VersionDiffModal, { type DiffPane } from "./components/VersionDiffModal";
import WebhookTriggerPanel from "./components/WebhookTriggerPanel";
import type { WebhookRuntimeState, WebhookTriggerHandle } from "./components/WebhookTriggerPanel";
import WebhookWorkbenchHeader from "./components/WebhookWorkbenchHeader";
import WorkerStatus from "./components/WorkerStatus";
import { useExecutionWatcher } from "./hooks/useExecutionWatcher";
import { DEPENDENCY_UI, TASK_STARTER_CODE, WEBHOOK_STARTER_CODE } from "./languages";
import { RUNTIME_REFRESH_POLICY } from "./runtime-refresh-policy";
import { isTerminal } from "./status";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  Adapter,
  AdapterLanguage,
  AdapterType,
  AiCandidate,
  AiSelectionContext,
  Execution,
  VersionDetail,
  VersionSummary,
  Worker,
} from "./types";
import { userErrorMessage } from "./user-message";

const INITIAL_TASK_RUNTIME_STATE: TaskRuntimeState = {
  scheduleEnabled: false,
  loading: true,
  activeExecution: false,
  canRun: false,
  scheduleEnableBlockedReason: "运行设置正在加载",
};

const INITIAL_WEBHOOK_RUNTIME_STATE: WebhookRuntimeState = {
  loaded: false,
  enabled: false,
  runtimeLocked: false,
  changingState: false,
  startBlockedReason: "Webhook 运行设置正在加载",
};

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
  return userErrorMessage(error);
}

type WorkbenchTabKey = "edit" | "runtime" | "history";

// 编辑页次级配置区（语言依赖 | 凭据绑定）。M5.5.9：运行参数（JSON）已退出
// 用户主流程；普通、非敏感配置由代码本身表达。
type ConfigTabKey = "requirements" | "bindings";

/** Working Copy / AI Candidate diff modal state. */
interface DiffViewState {
  title: string;
  originalTitle: string;
  modifiedTitle: string;
  panes: DiffPane[];
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

// M5.5.9：活跃 Adapter 名称唯一的前端预检。trim 后精确匹配（与 Backend 一致），
// 已软删除（archived_at）或自身（excludeId）不参与冲突。
function activeNameConflict(
  adapters: Adapter[],
  name: string,
  excludeId: number | null,
): boolean {
  const trimmed = name.trim();
  return adapters.some(
    (adapter) =>
      !adapter.archived_at &&
      adapter.id !== excludeId &&
      adapter.name === trimmed,
  );
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
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [workersLoading, setWorkersLoading] = useState(true);
  const [workersError, setWorkersError] = useState<string | null>(null);

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
  // Save is gated on it so stale or failed loads can never be persisted.
  const [contentReady, setContentReady] = useState(false);
  // Low-frequency Adapter settings live in a drawer, outside the main work area.
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Controlled Workbench tab.
  const [activeTabKey, setActiveTabKey] = useState<WorkbenchTabKey>("edit");
  // M3.2：编辑页次级配置 Tabs 与系统设置抽屉（凭据管理 + Python 包源）。
  const [configTabKey, setConfigTabKey] = useState<ConfigTabKey>("requirements");
  const [systemSettingsOpen, setSystemSettingsOpen] = useState(false);
  const [aiPanelOpen, setAiPanelOpen] = useState(false);
  // M5.5.5：Monaco 非空选区一键加入 AI 上下文。
  // editorRef 只在点击“加入对话上下文”时读取本次实际选择；editorHasSelection
  // 只驱动按钮可用性（空选区不提供无意义操作）；aiSelectedContext 是已确认的
  // 快照，属于当前 Adapter / 当前会话，光标后续移动不会改变它。
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const [editorHasSelection, setEditorHasSelection] = useState(false);
  const [aiSelectedContext, setAiSelectedContext] = useState<AiSelectionContext | null>(null);
  const [diffView, setDiffView] = useState<DiffViewState | null>(null);
  const [taskRuntimeState, setTaskRuntimeState] = useState<TaskRuntimeState>(INITIAL_TASK_RUNTIME_STATE);
  const [webhookRuntimeState, setWebhookRuntimeState] = useState<WebhookRuntimeState>(INITIAL_WEBHOOK_RUNTIME_STATE);
  const taskRuntimeRef = useRef<TaskRunSettingsHandle>(null);
  const webhookRuntimeRef = useRef<WebhookTriggerHandle>(null);
  const [liveLogOpen, setLiveLogOpen] = useState(false);
  const [liveLogFullscreen, setLiveLogFullscreen] = useState(false);
  const [waitingForWebhook, setWaitingForWebhook] = useState(false);
  const [saveWorkerPromptOpen, setSaveWorkerPromptOpen] = useState(false);
  const [saveWorkerId, setSaveWorkerId] = useState<number | null>(null);
  const [cloneSource, setCloneSource] = useState<Adapter | null>(null);
  const [cloneName, setCloneName] = useState("");
  // Known version-id -> seq values, cached once a version list has loaded and
  // kept up to date on save. Unvisited Catalog rows keep internal Revision ids
  // secondary, so this cache never causes extra list requests.
  const [versionSeqById, setVersionSeqById] = useState<Map<number, number>>(new Map());
  const { preference: themePreference, resolvedTheme: editorTheme, setPreference: setThemePreference } = useMonacoTheme();
  // M5.5.9：Monaco 主题稳定。手工选择深色/浅色必须跨刷新/切换/remount 保持；
  // 除主编辑器外（Diff 弹窗等）任何组件都不允许把全局主题改回默认。这里在
  // 每次 resolvedTheme 变化后通过 loader 重新落一次全局主题，作为最后防线，
  // 避免其他组件 setTheme 竞态把用户的手工选择覆盖掉。
  useEffect(() => {
    let cancelled = false;
    void loader.init().then((monacoInstance) => {
      if (!cancelled) {
        monacoInstance.editor.setTheme(editorTheme);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [editorTheme]);
  // Monotonic guard: only the newest content-loading request may commit state, so
  // rapid adapter switches cannot mix state or save one adapter's snapshot into another.
  const requestGeneration = useRef(0);
  const liveWatcher = useExecutionWatcher((watchError) => setError(watchError));
  const liveWatchRef = useRef(liveWatcher.watch);
  const refreshedTerminalExecutionId = useRef<number | null>(null);

  const dirty =
    snapshot.code !== baseline.code ||
    snapshot.requirements !== baseline.requirements ||
    snapshot.runtimeConfigText !== baseline.runtimeConfigText;
  const selectedAdapterId = selected?.id ?? null;
  const activeExecutionId = selected?.running_execution_id ?? null;
  const selectedTriggerLocked = selected?.runtime_locked === true;

  useEffect(() => {
    liveWatchRef.current = liveWatcher.watch;
  });

  const handleTaskRuntimeStateChange = useCallback((state: TaskRuntimeState) => {
    setTaskRuntimeState(state);
  }, []);

  const handleWebhookRuntimeStateChange = useCallback((state: WebhookRuntimeState) => {
    setWebhookRuntimeState(state);
  }, []);

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

  // Reconcile selected runtime state while an Execution is active. An enabled
  // Task Schedule also polls while idle so a newly created background run is
  // discovered and exposes Stop immediately; cleanup prevents an old Adapter
  // response from overwriting a newly selected one.
  useEffect(() => {
    if (
      busy ||
      selectedAdapterId === null ||
      (activeExecutionId === null && !selectedTriggerLocked)
    ) {
      return;
    }
    const adapterId = selectedAdapterId;
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    async function refreshActiveRuntime() {
      try {
        const refreshed = await api.getAdapter(adapterId);
        if (cancelled) {
          return;
        }
        setSelected((current) => (current?.id === adapterId ? refreshed : current));
        setAdapters((current) =>
          current.map((item) => (item.id === adapterId ? refreshed : item)),
        );
        if (
          refreshed.running_execution_id != null ||
          refreshed.runtime_locked === true
        ) {
          timeoutId = setTimeout(
            () => void refreshActiveRuntime(),
            RUNTIME_REFRESH_POLICY.pollIntervalMs,
          );
        }
      } catch {
        // A transient read failure must not unlock lifecycle actions. Keep the
        // last authoritative active pointer and retry quietly.
        if (!cancelled) {
          timeoutId = setTimeout(
            () => void refreshActiveRuntime(),
            RUNTIME_REFRESH_POLICY.pollIntervalMs,
          );
        }
      }
    }

    timeoutId = setTimeout(
      () => void refreshActiveRuntime(),
      RUNTIME_REFRESH_POLICY.pollIntervalMs,
    );
    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        clearTimeout(timeoutId);
      }
    };
  }, [
    activeExecutionId,
    busy,
    selectedAdapterId,
    selectedTriggerLocked,
  ]);

  // A Manual click is handed to the watcher immediately. Schedule and
  // Webhook runs can begin in the background, so reconcile the Adapter's
  // authoritative active pointer without switching the user's current tab or
  // closing a historical detail drawer.
  useEffect(() => {
    if (
      selectedAdapterId === null ||
      activeExecutionId === null ||
      liveWatcher.execution?.id === activeExecutionId
    ) {
      return;
    }
    const adapterId = selectedAdapterId;
    let cancelled = false;
    void api.getExecution(activeExecutionId).then((execution) => {
      if (cancelled || execution.adapter_id !== adapterId) {
        return;
      }
      refreshedTerminalExecutionId.current = null;
      liveWatchRef.current(execution);
      setWaitingForWebhook(false);
      setLiveLogOpen(true);
      setLiveLogFullscreen(false);
      if (execution.trigger === "schedule") {
        messageApi.info(`定时执行 #${execution.id} 已开始，实时日志已在页面底部打开。`);
      }
    }).catch((watchError) => {
      if (!cancelled) {
        setError(errorMessage(watchError));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeExecutionId, liveWatcher.execution?.id, messageApi, selectedAdapterId]);

  useEffect(() => {
    const execution = liveWatcher.execution;
    if (
      execution === null ||
      !isTerminal(execution.status) ||
      execution.adapter_id !== selectedAdapterId ||
      refreshedTerminalExecutionId.current === execution.id
    ) {
      return;
    }
    refreshedTerminalExecutionId.current = execution.id;
    void api.getAdapter(execution.adapter_id).then((refreshed) => {
      setSelected((current) => (current?.id === refreshed.id ? refreshed : current));
      setAdapters((current) => current.map((item) => item.id === refreshed.id ? refreshed : item));
    }).catch((refreshError) => setError(errorMessage(refreshError)));
  }, [liveWatcher.execution, selectedAdapterId]);

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

  const handleTaskAdapterChange = useCallback((refreshed: Adapter) => {
    setSelected((current) => (current?.id === refreshed.id ? refreshed : current));
    setAdapters((current) =>
      current.map((item) => (item.id === refreshed.id ? refreshed : item)),
    );
  }, []);

  const handleExecutionStarted = useCallback((execution: Execution) => {
    refreshedTerminalExecutionId.current = null;
    liveWatchRef.current(execution);
    setWaitingForWebhook(false);
    setLiveLogOpen(true);
    setLiveLogFullscreen(false);
  }, []);

  const handleWebhookReceivingChange = useCallback((enabled: boolean) => {
    setWaitingForWebhook(enabled);
    if (enabled) {
      setLiveLogOpen(true);
      setLiveLogFullscreen(false);
    }
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
  // server-provided summaries instead of extra requests.
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
    setError(null);
    setVersions([]);
    setSelectedVersionId(null);
    setContentReady(false);
    setSettingsOpen(false);
    setActiveTabKey("edit");
    setConfigTabKey("requirements");
    liveWatcher.stop();
    setLiveLogOpen(false);
    setLiveLogFullscreen(false);
    setWaitingForWebhook(false);
    setTaskRuntimeState(INITIAL_TASK_RUNTIME_STATE);
    setWebhookRuntimeState(INITIAL_WEBHOOK_RUNTIME_STATE);
    // M5.5.5：选区上下文只属于当前 Adapter/会话；切换时立即清理，
    // 旧 Adapter 的选区不会串到新 Adapter。
    setAiSelectedContext(null);
    setEditorHasSelection(false);
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
          code:
            adapter.adapter_type === "task"
              ? TASK_STARTER_CODE[adapter.language]
              : WEBHOOK_STARTER_CODE[adapter.language],
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
    adapterType: AdapterType,
  ): Promise<boolean> {
    if (busy) {
      return false;
    }
    if (!confirmDiscard()) {
      return false;
    }
    // M5.5.9：前端预检同名（活跃）适配器，给出明确中文提示。
    if (activeNameConflict(adapters, createdName, null)) {
      messageApi.error("已存在同名适配器，请使用其他名称。");
      return false;
    }
    setBusy(true);
    try {
      setError(null);
      const created = await api.createAdapter({
        name: createdName,
        description: createdDescription,
        language,
        adapter_type: adapterType,
      });
      await refreshAdapters();
      await loadAdapterContent(created);
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.code === "adapter_name_conflict") {
        messageApi.error("已存在同名适配器，请使用其他名称。");
      } else {
        setError(errorMessage(err));
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function persistVersion(runtimeWorkerId?: number) {
    if (!selected || busy || !contentReady || selected.runtime_locked === true) {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(snapshot.runtimeConfigText);
    if (runtimeConfig === null) {
      setError("运行参数必须是合法的 JSON 对象");
      return;
    }
    if (!snapshot.code.trim()) {
      setError("代码不能为空");
      return;
    }
    setBusy(true);
    try {
      setError(null);
      let saveTarget = selected;
      if (runtimeWorkerId !== undefined && selected.runtime_worker_id == null) {
        saveTarget = await api.updateAdapter(selected.id, { runtime_worker_id: runtimeWorkerId });
        setSelected(saveTarget);
        setAdapters((current) => current.map((item) => item.id === saveTarget.id ? saveTarget : item));
      }
      const saved = await api.saveVersion(saveTarget.id, {
        code: snapshot.code,
        requirements: snapshot.requirements,
        runtime_config: runtimeConfig,
      });
      // The immutable version exists as soon as POST succeeds: acknowledge it locally
      // right away so a follow-up refresh failure cannot be mistaken for a failed save
      // (which would invite retrying into a duplicate immutable version). Only
      // latest_version_id is derived from the response; Adapter.updated_at stays
      // the server-owned value until a real Adapter refresh succeeds.
      const optimistic: Adapter = { ...saveTarget, latest_version_id: saved.id };
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
        const versionList = await api.listVersions(saveTarget.id);
        setVersions(versionList);
      } catch (refreshErr) {
        refreshFailures.push(`刷新版本列表失败：${errorMessage(refreshErr)}`);
      }
      try {
        // Best-effort refresh of the real Adapter (server-owned updated_at);
        // failure is non-fatal because the save itself is already acknowledged.
        const real = await api.getAdapter(saveTarget.id);
        setSelected(real);
        setAdapters((current) => current.map((item) => (item.id === real.id ? real : item)));
      } catch (refreshErr) {
        refreshFailures.push(`刷新适配器失败：${errorMessage(refreshErr)}`);
      }
      if (refreshFailures.length === 0) {
        messageApi.success("适配器已保存");
      } else {
        setError(`适配器已保存，但${refreshFailures.join("；")}`);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function handleSaveVersion() {
    if (!selected || busy || !contentReady || selected.runtime_locked === true) {
      return;
    }
    if (parseRuntimeConfig(snapshot.runtimeConfigText) === null) {
      setError("运行参数必须是合法的 JSON 对象");
      return;
    }
    if (!snapshot.code.trim()) {
      setError("代码不能为空");
      return;
    }
    if (selected.runtime_worker_id != null) {
      void persistVersion();
      return;
    }
    const compatibleOnlineWorkers = workers.filter(
      (worker) => worker.status === "online" && worker.capabilities.includes(selected.language),
    );
    if (compatibleOnlineWorkers.length === 1) {
      void persistVersion(compatibleOnlineWorkers[0].id);
      return;
    }
    setSaveWorkerId(null);
    setSaveWorkerPromptOpen(true);
  }

  function handleClone(source?: Adapter) {
    const cloneTarget = source ?? selected;
    if (!cloneTarget || busy) {
      return;
    }
    setCloneSource(cloneTarget);
    setCloneName(`${cloneTarget.name}-copy`);
  }

  // M5.5.9：目录三点菜单“设置”——选中该 Adapter（尊重 busy/未保存确认）并打开设置。
  function handleCatalogOpenSettings(adapter: Adapter) {
    if (busy) {
      return;
    }
    if (selected?.id === adapter.id) {
      setSettingsOpen(true);
      return;
    }
    if (!confirmDiscard()) {
      return;
    }
    void loadAdapterContent(adapter);
    setSettingsOpen(true);
  }

  async function performClone() {
    if (cloneSource === null || cloneName.trim() === "" || busy) {
      return;
    }
    const source = cloneSource;
    const targetName = cloneName.trim();
    // M5.5.9：前端预检同名（活跃）适配器。
    if (activeNameConflict(adapters, targetName, null)) {
      messageApi.error("已存在同名适配器，请使用其他名称。");
      return;
    }
    setCloneSource(null);
    setBusy(true);
    try {
      setError(null);
      const created = await api.cloneAdapter(source.id, { name: targetName });
      const list = await refreshAdapters();
      const target = list.find((item) => item.id === created.id) ?? created;
      await loadAdapterContent(target);
    } catch (err) {
      if (err instanceof ApiError && err.code === "adapter_name_conflict") {
        messageApi.error("已存在同名适配器，请使用其他名称。");
      } else {
        setError(errorMessage(err));
      }
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
      title: "版本差异：工作副本与基准版本",
      originalTitle: baseLabel,
      modifiedTitle: "工作副本（当前编辑内容）",
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

  function handleApplyAiCandidate(candidate: AiCandidate) {
    if (
      !selected ||
      selected.archived_at ||
      selected.runtime_locked === true ||
      !contentReady ||
      busy
    ) {
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

  // M5.5.5：把 Monaco 当前选区作为精确快照加入 AI 上下文。文本与行号在点击
  // 瞬间从编辑器读取，之后光标移动不会偷偷改变已加入的上下文。
  function handleAddSelectedContext() {
    const editor = editorRef.current;
    if (editor === null || busy || !contentReady) {
      return;
    }
    const selection = editor.getSelection();
    const model = editor.getModel();
    if (selection === null || model === null || selection.isEmpty()) {
      return;
    }
    const text = model.getValueInRange(selection);
    if (text.trim() === "") {
      return;
    }
    setAiSelectedContext({
      text,
      start_line: selection.startLineNumber,
      end_line: selection.endLineNumber,
    });
  }

  async function handleUpdateDetails() {
    if (!selected || busy) {
      return;
    }
    // M5.5.9：重命名预检——trim 后与活跃同名拒绝。
    if (name.trim() !== "" && activeNameConflict(adapters, name, selected.id)) {
      messageApi.error("已存在同名适配器，请使用其他名称。");
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
        messageApi.success("适配器信息已保存");
      } catch {
        setAdapters((current) =>
          current.map((item) => (item.id === refreshed.id ? refreshed : item)),
        );
        setError("适配器信息已保存，但列表刷新失败；请手动刷新确认。");
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "adapter_name_conflict") {
        messageApi.error("已存在同名适配器，请使用其他名称。");
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!selected || busy) {
      return;
    }
    const warning = dirty ? "该适配器存在未保存的编辑器修改。" : "";
    if (
      !window.confirm(`确定删除适配器“${selected.name}”吗？删除后它会从活跃列表移除。${warning}`)
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
      liveWatcher.stop();
      setLiveLogOpen(false);
      setWaitingForWebhook(false);
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
      ? "控制服务检查中…"
      : health === "ok"
        ? "控制服务正常"
        : health === "degraded"
          ? "控制服务降级"
          : "控制服务不可达";

  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const selectedRuntimeWorker = selected?.runtime_worker_id == null
    ? null
    : (workers.find((worker) => worker.id === selected.runtime_worker_id) ?? null);
  const liveExecution = liveWatcher.execution?.adapter_id === selected?.id
    ? liveWatcher.execution
    : null;

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
          onOpenSettings={handleCatalogOpenSettings}
          onClone={(adapter) => void handleClone(adapter)}
        />

        {/*
          M5.5.4：AI 助手放在 Workbench 之前的 DOM 位置，视觉上仍通过 flex
          order 停留在最右侧。Monaco 会捕获 Tab 焦点，若助手位于编辑器之后，
          键盘用户将永远无法用 Tab 到达悬浮入口。
        */}
        <AiAssistantPanel
          key={`ai-assistant-${selected?.id ?? "none"}`}
          open={aiPanelOpen}
          adapter={selected}
          selectedVersionId={selectedVersionId}
          selectedVersionSeq={selectedVersion?.seq ?? null}
          workingCopy={snapshot}
          contentReady={contentReady}
          busy={busy}
          selectedContext={aiSelectedContext}
          theme={editorTheme}
          onOpen={() => setAiPanelOpen(true)}
          onClose={() => setAiPanelOpen(false)}
          onApply={handleApplyAiCandidate}
          onClearSelectedContext={() => setAiSelectedContext(null)}
        />

        <main className="workbench">
          {selected === null ? (
            <div className="workbench-empty">请选择一个适配器进行管理。</div>
          ) : (
            <section className="detail">
              {selected.adapter_type === "task" ? (
                <TaskWorkbenchHeader
                  adapter={selected}
                  runtimeWorker={selectedRuntimeWorker}
                  runtimeState={taskRuntimeState}
                  dirty={dirty}
                  busy={busy}
                  contentReady={contentReady}
                  onSave={() => void handleSaveVersion()}
                  onOpenSettings={() => setSettingsOpen(true)}
                  onRunOnce={() => taskRuntimeRef.current?.runOnce()}
                  onStopExecution={() => taskRuntimeRef.current?.stopExecution()}
                  onToggleSchedule={() => taskRuntimeRef.current?.toggleSchedule()}
                />
              ) : (
              <WebhookWorkbenchHeader
                adapter={selected}
                runtimeWorker={selectedRuntimeWorker}
                runtimeState={webhookRuntimeState}
                busy={busy}
                contentReady={contentReady}
                onSave={() => void handleSaveVersion()}
                onOpenSettings={() => setSettingsOpen(true)}
                onToggleReceiving={() => webhookRuntimeRef.current?.toggleReceiving()}
              />
              )}

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
                            查看差异
                          </Button>
                          <Button
                            size="small"
                            data-testid="add-ai-selection"
                            disabled={busy || !contentReady || !editorHasSelection}
                            onClick={handleAddSelectedContext}
                          >
                            加入对话上下文
                          </Button>
                        </div>
                        <div className="editor-main" data-testid="editor-main" data-monaco-theme={editorTheme}>
                          <Editor
                            height="100%"
                            theme={editorTheme}
                            language={selected.language}
                            value={snapshot.code}
                            onMount={(editor) => {
                              editorRef.current = editor;
                              const updateSelectionState = () => {
                                const selection = editor.getSelection();
                                setEditorHasSelection(
                                  selection !== null && !selection.isEmpty(),
                                );
                              };
                              updateSelectionState();
                              editor.onDidChangeCursorSelection(updateSelectionState);
                            }}
                            onChange={(value) => setSnapshot((current) => ({ ...current, code: value ?? "" }))}
                            options={{
                              minimap: { enabled: false },
                              readOnly: busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true,
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
                                    disabled={busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true}
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
                                key: "bindings",
                                label: "凭据绑定",
                                children: (
                                  <CredentialBindingsEditor
                                    adapterId={selected.id}
                                    disabled={busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true}
                                    onError={setError}
                                    onOpenSettings={() => setSystemSettingsOpen(true)}
                                  />
                                ),
                              },
                            ]}
                          />
                        </div>
                      </div>
                    ),
                  },
                  selected.adapter_type === "task"
                    ? {
                        key: "runtime",
                        label: "运行设置",
                        forceRender: true,
                        children: (
                          <TaskRunSettingsPanel
                            ref={taskRuntimeRef}
                            key={selected.id}
                            adapter={selected}
                            workers={workers}
                            workersLoading={workersLoading}
                            workersError={workersError}
                            execution={liveExecution}
                            dirty={dirty}
                            onAdapterChange={handleTaskAdapterChange}
                            onExecutionStarted={handleExecutionStarted}
                            onRuntimeStateChange={handleTaskRuntimeStateChange}
                            onError={setError}
                          />
                        ),
                      }
                    : {
                        key: "runtime",
                        label: "运行设置",
                        forceRender: true,
                        children: (
                          <WebhookTriggerPanel
                            ref={webhookRuntimeRef}
                            key={selected.id}
                            adapter={selected}
                            workers={workers}
                            workersLoading={workersLoading}
                            workersError={workersError}
                            onAdapterChange={handleTaskAdapterChange}
                            onReceivingChange={handleWebhookReceivingChange}
                            onRuntimeStateChange={handleWebhookRuntimeStateChange}
                            onError={setError}
                          />
                        ),
                      },
                  {
                    key: "history",
                    label: selected.adapter_type === "webhook" ? "调用记录" : "执行记录",
                    // antd Tabs render lazily: the history API is only called
                    // after this tab is activated for the first time.
                    children: (
                      <ExecutionHistoryPanel
                        key={selected.id}
                        adapterId={selected.id}
                        trigger={selected.adapter_type === "webhook" ? "webhook" : undefined}
                        recordLabel={selected.adapter_type === "webhook" ? "调用记录" : "执行记录"}
                      />
                    ),
                  },
                ]}
              />
              <LiveLogWorkspace
                execution={liveExecution}
                liveStdout={liveWatcher.liveStdout}
                liveStderr={liveWatcher.liveStderr}
                fallbackExhausted={liveWatcher.fallbackExhausted}
                waitingForWebhook={selected.adapter_type === "webhook" && waitingForWebhook && liveExecution === null}
                open={liveLogOpen}
                fullscreen={liveLogFullscreen}
                onOpen={() => setLiveLogOpen(true)}
                onClose={() => {
                  setLiveLogOpen(false);
                  setLiveLogFullscreen(false);
                }}
                onEnterFullscreen={() => setLiveLogFullscreen(true)}
                onRestoreBottom={() => setLiveLogFullscreen(false)}
              />
            </section>
          )}
        </main>
      </div>

      <AdapterSettingsDrawer
        open={settingsOpen}
        adapter={selected}
        name={name}
        description={description}
        busy={busy}
        contentReady={contentReady}
        onClose={() => setSettingsOpen(false)}
        onNameChange={setName}
        onDescriptionChange={setDescription}
        onUpdate={() => void handleUpdateDetails()}
        onDelete={() => void handleDelete()}
        onClone={() => void handleClone()}
      />

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
        theme={editorTheme}
        onClose={() => setDiffView(null)}
      />

      <Modal
        title="复制适配器"
        open={cloneSource !== null}
        okText="复制"
        cancelText="取消"
        confirmLoading={busy}
        okButtonProps={{ disabled: cloneName.trim() === "" }}
        onCancel={() => setCloneSource(null)}
        onOk={() => void performClone()}
      >
        <div className="clone-confirm">
          <p>将复制当前代码、依赖、运行配置、凭据引用和运行节点。</p>
          <p>执行历史不会复制；新适配器创建后保持停止，不会自动运行。</p>
          <label className="settings-field">
            <span className="settings-field-label">新适配器名称</span>
            <Input
              autoFocus
              data-testid="clone-adapter-name"
              value={cloneName}
              onChange={(event) => setCloneName(event.target.value)}
            />
          </label>
        </div>
      </Modal>

      <Modal
        title="保存适配器"
        open={saveWorkerPromptOpen}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ disabled: saveWorkerId === null }}
        onCancel={() => {
          setSaveWorkerPromptOpen(false);
          setSaveWorkerId(null);
        }}
        onOk={() => {
          if (saveWorkerId === null) {
            return;
          }
          setSaveWorkerPromptOpen(false);
          void persistVersion(saveWorkerId);
        }}
      >
        <div className="save-worker-prompt">
          <p>第一次保存需要确定运行节点。后续可在“运行设置”中查看或修改。</p>
          <label className="settings-field">
            <span className="settings-field-label">运行节点 *</span>
            <Select
              aria-label="保存适配器运行节点"
              data-testid="save-worker-select"
              value={saveWorkerId ?? undefined}
              placeholder="请选择在线且支持当前语言的运行节点"
              onChange={(value: number) => setSaveWorkerId(value)}
              options={workers
                .filter((worker) =>
                  selected !== null &&
                  worker.status === "online" &&
                  worker.capabilities.includes(selected.language),
                )
                .map((worker) => ({ value: worker.id, label: worker.name }))}
            />
          </label>
          {workers.filter((worker) =>
            selected !== null &&
            worker.status === "online" &&
            worker.capabilities.includes(selected.language),
          ).length === 0 && (
            <p className="settings-danger-hint" role="alert">当前没有可用的兼容运行节点，请先启动或注册运行节点。</p>
          )}
        </div>
      </Modal>
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
