import { useCallback, useEffect, useRef, useState } from "react";
import Editor from "@monaco-editor/react";
import { Button, Card, ConfigProvider, Input, Space, Tabs, Tag, Typography } from "antd";
import zhCN from "antd/locale/zh_CN";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import AdapterList from "./components/AdapterList";
import ExecutionHistoryPanel from "./components/ExecutionHistoryPanel";
import TestRunPanel from "./components/TestRunPanel";
import WorkerStatus from "./components/WorkerStatus";
import type { Adapter, VersionDetail, VersionSummary } from "./types";

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

// Browser-only starter shown while an Adapter still has no saved version.
// It is never written to the database before an explicit Save new version.
const STARTER_CODE = "def handle(context, input):\n    return input\n";

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

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
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
  const [health, setHealth] = useState<HealthStatus>("loading");

  const [adapters, setAdapters] = useState<Adapter[]>([]);
  const [selected, setSelected] = useState<Adapter | null>(null);
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

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
  // Monotonic guard: only the newest content-loading request may commit state, so
  // rapid adapter switches cannot mix state or save one adapter's snapshot into another.
  const requestGeneration = useRef(0);

  const dirty =
    snapshot.code !== baseline.code ||
    snapshot.requirements !== baseline.requirements ||
    snapshot.runtimeConfigText !== baseline.runtimeConfigText;

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
    applySnapshot({ code: "", requirements: "", runtimeConfigText: "{}" });
    try {
      const list = await api.listVersions(adapter.id);
      if (generation !== requestGeneration.current) {
        return;
      }
      setVersions(list);
      if (adapter.latest_version_id === null) {
        setSelectedVersionId(null);
        applySnapshot({ code: STARTER_CODE, requirements: "", runtimeConfigText: "{}" });
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

  async function handleCreateAdapter(createdName: string, createdDescription: string): Promise<boolean> {
    if (busy) {
      return false;
    }
    if (!confirmDiscard()) {
      return false;
    }
    setBusy(true);
    try {
      setError(null);
      const created = await api.createAdapter({ name: createdName, description: createdDescription });
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
      setSelectedVersionId(saved.id);
      applySnapshot(versionSnapshot(saved));
      try {
        const versionList = await api.listVersions(selected.id);
        setVersions(versionList);
      } catch (refreshErr) {
        setError(`版本已保存，但刷新版本列表失败：${errorMessage(refreshErr)}`);
      }
      try {
        // Best-effort refresh of the real Adapter (server-owned updated_at);
        // failure is non-fatal because the save itself is already acknowledged.
        const real = await api.getAdapter(selected.id);
        setSelected(real);
        setAdapters((current) => current.map((item) => (item.id === real.id ? real : item)));
      } catch (refreshErr) {
        setError(`版本已保存，但刷新 Adapter 失败：${errorMessage(refreshErr)}`);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handlePublish() {
    if (!selected || selectedVersionId === null || busy || !contentReady) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      const refreshed = await api.publishVersion(selected.id, selectedVersionId);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
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
      await refreshAdapters();
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
    if (!window.confirm(`确定删除 Adapter “${selected.name}” 及其全部版本吗？${warning}`)) {
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
      ? "Checking control health..."
      : health === "ok"
        ? "Control: ok"
        : health === "degraded"
          ? "Control: degraded"
          : "Control: unreachable";

  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const isLatest = selectedVersionId !== null && selected?.latest_version_id === selectedVersionId;
  const isPublished =
    selectedVersionId !== null && selected?.published_version_id === selectedVersionId;

  return (
    <main className="layout">
      <header className="header">
        <h1>DataLinkRuntime</h1>
        <Space size={12} align="center">
          <span data-testid="control-status">{healthText}</span>
          <WorkerStatus />
        </Space>
      </header>

      {error && (
        <p className="error-banner" role="alert" data-testid="error-banner">
          {error}
        </p>
      )}

      <div className="panels">
        <AdapterList
          adapters={adapters}
          selectedId={selected?.id ?? null}
          busy={busy}
          onSelect={handleSelectAdapter}
          onCreate={handleCreateAdapter}
        />

        {selected === null ? (
          <section className="detail-empty">请选择一个 Adapter 进行管理。</section>
        ) : (
          <section className="detail">
            <div className="detail-header">
              <h2>{selected.name}</h2>
              {dirty && (
                <Tag color="warning" data-testid="dirty-indicator">
                  未保存修改
                </Tag>
              )}
              <Button danger data-testid="delete-adapter" disabled={busy} onClick={() => void handleDelete()}>
                删除
              </Button>
            </div>

            <div className="metadata">
              <Input
                data-testid="adapter-name"
                value={name}
                disabled={busy}
                onChange={(event) => setName(event.target.value)}
              />
              <Input
                data-testid="adapter-description"
                placeholder="描述"
                value={description}
                disabled={busy}
                onChange={(event) => setDescription(event.target.value)}
              />
              <Button
                data-testid="update-details"
                disabled={busy || !contentReady}
                onClick={() => void handleUpdateDetails()}
              >
                更新信息
              </Button>
            </div>

            <div className="version-controls">
              <label>
                版本{" "}
                <select
                  data-testid="version-selector"
                  value={selectedVersionId ?? ""}
                  disabled={busy || versions.length === 0}
                  onChange={(event) => void handleSelectVersion(Number(event.target.value))}
                >
                  {versions.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.seq}
                    </option>
                  ))}
                </select>
              </label>
              {selectedVersion && <span data-testid="version-seq">v{selectedVersion.seq}</span>}
              {isLatest && <Tag color="blue" data-testid="latest-badge">Latest</Tag>}
              {isPublished && <Tag color="green" data-testid="published-badge">Published</Tag>}
              <Button
                data-testid="publish-version"
                disabled={selectedVersionId === null || busy || !contentReady}
                onClick={() => void handlePublish()}
              >
                发布
              </Button>
            </div>

            <Tabs
              className="workspace-tabs"
              defaultActiveKey="edit"
              items={[
                {
                  key: "edit",
                  label: "编辑",
                  children: (
                    <div className="editor-pane">
                      <Editor
                        height="320px"
                        defaultLanguage="python"
                        value={snapshot.code}
                        onChange={(value) => setSnapshot((current) => ({ ...current, code: value ?? "" }))}
                        options={{ minimap: { enabled: false }, readOnly: busy || !contentReady }}
                      />

                      <div className="version-fields">
                        <label>
                          Requirements
                          <textarea
                            data-testid="requirements-input"
                            rows={4}
                            value={snapshot.requirements}
                            disabled={busy || !contentReady}
                            onChange={(event) =>
                              setSnapshot((current) => ({ ...current, requirements: event.target.value }))
                            }
                          />
                        </label>
                        <label>
                          Runtime config（JSON 对象）
                          <textarea
                            data-testid="runtime-config-input"
                            rows={4}
                            value={snapshot.runtimeConfigText}
                            disabled={busy || !contentReady}
                            onChange={(event) =>
                              setSnapshot((current) => ({
                                ...current,
                                runtimeConfigText: event.target.value,
                              }))
                            }
                          />
                        </label>
                      </div>

                      <Button
                        type="primary"
                        data-testid="save-version"
                        disabled={busy || !contentReady}
                        onClick={() => void handleSaveVersion()}
                      >
                        保存新版本
                      </Button>
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
                      selectedVersionId={selectedVersionId}
                      selectedVersionSeq={selectedVersion?.seq ?? null}
                      isLatest={isLatest}
                      isPublished={isPublished}
                      dirty={dirty}
                      contentReady={contentReady}
                      busy={busy}
                      onError={setError}
                    />
                  ),
                },
                {
                  key: "history",
                  label: "执行记录",
                  // antd Tabs render lazily: the history API is only called
                  // after this tab is activated for the first time.
                  children: <ExecutionHistoryPanel key={selected.id} adapterId={selected.id} />,
                },
              ]}
            />
          </section>
        )}
      </div>
    </main>
  );
}

// Minimal admin token input shown while no valid token is present.
function TokenLogin(props: { notice: string | null; onSubmit: (token: string) => Promise<void> }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    if (busy || !token.trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await props.onSubmit(token.trim());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="layout login-layout">
      <header className="header">
        <h1>DataLinkRuntime</h1>
      </header>
      <Card className="login-card" title="需要 Admin Token">
        <Typography.Paragraph type="secondary">
          请输入管理 Token 后进入控制台。Token 仅保存在当前浏览器会话中。
        </Typography.Paragraph>
        {props.notice && <p data-testid="auth-notice">{props.notice}</p>}
        {error && (
          <p className="error-banner" role="alert" data-testid="login-error">
            {error}
          </p>
        )}
        <Input.Password
          data-testid="admin-token-input"
          placeholder="Admin token"
          value={token}
          disabled={busy}
          onChange={(event) => setToken(event.target.value)}
        />
        <Button
          type="primary"
          data-testid="admin-token-submit"
          loading={busy}
          disabled={busy || !token.trim()}
          onClick={() => void handleSubmit()}
        >
          登录
        </Button>
      </Card>
    </main>
  );
}

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
    <ConfigProvider locale={zhCN}>
      {authed ? <AdapterConsole /> : <TokenLogin notice={notice} onSubmit={handleLogin} />}
    </ConfigProvider>
  );
}
