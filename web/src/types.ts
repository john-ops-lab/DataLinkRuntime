/** API shapes shared across the web UI (mirrors the Control API schemas). */

export interface Adapter {
  id: number;
  name: string;
  description: string;
  language: string;
  latest_version_id: number | null;
  published_version_id: number | null;
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
