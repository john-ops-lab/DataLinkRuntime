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
