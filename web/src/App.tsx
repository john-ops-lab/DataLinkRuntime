import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import Editor, { loader } from "@monaco-editor/react";
import type * as monaco from "monaco-editor";
import { Button, Input, message, Modal, Segmented, Select, Tabs, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import { adapterAccessLevel, canEditAdapter, canManageAdapter } from "./adapter-access";
import AccountApp from "./AccountApp";
import AdapterCatalog from "./components/AdapterCatalog";
import AdapterSettingsDrawer from "./components/AdapterSettingsDrawer";
import AiAssistantPanel, {
  type AiContextSnippetEntry,
} from "./components/AiAssistantPanel";
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
import UserManagementDrawer from "./components/UserManagementDrawer";
import { useExecutionWatcher } from "./hooks/useExecutionWatcher";
import { applySystemLocale, currentSystemLocale, i18n, isSystemLocale } from "./i18n";
import { currentEntryMode } from "./entry-mode";
import DlrDesignSystemProvider from "./design-system";
export { ANT_DESIGN_LOCALES } from "./design-system";
import {
  dependencyNoteFor,
  dependencyUiFor,
  starterCodeFor,
} from "./languages";
import { RUNTIME_REFRESH_POLICY } from "./runtime-refresh-policy";
import { isTerminal } from "./status";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  AccountPrincipal,
  Adapter,
  AdapterAccessLevel,
  AdapterLanguage,
  AdapterType,
  AiCandidate,
  AiContextSnippet,
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
  scheduleEnableBlockedReason: null,
};

const INITIAL_WEBHOOK_RUNTIME_STATE: WebhookRuntimeState = {
  loaded: false,
  enabled: false,
  runtimeLocked: false,
  changingState: false,
  startBlockedReason: null,
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

// M5.5.10：实时日志是独立 Tab，不再有覆盖页面的底部浮层。
type WorkbenchTabKey = "edit" | "runtime" | "history" | "live";

// 编辑页次级配置区（语言依赖 | 凭据绑定）。M5.5.9：运行参数（JSON）已退出
// 用户主流程；普通、非敏感配置由代码本身表达。
type ConfigTabKey = "requirements" | "bindings";

/** Working Copy / AI Candidate diff modal state (display strings are derived
 * at render time so an open modal switches language immediately). */
interface DiffViewState {
  baseSeq: number | null;
  adapterLanguage: AdapterLanguage;
  panes: Omit<DiffPane, "label">[];
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

interface AdapterConsoleProps {
  accountPrincipal?: AccountPrincipal;
  onAccountLogout?: () => Promise<void>;
  onOpenAccountProfile?: () => void;
}

export function AdapterConsole({
  accountPrincipal,
  onAccountLogout,
  onOpenAccountProfile,
}: AdapterConsoleProps = {}) {
  const { t } = useTranslation(["common", "adapter", "runtime"]);
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
  const [userManagementOpen, setUserManagementOpen] = useState(false);
  const [aiPanelOpen, setAiPanelOpen] = useState(false);
  // M5.5.13：已确认的多上下文片段（Monaco 代码选区 / 实时日志脱敏文本选区），
  // 属于当前 Adapter / 当前会话，光标后续移动不会改变已加入的快照。
  // editorRef 只在点击「加入对话上下文」时读取本次实际选择；editorHasSelection
  // 只驱动按钮可用性（空选区不提供无意义操作）。
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const [editorHasSelection, setEditorHasSelection] = useState(false);
  const nextSnippetId = useRef(1);
  const [aiContextSnippets, setAiContextSnippets] = useState<AiContextSnippetEntry[]>([]);
  const [diffView, setDiffView] = useState<DiffViewState | null>(null);
  const [taskRuntimeState, setTaskRuntimeState] = useState<TaskRuntimeState>(INITIAL_TASK_RUNTIME_STATE);
  const [webhookRuntimeState, setWebhookRuntimeState] = useState<WebhookRuntimeState>(INITIAL_WEBHOOK_RUNTIME_STATE);
  const taskRuntimeRef = useRef<TaskRunSettingsHandle>(null);
  const webhookRuntimeRef = useRef<WebhookTriggerHandle>(null);
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
  // A no-version Adapter still has a browser-only starter snapshot. Keep the
  // locale chosen when it was first created/loaded so navigation cannot rewrite
  // that Working Copy after a system-locale switch.
  const starterLocaleByAdapter = useRef(
    new Map<number, ReturnType<typeof currentSystemLocale>>(),
  );
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
  const canManageUsers = accountPrincipal === undefined || accountPrincipal.role === "admin";
  const selectedAccessLevel: AdapterAccessLevel = selected === null
    ? "admin"
    : adapterAccessLevel(selected, accountPrincipal);
  const selectedCanEdit = canEditAdapter(selectedAccessLevel);
  const selectedCanManage = canManageAdapter(selectedAccessLevel);
  const selectedCanUseAi = selectedCanEdit;

  useEffect(() => {
    if (!selectedCanUseAi) {
      // ACL changes close an already-open AI surface immediately; the backend
      // remains authoritative for any in-flight request.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAiPanelOpen(false);
    }
  }, [selectedCanUseAi]);

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
      if (execution.trigger === "schedule") {
        // The message is a transient toast: translate it with the locale at
        // fire time without subscribing this effect to language changes.
        messageApi.info(i18n.t("messages.scheduleStarted"));
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
    // M5.5.10：用户手动触发的运行直接切换到「实时日志」Tab。
    refreshedTerminalExecutionId.current = null;
    liveWatchRef.current(execution);
    setWaitingForWebhook(false);
    setActiveTabKey("live");
  }, []);

  const handleWebhookReceivingChange = useCallback((enabled: boolean) => {
    setWaitingForWebhook(enabled);
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
    return window.confirm(t("confirm.discardChanges"));
  }

  async function loadAdapterContent(
    adapter: Adapter,
    starterLocaleOverride?: ReturnType<typeof currentSystemLocale>,
  ) {
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
    setWaitingForWebhook(false);
    setTaskRuntimeState(INITIAL_TASK_RUNTIME_STATE);
    setWebhookRuntimeState(INITIAL_WEBHOOK_RUNTIME_STATE);
    // M5.5.13：上下文片段只属于当前 Adapter/会话；切换时立即清理，
    // 旧 Adapter 的片段不会串到新 Adapter。
    setAiContextSnippets([]);
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
        const starterLocale =
          starterLocaleOverride ??
          starterLocaleByAdapter.current.get(adapter.id) ??
          currentSystemLocale();
        starterLocaleByAdapter.current.set(adapter.id, starterLocale);
        applySnapshot({
          code: starterCodeFor(adapter.language, adapter.adapter_type, starterLocale),
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
      messageApi.error(t("messages.nameConflict"));
      return false;
    }
    const starterLocale = currentSystemLocale();
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
      starterLocaleByAdapter.current.set(created.id, starterLocale);
      await loadAdapterContent(created, starterLocale);
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.code === "adapter_name_conflict") {
        messageApi.error(t("messages.nameConflict"));
      } else {
        setError(errorMessage(err));
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function persistVersion(runtimeWorkerId?: number) {
    if (!selected || !selectedCanEdit || busy || !contentReady || selected.runtime_locked === true) {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(snapshot.runtimeConfigText);
    if (runtimeConfig === null) {
      setError(t("validation.runtimeConfigJson"));
      return;
    }
    if (!snapshot.code.trim()) {
      setError(t("validation.codeRequired"));
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
        refreshFailures.push(t("messages.versionListRefreshFailed", { error: errorMessage(refreshErr) }));
      }
      try {
        // Best-effort refresh of the real Adapter (server-owned updated_at);
        // failure is non-fatal because the save itself is already acknowledged.
        const real = await api.getAdapter(saveTarget.id);
        setSelected(real);
        setAdapters((current) => current.map((item) => (item.id === real.id ? real : item)));
      } catch (refreshErr) {
        refreshFailures.push(t("messages.adapterRefreshFailed", { error: errorMessage(refreshErr) }));
      }
      if (refreshFailures.length === 0) {
        messageApi.success(t("messages.adapterSaved"));
      } else {
        setError(t("messages.adapterSavedRefreshSummary", {
          details: refreshFailures.join(i18n.t("punctuation.listSeparator")),
        }));
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function handleSaveVersion() {
    if (!selected || !selectedCanEdit || busy || !contentReady || selected.runtime_locked === true) {
      return;
    }
    if (parseRuntimeConfig(snapshot.runtimeConfigText) === null) {
      setError(t("validation.runtimeConfigJson"));
      return;
    }
    if (!snapshot.code.trim()) {
      setError(t("validation.codeRequired"));
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
    if (
      !cloneTarget ||
      busy ||
      !canEditAdapter(adapterAccessLevel(cloneTarget, accountPrincipal))
    ) {
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
    if (
      cloneSource === null ||
      cloneName.trim() === "" ||
      !canEditAdapter(adapterAccessLevel(cloneSource, accountPrincipal)) ||
      busy
    ) {
      return;
    }
    const source = cloneSource;
    const targetName = cloneName.trim();
    // M5.5.9：前端预检同名（活跃）适配器。
    if (activeNameConflict(adapters, targetName, null)) {
      messageApi.error(t("messages.nameConflict"));
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
        messageApi.error(t("messages.nameConflict"));
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
    setDiffView({
      baseSeq: selectedVersion?.seq ?? null,
      adapterLanguage: selected.language,
      panes: [
        {
          key: "code",
          language: selected.language,
          original: baseline.code,
          modified: snapshot.code,
        },
        {
          key: "requirements",
          language: "plaintext",
          original: baseline.requirements,
          modified: snapshot.requirements,
        },
        {
          key: "runtime-config",
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
      !selectedCanUseAi ||
      selected.archived_at ||
      selected.runtime_locked === true ||
      !contentReady ||
      busy
    ) {
      return;
    }
    // M5.8-003: AI Apply is code-only. Requirements, runtime_config,
    // Credential Binding and runtime configuration remain the administrator's
    // manual Working Copy and are never replaced by a Candidate.
    setSnapshot((current) => ({ ...current, code: candidate.code }));
  }

  // M5.5.13：把 Monaco 当前选区作为精确快照追加进 AI 上下文，并自动展开
  // AI 面板。文本与行号在点击瞬间从编辑器读取，之后光标移动不会偷偷改变
  // 已加入的上下文；新片段追加，不覆盖已有片段。
  function handleAddSelectedContext() {
    const editor = editorRef.current;
    if (!selectedCanUseAi || editor === null || busy || !contentReady) {
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
    appendContextSnippet({
      source: "code",
      text,
      start_line: selection.startLineNumber,
      end_line: selection.endLineNumber,
    });
  }

  /** 追加一个上下文片段（代码或实时日志脱敏文本），并自动展开 AI 面板。 */
  function appendContextSnippet(snippet: AiContextSnippet) {
    setAiContextSnippets((current) => [
      ...current,
      { id: nextSnippetId.current++, ...snippet },
    ]);
    setAiPanelOpen(true);
  }

  /** 实时日志 Tab：选中浏览器可见的已脱敏日志文本后加入上下文。 */
  function handleAddLogContext(snippet: AiContextSnippet) {
    if (!selectedCanUseAi) {
      return;
    }
    appendContextSnippet(snippet);
  }

  /** 删除某一片段（其余片段保持加入顺序）。 */
  function handleRemoveContextSnippet(id: number) {
    setAiContextSnippets((current) => current.filter((snippet) => snippet.id !== id));
  }

  /** 清空全部上下文片段。 */
  function handleClearContextSnippets() {
    setAiContextSnippets([]);
  }

  async function handleUpdateDetails() {
    if (!selected || !selectedCanEdit || busy) {
      return;
    }
    // M5.5.9：重命名预检——trim 后与活跃同名拒绝。
    if (name.trim() !== "" && activeNameConflict(adapters, name, selected.id)) {
      messageApi.error(t("messages.nameConflict"));
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
        messageApi.success(t("messages.adapterInfoSaved"));
      } catch {
        setAdapters((current) =>
          current.map((item) => (item.id === refreshed.id ? refreshed : item)),
        );
        setError(t("messages.adapterInfoSavedRefreshFailed"));
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "adapter_name_conflict") {
        messageApi.error(t("messages.nameConflict"));
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!selected || !selectedCanManage || busy) {
      return;
    }
    const warning = dirty ? t("messages.discardWarning") : "";
    if (
      !window.confirm(t("confirm.deleteAdapter", { name: selected.name, warning }))
    ) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      await api.deleteAdapter(selected.id);
      starterLocaleByAdapter.current.delete(selected.id);
      requestGeneration.current += 1;
      setSelected(null);
      setSelectedVersionId(null);
      setVersions([]);
      setContentReady(false);
      setSettingsOpen(false);
      setSystemSettingsOpen(false);
      liveWatcher.stop();
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
      ? t("health.loading")
      : health === "ok"
        ? t("health.ok")
        : health === "degraded"
          ? t("health.degraded")
          : t("health.unreachable");

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
          <span className="app-header-product">{t("product.tagline")}</span>
        </div>
        <div className="app-header-status">
          <span className="health-status">
            <span className={`health-dot ${healthDotClass}`.trim()} />
            <span data-testid="control-status">{healthText}</span>
          </span>
          {canManageUsers && (
            <>
              <Button
                size="small"
                data-testid="user-management"
                onClick={() => setUserManagementOpen(true)}
              >
                {t("actions.userManagement")}
              </Button>
              <Button
                size="small"
                data-testid="system-settings"
                onClick={() => setSystemSettingsOpen(true)}
              >
                {t("actions.systemSettings")}
              </Button>
            </>
          )}
          {accountPrincipal && onAccountLogout && (
            <>
              <span className="account-principal" data-testid="account-principal">
                {accountPrincipal.username} · {t(`auth.role.${accountPrincipal.role}`)}
              </span>
              {onOpenAccountProfile && (
                <Button
                  size="small"
                  data-testid="account-profile"
                  onClick={onOpenAccountProfile}
                >
                  {t("auth.profile")}
                </Button>
              )}
              <Button
                size="small"
                data-testid="account-logout"
                onClick={() => void onAccountLogout()}
              >
                {t("auth.logout")}
              </Button>
            </>
          )}
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
          accountPrincipal={accountPrincipal}
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
          contextSnippets={aiContextSnippets}
          theme={editorTheme}
          onOpen={() => setAiPanelOpen(true)}
          onClose={() => setAiPanelOpen(false)}
          onApply={handleApplyAiCandidate}
          canUseAi={selectedCanUseAi}
          onRemoveContextSnippet={handleRemoveContextSnippet}
          onClearContextSnippets={handleClearContextSnippets}
        />

        <main className="workbench">
          {selected === null ? (
            <div className="workbench-empty">{t("empty.noAdapter")}</div>
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
                  readOnly={!selectedCanEdit}
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
                readOnly={!selectedCanEdit}
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
                    label: t("labels.edit"),
                    children: (
                      <div className="editor-pane">
                        <div className="editor-toolbar">
                          <span className="editor-toolbar-label">{t("editor.theme")}</span>
                          <Segmented
                            size="small"
                            data-testid="editor-theme-picker"
                            value={themePreference}
                            options={[
                              { label: t("editor.dark"), value: "dark" },
                              { label: t("editor.light"), value: "light" },
                              { label: t("editor.system"), value: "system" },
                            ]}
                            onChange={(value) => setThemePreference(value as EditorThemePreference)}
                          />
                          <Button
                            size="small"
                            data-testid="working-diff"
                            disabled={busy || !contentReady}
                            onClick={handleOpenWorkingDiff}
                          >
                            {t("actions.viewDiff")}
                          </Button>
                          <Button
                            size="small"
                            data-testid="add-ai-selection"
                            disabled={!selectedCanUseAi || busy || !contentReady || !editorHasSelection}
                            onClick={handleAddSelectedContext}
                          >
                            {t("actions.addContext")}
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
                              readOnly: busy || !selectedCanEdit || !contentReady || !!selected.archived_at || selected.runtime_locked === true,
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
                                label: dependencyUiFor(selected.language).label,
                                children: (
                                  <>
                                    <textarea
                                      data-testid="requirements-input"
                                      rows={4}
                                      value={snapshot.requirements}
                                      disabled={busy || !selectedCanEdit || !contentReady || !!selected.archived_at || selected.runtime_locked === true}
                                      placeholder={dependencyUiFor(selected.language).placeholder}
                                      onChange={(event) =>
                                        setSnapshot((current) => ({
                                          ...current,
                                          requirements: event.target.value,
                                        }))
                                      }
                                    />
                                    <Typography.Text type="secondary" data-testid="dependency-note">
                                      {dependencyNoteFor()}
                                    </Typography.Text>
                                  </>
                                ),
                              },
                              {
                                key: "bindings",
                                label: t("labels.credentialBindings"),
                                children: (
                                  <CredentialBindingsEditor
                                    adapterId={selected.id}
                                    disabled={busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true || !selectedCanManage}
                                    accessLevel={selectedAccessLevel}
                                    useScopedCredentialOptions={accountPrincipal !== undefined}
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
                        label: t("labels.runtimeSettings"),
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
                            readOnly={!selectedCanEdit}
                          />
                        ),
                      }
                    : {
                        key: "runtime",
                        label: t("labels.runtimeSettings"),
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
                            readOnly={!selectedCanEdit}
                            canManageCredentials={selectedCanManage}
                            useScopedCredentialOptions={accountPrincipal !== undefined}
                          />
                        ),
                      },
                  {
                    key: "history",
                    label: selected.adapter_type === "webhook" ? t("labels.callHistory") : t("labels.history"),
                    // antd Tabs render lazily: the history API is only called
                    // after this tab is activated for the first time.
                    children: (
                      <ExecutionHistoryPanel
                        key={selected.id}
                        adapterId={selected.id}
                        trigger={selected.adapter_type === "webhook" ? "webhook" : undefined}
                        recordKind={selected.adapter_type === "webhook" ? "call" : "execution"}
                      />
                    ),
                  },
                  {
                    // M5.5.10：Task/Webhook 共用的独立「实时日志」Tab；forceRender
                    // 让后台定时/Webhook 运行与等待状态在切换到本 Tab 前就已就绪。
                    key: "live",
                    label: t("labels.liveLog"),
                    forceRender: true,
                    children: (
                      <LiveLogWorkspace
                        execution={liveExecution}
                        liveStdout={liveWatcher.liveStdout}
                        liveStderr={liveWatcher.liveStderr}
                        fallbackExhausted={liveWatcher.fallbackExhausted}
                        waitingForWebhook={
                          selected.adapter_type === "webhook" &&
                          waitingForWebhook &&
                          liveExecution === null
                        }
                        onAddContext={selectedCanUseAi ? handleAddLogContext : undefined}
                      />
                    ),
                  },
                ]}
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
        accessLevel={selectedAccessLevel}
        onPermissionsChanged={() => void refreshAdapters()}
      />

      <SystemSettingsDrawer
        open={systemSettingsOpen}
        onClose={() => setSystemSettingsOpen(false)}
      />

      {canManageUsers && (
        <UserManagementDrawer
          open={userManagementOpen}
          onClose={() => setUserManagementOpen(false)}
        />
      )}

      <VersionDiffModal
        open={diffView !== null}
        title={t("diff.workingTitle", { ns: "runtime" })}
        originalTitle={
          diffView?.baseSeq == null
            ? t("diff.baseVersionNone", { ns: "runtime" })
            : t("diff.baseVersion", { ns: "runtime", seq: diffView.baseSeq })
        }
        modifiedTitle={t("diff.workingModified", { ns: "runtime" })}
        panes={(diffView?.panes ?? []).map((pane) => ({
          ...pane,
          label:
            pane.key === "code"
              ? t("labels.code", { ns: "common" })
              : pane.key === "requirements"
                ? dependencyUiFor(diffView?.adapterLanguage ?? "python").label
                : t("labels.runtimeConfig", { ns: "common" }),
        }))}
        theme={editorTheme}
        onClose={() => setDiffView(null)}
      />

      <Modal
        title={t("clone.title", { ns: "adapter" })}
        open={cloneSource !== null}
        okText={t("clone.ok", { ns: "adapter" })}
        cancelText={t("actions.cancel")}
        confirmLoading={busy}
        okButtonProps={{ disabled: cloneName.trim() === "" }}
        onCancel={() => setCloneSource(null)}
        onOk={() => void performClone()}
      >
        <div className="clone-confirm">
          <p>{t("clone.description", { ns: "adapter" })}</p>
          <p>{t("clone.historyNote", { ns: "adapter" })}</p>
          <label className="settings-field">
            <span className="settings-field-label">{t("clone.name", { ns: "adapter" })}</span>
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
        title={t("save.title", { ns: "adapter" })}
        open={saveWorkerPromptOpen}
        okText={t("actions.save")}
        cancelText={t("actions.cancel")}
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
          <p>{t("save.firstSaveHint", { ns: "adapter" })}</p>
          <label className="settings-field">
            <span className="settings-field-label">{t("save.worker", { ns: "adapter" })}</span>
            <Select
              aria-label={t("save.workerAria", { ns: "adapter" })}
              data-testid="save-worker-select"
              value={saveWorkerId ?? undefined}
              placeholder={t("save.workerPlaceholder", { ns: "adapter" })}
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
            <p className="settings-danger-hint" role="alert">{t("save.noWorker", { ns: "adapter" })}</p>
          )}
        </div>
      </Modal>
    </div>
  );
}

// Minimal admin token input shown while no valid token is present;
// the M3.1 login page keeps the M2 auth contract unchanged.

function TokenApp() {
  const { t } = useTranslation("common");
  const [authed, setAuthed] = useState<boolean>(() => {
    const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored !== null) {
      setAuthToken(stored);
      return true;
    }
    return false;
  });
  const [noticeKey, setNoticeKey] = useState<"auth.sessionRejected" | null>(null);

  const refreshSystemLocale = useCallback(async () => {
    try {
      const response = await api.getSystemLocale();
      if (isSystemLocale(response.locale)) {
        await applySystemLocale(response.locale);
      }
    } catch {
      // The cached locale is only a first-paint fallback; keep it when the
      // public bootstrap read is temporarily unavailable.
    }
  }, []);

  useEffect(() => {
    void refreshSystemLocale();
  }, [refreshSystemLocale]);

  useEffect(() => {
    onUnauthorized(() => {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      setAuthToken(null);
      setNoticeKey("auth.sessionRejected");
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
    await refreshSystemLocale();
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    setNoticeKey(null);
    setAuthed(true);
  }

  return authed ? (
    <AdapterConsole />
  ) : (
    <LoginPage
      notice={noticeKey === null ? null : t(noticeKey)}
      onSubmit={handleLogin}
    />
  );
}

export default function App() {
  return (
    <DlrDesignSystemProvider>
      {currentEntryMode() === "account" ? <AccountApp /> : <TokenApp />}
    </DlrDesignSystemProvider>
  );
}
