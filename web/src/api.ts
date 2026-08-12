/** Minimal typed client for the Control API. */

import type {
  Adapter,
  Credential,
  CredentialBinding,
  CredentialType,
  Execution,
  ExecutionHistoryPage,
  PackageSource,
  PublishGate,
  ReachabilityResult,
  VersionDetail,
  VersionSummary,
  Worker,
} from "./types";

// M2 minimal Token UX: the admin token lives only in memory plus the
// browser's sessionStorage (managed by App). Every request automatically
// carries it as a Bearer header; a 401 clears the session and notifies the
// UI so it can return to the token input screen.
let authToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

/** Read-only access for non-request clients (e.g. the SSE stream reader). */
export function getAuthToken(): string | null {
  return authToken;
}

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler;
}

/** Shared 401 handling for plain requests and the SSE stream reader. */
export function handleUnauthorized(): void {
  authToken = null;
  unauthorizedHandler?.();
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// Domain errors arrive as {"detail": {"code": ..., "message": ...}}; anything
// else falls back to a generic message. Failures are never masked as success.
async function parseError(response: Response): Promise<ApiError> {
  let code = "unknown_error";
  let message = `Request failed with status ${response.status}`;
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const detail = (body as Record<string, unknown>).detail;
      if (typeof detail === "object" && detail !== null) {
        const record = detail as Record<string, unknown>;
        if (typeof record.code === "string") {
          code = record.code;
        }
        if (typeof record.message === "string") {
          message = record.message;
        }
      }
    }
  } catch {
    // keep the fallback message when the body is not JSON
  }
  return new ApiError(response.status, code, message);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  let response: Response;
  try {
    response = await fetch(path, { headers, ...init });
  } catch {
    throw new ApiError(0, "network_error", "Control is unreachable");
  }
  if (response.status === 401) {
    handleUnauthorized();
  }
  if (!response.ok) {
    throw await parseError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  verifyAdminToken: (): Promise<{ status: string }> => request("/api/auth/admin/verify"),

  listAdapters: (): Promise<Adapter[]> => request("/api/adapters"),

  createAdapter: (payload: {
    name: string;
    description: string;
    language: "python" | "javascript" | "java";
  }): Promise<Adapter> =>
    request("/api/adapters", { method: "POST", body: JSON.stringify(payload) }),

  getAdapter: (adapterId: number): Promise<Adapter> => request(`/api/adapters/${adapterId}`),

  updateAdapter: (
    adapterId: number,
    payload: { name?: string; description?: string; production_worker_id?: number | null },
  ): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}`, { method: "PATCH", body: JSON.stringify(payload) }),

  deleteAdapter: (adapterId: number): Promise<void> =>
    request(`/api/adapters/${adapterId}`, { method: "DELETE" }),

  listVersions: (adapterId: number): Promise<VersionSummary[]> =>
    request(`/api/adapters/${adapterId}/versions`),

  getVersion: (adapterId: number, versionId: number): Promise<VersionDetail> =>
    request(`/api/adapters/${adapterId}/versions/${versionId}`),

  saveVersion: (
    adapterId: number,
    payload: { code: string; requirements: string; runtime_config: Record<string, unknown> },
  ): Promise<VersionDetail> =>
    request(`/api/adapters/${adapterId}/versions`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  publishVersion: (adapterId: number, versionId: number): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/versions/${versionId}/publish`, { method: "POST" }),

  // --- M3.2: production lifecycle -------------------------------------------

  /** Read-only publish gate evaluation for the Publish confirmation dialog. */
  getPublishGate: (adapterId: number, versionId: number): Promise<PublishGate> =>
    request(`/api/adapters/${adapterId}/versions/${versionId}/publish-gate`),

  /** Open the production entry; returns the created pending Execution. */
  startProduction: (adapterId: number): Promise<Execution> =>
    request(`/api/adapters/${adapterId}/production/start`, { method: "POST" }),

  /** Close the production entry; ``terminate`` also cancels the active run. */
  stopProduction: (adapterId: number, mode: "wait" | "terminate"): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/production/stop`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),

  /** Clear the published pointer; requires production to be stopped. */
  unpublishAdapter: (adapterId: number): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/unpublish`, { method: "POST" }),

  archiveAdapter: (adapterId: number): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/archive`, { method: "POST" }),

  restoreAdapter: (adapterId: number): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/restore`, { method: "POST" }),

  /** Copy the Adapter: working copy becomes v1, unpublished and not running. */
  cloneAdapter: (
    adapterId: number,
    payload: { name: string; description?: string },
  ): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/clone`, { method: "POST", body: JSON.stringify(payload) }),

  // --- M3: executions, history and workers ---------------------------------

  createExecution: (
    adapterId: number,
    payload: { input?: unknown; version_id: number },
  ): Promise<Execution> =>
    request(`/api/adapters/${adapterId}/executions`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getExecution: (executionId: number): Promise<Execution> =>
    request(`/api/executions/${executionId}`),

  listExecutions: (
    adapterId: number,
    options: { limit?: number; before_id?: number } = {},
  ): Promise<ExecutionHistoryPage> => {
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 50));
    if (options.before_id !== undefined) {
      params.set("before_id", String(options.before_id));
    }
    return request(`/api/adapters/${adapterId}/executions?${params.toString()}`);
  },

  listWorkers: (): Promise<Worker[]> => request("/api/workers"),

  // --- M3.2: Secret Store credentials and bindings ---------------------------

  listCredentials: (): Promise<Credential[]> => request("/api/credentials"),

  createCredential: (payload: {
    name: string;
    type: CredentialType;
    fields: Record<string, string>;
  }): Promise<Credential> =>
    request("/api/credentials", { method: "POST", body: JSON.stringify(payload) }),

  updateCredential: (
    credentialId: number,
    payload: { name?: string; fields?: Record<string, string> },
  ): Promise<Credential> =>
    request(`/api/credentials/${credentialId}`, { method: "PATCH", body: JSON.stringify(payload) }),

  deleteCredential: (credentialId: number): Promise<void> =>
    request(`/api/credentials/${credentialId}`, { method: "DELETE" }),

  listAdapterBindings: (adapterId: number): Promise<CredentialBinding[]> =>
    request(`/api/adapters/${adapterId}/credential-bindings`),

  /** Full replacement: the submitted list becomes the complete binding set. */
  setAdapterBindings: (
    adapterId: number,
    bindings: { env_key: string; credential_id: number; field: string }[],
  ): Promise<CredentialBinding[]> =>
    request(`/api/adapters/${adapterId}/credential-bindings`, {
      method: "PUT",
      body: JSON.stringify({ bindings }),
    }),

  // --- M3.3: language-specific dependency sources ------------------------------

  listPackageSources: (): Promise<PackageSource[]> => request("/api/package-sources"),

  createPackageSource: (payload: {
    name: string;
    kind: "pypi" | "npm" | "maven";
    index_url: string;
    is_default: boolean;
    credential_id: number | null;
  }): Promise<PackageSource> =>
    request("/api/package-sources", { method: "POST", body: JSON.stringify(payload) }),

  updatePackageSource: (
    sourceId: number,
    payload: {
      name?: string;
      kind?: "pypi" | "npm" | "maven";
      index_url?: string;
      is_default?: boolean;
      credential_id?: number | null;
    },
  ): Promise<PackageSource> =>
    request(`/api/package-sources/${sourceId}`, { method: "PATCH", body: JSON.stringify(payload) }),

  deletePackageSource: (sourceId: number): Promise<void> =>
    request(`/api/package-sources/${sourceId}`, { method: "DELETE" }),

  /** Control-side reachability probe against the saved source's index URL. */
  testPackageSource: (sourceId: number): Promise<ReachabilityResult> =>
    request(`/api/package-sources/${sourceId}/test`, { method: "POST" }),
};
