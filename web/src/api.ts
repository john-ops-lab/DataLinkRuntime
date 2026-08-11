/** Minimal typed client for the Control API. */

import type {
  Adapter,
  Execution,
  ExecutionHistoryPage,
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

  createAdapter: (payload: { name: string; description: string }): Promise<Adapter> =>
    request("/api/adapters", { method: "POST", body: JSON.stringify(payload) }),

  getAdapter: (adapterId: number): Promise<Adapter> => request(`/api/adapters/${adapterId}`),

  updateAdapter: (
    adapterId: number,
    payload: { name?: string; description?: string },
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
};
