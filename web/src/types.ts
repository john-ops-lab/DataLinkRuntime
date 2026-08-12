/** API shapes shared across the web UI (mirrors the Control API schemas). */

export interface Adapter {
  id: number;
  name: string;
  description: string;
  language: string;
  latest_version_id: number | null;
  published_version_id: number | null;
  /** Server-derived display fields used by the Catalog without per-row requests. */
  published_version_seq?: number | null;
  running_version_seq?: number | null;
  // M3.2 production lifecycle fields; optional so older fixtures and tests
  // that predate them stay valid. Consumers derive safe defaults (idle / not
  // archived) whenever they are absent.
  production_worker_id?: number | null;
  production_state?: "idle" | "running" | "stopped";
  archived_at?: string | null;
  /** Derived from the active Production Execution; null when none exists. */
  running_version_id?: number | null;
  running_execution_id?: number | null;
  /** Minimal summary of the newest Production Execution, including terminal runs. */
  last_production_execution_id?: number | null;
  last_production_execution_status?: ExecutionStatus | null;
  last_production_version_id?: number | null;
  last_production_version_seq?: number | null;
  created_at: string;
  updated_at: string;
}

export interface VersionSummary {
  id: number;
  adapter_id: number;
  seq: number;
  created_at: string;
}

export interface VersionDetail extends VersionSummary {
  code: string;
  requirements: string;
  runtime_config: Record<string, unknown>;
}

/** Full current state of one Execution (GET /api/executions/{id}). */
export interface Execution {
  id: number;
  adapter_id: number;
  version_id: number;
  worker_id: number | null;
  trigger: string;
  status: ExecutionStatus;
  input: unknown;
  output: unknown;
  output_size: number | null;
  output_truncated: boolean;
  output_preview: string | null;
  stdout: string;
  stdout_truncated: boolean;
  stderr: string;
  stderr_truncated: boolean;
  error: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
}

export type ExecutionStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "timeout"
  | "cancelled";

/** Lightweight history row; never carries input/output/stdout/stderr. */
export interface ExecutionSummary {
  id: number;
  adapter_id: number;
  version_id: number;
  version_seq: number;
  worker_id: number | null;
  worker_name: string | null;
  trigger: string;
  status: ExecutionStatus;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
}

/** One cursor page of an Adapter's execution history (newest first). */
export interface ExecutionHistoryPage {
  items: ExecutionSummary[];
  next_before_id: number | null;
}

export interface Worker {
  id: number;
  name: string;
  status: string;
  last_heartbeat: string;
  capabilities: string[];
}

/** The most recent test run of one version on the production Worker. */
export interface PublishGateLastTest {
  execution_id: number;
  status: string;
  ended_at: string | null;
}

/** Read-only publish gate evaluation shown in the Publish confirmation. */
export interface PublishGate {
  allowed: boolean;
  /** null exactly when allowed; codes: no_production_worker,
   * not_tested_on_production_worker, last_test_not_succeeded. */
  reason: string | null;
  last_test: PublishGateLastTest | null;
}

// --- M3.2: Secret Store credentials and package sources --------------------

export type CredentialType = "password" | "token" | "access_key" | "secret";

/** Credential metadata; plaintext values are never returned by the API. */
export interface Credential {
  id: number;
  name: string;
  type: string;
  created_at: string;
  updated_at: string;
}

/** One env_key -> credential field binding on an Adapter. */
export interface CredentialBinding {
  env_key: string;
  credential_id: number;
  field: string;
  /** Enriched server-side on read; absent in write payloads. */
  credential_name?: string;
  credential_type?: string;
}

export interface PackageSource {
  id: number;
  name: string;
  index_url: string;
  is_default: boolean;
  credential_id: number | null;
  credential_name: string | null;
  created_at: string;
  updated_at: string;
}

/** Control-side reachability probe result for one package source. */
export interface ReachabilityResult {
  ok: boolean;
  status_code: number | null;
  error: string | null;
}
