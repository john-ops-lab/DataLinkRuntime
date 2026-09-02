/** Task 运行设置：运行参数与唯一 Input Object 配置（M5.5.11 / Issue #127 A2）。
 *
 * 运行方式只决定 Task 的触发配置；输入对象是一个独立的 Adapter 资源。
 * manual、schedule 与 schedule 的“立即运行一次”都读取同一个已保存
 * InputConfig，页面不再维护两套 JSON 编辑器，也不向 Execution API 传入 input。
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Alert, Button, Card, Form, Input, InputNumber, Progress, Radio, Select, Space, Spin, Tag, Tooltip, Typography, Upload } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, api } from "../api";
import { uploadManagedInputArtifact } from "../managed-input-client";
import { isTerminal } from "../status";
import type {
  Adapter,
  AdapterInputConfig,
  AdapterSchedule,
  Execution,
  InputArtifactSummary,
  InputRetention,
  InputSourceType,
  ManagedInputArtifact,
  ManagedInputCapability,
  ScheduleMisfirePolicy,
  ScheduleOutcome,
  TaskRunMode,
  Worker,
} from "../types";
import { userErrorMessage } from "../user-message";
import ManagedInputExamples from "./ManagedInputExamples";

export interface TaskRuntimeState {
  scheduleEnabled: boolean;
  loading: boolean;
  activeExecution: boolean;
  canRun: boolean;
  scheduleEnableBlockedReason: string | null;
  /** Optional for callers that construct the state outside this component. */
  runBlockedReason?: string | null;
}

export interface TaskRunSettingsHandle {
  runOnce: () => void;
  stopExecution: () => void;
  toggleSchedule: () => void;
  confirmLeave: () => boolean;
}

interface TaskRunSettingsPanelProps {
  adapter: Adapter;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  execution: Execution | null;
  /** M5.5.9：编辑页存在未保存修改时禁止启动运行。 */
  dirty: boolean;
  onAdapterChange: (adapter: Adapter) => void;
  onExecutionStarted: (execution: Execution) => void;
  onRuntimeStateChange: (state: TaskRuntimeState) => void;
  onError: (message: string | null) => void;
  readOnly?: boolean;
}

// M5.5.11 单次执行超时合同（秒为权威值）。
const DEFAULT_TIMEOUT_SECONDS = 300;
const MAX_TIMEOUT_SECONDS = 24 * 60 * 60; // 24 小时
const TIMEOUT_PRESET_MINUTES = [1, 5, 10, 30, 60] as const;
const DEFAULT_SCHEDULE_MISFIRE_POLICY: ScheduleMisfirePolicy = "coalesce_latest";
const DEFAULT_SCHEDULE_CATCHUP_COUNT = 100;
const DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS = 86_400;

const SCHEDULE_OUTCOME_LABEL_KEYS: Record<ScheduleOutcome, string> = {
  enqueued: "task.settings.outcomeEnqueued",
  coalesced: "task.settings.outcomeCoalesced",
  skipped: "task.settings.outcomeSkipped",
  expired: "task.settings.outcomeExpired",
};

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "null";
}

function parseJson(text: string): { ok: true; value: unknown } | { ok: false } {
  if (text.trim() === "") {
    return { ok: false };
  }
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}

type InputErrorTranslationKey = "revisionConflict" | "sourceNotAvailable" | "notInitialized" | "managedFilesEmpty";

type ManagedFileErrorTranslationKey =
  | InputErrorTranslationKey
  | "fileNameConflict"
  | "fileLimit"
  | "retentionOutOfRange"
  | "manualDeleteNotAllowed"
  | "uploadFailed";

type ManagedFileDraft = ManagedInputArtifact | InputArtifactSummary;

const MANAGED_FILE_LIMIT = 8;

function canonicalFilename(filename: string): string {
  const basename = filename.replaceAll("\\", "/").split("/").pop() ?? filename;
  return basename.normalize("NFC").toLocaleLowerCase("en-US");
}

function displayFilename(filename: string): string {
  return filename.replaceAll("\\", "/").split("/").pop() ?? filename;
}

function fileExtension(filename: string): string {
  const name = displayFilename(filename).toLocaleLowerCase("en-US");
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot) : "";
}

function managedArtifactIds(files: readonly ManagedFileDraft[]): number[] {
  return files.map((file) => file.id);
}

function mergeManagedFileDrafts(
  ...groups: readonly (readonly ManagedFileDraft[])[]
): ManagedFileDraft[] {
  const merged: ManagedFileDraft[] = [];
  const seen = new Set<number>();
  for (const group of groups) {
    for (const artifact of group) {
      if (seen.has(artifact.id)) {
        continue;
      }
      seen.add(artifact.id);
      if (merged.length < MANAGED_FILE_LIMIT) {
        merged.push(artifact);
      }
    }
  }
  return merged;
}

function sameIds(left: readonly number[], right: readonly number[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

function hasManagedFilenameConflict(files: readonly ManagedFileDraft[]): boolean {
  const canonicalNames = new Set<string>();
  for (const file of files) {
    const canonical = canonicalFilename(file.original_filename);
    if (canonicalNames.has(canonical)) {
      return true;
    }
    canonicalNames.add(canonical);
  }
  return false;
}

function isStagedArtifact(artifact: ManagedFileDraft | undefined): artifact is ManagedInputArtifact {
  return artifact?.status === "STAGED" && artifact.ordinal === undefined;
}

function localizedInputReason(
  codeOrReason: string | null,
  reason: unknown,
  translate: (key: InputErrorTranslationKey) => string,
): string | null {
  const key = codeOrReason === "input_config_revision_conflict"
    ? "revisionConflict"
    : codeOrReason === "input_source_not_available"
      ? "sourceNotAvailable"
      : codeOrReason === "input_config_not_initialized"
        ? "notInitialized"
        : codeOrReason === "managed_files_empty" || (codeOrReason === "input_invalid" && reason === "managed_files_empty")
          ? "managedFilesEmpty"
          : null;
  return key === null ? null : translate(key);
}

function localizedManagedInputReason(
  error: ApiError,
  translate: (key: ManagedFileErrorTranslationKey, options?: Record<string, unknown>) => string,
): string | null {
  const reason = typeof error.params.reason === "string" ? error.params.reason : null;
  const key = error.code === "input_config_revision_conflict"
    ? "revisionConflict"
    : error.code === "input_source_not_available"
      ? "sourceNotAvailable"
      : error.code === "input_config_not_initialized"
        ? "notInitialized"
        : error.code === "input_invalid" && reason === "managed_files_empty"
          ? "managedFilesEmpty"
          : reason === "artifact_name_conflict"
            ? "fileNameConflict"
            : reason === "managed_files_limit"
              ? "fileLimit"
              : reason === "retention_out_of_range"
                ? "retentionOutOfRange"
                : reason === "manual_delete_not_allowed"
                  ? "manualDeleteNotAllowed"
                  : error.code === "input_upload_failed" || error.code === "input_upload_interrupted"
                    ? "uploadFailed"
                    : null;
  if (key === null) {
    return null;
  }
  if (key === "retentionOutOfRange") {
    const max = error.params.max_seconds;
    return translate(key, { max: typeof max === "number" ? max : "—" });
  }
  return translate(key);
}

function formatTime(value: string | null, locale: "zh-CN" | "en"): string {
  return value === null
    ? "—"
    : new Date(value).toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function presetMinutesFor(seconds: number): number | undefined {
  return TIMEOUT_PRESET_MINUTES.find((minutes) => minutes * 60 === seconds);
}

const TaskRunSettingsPanel = forwardRef<TaskRunSettingsHandle, TaskRunSettingsPanelProps>(function TaskRunSettingsPanel(props, ref) {
  const { i18n, t } = useTranslation(["runtime", "common", "adapter"]);
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const adapterId = props.adapter.id;
  const readOnly = props.readOnly === true;
  const onAdapterChange = props.onAdapterChange;
  const onError = props.onError;
  const onRuntimeStateChange = props.onRuntimeStateChange;
  const [workerOverride, setWorkerOverride] = useState<number | null | undefined>(undefined);
  const [runModeOverride, setRunModeOverride] = useState<TaskRunMode | undefined>(undefined);
  // M5.5.11: 表单内超时值（秒）；null = 跟随 Adapter 保存值。
  const [timeoutOverride, setTimeoutOverride] = useState<number | null>(null);
  const [timeoutCustomMode, setTimeoutCustomMode] = useState(false);
  const [schedule, setSchedule] = useState<AdapterSchedule | null>(null);
  const [cron, setCron] = useState("*/5 * * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [misfirePolicy, setMisfirePolicy] = useState<ScheduleMisfirePolicy>(DEFAULT_SCHEDULE_MISFIRE_POLICY);
  const [maxCatchupCount, setMaxCatchupCount] = useState(DEFAULT_SCHEDULE_CATCHUP_COUNT);
  const [maxCatchupAgeSeconds, setMaxCatchupAgeSeconds] = useState(DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
  const [loadingSchedule, setLoadingSchedule] = useState(props.adapter.run_mode === "schedule");
  // 用户是否实际修改过定时字段（cron/timezone）。未修改时统一保存
  // 不得用表单值整体 PUT，避免把线上真实 Schedule 冲掉。
  const [scheduleTouched, setScheduleTouched] = useState(false);
  const [schedulePolicyTouched, setSchedulePolicyTouched] = useState(false);
  // 初始 Schedule GET 非 404 失败时置位：此时表单仍是默认值，禁止 PUT。
  const [scheduleLoadFailed, setScheduleLoadFailed] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  // Input Object is one resource with one saved revision and one draft. These
  // states intentionally do not depend on runMode, so changing manual/schedule
  // cannot reset an unsaved JSON draft.
  const [inputConfig, setInputConfig] = useState<AdapterInputConfig | null>(null);
  const [inputSourceDraft, setInputSourceDraft] = useState<InputSourceType>("none");
  const [inputJsonDraft, setInputJsonDraft] = useState("null");
  const [loadingInput, setLoadingInput] = useState(true);
  const [inputLoadFailed, setInputLoadFailed] = useState(false);
  const [savingInput, setSavingInput] = useState(false);
  const [inputValidationError, setInputValidationError] = useState<string | null>(null);
  const [managedCapability, setManagedCapability] = useState<ManagedInputCapability | null>(null);
  const [loadingManagedCapability, setLoadingManagedCapability] = useState(true);
  const [managedCapabilityLoadFailed, setManagedCapabilityLoadFailed] = useState(false);
  const [loadingStagedArtifacts, setLoadingStagedArtifacts] = useState(false);
  const [stagedArtifactsLoadFailed, setStagedArtifactsLoadFailed] = useState(false);
  const [stagedArtifacts, setStagedArtifacts] = useState<ManagedInputArtifact[]>([]);
  const [managedFilesDraft, setManagedFilesDraft] = useState<ManagedFileDraft[]>([]);
  const [retentionDraft, setRetentionDraft] = useState<InputRetention>({
    mode: "system_default",
    seconds: null,
  });
  const [uploadProgress, setUploadProgress] = useState<{
    key: string;
    filename: string;
    percent: number;
  }[]>([]);
  const [deletingArtifactId, setDeletingArtifactId] = useState<number | null>(null);

  // 保存流程（PATCH + PUT）完成后递增，使保存前发出的 Schedule GET 响应
  // 成为陈旧信号，不能覆盖刚保存成功的表单值。
  const scheduleLoadEpoch = useRef(0);
  const inputLoadEpoch = useRef(0);
  const runMode = runModeOverride ?? props.adapter.run_mode;

  const refreshAdapter = useCallback(async () => {
    const refreshed = await api.getAdapter(adapterId);
    onAdapterChange(refreshed);
    setWorkerOverride(undefined);
    setRunModeOverride(undefined);
    setTimeoutOverride(null);
    setTimeoutCustomMode(false);
  }, [adapterId, onAdapterChange]);

  const loadInputConfig = useCallback(async () => {
    const epoch = inputLoadEpoch.current + 1;
    inputLoadEpoch.current = epoch;
    setLoadingInput(true);
    setInputLoadFailed(false);
    try {
      const loaded = await api.getInputConfig(adapterId);
      if (inputLoadEpoch.current !== epoch) {
        return;
      }
      setInputConfig(loaded);
      setInputSourceDraft(loaded.source_type);
      setInputJsonDraft(loaded.source_type === "json" ? formatJson(loaded.json_value) : "null");
      setRetentionDraft(loaded.retention);
      setManagedFilesDraft((current) => {
        const staged = current.filter((artifact) => artifact.status === "STAGED");
        return mergeManagedFileDrafts(loaded.artifacts, staged);
      });
      setInputValidationError(null);
    } catch (error) {
      if (inputLoadEpoch.current === epoch) {
        setInputLoadFailed(true);
        onError(errorMessage(error));
      }
    } finally {
      if (inputLoadEpoch.current === epoch) {
        setLoadingInput(false);
      }
    }
  }, [adapterId, onError]);

  useEffect(() => {
    void loadInputConfig();
    return () => {
      inputLoadEpoch.current += 1;
    };
  }, [loadInputConfig]);

  const loadStagedArtifacts = useCallback(async () => {
    setLoadingStagedArtifacts(true);
    setStagedArtifactsLoadFailed(false);
    try {
      const staged = await api.listInputArtifacts(adapterId);
      setStagedArtifacts(staged);
      setManagedFilesDraft((current) => mergeManagedFileDrafts(current, staged));
    } catch {
      // A staged-list outage does not invalidate already loaded capability,
      // retention policy, or the user's current draft.
      setStagedArtifactsLoadFailed(true);
    } finally {
      setLoadingStagedArtifacts(false);
    }
  }, [adapterId]);

  const loadManagedInput = useCallback(async () => {
    setLoadingManagedCapability(true);
    setManagedCapabilityLoadFailed(false);
    setStagedArtifactsLoadFailed(false);
    try {
      const capability = await api.getManagedInputCapability();
      setManagedCapability(capability);
      if (capability.managed_files_enabled && capability.ready) {
        await loadStagedArtifacts();
      } else {
        setStagedArtifacts([]);
      }
    } catch {
      // Capability is the sole policy authority. Fail closed and make the
      // retry explicit instead of inventing client-side retention limits.
      setManagedCapability(null);
      setManagedCapabilityLoadFailed(true);
    } finally {
      setLoadingManagedCapability(false);
    }
  }, [loadStagedArtifacts]);

  useEffect(() => {
    void loadManagedInput();
  }, [loadManagedInput]);

  useEffect(() => {
    if (stagedArtifacts.length === 0) {
      return;
    }
    const warnBeforeLeaving = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = t("task.input.stagedLeaveWarning");
    };
    window.addEventListener("beforeunload", warnBeforeLeaving);
    return () => window.removeEventListener("beforeunload", warnBeforeLeaving);
  }, [stagedArtifacts.length, t]);

  const loadSchedule = useCallback(async () => {
    setLoadingSchedule(true);
    try {
      const loaded = await api.getSchedule(adapterId);
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setMisfirePolicy(loaded.misfire_policy ?? DEFAULT_SCHEDULE_MISFIRE_POLICY);
      setMaxCatchupCount(loaded.max_catchup_count ?? DEFAULT_SCHEDULE_CATCHUP_COUNT);
      setMaxCatchupAgeSeconds(loaded.max_catchup_age_seconds ?? DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
      setScheduleLoadFailed(false);
      setScheduleTouched(false);
      setSchedulePolicyTouched(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && error.code === "schedule_not_configured") {
        setSchedule(null);
        setMisfirePolicy(DEFAULT_SCHEDULE_MISFIRE_POLICY);
        setMaxCatchupCount(DEFAULT_SCHEDULE_CATCHUP_COUNT);
        setMaxCatchupAgeSeconds(DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
        setScheduleLoadFailed(false);
        setScheduleTouched(false);
        setSchedulePolicyTouched(false);
      } else {
        // 加载失败时表单仍是默认值：标记后统一保存会跳过 Schedule PUT。
        setScheduleLoadFailed(true);
        onError(errorMessage(error));
      }
    } finally {
      setLoadingSchedule(false);
    }
  }, [adapterId, onError]);

  useEffect(() => {
    if (runMode !== "schedule") {
      return;
    }
    let cancelled = false;
    const epoch = scheduleLoadEpoch.current;
    api.getSchedule(adapterId).then((loaded) => {
      if (cancelled || scheduleLoadEpoch.current !== epoch) return;
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setMisfirePolicy(loaded.misfire_policy ?? DEFAULT_SCHEDULE_MISFIRE_POLICY);
      setMaxCatchupCount(loaded.max_catchup_count ?? DEFAULT_SCHEDULE_CATCHUP_COUNT);
      setMaxCatchupAgeSeconds(loaded.max_catchup_age_seconds ?? DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
      setScheduleLoadFailed(false);
      setScheduleTouched(false);
      setSchedulePolicyTouched(false);
    }).catch((error) => {
      // 404 = Schedule 尚未配置：保持空状态即可。保存流程里 PATCH 先于 PUT，
      // 迟到的 404 响应是陈旧信号，必须忽略，不能覆盖刚保存成功的 Schedule。
      if (!cancelled && !(error instanceof ApiError && error.status === 404)) {
        setScheduleLoadFailed(true);
        onError(errorMessage(error));
      }
    }).finally(() => {
      if (!cancelled) setLoadingSchedule(false);
    });
    return () => {
      cancelled = true;
    };
  }, [adapterId, onError, runMode]);

  const workerId =
    workerOverride === undefined ? (props.adapter.runtime_worker_id ?? null) : workerOverride;

  // M5.5.11: 表单显示值 = 表单覆盖 ?? Adapter 权威值 ?? 默认 300 秒。
  const effectiveTimeoutSeconds =
    timeoutOverride ?? props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS;
  const effectiveCustom =
    timeoutCustomMode || presetMinutesFor(effectiveTimeoutSeconds) === undefined;

  function resolveTimeoutSeconds(): number | null {
    const minutes = presetMinutesFor(effectiveTimeoutSeconds);
    if (!effectiveCustom && minutes !== undefined) {
      return minutes * 60;
    }
    if (!Number.isInteger(effectiveTimeoutSeconds) || effectiveTimeoutSeconds < 1 || effectiveTimeoutSeconds > MAX_TIMEOUT_SECONDS) {
      onError(t("task.settings.invalidTimeout", { max: MAX_TIMEOUT_SECONDS }));
      return null;
    }
    return effectiveTimeoutSeconds;
  }

  function localizedInputError(error: unknown): string {
    if (error instanceof ApiError) {
      const localizedReason = localizedManagedInputReason(
        error,
        (key, options) => t(`task.input.errors.${key}`, options),
      ) ?? localizedInputReason(
        error.code,
        error.params.reason,
        (key) => t(`task.input.errors.${key}`),
      );
      if (localizedReason !== null) {
        return userErrorMessage(error, localizedReason);
      }
    }
    return errorMessage(error);
  }

  const managedFileExtensions = managedCapability?.allowed_extensions ?? [];
  const managedFileExtensionSet = new Set(managedFileExtensions);
  const managedFileAccept = managedFileExtensions.join(",");
  const managedFileTypesLabel = managedFileExtensions.join(locale === "en" ? ", " : "、");
  const managedFilesEnabled =
    managedCapability?.managed_files_enabled === true
    && managedCapability.ready === true
    && managedFileExtensions.length > 0;
  const managedFilesSaveBlocked = inputSourceDraft === "managed_files" && !managedFilesEnabled;

  function setManagedInputError(message: string): void {
    setInputValidationError(message);
    onError(message);
  }

  function stagedOrDraftArtifact(artifactId: number): ManagedFileDraft | undefined {
    return managedFilesDraft.find((artifact) => artifact.id === artifactId)
      ?? stagedArtifacts.find((artifact) => artifact.id === artifactId);
  }

  function validateManagedFileSelection(
    file: File,
    replacementId: number | null,
  ): string | null {
    if (!managedFileExtensionSet.has(fileExtension(file.name))) {
      return t("task.input.fileTypeNotAllowed");
    }
    const selectedWithoutReplacement = managedFilesDraft.filter(
      (artifact) => artifact.id !== replacementId,
    );
    if (replacementId === null && selectedWithoutReplacement.length >= MANAGED_FILE_LIMIT) {
      return t("task.input.errors.fileLimit");
    }
    const canonical = canonicalFilename(file.name);
    if (selectedWithoutReplacement.some((artifact) => canonicalFilename(artifact.original_filename) === canonical)) {
      return t("task.input.errors.fileNameConflict");
    }
    return null;
  }

  async function uploadManagedFile(file: File, replacementId: number | null): Promise<void> {
    if (!managedFilesEnabled || readOnly) {
      return;
    }
    const validationError = validateManagedFileSelection(file, replacementId);
    if (validationError !== null) {
      setManagedInputError(validationError);
      return;
    }

    const progressKey = `${file.name}:${file.size}:${Date.now()}`;
    setInputValidationError(null);
    onError(null);
    setUploadProgress((current) => [...current, { key: progressKey, filename: file.name, percent: 0 }]);
    try {
      const uploaded = await uploadManagedInputArtifact(adapterId, file, {
        onProgress: ({ loaded, total }) => {
          const percent = total === null || total <= 0
            ? 0
            : Math.min(100, Math.round((loaded / total) * 100));
          setUploadProgress((current) => current.map((entry) =>
            entry.key === progressKey ? { ...entry, percent } : entry,
          ));
        },
      });
      const previous = replacementId === null ? undefined : stagedOrDraftArtifact(replacementId);
      setManagedFilesDraft((current) => mergeManagedFileDrafts(
        current.filter((artifact) => artifact.id !== replacementId),
        [uploaded],
      ));
      setStagedArtifacts((current) => [
        ...current.filter((artifact) => artifact.id !== replacementId && artifact.id !== uploaded.id),
        uploaded,
      ]);
      if (!inputEditingLocked) {
        setInputSourceDraft("managed_files");
      }
      if (isStagedArtifact(previous) && previous.id !== uploaded.id) {
        try {
          await api.deleteInputArtifact(adapterId, previous.id);
          setStagedArtifacts((current) => current.filter((artifact) => artifact.id !== previous.id));
        } catch (error) {
          // The replacement remains a valid staged draft; leave the old
          // staged item visible so a later explicit delete can recover it.
          setManagedFilesDraft((current) => current.some((item) => item.id === previous.id)
            ? current
            : mergeManagedFileDrafts(current, [previous]));
          setStagedArtifacts((current) => current.some((item) => item.id === previous.id)
            ? current
            : [...current, previous]);
          setManagedInputError(localizedInputError(error));
        }
      }
    } catch (error) {
      setManagedInputError(localizedInputError(error));
    } finally {
      setUploadProgress((current) => current.filter((entry) => entry.key !== progressKey));
    }
  }

  async function deleteManagedFile(artifact: ManagedFileDraft): Promise<void> {
    if (readOnly || deletingArtifactId !== null) {
      return;
    }
    if (artifact.status === "STAGED") {
      setDeletingArtifactId(artifact.id);
      setInputValidationError(null);
      onError(null);
      try {
        await api.deleteInputArtifact(adapterId, artifact.id);
        setManagedFilesDraft((current) => current.filter((item) => item.id !== artifact.id));
        setStagedArtifacts((current) => current.filter((item) => item.id !== artifact.id));
      } catch (error) {
        setManagedInputError(localizedInputError(error));
      } finally {
        setDeletingArtifactId(null);
      }
      return;
    }
    // READY/current files are removed from the draft only. The server marks
    // them PENDING_DELETE during the next revisioned save.
    setManagedFilesDraft((current) => current.filter((item) => item.id !== artifact.id));
  }

  async function saveInputObject() {
    if (
      readOnly ||
      savingInput ||
      loadingInput ||
      inputConfig === null ||
      inputSourceDraft === "remote_files" ||
      managedFilesSaveBlocked ||
      props.adapter.runtime_locked === true ||
      schedule?.enabled === true
    ) {
      return;
    }

    const payload = inputSourceDraft === "none"
      ? { expected_revision: inputConfig.revision, source_type: "none" as const }
      : inputSourceDraft === "managed_files"
        ? {
            expected_revision: inputConfig.revision,
            source_type: "managed_files" as const,
            artifact_ids: managedArtifactIds(managedFilesDraft),
            retention: retentionDraft,
          }
        : (() => {
            const parsed = parseJson(inputJsonDraft);
            if (!parsed.ok) {
              return null;
            }
            return {
              expected_revision: inputConfig.revision,
              source_type: "json" as const,
              json_value: parsed.value,
            };
          })();
    if (payload === null) {
      const message = t("task.input.invalidJson");
      setInputValidationError(message);
      onError(message);
      return;
    }
    if (inputSourceDraft === "managed_files" && hasManagedFilenameConflict(managedFilesDraft)) {
      const message = t("task.input.errors.fileNameConflict");
      setInputValidationError(message);
      onError(message);
      return;
    }
    if (
      inputSourceDraft === "managed_files"
      && managedArtifactIds(managedFilesDraft).length > MANAGED_FILE_LIMIT
    ) {
      const message = t("task.input.errors.fileLimit");
      setInputValidationError(message);
      onError(message);
      return;
    }

    setSavingInput(true);
    setInputValidationError(null);
    onError(null);
    try {
      const saved = await api.putInputConfig(adapterId, payload);
      setInputConfig(saved);
      setInputSourceDraft(saved.source_type);
      setInputJsonDraft(saved.source_type === "json" ? formatJson(saved.json_value) : "null");
      setRetentionDraft(saved.retention);
      const remainingStaged = stagedArtifacts.filter(
        (artifact) => !saved.artifacts.some((item) => item.id === artifact.id),
      );
      setManagedFilesDraft(mergeManagedFileDrafts(saved.artifacts, remainingStaged));
      setStagedArtifacts(remainingStaged);
      setInputValidationError(null);
    } catch (error) {
      // Keep both the last valid server resource and the user's draft intact.
      // In particular, a 409 must not silently replace the stale page's text.
      const message = localizedInputError(error);
      setInputValidationError(message);
      onError(message);
      if (error instanceof ApiError && error.code === "adapter_runtime_locked") {
        // Re-read the Adapter lock without replacing the user's Input Object
        // draft. The server remains authoritative for the next save attempt.
        void refreshAdapter().catch(() => undefined);
      }
    } finally {
      setSavingInput(false);
    }
  }

  async function saveRunConfig() {
    if (readOnly || savingRuntime || props.adapter.runtime_locked) {
      return;
    }
    if (workerId === null) {
      onError(t("task.settings.chooseWorker"));
      return;
    }
    const timeoutSeconds = resolveTimeoutSeconds();
    if (timeoutSeconds === null) {
      return;
    }
    setSavingRuntime(true);
    onError(null);
    try {
      const refreshed = await api.updateAdapter(adapterId, {
        runtime_worker_id: workerId,
        run_mode: runMode,
        timeout_seconds: timeoutSeconds,
      });
      onAdapterChange(refreshed);
      setWorkerOverride(undefined);
      setRunModeOverride(undefined);
      setTimeoutOverride(null);
      setTimeoutCustomMode(false);
      if (refreshed.run_mode === "schedule") {
        // 只在两种情况下整体 PUT Schedule：用户实际修改过定时字段，或确认
        // 从未配置过（GET 已返回 404）。加载中/加载失败时表单仍是默认值，
        // 此时 PUT 会静默冲掉线上真实配置，必须跳过并明确提示。
        const scheduleUnknown =
          schedule === null && (loadingSchedule || scheduleLoadFailed);
        const shouldPutSchedule = scheduleTouched || (schedule === null && !scheduleUnknown);
        if (shouldPutSchedule) {
          const saved = await api.putSchedule(adapterId, {
            enabled: schedule?.enabled === true,
            cron,
            timezone,
            ...(schedulePolicyTouched
              ? {
                  misfire_policy: misfirePolicy,
                  max_catchup_count: maxCatchupCount,
                  max_catchup_age_seconds: maxCatchupAgeSeconds,
                }
              : {}),
          });
          // 保存成功后的值立即落回表单，并作废任何在途的 Schedule GET。
          scheduleLoadEpoch.current += 1;
          setSchedule(saved);
          setCron(saved.cron);
          setTimezone(saved.timezone);
          setMisfirePolicy(saved.misfire_policy ?? DEFAULT_SCHEDULE_MISFIRE_POLICY);
          setMaxCatchupCount(saved.max_catchup_count ?? DEFAULT_SCHEDULE_CATCHUP_COUNT);
          setMaxCatchupAgeSeconds(saved.max_catchup_age_seconds ?? DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
          setScheduleTouched(false);
          setSchedulePolicyTouched(false);
        } else if (scheduleUnknown) {
          onError(t("task.settings.savedScheduleNotLoaded"));
        }
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingRuntime(false);
    }
  }

  async function saveSchedule(enabled: boolean) {
    if (readOnly) {
      return;
    }
    if (enabled && activeExecution) {
      onError(t("task.reasons.activeSchedule"));
      return;
    }
    if (enabled && scheduleEnableBlockedReason !== null) {
      onError(scheduleEnableBlockedReason);
      return;
    }
    setSavingSchedule(true);
    onError(null);
    try {
      // InputConfig is the only input editor. Schedule PUT keeps the legacy
      // mirror server-owned by omitting its old input field entirely.
      const saved = await api.putSchedule(adapterId, {
        enabled,
        cron,
        timezone,
        ...(schedulePolicyTouched
          ? {
              misfire_policy: misfirePolicy,
              max_catchup_count: maxCatchupCount,
              max_catchup_age_seconds: maxCatchupAgeSeconds,
            }
          : {}),
      });
      setSchedule(saved);
      setCron(saved.cron);
      setTimezone(saved.timezone);
      setMisfirePolicy(saved.misfire_policy ?? DEFAULT_SCHEDULE_MISFIRE_POLICY);
      setMaxCatchupCount(saved.max_catchup_count ?? DEFAULT_SCHEDULE_CATCHUP_COUNT);
      setMaxCatchupAgeSeconds(saved.max_catchup_age_seconds ?? DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
      setScheduleTouched(false);
      setSchedulePolicyTouched(false);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingSchedule(false);
    }
  }

  async function runOnce() {
    if (!canRun) {
      onError(runBlockedReason ?? t("task.reasons.unavailable"));
      return;
    }
    setSubmitting(true);
    onError(null);
    try {
      // Control resolves the saved InputConfig. The new Web never sends an
      // input override, including for schedule-mode run-now.
      const execution = await api.createExecution(adapterId);
      props.onExecutionStarted(execution);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopExecution() {
    if (readOnly) {
      return;
    }
    const watchedExecutionId =
      props.execution !== null &&
      props.execution.adapter_id === adapterId &&
      !isTerminal(props.execution.status)
        ? props.execution.id
        : null;
    const executionId = props.adapter.running_execution_id ?? watchedExecutionId;
    if (executionId == null) {
      return;
    }
    setCancelling(true);
    onError(null);
    try {
      const execution = await api.cancelExecution(executionId);
      props.onExecutionStarted(execution);
      if (isTerminal(execution.status)) {
        await refreshAdapter();
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setCancelling(false);
    }
  }

  const execution = props.execution;
  const activeExecution =
    props.adapter.running_execution_id != null ||
    (execution !== null && !isTerminal(execution.status));
  const runtimeLocked = props.adapter.runtime_locked === true;
  const scheduleEnabled = schedule?.enabled === true;
  const scheduleFieldsLocked = scheduleEnabled || runtimeLocked;
  const scheduleConfigMissing =
    runMode === "schedule" && schedule === null && !loadingSchedule && !scheduleLoadFailed;
  const runtimeConfigDirty =
    runMode !== props.adapter.run_mode ||
    workerId !== (props.adapter.runtime_worker_id ?? null) ||
    effectiveTimeoutSeconds !== (props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS) ||
    scheduleTouched;

  const inputDirty = inputConfig !== null && (
    inputSourceDraft !== inputConfig.source_type ||
    (inputSourceDraft === "json" && inputJsonDraft !== formatJson(inputConfig.json_value)) ||
    (inputSourceDraft === "managed_files" && (
      !sameIds(managedArtifactIds(managedFilesDraft), inputConfig.artifacts.map((artifact) => artifact.id)) ||
      retentionDraft.mode !== inputConfig.retention.mode ||
      retentionDraft.seconds !== inputConfig.retention.seconds
    ))
  );
  const managedFilenameConflict = inputSourceDraft === "managed_files" && hasManagedFilenameConflict(managedFilesDraft);
  const managedDraftIds = new Set(managedFilesDraft.map((artifact) => artifact.id));
  const overflowStagedArtifacts = stagedArtifacts.filter(
    (artifact) => !managedDraftIds.has(artifact.id),
  );
  const savedManagedInput = inputConfig?.source_type === "managed_files";
  const managedInputExampleReady =
    savedManagedInput &&
    inputSourceDraft === "managed_files" &&
    inputConfig?.valid_for_run === true &&
    managedFilesDraft.length > 0 &&
    !inputDirty &&
    managedFilesEnabled;
  const managedInputExampleDisabledReason = inputDirty
    ? t("task.input.examples.saveFirst")
    : !managedFilesEnabled
      ? t("task.input.examples.disabledReason")
      : t("task.input.examples.notReady");
  const inputInvalidReason = inputConfig?.invalid_reason ?? null;
  const inputInvalidMessage = localizedInputReason(
    inputInvalidReason,
    undefined,
    (key) => t(`task.input.errors.${key}`),
  );
  const inputBlockedReason = (() => {
    if (loadingInput) return t("task.input.loading");
    if (inputLoadFailed || inputConfig === null) return t("task.input.loadFailed");
    if (inputDirty) return t("task.input.saveBeforeRun");
    if (managedFilenameConflict) return t("task.input.errors.fileNameConflict");
    if (!inputConfig.valid_for_run) {
      if (inputInvalidMessage !== null) return inputInvalidMessage;
      return t("task.input.invalidConfig");
    }
    if (inputSourceDraft === "managed_files" && loadingManagedCapability) {
      return t("task.input.managedFilesLoading");
    }
    if (inputSourceDraft === "managed_files" && managedCapabilityLoadFailed) {
      return t("task.input.capabilityLoadFailed");
    }
    if (inputSourceDraft === "managed_files" && !managedFilesEnabled) {
      return t("task.input.managedFilesDisabled");
    }
    return null;
  })();

  const scheduleEnableBlockedReason = (() => {
    if (loadingSchedule) return t("task.reasons.loadingSchedule");
    if (scheduleLoadFailed) return t("task.reasons.scheduleLoadFailed");
    if (inputBlockedReason !== null) return inputBlockedReason;
    if (props.adapter.latest_version_id === null) return t("task.reasons.saveVersionFirst");
    if (props.adapter.runtime_worker_id == null) return t("task.reasons.saveWorkerFirst");
    if (props.dirty) return t("task.reasons.saveChangesFirst");
    if (runtimeConfigDirty) return t("task.reasons.runtimeDirty");
    if (scheduleConfigMissing) return t("task.reasons.saveRuntimeFirst");
    return null;
  })();

  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );
  const canRun =
    !readOnly &&
    !props.dirty &&
    inputBlockedReason === null &&
    !props.adapter.archived_at &&
    props.adapter.latest_version_id !== null &&
    props.adapter.runtime_worker_id != null &&
    !activeExecution &&
    !submitting;
  const runBlockedReason = (() => {
    if (readOnly) return t("task.reasons.readOnly");
    if (props.dirty) return t("task.reasons.dirtyRun");
    if (props.adapter.archived_at) return t("task.reasons.deleted");
    if (inputBlockedReason !== null) return inputBlockedReason;
    if (props.adapter.latest_version_id === null) return t("task.reasons.noVersion");
    if (props.adapter.runtime_worker_id == null) return t("task.reasons.noWorker");
    if (activeExecution) return t("task.reasons.activeRun");
    if (submitting) return t("task.reasons.processing");
    // 防御性兜底：未来若新增 canRun 条件但遗漏对应文案，命令式入口仍保持阻断。
    return canRun ? null : t("task.reasons.unavailable");
  })();

  const inputEditingLocked =
    readOnly || runtimeLocked || scheduleEnabled || savingInput || loadingInput || inputLoadFailed || inputConfig === null;
  const sourceCards: {
    sourceType: InputSourceType;
    title: string;
    description: string;
    status: string;
    disabledReason: string | null;
  }[] = [
    {
      sourceType: "none",
      title: t("input.sources.none.title", { ns: "adapter" }),
      description: t("input.sources.none.description", { ns: "adapter" }),
      status: t("input.sources.none.status", { ns: "adapter" }),
      disabledReason: inputEditingLocked ? t("task.input.locked") : null,
    },
    {
      sourceType: "json",
      title: t("input.sources.json.title", { ns: "adapter" }),
      description: t("input.sources.json.description", { ns: "adapter" }),
      status: t("input.sources.json.status", { ns: "adapter" }),
      disabledReason: inputEditingLocked ? t("task.input.locked") : null,
    },
    {
      sourceType: "managed_files",
      title: t("input.sources.managedFiles.title", { ns: "adapter" }),
      description: t("input.sources.managedFiles.description", { ns: "adapter" }),
      status: managedFilesEnabled
        ? t("input.sources.managedFiles.status", { ns: "adapter" })
        : t("input.sources.managedFiles.disabled", { ns: "adapter" }),
      disabledReason: inputEditingLocked
        ? t("task.input.locked")
        : loadingManagedCapability
          ? t("task.input.managedFilesLoading")
          : managedCapabilityLoadFailed
            ? t("task.input.capabilityLoadFailed")
          : managedFilesEnabled
            ? null
            : t("input.sources.managedFiles.disabled", { ns: "adapter" }),
    },
    {
      sourceType: "remote_files",
      title: t("input.sources.remoteFiles.title", { ns: "adapter" }),
      description: t("input.sources.remoteFiles.description", { ns: "adapter" }),
      status: t("input.sources.remoteFiles.disabled", { ns: "adapter" }),
      disabledReason: t("input.sources.remoteFiles.disabled", { ns: "adapter" }),
    },
  ];

  function selectInputSource(sourceType: InputSourceType, disabledReason: string | null): void {
    if (disabledReason !== null) {
      return;
    }
    setInputSourceDraft(sourceType);
    if (sourceType === "managed_files") {
      setManagedFilesDraft((current) => mergeManagedFileDrafts(current, stagedArtifacts));
      setRetentionDraft(inputConfig?.source_type === "managed_files"
        ? inputConfig.retention
        : { mode: "system_default", seconds: null });
    }
    setInputValidationError(null);
    onError(null);
  }

  useEffect(() => {
    onRuntimeStateChange({
      scheduleEnabled,
      loading: loadingInput || loadingSchedule || savingRuntime || savingInput || savingSchedule || submitting || cancelling,
      activeExecution,
      canRun,
      scheduleEnableBlockedReason,
      runBlockedReason,
    });
  }, [
    activeExecution,
    canRun,
    cancelling,
    loadingInput,
    loadingSchedule,
    onRuntimeStateChange,
    runBlockedReason,
    savingInput,
    savingRuntime,
    savingSchedule,
    scheduleEnableBlockedReason,
    scheduleEnabled,
    submitting,
  ]);

  useImperativeHandle(ref, () => ({
    runOnce: () => void runOnce(),
    stopExecution: () => void stopExecution(),
    toggleSchedule: () => void saveSchedule(!scheduleEnabled),
    confirmLeave: () => stagedArtifacts.length === 0 || window.confirm(t("task.input.stagedLeaveWarning")),
  }));

  const scheduleVisible = runMode === "schedule";

  return (
    <div className="task-run-settings" data-testid="task-run-settings">
      <section className="task-runtime-config">
        <Typography.Title level={5}>{t("task.settings.title")}</Typography.Title>
        <Space direction="vertical" size="middle" className="schedule-form">
          <label className="settings-field">
            <span className="settings-field-label">{t("task.settings.worker")}</span>
            <Select
              data-testid="task-runtime-worker"
              value={workerId ?? undefined}
              placeholder={t("task.settings.workerPlaceholder")}
              loading={props.workersLoading}
              disabled={readOnly || runtimeLocked || savingRuntime || props.workersLoading}
              onChange={(value: number) => setWorkerOverride(value)}
              options={compatibleWorkers.map((worker) => ({
                value: worker.id,
                label: t("worker.option", {
                  ns: "common",
                  name: worker.name,
                  status: worker.status === "online"
                    ? t("worker.online", { ns: "common" })
                    : t("worker.offline", { ns: "common" }),
                }),
              }))}
            />
          </label>
          {props.workersError !== null && <Alert type="error" showIcon message={props.workersError} />}
          <div className="settings-field">
            <span className="settings-field-label">{t("task.settings.mode")}</span>
            <Radio.Group
              data-testid="task-run-mode"
              value={runMode}
              disabled={readOnly || runtimeLocked || savingRuntime}
              onChange={(event) => {
                const nextRunMode = event.target.value as TaskRunMode;
                setRunModeOverride(nextRunMode);
                // 初始 manual 模式尚未读取 Schedule；切换时先进入加载态，
                // 防止 effect 发起 GET 前保存默认值覆盖已有的停用配置。
                setLoadingSchedule(nextRunMode === "schedule");
              }}
            >
              <Radio value="manual">{t("task.settings.manual")}</Radio>
              <Radio value="schedule">{t("task.settings.schedule")}</Radio>
            </Radio.Group>
          </div>
          <div className="settings-field">
            <span className="settings-field-label">{t("task.settings.timeout")}</span>
            <Radio.Group
              data-testid="task-timeout-preset"
              value={effectiveCustom ? "custom" : presetMinutesFor(effectiveTimeoutSeconds)}
              disabled={readOnly || runtimeLocked || savingRuntime}
              onChange={(event) => {
                const value = event.target.value as number | "custom";
                if (value === "custom") {
                  setTimeoutCustomMode(true);
                } else {
                  setTimeoutOverride(value * 60);
                  setTimeoutCustomMode(false);
                }
              }}
            >
              {TIMEOUT_PRESET_MINUTES.map((minutes) => (
                <Radio key={minutes} value={minutes}>
                  {t("units.minutes", { value: minutes })}
                </Radio>
              ))}
              <Radio value="custom">{t("task.settings.custom")}</Radio>
            </Radio.Group>
            {effectiveCustom && (
              <InputNumber
                data-testid="task-timeout-custom"
                min={1}
                max={MAX_TIMEOUT_SECONDS}
                precision={0}
                value={effectiveTimeoutSeconds}
                suffix={t("task.settings.seconds")}
                disabled={readOnly || runtimeLocked || savingRuntime}
                onChange={(value) => setTimeoutOverride(value ?? null)}
              />
            )}
            <Typography.Text type="secondary" className="settings-field-hint">
              {t("task.settings.timeoutHint")}
            </Typography.Text>
          </div>
          {runtimeLocked && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-runtime-locked"
              message={t("task.settings.locked")}
              description={t("task.settings.lockedDescription")}
            />
          )}
        </Space>
      </section>

      {scheduleVisible && (
        <section className="task-schedule-config">
          <Typography.Title level={5}>{t("task.settings.scheduleTitle")}</Typography.Title>
          {loadingSchedule ? <Spin /> : (
            <Space direction="vertical" size="middle" className="schedule-form">
              <label className="settings-field">
                <span className="settings-field-label">{t("task.settings.cron")}</span>
                <Input data-testid="task-schedule-cron" value={cron} disabled={readOnly || scheduleFieldsLocked} onChange={(event) => { setCron(event.target.value); setScheduleTouched(true); }} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">{t("task.settings.timezone")}</span>
                <Input data-testid="task-schedule-timezone" value={timezone} disabled={readOnly || scheduleFieldsLocked} onChange={(event) => { setTimezone(event.target.value); setScheduleTouched(true); }} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">{t("task.settings.misfirePolicy")}</span>
                <Select
                  data-testid="task-schedule-misfire-policy"
                  value={misfirePolicy}
                  disabled={readOnly || scheduleFieldsLocked}
                  onChange={(value: ScheduleMisfirePolicy) => {
                    setMisfirePolicy(value);
                    setScheduleTouched(true);
                    setSchedulePolicyTouched(true);
                  }}
                  options={[
                    {
                      value: "coalesce_latest",
                      label: (
                        <span title={t("task.settings.coalesceLatestHint")}>
                          {t("task.settings.coalesceLatest")}
                        </span>
                      ),
                    },
                    {
                      value: "queue_every_occurrence",
                      label: (
                        <span title={t("task.settings.queueEveryOccurrenceHint")}>
                          {t("task.settings.queueEveryOccurrence")}
                        </span>
                      ),
                    },
                    {
                      value: "skip_while_busy",
                      label: (
                        <span title={t("task.settings.skipWhileBusyHint")}>
                          {t("task.settings.skipWhileBusy")}
                        </span>
                      ),
                    },
                  ]}
                />
              </label>
              <div className="settings-field schedule-catchup-fields">
                <label>
                  <span className="settings-field-label">{t("task.settings.catchupCount")}</span>
                  <InputNumber
                    data-testid="task-schedule-max-catchup-count"
                    min={1}
                    max={1000}
                    precision={0}
                    value={maxCatchupCount}
                    disabled={readOnly || scheduleFieldsLocked}
                    onChange={(value) => {
                      setMaxCatchupCount(value ?? DEFAULT_SCHEDULE_CATCHUP_COUNT);
                      setScheduleTouched(true);
                      setSchedulePolicyTouched(true);
                    }}
                  />
                </label>
                <label>
                  <span className="settings-field-label">{t("task.settings.catchupAge")}</span>
                  <InputNumber
                    data-testid="task-schedule-max-catchup-age"
                    min={60}
                    max={604800}
                    precision={0}
                    value={maxCatchupAgeSeconds}
                    disabled={readOnly || scheduleFieldsLocked}
                    onChange={(value) => {
                      setMaxCatchupAgeSeconds(value ?? DEFAULT_SCHEDULE_CATCHUP_AGE_SECONDS);
                      setScheduleTouched(true);
                      setSchedulePolicyTouched(true);
                    }}
                  />
                </label>
              </div>
              <Typography.Text type="secondary" className="settings-field-hint">
                {t("task.settings.policyHint")}
              </Typography.Text>
              <div className="settings-field">
                <span className="settings-field-label">{t("task.settings.scheduleStatus")}</span>
                <Space>
                  <Tag color={scheduleEnabled ? "green" : "default"}>{scheduleEnabled ? t("task.settings.scheduleRunning") : t("task.settings.disabled")}</Tag>
                  <span data-testid="task-schedule-next-run">{t("task.settings.nextRun", { time: formatTime(schedule?.next_run_at ?? null, locale) })}</span>
                  <Button size="small" onClick={() => void loadSchedule()}>{t("actions.refresh", { ns: "common" })}</Button>
                </Space>
              </div>
              <div className="settings-field" data-testid="task-schedule-outcomes">
                <span className="settings-field-label">{t("task.settings.recentOutcomes")}</span>
                {schedule?.recent_outcomes && schedule.recent_outcomes.length > 0 ? (
                  <Space direction="vertical" size="small">
                    {schedule.recent_outcomes.slice(0, 5).map((outcome) => (
                      <Space key={outcome.id} wrap>
                        <Tag color={outcome.outcome === "enqueued" ? "green" : outcome.outcome === "expired" ? "warning" : "default"}>
                          {t(SCHEDULE_OUTCOME_LABEL_KEYS[outcome.outcome])}
                        </Tag>
                        <Typography.Text type="secondary">
                          {formatTime(outcome.first_scheduled_for, locale)}
                          {outcome.last_scheduled_for !== outcome.first_scheduled_for
                            ? ` – ${formatTime(outcome.last_scheduled_for, locale)}`
                            : ""}
                          {` · ${outcome.occurrence_count}`}
                          {outcome.reason ? ` · ${outcome.reason}` : ""}
                        </Typography.Text>
                      </Space>
                    ))}
                  </Space>
                ) : (
                  <Typography.Text type="secondary">{t("task.settings.noRecentOutcomes")}</Typography.Text>
                )}
              </div>
            </Space>
          )}
        </section>
      )}

      <section className="task-input-config" data-testid="task-input-config">
        <div className="task-input-heading">
          <div>
            <Typography.Title level={5}>{t("input.objectTitle", { ns: "adapter" })}</Typography.Title>
            <Typography.Text type="secondary">{t("task.input.description")}</Typography.Text>
          </div>
          <div className="task-input-state" data-testid="task-input-state" aria-live="polite">
            <Tag color={inputDirty ? "gold" : "green"}>
              {inputDirty ? t("task.input.draft") : t("task.input.saved")}
            </Tag>
          </div>
        </div>

        <div className="task-input-source-grid" role="radiogroup" aria-label={t("task.input.sourceGroup")}>
          {sourceCards.map((card) => {
            const selected = inputSourceDraft === card.sourceType;
            const cardContent = (
              <Card
                key={card.sourceType}
                size="small"
                className={`task-input-source-card${selected ? " is-selected" : ""}${card.disabledReason !== null ? " is-disabled" : ""}`}
                data-testid={`task-input-source-${card.sourceType}`}
                role="radio"
                aria-checked={selected}
                aria-disabled={card.disabledReason !== null}
                aria-label={`${card.title}: ${card.status}`}
                tabIndex={0}
                hoverable={card.disabledReason === null}
                onClick={() => selectInputSource(card.sourceType, card.disabledReason)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    selectInputSource(card.sourceType, card.disabledReason);
                  }
                }}
              >
                <div className="task-input-source-card-title">{card.title}</div>
                <Typography.Text type="secondary">{card.description}</Typography.Text>
                <Tag color={selected ? "blue" : card.disabledReason === null ? "default" : "gold"}>
                  {card.status}
                </Tag>
              </Card>
            );
            return card.disabledReason === null ? cardContent : (
              <Tooltip key={card.sourceType} title={card.disabledReason} trigger={["hover", "focus"]}>
                {cardContent}
              </Tooltip>
            );
          })}
        </div>

        <Form layout="vertical" className="task-input-form">
          {inputSourceDraft === "json" && (
            <Form.Item
              label={t("task.input.jsonLabel")}
              validateStatus={inputValidationError !== null ? "error" : undefined}
              help={inputValidationError ?? t("task.input.jsonHint")}
            >
              <Input.TextArea
                data-testid="task-input-json"
                aria-label={t("task.input.jsonLabel")}
                aria-invalid={inputValidationError !== null}
                rows={8}
                value={inputJsonDraft}
                disabled={inputEditingLocked}
                placeholder={t("task.input.jsonPlaceholder")}
                onChange={(event) => {
                  setInputJsonDraft(event.target.value);
                  setInputValidationError(null);
                }}
              />
            </Form.Item>
          )}
          {(inputSourceDraft === "managed_files" || stagedArtifacts.length > 0 || inputConfig?.source_type === "managed_files") && (
            <div className="managed-input-editor" data-testid="managed-input-editor">
              {managedCapabilityLoadFailed && (
                <Alert
                  type="warning"
                  showIcon
                  data-testid="managed-input-capability-error"
                  message={t("task.input.capabilityLoadFailed")}
                  action={(
                    <Button
                      size="small"
                      loading={loadingManagedCapability}
                      data-testid="managed-input-capability-retry"
                      onClick={() => void loadManagedInput()}
                    >
                      {t("actions.refresh", { ns: "common" })}
                    </Button>
                  )}
                />
              )}
              {stagedArtifactsLoadFailed && (
                <Alert
                  type="warning"
                  showIcon
                  data-testid="managed-input-staged-list-error"
                  message={t("task.input.stagedListLoadFailed")}
                  action={(
                    <Button
                      size="small"
                      loading={loadingStagedArtifacts}
                      data-testid="managed-input-staged-list-retry"
                      onClick={() => void loadStagedArtifacts()}
                    >
                      {t("actions.refresh", { ns: "common" })}
                    </Button>
                  )}
                />
              )}
              <div className="managed-input-editor-header">
                <div>
                  <Typography.Text strong>{t("task.input.managedFilesTitle")}</Typography.Text>
                  <Typography.Text type="secondary" className="settings-field-hint">
                    {t("task.input.managedFilesHint", { max: MANAGED_FILE_LIMIT })}
                  </Typography.Text>
                </div>
                <Tag color={managedFilesDraft.length === 0 ? "gold" : "blue"} data-testid="managed-input-count">
                  {t("task.input.fileCount", { count: managedFilesDraft.length, max: MANAGED_FILE_LIMIT })}
                </Tag>
              </div>

              {managedFilesEnabled && !readOnly && !savingInput && (
                <Space wrap>
                  <Upload
                    accept={managedFileAccept}
                    fileList={[]}
                    multiple={false}
                    showUploadList={false}
                    onChange={() => undefined}
                    beforeUpload={(file) => {
                      void uploadManagedFile(file, null);
                      return Upload.LIST_IGNORE;
                    }}
                  >
                    <Tooltip
                      title={t("task.input.uploadSupportedTypes", { extensions: managedFileTypesLabel })}
                      trigger={["hover", "focus"]}
                    >
                      <Button data-testid="managed-input-upload">{t("task.input.upload")}</Button>
                    </Tooltip>
                  </Upload>
                  <Button
                    data-testid="managed-input-refresh"
                    loading={loadingManagedCapability || loadingStagedArtifacts}
                    onClick={() => void loadManagedInput()}
                  >
                    {t("task.input.refreshStaged")}
                  </Button>
                </Space>
              )}

              {uploadProgress.length > 0 && (
                <div className="managed-input-progress" data-testid="managed-input-upload-progress" aria-live="polite">
                  {uploadProgress.map((progress) => (
                    <div className="managed-input-progress-row" key={progress.key}>
                      <Typography.Text className="managed-input-filename" title={progress.filename}>
                        {displayFilename(progress.filename)}
                      </Typography.Text>
                      <Progress percent={progress.percent} size="small" />
                    </div>
                  ))}
                </div>
              )}

              {managedFilesDraft.length === 0 ? (
                <Typography.Text type="secondary" data-testid="managed-input-empty">
                  {t("task.input.managedFilesEmpty")}
                </Typography.Text>
              ) : (
                <div className="managed-input-file-list" data-testid="managed-input-file-list">
                  {managedFilesDraft.map((artifact) => {
                    const statusLabel = artifact.status === "READY"
                      ? t("task.input.fileStatus.ready")
                      : t("task.input.fileStatus.staged");
                    const isStaged = artifact.status === "STAGED";
                    return (
                      <Card key={artifact.id} size="small" className="managed-input-file-card">
                        <div className="managed-input-file-main">
                          <Tooltip title={artifact.original_filename}>
                            <Typography.Text
                              className="managed-input-filename"
                              data-testid={`managed-input-filename-${artifact.id}`}
                              title={artifact.original_filename}
                            >
                              {displayFilename(artifact.original_filename)}
                            </Typography.Text>
                          </Tooltip>
                          <Typography.Text type="secondary">
                            {t("task.input.fileMeta", {
                              extension: fileExtension(artifact.original_filename),
                              size: artifact.size_bytes,
                            })}
                          </Typography.Text>
                          <Typography.Text
                            type="secondary"
                            data-testid={`managed-input-created-${artifact.id}`}
                          >
                            {t("task.input.uploadedAt", {
                              time: formatTime(artifact.created_at ?? null, locale),
                            })}
                          </Typography.Text>
                          <Tag color={isStaged ? "gold" : "green"} data-testid={`managed-input-status-${artifact.id}`}>
                            {statusLabel}
                          </Tag>
                          <Typography.Text type="secondary" data-testid={`managed-input-expires-${artifact.id}`}>
                            {artifact.expires_at === null
                              ? t("task.input.expiresNever")
                              : t("task.input.expiresAt", { time: formatTime(artifact.expires_at, locale) })}
                          </Typography.Text>
                        </div>
                        <Space className="managed-input-file-actions">
                          {managedFilesEnabled && !readOnly && !inputEditingLocked && (
                            <Upload
                              accept={managedFileAccept}
                              fileList={[]}
                              multiple={false}
                              showUploadList={false}
                              onChange={() => undefined}
                              beforeUpload={(file) => {
                                void uploadManagedFile(file, artifact.id);
                                return Upload.LIST_IGNORE;
                              }}
                            >
                              <Tooltip
                                title={t("task.input.uploadSupportedTypes", { extensions: managedFileTypesLabel })}
                                trigger={["hover", "focus"]}
                              >
                                <Button size="small" data-testid={`replace-managed-file-${artifact.id}`}>
                                  {t("task.input.replace")}
                                </Button>
                              </Tooltip>
                            </Upload>
                          )}
                          <Button
                            size="small"
                            danger={!isStaged}
                            loading={deletingArtifactId === artifact.id}
                            disabled={readOnly || deletingArtifactId !== null || (!isStaged && inputEditingLocked)}
                            data-testid={`delete-managed-file-${artifact.id}`}
                            onClick={() => void deleteManagedFile(artifact)}
                          >
                            {t("task.input.delete")}
                          </Button>
                        </Space>
                      </Card>
                    );
                  })}
                </div>
              )}

              {overflowStagedArtifacts.length > 0 && (
                <div data-testid="managed-input-overflow-staged">
                  <Alert
                    type="warning"
                    showIcon
                    message={t("task.input.overflowStaged", {
                      count: overflowStagedArtifacts.length,
                      max: MANAGED_FILE_LIMIT,
                    })}
                  />
                  <div className="managed-input-file-list">
                    {overflowStagedArtifacts.map((artifact) => (
                      <Card key={artifact.id} size="small" className="managed-input-file-card">
                        <div className="managed-input-file-main">
                          <Typography.Text className="managed-input-filename">
                            {displayFilename(artifact.original_filename)}
                          </Typography.Text>
                          <Tag color="gold">{t("task.input.fileStatus.staged")}</Tag>
                        </div>
                        <Button
                          size="small"
                          loading={deletingArtifactId === artifact.id}
                          disabled={readOnly || deletingArtifactId !== null}
                          data-testid={`delete-overflow-staged-${artifact.id}`}
                          onClick={() => void deleteManagedFile(artifact)}
                        >
                          {t("task.input.delete")}
                        </Button>
                      </Card>
                    ))}
                  </div>
                </div>
              )}

              {managedFilenameConflict && (
                <Alert
                  type="error"
                  showIcon
                  data-testid="managed-input-name-conflict"
                  message={t("task.input.errors.fileNameConflict")}
                />
              )}
              {inputValidationError !== null && !managedFilenameConflict && (
                <Alert
                  type="error"
                  showIcon
                  data-testid="managed-input-error"
                  message={inputValidationError}
                />
              )}

              <Form.Item label={t("task.input.retentionLabel")} className="managed-input-retention-item">
                <Space wrap>
                  <Select
                    data-testid="managed-input-retention-mode"
                    value={retentionDraft.mode}
                    disabled={inputEditingLocked || !managedFilesEnabled}
                    options={[
                      { value: "system_default", label: t("task.input.retention.systemDefault") },
                      { value: "custom", label: t("task.input.retention.custom") },
                      {
                        value: "manual_delete",
                        label: t("task.input.retention.manualDelete"),
                        disabled: managedCapability?.allow_manual_delete !== true,
                      },
                    ]}
                    onChange={(value: InputRetention["mode"]) => {
                      setRetentionDraft(value === "custom"
                        ? {
                            mode: "custom",
                            seconds: retentionDraft.seconds
                              ?? managedCapability?.default_retention_seconds
                              ?? null,
                          }
                        : { mode: value, seconds: null });
                      setInputValidationError(null);
                    }}
                  />
                  {retentionDraft.mode === "custom" && (
                    <InputNumber
                      data-testid="managed-input-retention-seconds"
                      min={3_600}
                      max={managedCapability?.max_custom_retention_seconds}
                      precision={0}
                      value={retentionDraft.seconds ?? undefined}
                      suffix={t("task.input.seconds")}
                      disabled={inputEditingLocked || !managedFilesEnabled}
                      onChange={(value) => setRetentionDraft({ mode: "custom", seconds: value ?? null })}
                    />
                  )}
                </Space>
                <Typography.Text type="secondary" className="settings-field-hint">
                  {t("task.input.retentionHint")}
                </Typography.Text>
              </Form.Item>
              {savedManagedInput && inputSourceDraft === "managed_files" && managedFilesDraft.length === 0 && (
                <Alert
                  type="info"
                  showIcon
                  data-testid="managed-input-clone-notice"
                  message={t("task.input.cloneReuploadHint")}
                />
              )}
              {savedManagedInput && (
                <ManagedInputExamples
                  language={props.adapter.language}
                  ready={managedInputExampleReady}
                  disabledReason={managedInputExampleDisabledReason}
                />
              )}
            </div>
          )}
          {inputConfig !== null && !inputConfig.valid_for_run && inputSourceDraft === inputConfig.source_type && !inputDirty && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-input-invalid"
              message={inputInvalidMessage ?? t("task.input.invalidConfig")}
            />
          )}
          <div className="task-input-actions">
            <Button
              type="primary"
              htmlType="button"
              data-testid="save-task-input"
              loading={savingInput}
              disabled={inputEditingLocked || inputConfig === null || managedFilesSaveBlocked}
              onClick={() => void saveInputObject()}
            >
              {t("task.input.save")}
            </Button>
            {inputDirty && <Typography.Text type="warning">{t("task.input.unsavedHint")}</Typography.Text>}
          </div>
        </Form>
      </section>

      <Button
        type="primary"
        className="task-run-config-save"
        data-testid="save-task-runtime"
        loading={savingRuntime}
        disabled={readOnly || runtimeLocked || workerId === null}
        onClick={() => void saveRunConfig()}
      >
        {t("task.settings.save")}
      </Button>
    </div>
  );
});

export default TaskRunSettingsPanel;
