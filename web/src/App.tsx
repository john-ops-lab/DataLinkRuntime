import { useCallback, useEffect, useState } from "react";
import Editor from "@monaco-editor/react";

import { ApiError, api } from "./api";
import AdapterList from "./components/AdapterList";
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

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "Request failed";
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

export default function App() {
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
    return window.confirm("You have unsaved changes. Discard them?");
  }

  async function loadAdapterContent(adapter: Adapter) {
    setSelected(adapter);
    setName(adapter.name);
    setDescription(adapter.description);
    setError(null);
    try {
      const list = await api.listVersions(adapter.id);
      setVersions(list);
      if (adapter.latest_version_id === null) {
        setSelectedVersionId(null);
        applySnapshot({ code: STARTER_CODE, requirements: "", runtimeConfigText: "{}" });
        return;
      }
      const detail = await api.getVersion(adapter.id, adapter.latest_version_id);
      setSelectedVersionId(detail.id);
      applySnapshot(versionSnapshot(detail));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  function handleSelectAdapter(adapter: Adapter) {
    if (selected?.id === adapter.id) {
      return;
    }
    if (!confirmDiscard()) {
      return;
    }
    void loadAdapterContent(adapter);
  }

  async function handleCreateAdapter(createdName: string, createdDescription: string) {
    if (!confirmDiscard()) {
      return;
    }
    try {
      setError(null);
      const created = await api.createAdapter({ name: createdName, description: createdDescription });
      await refreshAdapters();
      await loadAdapterContent(created);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function handleSelectVersion(versionId: number) {
    if (!selected || versionId === selectedVersionId) {
      return;
    }
    if (!confirmDiscard()) {
      return;
    }
    try {
      setError(null);
      const detail = await api.getVersion(selected.id, versionId);
      setSelectedVersionId(detail.id);
      applySnapshot(versionSnapshot(detail));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function handleSaveVersion() {
    if (!selected || busy) {
      return;
    }
    const runtimeConfig = parseRuntimeConfig(snapshot.runtimeConfigText);
    if (runtimeConfig === null) {
      setError("Runtime config must be a valid JSON object");
      return;
    }
    if (!snapshot.code.trim()) {
      setError("Code must not be blank");
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
      const [refreshed, versionList] = await Promise.all([
        api.getAdapter(selected.id),
        api.listVersions(selected.id),
      ]);
      setSelected(refreshed);
      setAdapters((current) => current.map((item) => (item.id === refreshed.id ? refreshed : item)));
      setVersions(versionList);
      setSelectedVersionId(saved.id);
      applySnapshot(versionSnapshot(saved));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handlePublish() {
    if (!selected || selectedVersionId === null || busy) {
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
    const warning = dirty ? " The adapter has unsaved editor changes." : "";
    if (!window.confirm(`Delete adapter "${selected.name}" and all of its versions?${warning}`)) {
      return;
    }
    setBusy(true);
    try {
      setError(null);
      await api.deleteAdapter(selected.id);
      setSelected(null);
      setSelectedVersionId(null);
      setVersions([]);
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
        <span data-testid="control-status">{healthText}</span>
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
          onSelect={handleSelectAdapter}
          onCreate={handleCreateAdapter}
        />

        {selected === null ? (
          <section className="detail-empty">Select an adapter to manage it.</section>
        ) : (
          <section className="detail">
            <div className="detail-header">
              <h2>{selected.name}</h2>
              {dirty && <span data-testid="dirty-indicator">Unsaved changes</span>}
              <button type="button" data-testid="delete-adapter" onClick={() => void handleDelete()}>
                Delete
              </button>
            </div>

            <div className="metadata">
              <input
                data-testid="adapter-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
              <input
                data-testid="adapter-description"
                placeholder="description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
              <button
                type="button"
                data-testid="update-details"
                disabled={busy}
                onClick={() => void handleUpdateDetails()}
              >
                Update details
              </button>
            </div>

            <div className="version-controls">
              <label>
                Version{" "}
                <select
                  data-testid="version-selector"
                  value={selectedVersionId ?? ""}
                  disabled={versions.length === 0}
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
              {isLatest && <span data-testid="latest-badge">Latest</span>}
              {isPublished && <span data-testid="published-badge">Published</span>}
              <button
                type="button"
                data-testid="publish-version"
                disabled={selectedVersionId === null || busy}
                onClick={() => void handlePublish()}
              >
                Publish
              </button>
            </div>

            <Editor
              height="320px"
              defaultLanguage="python"
              value={snapshot.code}
              onChange={(value) => setSnapshot((current) => ({ ...current, code: value ?? "" }))}
              options={{ minimap: { enabled: false } }}
            />

            <div className="version-fields">
              <label>
                Requirements
                <textarea
                  data-testid="requirements-input"
                  rows={4}
                  value={snapshot.requirements}
                  onChange={(event) =>
                    setSnapshot((current) => ({ ...current, requirements: event.target.value }))
                  }
                />
              </label>
              <label>
                Runtime config (JSON object)
                <textarea
                  data-testid="runtime-config-input"
                  rows={4}
                  value={snapshot.runtimeConfigText}
                  onChange={(event) =>
                    setSnapshot((current) => ({
                      ...current,
                      runtimeConfigText: event.target.value,
                    }))
                  }
                />
              </label>
            </div>

            <button
              type="button"
              data-testid="save-version"
              disabled={busy}
              onClick={() => void handleSaveVersion()}
            >
              Save new version
            </button>
          </section>
        )}
      </div>
    </main>
  );
}
