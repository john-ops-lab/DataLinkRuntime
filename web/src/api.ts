/** Minimal typed client for the Control API. */

import type {
  Adapter,
  AdapterPermission,
  AdapterPermissionCandidate,
  AdapterLanguage,
  AdapterSchedule,
  AdapterScheduleDraft,
  AdapterWebhook,
  AdapterWebhookDraft,
  AccountPrincipal,
  AccountUser,
  AdapterType,
  TaskRunMode,
  AiAssistResponse,
  AiAttachment,
  AiAttachmentCapabilities,
  AiConnectionTestResult,
  AiConversationMessage,
  AiContextSnippet,
  AiCustomProvider,
  AiCustomProviderDraft,
  AiKnowledgeCapability,
  AiModelSetting,
  AiModelSettingDraft,
  AiProviderCapability,
  Credential,
  CredentialBinding,
  CredentialType,
  Execution,
  ExecutionHistoryPage,
  KnowledgeBase,
  KnowledgeSource,
  KnowledgeSourceTestResult,
  PackageSource,
  PackageSourceDefaults,
  ReachabilityResult,
  SystemLocaleResponse,
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
  readonly params: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    params: Record<string, unknown> = {},
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.params = params;
  }
}

// Domain errors arrive as {"detail": {"code": ..., "message": ...}}; anything
// else falls back to a generic message. Failures are never masked as success.
async function parseError(response: Response): Promise<ApiError> {
  let code = "unknown_error";
  let message = `Request failed with status ${response.status}`;
  let params: Record<string, unknown> = {};
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
        if (typeof record.params === "object" && record.params !== null && !Array.isArray(record.params)) {
          params = record.params as Record<string, unknown>;
        }
      }
    }
  } catch {
    // Nginx/proxies commonly return an HTML/plain-text 413/504. Preserve a
    // stable code so the Assistant can render a localized actionable fallback
    // without exposing the proxy body.
    if (response.status === 413) {
      code = "ai_gateway_payload_too_large";
    } else if (response.status === 504) {
      code = "ai_gateway_timeout";
    }
  }
  return new ApiError(response.status, code, message, params);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  const method = (init?.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    const csrf = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith("dlr_account_csrf="))
      ?.slice("dlr_account_csrf=".length);
    if (csrf) {
      headers["X-CSRF-Token"] = decodeURIComponent(csrf);
    }
  }
  let response: Response;
  try {
    response = await fetch(path, { ...init, credentials: "same-origin", headers });
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
  /** Public bootstrap read; the response contains no other system settings. */
  getSystemLocale: (): Promise<SystemLocaleResponse> => request("/api/locale"),

  /** Administrator-only deployment locale update. */
  updateSystemLocale: (locale: SystemLocaleResponse["locale"]): Promise<SystemLocaleResponse> =>
    request("/api/locale", { method: "PUT", body: JSON.stringify({ locale }) }),

  verifyAdminToken: (): Promise<{ status: string }> => request("/api/auth/admin/verify"),

  // --- M5.9 Wave A account identity ----------------------------------------

  getAccountCsrf: (): Promise<{ status: string }> => request("/api/auth/account/csrf"),

  loginAccount: (payload: { username: string; password: string }): Promise<{ principal: AccountPrincipal }> =>
    request("/api/auth/account/login", { method: "POST", body: JSON.stringify(payload) }),

  getAccountPrincipal: (): Promise<{ principal: AccountPrincipal }> =>
    request("/api/auth/account/me"),

  logoutAccount: (): Promise<{ status: string }> =>
    request("/api/auth/account/logout", { method: "POST" }),

  changeAccountPassword: (payload: {
    current_password: string;
    new_password: string;
  }): Promise<{ status: string }> =>
    request("/api/auth/account/change-password", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  resetAccountPassword: (payload: { username: string; new_password: string }): Promise<{ status: string }> =>
    request("/api/auth/account/reset", { method: "POST", body: JSON.stringify(payload) }),

  // --- M5.9 Wave B account management --------------------------------------

  listUsers: (): Promise<AccountUser[]> => request("/api/users"),

  createUser: (payload: {
    username: string;
    password: string;
    role: "admin" | "user";
  }): Promise<AccountUser> => request("/api/users", { method: "POST", body: JSON.stringify(payload) }),

  getUser: (userId: number): Promise<AccountUser> => request(`/api/users/${userId}`),

  updateUser: (
    userId: number,
    payload: { username?: string; role?: "admin" | "user"; enabled?: boolean },
  ): Promise<AccountUser> =>
    request(`/api/users/${userId}`, { method: "PATCH", body: JSON.stringify(payload) }),

  resetUserPassword: (userId: number, newPassword: string): Promise<AccountUser> =>
    request(`/api/users/${userId}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),

  listAdapters: (): Promise<Adapter[]> => request("/api/adapters"),

  createAdapter: (payload: {
    name: string;
    description: string;
    language: AdapterLanguage;
    adapter_type: AdapterType;
  }): Promise<Adapter> =>
    request("/api/adapters", { method: "POST", body: JSON.stringify(payload) }),

  getAdapter: (adapterId: number): Promise<Adapter> => request(`/api/adapters/${adapterId}`),

  updateAdapter: (
    adapterId: number,
    payload: {
      name?: string;
      description?: string;
      runtime_worker_id?: number | null;
      run_mode?: TaskRunMode;
      /** M5.5.11: single-run execution timeout in seconds (1..86400). */
      timeout_seconds?: number;
    },
  ): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}`, { method: "PATCH", body: JSON.stringify(payload) }),

  deleteAdapter: (adapterId: number, stop = false): Promise<{ detail?: { code?: string } } | void> =>
    request(`/api/adapters/${adapterId}${stop ? "?stop=true" : ""}`, { method: "DELETE" }),

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

  /** Copy the Adapter: working copy becomes v1 and the new Adapter stays stopped. */
  cloneAdapter: (
    adapterId: number,
    payload: { name: string; description?: string },
  ): Promise<Adapter> =>
    request(`/api/adapters/${adapterId}/clone`, { method: "POST", body: JSON.stringify(payload) }),

  // --- M5.9 Wave D: Adapter sharing ---------------------------------------

  listAdapterPermissions: (adapterId: number): Promise<AdapterPermission[]> =>
    request(`/api/adapters/${adapterId}/permissions`),

  listAdapterPermissionCandidates: (adapterId: number): Promise<AdapterPermissionCandidate[]> =>
    request(`/api/adapters/${adapterId}/permission-candidates`),

  setAdapterPermission: (
    adapterId: number,
    userId: number,
    permission: "read" | "edit",
  ): Promise<AdapterPermission> =>
    request(`/api/adapters/${adapterId}/permissions/${userId}`, {
      method: "PUT",
      body: JSON.stringify({ permission }),
    }),

  revokeAdapterPermission: (adapterId: number, userId: number): Promise<void> =>
    request(`/api/adapters/${adapterId}/permissions/${userId}`, { method: "DELETE" }),

  // --- M5.2: Schedule Trigger -------------------------------------------------

  /** Singleton Schedule config; throws ApiError schedule_not_configured (404) before configuration. */
  getSchedule: (adapterId: number): Promise<AdapterSchedule> =>
    request(`/api/adapters/${adapterId}/schedule`),

  /** Create or fully replace the Schedule; the cursor re-bases to the next future point. */
  putSchedule: (adapterId: number, payload: AdapterScheduleDraft): Promise<AdapterSchedule> =>
    request(`/api/adapters/${adapterId}/schedule`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  // --- M5.3: Webhook Trigger --------------------------------------------------

  /** Singleton Webhook config; throws ApiError webhook_not_configured (404) before configuration. */
  getWebhook: (adapterId: number): Promise<AdapterWebhook> =>
    request(`/api/adapters/${adapterId}/webhook`),

  /** Replace the stopped Webhook config or Start/Stop receiving. */
  putWebhook: (adapterId: number, payload: AdapterWebhookDraft): Promise<AdapterWebhook> =>
    request(`/api/adapters/${adapterId}/webhook`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  // --- M3: executions, history and workers ---------------------------------

  createExecution: (
    adapterId: number,
    payload: { input?: unknown },
  ): Promise<Execution> =>
    request(`/api/adapters/${adapterId}/executions`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getExecution: (executionId: number): Promise<Execution> =>
    request(`/api/executions/${executionId}`),

  cancelExecution: (executionId: number): Promise<Execution> =>
    request(`/api/executions/${executionId}/cancel`, { method: "POST" }),

  listExecutions: (
    adapterId: number,
    options: { limit?: number; before_id?: number; trigger?: "manual" | "schedule" | "webhook" } = {},
  ): Promise<ExecutionHistoryPage> => {
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 50));
    if (options.before_id !== undefined) {
      params.set("before_id", String(options.before_id));
    }
    if (options.trigger !== undefined) {
      params.set("trigger", options.trigger);
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

  /** Metadata-only choices for the current Adapter owner/admin binding UI. */
  listAdapterCredentialOptions: (adapterId: number): Promise<Credential[]> =>
    request(`/api/adapters/${adapterId}/credential-options`),

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

  /** Canonical fresh-deployment defaults for every dependency kind (M5.5.8). */
  getPackageSourceDefaults: (): Promise<PackageSourceDefaults> =>
    request("/api/package-sources/defaults"),

  /** Reset one kind back to its canonical default source (restore default). */
  restorePackageSourceDefault: (kind: "pypi" | "npm" | "maven"): Promise<PackageSource> =>
    request(`/api/package-sources/defaults/${kind}`, { method: "POST" }),

  // --- M5.8-006: productized read-only KnowledgeSource ----------------------

  listKnowledgeSources: (): Promise<KnowledgeSource[]> => request("/api/knowledge-sources"),

  getKnowledgeSource: (sourceId: "ima" = "ima"): Promise<KnowledgeSource> =>
    request(`/api/knowledge-sources/${sourceId}`),

  updateKnowledgeSource: (
    sourceId: "ima",
    payload: { enabled: boolean; credential_id: number | null },
  ): Promise<KnowledgeSource> =>
    request(`/api/knowledge-sources/${sourceId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  testKnowledgeSource: (sourceId: "ima" = "ima"): Promise<KnowledgeSourceTestResult> =>
    request(`/api/knowledge-sources/${sourceId}/test`, { method: "POST" }),

  validateKnowledgeSource: (sourceId: "ima" = "ima"): Promise<KnowledgeSourceTestResult> =>
    request(`/api/knowledge-sources/${sourceId}/validate`, { method: "POST" }),

  listKnowledgeBases: (sourceId: "ima" = "ima"): Promise<KnowledgeBase[]> =>
    request(`/api/knowledge-sources/${sourceId}/knowledge-bases`),

  // --- M4: AI Editor ---------------------------------------------------------

  getAiSetting: (): Promise<AiModelSetting | null> => request("/api/ai/settings"),

  getAiProviders: (): Promise<{ providers: AiProviderCapability[] }> =>
    request("/api/ai/providers"),

  listAiCustomProviders: (): Promise<{ providers: AiCustomProvider[] }> =>
    request("/api/ai/custom-providers"),

  createAiCustomProvider: (payload: AiCustomProviderDraft): Promise<AiCustomProvider> =>
    request("/api/ai/custom-providers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateAiCustomProvider: (
    providerId: number,
    payload: AiCustomProviderDraft,
  ): Promise<AiCustomProvider> =>
    request(`/api/ai/custom-providers/${providerId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  deleteAiCustomProvider: (providerId: number): Promise<void> =>
    request(`/api/ai/custom-providers/${providerId}`, { method: "DELETE" }),

  testAiCustomProvider: (providerId: number, model: string): Promise<AiConnectionTestResult> =>
    request(`/api/ai/custom-providers/${providerId}/test`, {
      method: "POST",
      body: JSON.stringify({ model }),
    }),

  updateAiSetting: (payload: AiModelSettingDraft): Promise<AiModelSetting> =>
    request("/api/ai/settings", { method: "PUT", body: JSON.stringify(payload) }),

  testAiSetting: (payload: AiModelSettingDraft): Promise<AiConnectionTestResult> =>
    request("/api/ai/settings/test", { method: "POST", body: JSON.stringify(payload) }),

  refreshAiModels: (payload: {
    provider: AiModelSettingDraft["provider"];
    base_url: string;
    credential_id: number | null;
    custom_provider_id?: number | null;
  }): Promise<{ models: string[] }> =>
    request("/api/ai/models/refresh", { method: "POST", body: JSON.stringify(payload) }),

  /** M5.7 Wave B2: stable attachment limits/MIME/capability contract. */
  getAiAttachmentCapabilities: (): Promise<AiAttachmentCapabilities> =>
    request("/api/ai/attachment-capabilities"),

  getAiKnowledgeCapability: (adapterId: number): Promise<AiKnowledgeCapability> =>
    request(`/api/adapters/${adapterId}/ai/knowledge-capability`),

  assistAdapter: (
    adapterId: number,
    payload: {
      message: string;
      working_copy: {
        code: string;
        requirements: string;
        runtime_config: Record<string, unknown>;
      };
      recent_messages: AiConversationMessage[];
      base_version_id?: number | null;
      /** M5.5.13: ordered exact context snippets (code and/or masked log). */
      context_snippets?: AiContextSnippet[];
      /** M5.7 Wave B2: request-only attachments (base64 bodies). */
      attachments?: AiAttachment[];
      /** M5.11 Wave D: opt-in knowledge search, frozen for this round. */
      knowledge_search_enabled?: boolean;
    },
  ): Promise<AiAssistResponse> =>
    request(`/api/adapters/${adapterId}/ai/assist`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
