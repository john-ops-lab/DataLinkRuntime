/** API shapes shared across the web UI (mirrors the Control API schemas). */

export type AdapterLanguage = "python" | "javascript" | "java";
export type AdapterType = "task" | "webhook";
export type TaskRunMode = "manual" | "schedule";
export type SystemLocale = "zh-CN" | "en";
export type InputSourceType = "none" | "json" | "managed_files" | "remote_files";
export type InputRetentionMode = "system_default" | "custom" | "manual_delete";

export type AccountRole = "admin" | "user";
export type AdapterAccessLevel = "admin" | "owner" | "edit" | "read";

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
  /** M5.9 Wave C: null means a system-owned Adapter. */
  owner_user_id?: number | null;
  /** M5.9 Wave D: server-resolved relationship for the current Principal. */
  access_level?: AdapterAccessLevel | null;
  /** Safe owner display metadata; null means system-owned. */
  owner_username?: string | null;
  latest_version_id: number | null;
  runtime_worker_id?: number | null;
  runtime_locked?: boolean;
  archived_at?: string | null;
  /** Legacy provenance; new copies are ordinary independent adapters. */
  template_scenario_slug?: string | null;
  template_version?: string | null;
  running_execution_id?: number | null;
  created_at: string;
  updated_at: string;
}

// --- Issue #132: immutable Template Gallery catalog ------------------------

export interface LocalizedText {
  "zh-CN": string;
  en: string;
}

export interface TemplateTheme {
  slug: string;
  name: LocalizedText;
  description: LocalizedText;
  sort_order: number;
  scenario_count: number;
}

export interface TemplateVariantSummary {
  language: AdapterLanguage;
  available: boolean;
}

export interface TemplateScenarioSummary {
  slug: string;
  theme_slug: string;
  title: LocalizedText;
  summary: LocalizedText;
  vendor: string;
  adapter_type: AdapterType;
  protocols: string[];
  tags: string[];
  logo_key: string;
  template_version: string;
  updated_at: string;
  variants: TemplateVariantSummary[];
}

export interface TemplateScenarioListResponse {
  items: TemplateScenarioSummary[];
  page: number;
  page_size: number;
  total: number;
}

export interface TemplateScenarioDetail extends TemplateScenarioSummary {
  details: LocalizedText;
}

export interface TemplateVariant {
  scenario_slug: string;
  theme_slug: string;
  title: LocalizedText;
  language: AdapterLanguage;
  adapter_type: AdapterType;
  template_version: string;
  code: string;
  requirements: string;
  input_skeleton: Record<string, unknown>;
  output_example: Record<string, unknown>;
  runtime_config: Record<string, unknown>;
}

export interface TemplateScenarioQuery {
  theme?: string;
  q?: string;
  vendor?: string;
  adapter_type?: AdapterType;
  protocol?: string;
  language?: AdapterLanguage;
  page?: number;
  page_size?: number;
}

export interface TemplateInstantiatePayload {
  name: string;
  description?: string;
  expected_template_version: string;
}

export interface AdapterPermission {
  user_id: number;
  username: string;
  enabled: boolean;
  permission: "read" | "edit";
}

/** Minimal account metadata returned only for Adapter sharing. */
export interface AdapterPermissionCandidate {
  id: number;
  username: string;
  role: AccountRole;
  enabled: boolean;
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
  /** All executions use the durable message runtime. */
  dispatch_backend: "rabbitmq";
  dispatch_generation?: number;
  queued_at?: string | null;
  next_attempt_at?: string | null;
  attempt_count?: number;
  max_attempts_snapshot?: number;
  retry_policy_snapshot?: Record<string, unknown>;
  resource_profile_snapshot?: Record<string, unknown>;
  resource_class?: string | null;
  last_error_code?: string | null;
  /** Server-provided replay decision. */
  replay_available?: boolean;
  replay_unavailable_reason?: string | null;
  input: unknown;
  /** Issue #127: immutable input source and configuration revision at creation. */
  input_source_type?: InputSourceType;
  input_config_revision?: number;
  input_snapshot?: ExecutionInputSnapshot;
  /** C0/C3 cleanup and deadline facts; all are read-only snapshots. */
  cancel_requested?: boolean;
  timeout_seconds_snapshot?: number | null;
  recovery_grace_seconds_snapshot?: number | null;
  workspace_cleanup_attempt_timeout_seconds_snapshot?: number | null;
  workspace_cleanup_total_timeout_seconds_snapshot?: number | null;
  claim_deadline_at?: string | null;
  execution_deadline_at?: string | null;
  workspace_cleanup_status?: WorkspaceCleanupStatus | null;
  workspace_cleanup_error_code?: string | null;
  error_code?: string | null;
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
  | "queued"
  | "retry_wait"
  | "succeeded"
  | "failed"
  | "timeout"
  | "cancelled"
  | "dead_letter"
  | "expired";

export type WorkspaceCleanupStatus = "pending" | "completed" | "deferred";

/** Immutable, safe file facts retained in an Execution input snapshot. */
export interface ExecutionInputArtifactSnapshot {
  ordinal: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
}

/**
 * The public Execution snapshot deliberately has no Artifact identity,
 * storage reference, Worker path or credential/token field.
 */
export type ExecutionInputSnapshot =
  | { source_type: "none" | "json"; revision: number }
  | {
      source_type: "managed_files";
      revision: number;
      artifacts: ExecutionInputArtifactSnapshot[];
    }
  | { source_type: "remote_files"; revision: number };

/** Short alias used by history/detail consumers. */
export type ExecutionSnapshot = ExecutionInputSnapshot;

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
  dispatch_backend: "rabbitmq";
  queued_at?: string | null;
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

/** Safe attempt facts returned by the execution detail endpoint. */
export interface AttemptSummary {
  id: number;
  execution_id: number;
  adapter_id: number;
  attempt_no: number;
  worker_id: number;
  fencing_token: number;
  lease_expires_at: string;
  status: string;
  claimed_at: string;
  started_at: string | null;
  ended_at: string | null;
  error_code: string | null;
  resource_usage_json: Record<string, unknown> | null;
  output_summary: Record<string, unknown> | null;
  cleanup_summary: Record<string, unknown> | null;
}

export interface ReliableExecutionIncident {
  id: number;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface ReliableExecutionDetail {
  execution_id: number;
  dispatch_backend: "rabbitmq";
  status: string;
  attempts: AttemptSummary[];
  incidents: ReliableExecutionIncident[];
  replay_available: boolean;
  replay_reason: string | null;
}

export interface ReplayResponse {
  execution_id: number;
  replay_of_execution_id: number;
}

// --- M5.2: Schedule Trigger --------------------------------------------------

export type ScheduleMisfirePolicy =
  | "coalesce_latest"
  | "queue_every_occurrence"
  | "skip_while_busy";

export type ScheduleOutcome = "enqueued" | "coalesced" | "skipped" | "expired";

export interface ScheduleDispatchOutcome {
  id: number;
  first_scheduled_for: string;
  last_scheduled_for: string;
  occurrence_count: number;
  outcome: ScheduleOutcome;
  reason: string | null;
  cron_snapshot?: string;
  timezone_snapshot?: string;
}

/** Singleton Schedule configuration of one Adapter (GET/PUT response body). */
export interface AdapterSchedule {
  adapter_id: number;
  enabled: boolean;
  cron: string;
  timezone: string;
  input: unknown;
  /** Scheduler cursor (UTC); null while disabled. */
  next_run_at: string | null;
  last_blocked_reason?: string | null;
  last_blocked_detail?: Record<string, unknown> | null;
  last_blocked_at?: string | null;
  last_processed_due_at?: string | null;
  misfire_policy?: ScheduleMisfirePolicy;
  max_catchup_count?: number;
  max_catchup_age_seconds?: number;
  recent_outcomes?: ScheduleDispatchOutcome[];
  updated_at: string;
}

export interface AdapterScheduleDraft {
  enabled: boolean;
  cron: string;
  timezone: string;
  /** Legacy mirror only; new Web input edits use AdapterInputConfig. */
  input?: unknown;
  misfire_policy?: ScheduleMisfirePolicy;
  max_catchup_count?: number;
  max_catchup_age_seconds?: number;
}

export interface InputRetention {
  mode: InputRetentionMode;
  seconds: number | null;
}

export type ManagedInputArtifactStatus =
  | "UPLOADING"
  | "STAGED"
  | "READY"
  | "PENDING_DELETE"
  | "DELETING"
  | "DELETE_FAILED"
  | "DELETED";

/** Safe Artifact facts shared by current-input and staged-list responses. */
export interface Artifact {
  id: number;
  /** Present only for current-input summaries; absent from staged responses. */
  ordinal?: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string | null;
  status: ManagedInputArtifactStatus;
  /** Current-input summaries include the applied retention mode. */
  retention_mode?: InputRetentionMode;
  /** Staged-list responses include creation time. */
  created_at?: string;
  expires_at: string | null;
}

/** Safe metadata returned by the staged Artifact list and upload endpoint. */
export interface ManagedInputArtifact extends Artifact {
  status: "STAGED";
  sha256: string;
  created_at: string;
}

/** Safe current-input Artifact summary, ordered by ``ordinal``. */
export interface InputArtifactSummary extends Artifact {
  ordinal: number;
  retention_mode: InputRetentionMode;
}

export interface ManagedInputAdapterUsage {
  adapter_id: number;
  actual_bytes: number;
  reserved_bytes: number;
  total_bytes: number;
  quota_bytes: number;
  over_quota: boolean;
}

export interface ManagedInputUsage {
  platform_actual_bytes: number;
  platform_reserved_bytes: number;
  platform_total_bytes: number;
  adapters: ManagedInputAdapterUsage[];
}

export interface ManagedInputSettingsUpdate {
  default_retention_seconds: number;
  max_file_bytes: number;
  platform_quota_bytes: number;
  adapter_quota_bytes: number;
  allow_manual_delete: boolean;
  max_custom_retention_seconds: number;
  min_free_space_bytes: number;
  staged_ttl_seconds: number;
}

/** Administrator-only policy response; deployment paths and credentials are absent. */
export interface ManagedInputSettings extends ManagedInputSettingsUpdate {
  id: number;
  usage: ManagedInputUsage;
  over_quota: boolean;
  platform_over_quota: boolean;
  adapter_over_quota: number[];
  created_at: string;
  updated_at: string;
}

/** Ordinary business-user release and retention-policy facts. */
export interface ManagedInputCapability {
  managed_files_enabled: boolean;
  ready: boolean;
  default_retention_seconds: number;
  max_custom_retention_seconds: number;
  allow_manual_delete: boolean;
  allowed_extensions: string[];
}

/** Compatibility aliases for the public D0 vocabulary. */
export type ManagedSettings = ManagedInputSettings;
export type ManagedSettingsUpdate = ManagedInputSettingsUpdate;

/** Safe current Input Object state; operational file identities are omitted. */
export interface AdapterInputConfig {
  adapter_id: number;
  revision: number;
  source_type: InputSourceType;
  json_value: unknown;
  retention: InputRetention;
  artifacts: InputArtifactSummary[];
  valid_for_run: boolean;
  invalid_reason: string | null;
}

export type AdapterInputConfigDraft =
  | {
      expected_revision: number;
      source_type: "none";
    }
  | {
      expected_revision: number;
      source_type: "json";
      json_value: unknown;
    }
  | {
      expected_revision: number;
      source_type: "managed_files";
      artifact_ids: number[];
      retention: InputRetention;
    }
  | {
      expected_revision: number;
      source_type: "remote_files";
    };

export type InputConfig = AdapterInputConfig;
export type InputConfigDraft = AdapterInputConfigDraft;

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
  /** Protocol negotiated by the Worker registration boundary. */
  protocol_version?: number;
  isolation_capabilities?: Record<string, boolean>;
  isolation_preflight_status?: "unknown" | "passed" | "failed" | string;
  isolation_preflight_at?: string | null;
  rabbitmq_execution_v3?: boolean;
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
  | "anthropic"
  | "gemini"
  | "deepseek"
  | "qwen"
  | "kimi"
  | "minimax"
  | "glm"
  | "doubao"
  | "hunyuan"
  | "openrouter"
  | "siliconflow"
  | "ollama"
  | "custom_openai_compatible";

export type AiProviderProtocol = "openai_compatible" | "anthropic" | "gemini";

export type AiReasoningMode = "default" | "enabled" | "disabled";
export type AiReasoningEffort = "low" | "medium" | "high" | "max" | "xhigh";

/** Metadata-only global setting. The referenced Credential value never reaches the browser. */
export interface AiModelSettingDraft {
  provider: AiProvider;
  custom_provider_id?: number | null;
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

export interface AiProviderCapability {
  id: string;
  name: string;
  preset: boolean;
  protocol: AiProviderProtocol;
  base_url: string;
  images_native: boolean;
  files_native: boolean;
  tools_supported: boolean;
  reasoning_efforts: AiReasoningEffort[];
}

export interface AiCustomProvider {
  id: number;
  name: string;
  protocol: AiProviderProtocol;
  base_url: string;
  credential_id: number | null;
  credential_name: string | null;
  images_native: boolean;
  files_native: boolean;
  tools_supported: boolean;
  referenced: boolean;
  created_at: string;
  updated_at: string;
}

export interface AiCustomProviderDraft {
  name: string;
  protocol: AiProviderProtocol;
  base_url: string;
  credential_id: number | null;
  images_native: boolean;
  files_native: boolean;
  tools_supported: boolean;
}

export interface AiKnowledgeCapability {
  available: boolean;
  reason: string | null;
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

/** Browser-only AI conversation correlation. ``conversation_id`` is created
 * in memory for one mounted Adapter session and is never authentication or
 * persisted conversation state. */
export interface AiAssistRequest {
  conversation_id: string;
  message: string;
  working_copy: {
    code: string;
    requirements: string;
    runtime_config: Record<string, unknown>;
  };
  recent_messages: AiConversationMessage[];
  base_version_id?: number | null;
  context_snippets?: AiContextSnippet[];
  attachments?: AiAttachment[];
  knowledge_search_enabled?: boolean;
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
 * Attachments are validated, bounded and (for PDF/DOCX/XLS/XLSX/text/code)
 * parsed server-side (including bounded spreadsheet-to-text extraction); they exist only for the current request and are never
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
