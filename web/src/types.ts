/** API shapes shared across the web UI (mirrors the Control API schemas). */

export type AdapterLanguage = "python" | "javascript" | "java";
export type AdapterType = "task" | "webhook";
export type TaskRunMode = "manual" | "schedule";
export type SystemLocale = "zh-CN" | "en";

export type AccountRole = "admin" | "user";

/** Secret-free identity returned by the account Session endpoints. */
export interface AccountPrincipal {
  id: number;
  username: string;
  role: AccountRole;
  enabled: boolean;
  must_change_password: boolean;
}

/** Secret-free account row returned by the Wave B user-management API. */
export interface AccountUser extends AccountPrincipal {
  created_at: string;
  updated_at: string;
}

export interface SystemLocaleResponse {
  locale: SystemLocale;
}

export interface Adapter {
  id: number;
  name: string;
  description: string;
  language: AdapterLanguage;
  adapter_type: AdapterType;
  run_mode: TaskRunMode;
  /** M5.5.11: authoritative single-run execution timeout in seconds (1..86400, default 300). */
  timeout_seconds?: number;
  latest_version_id: number | null;
  runtime_worker_id?: number | null;
  runtime_locked?: boolean;
  archived_at?: string | null;
  running_execution_id?: number | null;
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
  target_worker_id: number | null;
  trigger: string;
  /** M5.2: the planned point for trigger=schedule; null for other triggers. */
  scheduled_for: string | null;
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
  /** Locale captured at Execution creation for platform log rendering. */
  locale?: SystemLocale;
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
  /** M5.2: the planned point for trigger=schedule; null for other triggers. */
  scheduled_for: string | null;
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

// --- M5.2: Schedule Trigger --------------------------------------------------

/** Singleton Schedule configuration of one Adapter (GET/PUT response body). */
export interface AdapterSchedule {
  adapter_id: number;
  enabled: boolean;
  cron: string;
  timezone: string;
  input: unknown;
  /** Scheduler cursor (UTC); null while disabled. */
  next_run_at: string | null;
  updated_at: string;
}

export interface AdapterScheduleDraft {
  enabled: boolean;
  cron: string;
  timezone: string;
  input: unknown;
}

// --- M5.3: Webhook Trigger ---------------------------------------------------

/** Singleton Webhook configuration of one Adapter (GET/PUT response body).
 * Never carries Credential plaintext or ciphertext. */
export interface AdapterWebhook {
  adapter_id: number;
  enabled: boolean;
  /** Random routing identifier; never an authentication secret. */
  public_id: string;
  /** External entry path, e.g. /api/hooks/{public_id}. */
  hook_path: string;
  credential_id: number | null;
  credential_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdapterWebhookDraft {
  enabled: boolean;
  public_id: string;
  credential_id: number | null;
}

export interface Worker {
  id: number;
  name: string;
  status: string;
  last_heartbeat: string;
  capabilities: string[];
}

// --- M3.2/M3.3: Secret Store credentials and dependency sources -----------

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
  /** Stable system preset identity; user-created sources have no preset ID. */
  preset_id?: string | null;
  kind: "pypi" | "npm" | "maven";
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

/** Canonical fresh-deployment default for one dependency kind (M5.5.8). */
export interface DefaultPackageSourceInfo {
  preset_id?: string;
  kind: "pypi" | "npm" | "maven";
  name: string;
  index_url: string;
}

/** Canonical defaults for all three dependency kinds. */
export interface PackageSourceDefaults {
  pypi: DefaultPackageSourceInfo;
  npm: DefaultPackageSourceInfo;
  maven: DefaultPackageSourceInfo;
}

// --- M5.8-006: productized read-only KnowledgeSource configuration ---------

export type KnowledgeSourceConfigStatus = "disabled" | "unconfigured" | "configured";
export type KnowledgeSourceTestStatus =
  | "disabled"
  | "unconfigured"
  | "connected"
  | "error";

/** Metadata only; KnowledgeSource Credential values are never returned. */
export interface KnowledgeSource {
  source_id: "ima";
  kind: "ima";
  name: string;
  endpoint: string;
  enabled: boolean;
  status: KnowledgeSourceConfigStatus;
  credential_id: number | null;
  credential_name: string | null;
  credential_type: string | null;
  config_source: "database" | "environment";
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  status: "accessible";
}

export interface KnowledgeSourceTestResult {
  ok: boolean;
  status: KnowledgeSourceTestStatus;
  error_code: string | null;
  message: string;
  knowledge_bases: KnowledgeBase[];
}

// --- M4: AI Editor ----------------------------------------------------------

export type AiProvider =
  | "openai"
  | "deepseek"
  | "kimi"
  | "minimax"
  | "custom_openai_compatible";

export type AiReasoningMode = "default" | "enabled" | "disabled";
export type AiReasoningEffort = "low" | "medium" | "high" | "max" | "xhigh";

/** Metadata-only global setting. The referenced Credential value never reaches the browser. */
export interface AiModelSettingDraft {
  provider: AiProvider;
  base_url: string;
  model: string;
  credential_id: number | null;
  reasoning_mode: AiReasoningMode;
  reasoning_effort: AiReasoningEffort | null;
}

export interface AiModelSetting extends AiModelSettingDraft {
  id: number;
  credential_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface AiCandidate {
  summary: string;
  code: string;
  required_secret_keys: string[];
  /** M5.8-003: deprecated Provider compatibility echo; never applied by Web. */
  requirements?: string;
  /** M5.8-003: deprecated Provider compatibility echo; never applied by Web. */
  runtime_config?: Record<string, unknown>;
}

export interface AiConversationMessage {
  role: "user" | "assistant";
  content: string;
}

/** M5.5.13: one exact browser-captured context snippet added to the AI
 * context. ``source`` distinguishes a Monaco code selection from a selection
 * of the browser-visible, already-masked live-log text. Captured at click
 * time; later cursor movement never changes it. Line numbers are 1-based. */
export interface AiContextSnippet {
  source: "code" | "log";
  text: string;
  start_line: number;
  end_line: number;
}

export interface AiAssistResponse {
  message: string;
  candidate: AiCandidate | null;
  provider: AiProvider;
  model: string;
  /** M5.7 Wave C1: sanitized Tool execution summaries (empty for rounds that
   * never called a tool). Only bounded, secret-free metadata + summaries —
   * never raw payloads, Credentials or hidden reasoning. */
  tool_calls?: AiToolCallSummary[];
}

/** M5.7 Wave C1: one sanitized read-only tool execution for the Tool UI. */
export interface AiToolCallSummary {
  tool_name: string;
  status: "success" | "error";
  args_summary: string;
  result_summary: string;
  error_code: string | null;
  duration_ms: number;
  result_truncated: boolean;
  result_size: number;
  /** Auditable but non-sensitive source, e.g. "dlr-docs:v1:<id>". */
  source: string | null;
}

/** M5.7 Wave B2: one browser-uploaded attachment for this request only.
 * The file body travels as strict base64 inside the JSON assist request.
 * Attachments are validated, bounded and (for PDF/DOCX/text/code) parsed
 * server-side; they exist only for the current request and are never
 * persisted or logged. */
export interface AiAttachment {
  filename: string;
  content_type: string;
  data_base64: string;
}

/** M5.7 Wave B2: bounded attachment limits (upload UI shows these). */
export interface AiAttachmentLimits {
  max_attachments: number;
  max_file_bytes: number;
  max_total_bytes: number;
  max_parsed_chars_per_file: number;
  max_parsed_total_chars: number;
  parse_timeout_seconds: number;
}

/** M5.7 Wave B2: per-Provider native attachment capability. Only explicit
 * capability-table truth enables provider-native input; everything else goes
 * to the bounded server-side fallback or a stable actionable error. */
export interface AiProviderAttachmentCapability {
  provider: AiProvider;
  images_native: boolean;
  files_native: boolean;
}

/** M5.7 Wave B2: stable Wave B3 contract for the attachment upload UI. */
export interface AiAttachmentCapabilities {
  limits: AiAttachmentLimits;
  supported_content_types: string[];
  providers: AiProviderAttachmentCapability[];
}

export interface AiConnectionTestResult {
  ok: boolean;
  message: string;
}
