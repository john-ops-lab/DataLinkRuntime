import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import Editor from "@monaco-editor/react";
import { Button, ConfigProvider, message, Segmented, Tabs } from "antd";
import zhCN from "antd/locale/zh_CN";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import AdapterCatalog from "./components/AdapterCatalog";
import AdapterSettingsDrawer from "./components/AdapterSettingsDrawer";
import AiAssistantPanel from "./components/AiAssistantPanel";
import CredentialBindingsEditor from "./components/CredentialBindingsEditor";
import ExecutionHistoryPanel from "./components/ExecutionHistoryPanel";
import LoginPage from "./components/LoginPage";
import SystemSettingsDrawer from "./components/SystemSettingsDrawer";
import TaskRunSettingsPanel from "./components/TaskRunSettingsPanel";
import TaskWorkbenchHeader from "./components/TaskWorkbenchHeader";
import VersionDiffModal, { type DiffPane } from "./components/VersionDiffModal";
import WebhookTriggerPanel from "./components/WebhookTriggerPanel";
import WebhookWorkbenchHeader from "./components/WebhookWorkbenchHeader";
import WorkerStatus from "./components/WorkerStatus";
import { DEPENDENCY_UI, TASK_STARTER_CODE, WEBHOOK_STARTER_CODE } from "./languages";
import { PRODUCTION_REFRESH_POLICY } from "./production-refresh-policy";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  Adapter,
  AdapterLanguage,
  AdapterType,
  AiCandidate,
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

type WorkbenchTabKey = "edit" | "runtime" | "history";

// 编辑页次级配置区（语言依赖 | 运行参数（JSON） | 凭据绑定）。
type ConfigTabKey = "requirements" | "runtime-config" | "bindings";

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
  // Save/Publish are gated on it so stale or failed loads can never be persisted.
  const [contentReady, setContentReady] = useState(false);
  // Low-frequency Adapter settings live in a drawer, outside the main work area.
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Controlled Workbench tab.
  const [activeTabKey, setActiveTabKey] = useState<WorkbenchTabKey>("edit");
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
  const activeExecutionId = selected?.running_execution_id ?? null;
  const selectedTriggerLocked = selected?.runtime_locked === true;

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
        if (
          refreshed.running_execution_id != null ||
          refreshed.runtime_locked === true
        ) {
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
  }, [
    activeExecutionId,
    busy,
    selectedAdapterId,
    selectedTriggerLocked,
  ]);

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
    setError(null);
    setVersions([]);
    setSelectedVersionId(null);
    setContentReady(false);
    setSettingsOpen(false);
    setActiveTabKey("edit");
    setConfigTabKey("requirements");
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
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveVersion() {
    if (!selected || busy || !contentReady || selected.runtime_locked === true) {
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

  async function handleDelete() {
    if (!selected || busy) {
      return;
    }
    const warning = dirty ? "该 Adapter 存在未保存的编辑器修改。" : "";
    if (
      !window.confirm(`确定删除 Adapter “${selected.name}” 吗？删除后将只读保留 Revision 与 Execution 历史。${warning}`)
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
              {selected.adapter_type === "task" ? (
                <TaskWorkbenchHeader
                  adapter={selected}
                  selectedVersion={selectedVersion}
                  dirty={dirty}
                  busy={busy}
                  contentReady={contentReady}
                  onSave={() => void handleSaveVersion()}
                  onOpenSettings={() => setSettingsOpen(true)}
                />
              ) : (
              <WebhookWorkbenchHeader
                adapter={selected}
                selectedVersion={selectedVersion}
                dirty={dirty}
                busy={busy}
                contentReady={contentReady}
                onSave={() => void handleSaveVersion()}
                onOpenSettings={() => setSettingsOpen(true)}
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
                                key: "runtime-config",
                                label: "运行参数（JSON）",
                                children: (
                                  <textarea
                                    data-testid="runtime-config-input"
                                    rows={4}
                                    value={snapshot.runtimeConfigText}
                                    disabled={busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true}
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
                                    disabled={busy || !contentReady || !!selected.archived_at || selected.runtime_locked === true}
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
                  selected.adapter_type === "task"
                    ? {
                        key: "runtime",
                        label: "运行设置",
                        children: (
                          <TaskRunSettingsPanel
                            key={selected.id}
                            adapter={selected}
                            workers={workers}
                            workersLoading={workersLoading}
                            workersError={workersError}
                            onAdapterChange={handleTaskAdapterChange}
                            onError={setError}
                          />
                        ),
                      }
                    : {
                        key: "runtime",
                        label: "运行设置",
                        children: (
                          <WebhookTriggerPanel
                            key={selected.id}
                            adapter={selected}
                            workers={workers}
                            workersLoading={workersLoading}
                            workersError={workersError}
                            onAdapterChange={handleTaskAdapterChange}
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
